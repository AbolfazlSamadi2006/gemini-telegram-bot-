import os
import io
import time
import asyncio
import logging
import sqlite3
import datetime
import zipfile
import xml.etree.ElementTree as ET
import xml.sax.saxutils
import math
import sys
import uuid
import re
import contextlib
import json
from typing import List, Tuple, Optional, Dict, NamedTuple

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from google import genai
from google.genai import types
import edge_tts
import httpx
import bs4
from youtube_transcript_api import YouTubeTranscriptApi
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# ================= تنظیمات لاگ =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or "dummy_offline_key"
ALLOWED_USERS = set(filter(None, os.environ.get("ALLOWED_USERS", "").split(",")))
INVITE_CODE = os.environ.get("INVITE_CODE", "vip2026")

client = genai.Client(api_key=GEMINI_KEY)

# ================= دیتابیس ماندگار (SQLite) =================
class Database:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.environ.get("DB_PATH", "bot_database.db")
        self.db_path = db_path
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, query: str, params: tuple = ()):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.fetchall()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    active_chat_id INTEGER,
                    model TEXT DEFAULT 'flash_38_med',
                    persona TEXT DEFAULT 'general',
                    search_enabled INTEGER DEFAULT 0,
                    voice_reply INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    title TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    role TEXT,
                    content TEXT,
                    created_at TEXT,
                    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    reminder_text TEXT NOT NULL,
                    due_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    notified_at TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders (status, due_at_utc)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id, status)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reasoning_logs (
                    thought_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    chat_id INTEGER,
                    thought_content TEXT,
                    created_at TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reasoning_logs_user ON reasoning_logs (user_id)
            """)
            conn.commit()

    def init_reminders_table(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    reminder_text TEXT NOT NULL,
                    due_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    notified_at TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders (status, due_at_utc)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id, status)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reasoning_logs (
                    thought_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    chat_id INTEGER,
                    thought_content TEXT,
                    created_at TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reasoning_logs_user ON reasoning_logs (user_id)
            """)
            conn.commit()

    def get_or_create_user(self, user_id: str) -> Dict:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                cursor.execute(
                    "INSERT INTO users (user_id, model, persona, search_enabled, voice_reply) VALUES (?, 'flash_38_med', 'general', 0, 0)",
                    (user_id,)
                )
                cursor.execute(
                    "INSERT INTO chats (user_id, title, created_at, updated_at) VALUES (?, 'گفتگوی اول', ?, ?)",
                    (user_id, now, now)
                )
                chat_id = cursor.lastrowid
                cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = cursor.fetchone()
            elif not user["active_chat_id"]:
                cursor.execute(
                    "INSERT INTO chats (user_id, title, created_at, updated_at) VALUES (?, 'گفتگوی جدید', ?, ?)",
                    (user_id, now, now)
                )
                chat_id = cursor.lastrowid
                cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = cursor.fetchone()
            return dict(user)

    def update_user_setting(self, user_id: str, key: str, value):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))
            conn.commit()

    def create_new_chat(self, user_id: str, title: str = "گفتگوی جدید") -> int:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO chats (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, title, now, now)
            )
            chat_id = cursor.lastrowid
            cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
            conn.commit()
            return chat_id

    def get_user_chats(self, user_id: str) -> List[Dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT c.*, COUNT(m.id) as msg_count 
                FROM chats c 
                LEFT JOIN messages m ON c.id = m.chat_id 
                WHERE c.user_id = ? 
                GROUP BY c.id 
                ORDER BY c.id DESC 
                LIMIT 15
                """,
                (user_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_chat(self, user_id: str) -> Dict:
        self.get_or_create_user(user_id)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.* FROM chats c
                JOIN users u ON c.id = u.active_chat_id
                WHERE u.user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                cursor.execute(
                    "INSERT INTO chats (user_id, title, created_at, updated_at) VALUES (?, 'گفتگوی جدید', ?, ?)",
                    (user_id, now, now)
                )
                chat_id = cursor.lastrowid
                cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
                conn.commit()
                cursor.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
                row = cursor.fetchone()
            return dict(row)

    def switch_active_chat(self, user_id: str, chat_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
            if cursor.fetchone():
                cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
                conn.commit()
                return True
            return False

    def rename_chat(self, chat_id: int, new_title: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE chats SET title = ? WHERE id = ?", (new_title[:45], chat_id))
            conn.commit()

    def delete_chat(self, user_id: str, chat_id: int) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            cursor.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
            conn.commit()

            cursor.execute("SELECT active_chat_id FROM users WHERE user_id = ?", (user_id,))
            u = cursor.fetchone()
            if u and u["active_chat_id"] == chat_id:
                cursor.execute("SELECT id FROM chats WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
                latest = cursor.fetchone()
                if latest:
                    new_active = latest["id"]
                else:
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                    cursor.execute(
                        "INSERT INTO chats (user_id, title, created_at, updated_at) VALUES (?, 'گفتگوی جدید', ?, ?)",
                        (user_id, now, now)
                    )
                    new_active = cursor.lastrowid
                cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (new_active, user_id))
                conn.commit()
                return new_active
            return u["active_chat_id"]

    def add_message(self, chat_id: int, role: str, content: str):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now)
            )
            cursor.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            
            cursor.execute("SELECT COUNT(*) as cnt FROM messages WHERE chat_id = ? AND role = 'user'", (chat_id,))
            user_cnt = cursor.fetchone()["cnt"]
            if user_cnt == 1 and role == 'user':
                clean_title = content.strip().replace("\n", " ")
                if len(clean_title) > 28:
                    clean_title = clean_title[:28] + "..."
                cursor.execute("UPDATE chats SET title = ? WHERE id = ?", (clean_title, chat_id))
            conn.commit()

    def get_chat_messages(self, chat_id: int, limit: int = 30) -> List[Tuple[str, str]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC LIMIT ?",
                (chat_id, limit)
            )
            return [(row["role"], row["content"]) for row in cursor.fetchall()]

    def clear_chat_messages(self, chat_id: int):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            cursor.execute("UPDATE chats SET title = 'گفتگوی خالی' WHERE id = ?", (chat_id,))
            conn.commit()

    # ================= متدهای یادآورها (Reminders) =================
    def add_reminder(
        self,
        user_id: int | str,
        chat_id: int,
        message_id: Optional[int],
        reminder_text: str,
        due_at_utc: str
    ) -> int:
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO reminders (user_id, chat_id, message_id, reminder_text, due_at_utc, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (user_id, chat_id, message_id, reminder_text, due_at_utc, now_utc)
            )
            conn.commit()
            return cursor.lastrowid

    def get_due_reminders(self, now_utc_str: Optional[str] = None, limit: int = 50) -> List[Dict]:
        if now_utc_str is None:
            now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'pending' AND due_at_utc <= ?
                ORDER BY due_at_utc ASC
                LIMIT ?
                """,
                (now_utc_str, limit)
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_reminder_status(
        self,
        reminder_id: int,
        status: str,
        notified_at: Optional[str] = None
    ) -> bool:
        if notified_at is None and status in ("delivered", "missed", "cancelled", "dismissed"):
            notified_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE reminders
                SET status = ?, notified_at = COALESCE(?, notified_at)
                WHERE id = ?
                """,
                (status, notified_at, reminder_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def snooze_reminder(self, reminder_id: int, new_due_at_utc: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE reminders
                SET due_at_utc = ?, status = 'pending', notified_at = NULL
                WHERE id = ?
                """,
                (new_due_at_utc, reminder_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_user_reminders(
        self,
        user_id: int | str,
        status: Optional[str] = "pending",
        limit: int = 20
    ) -> List[Dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status is None or status.lower() == "all":
                cursor.execute(
                    """
                    SELECT * FROM reminders
                    WHERE user_id = ?
                    ORDER BY due_at_utc ASC
                    LIMIT ?
                    """,
                    (user_id, limit)
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM reminders
                    WHERE user_id = ? AND status = ?
                    ORDER BY due_at_utc ASC
                    LIMIT ?
                    """,
                    (user_id, status, limit)
                )
            return [dict(row) for row in cursor.fetchall()]

    def recover_missed_reminders(
        self,
        now_utc_str: Optional[str] = None,
        max_delay_hours: float = 24.0
    ) -> Dict:
        """
        Recovers reminders that matured while the server was down.
        Marks reminders overdue by > max_delay_hours (default 24h) as 'missed'.
        Returns a dict with 'recoverable' (list of overdue <= max_delay_hours)
        and 'missed' (list of expired reminders marked as missed).
        """
        if now_utc_str is None:
            now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        clean_now = now_utc_str.replace("Z", "+00:00")
        now_dt = datetime.datetime.fromisoformat(clean_now)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM reminders
                WHERE (status = 'pending' OR status = 'firing') AND due_at_utc <= ?
                ORDER BY due_at_utc ASC
                """,
                (now_utc_str,)
            )
            rows = [dict(r) for r in cursor.fetchall()]

            recoverable = []
            missed = []

            for rem in rows:
                try:
                    due_clean = rem["due_at_utc"].replace("Z", "+00:00")
                    due_dt = datetime.datetime.fromisoformat(due_clean)
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=datetime.timezone.utc)
                    delay_hours = (now_dt - due_dt).total_seconds() / 3600.0
                except Exception:
                    delay_hours = 999.0

                if delay_hours > max_delay_hours:
                    cursor.execute(
                        "UPDATE reminders SET status = 'missed', notified_at = ? WHERE id = ?",
                        (now_utc_str, rem["id"])
                    )
                    rem_copy = dict(rem)
                    rem_copy["status"] = "missed"
                    rem_copy["notified_at"] = now_utc_str
                    missed.append(rem_copy)
                else:
                    recoverable.append(rem)

            conn.commit()
            return {
                "recoverable": recoverable,
                "missed": missed,
                "missed_count": len(missed)
            }

    # ================= متدهای ثبت استدلال (Reasoning Logs) =================
    def save_thought(
        self,
        thought_id: str,
        user_id: int | str,
        chat_id: int,
        thought_content: str
    ):
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO reasoning_logs (thought_id, user_id, chat_id, thought_content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thought_id, str(user_id), chat_id, thought_content, now_utc)
            )
            conn.commit()

    def get_thought(self, thought_id: str) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT thought_content FROM reasoning_logs WHERE thought_id = ?",
                (thought_id,)
            )
            row = cursor.fetchone()
            return row["thought_content"] if row else None


db = Database()

# ================= مدل‌ها و پرسوناهای پیشرفته =================
AVAILABLE_MODELS = {
    "flash_38_med": {
        "model_id": "gemini-3.8-flash",
        "title": "🟡 3.8 Flash (تفکر استاندارد) [پیش‌فرض]",
        "thinking_budget": 4096,
    },
    "flash_38_high": {
        "model_id": "gemini-3.8-flash",
        "title": "🔴 3.8 Flash (تفکر عمیق و سنگین)",
        "thinking_budget": 8192,
    },
    "flash_36": {
        "model_id": "gemini-3.6-flash",
        "title": "⚡ Gemini 3.6 Flash (بسیار پایدار و سریع)",
        "thinking_budget": None,
    },
    "pro_model": {
        "model_id": "gemini-3.5-flash",
        "title": "💎 Gemini Flash 3.5 (سریع و هوشمند)",
        "thinking_budget": None,
    },
    "flash_35_lite": {
        "model_id": "gemini-3.5-flash-lite",
        "title": "🚀 Gemini 3.5 Flash Lite (سبک‌ترین)",
        "thinking_budget": None,
    },
    "argon_4": {
        "model_id": "gemini-4-argon",
        "title": "🛸 Gemini 4 Argon",
        "thinking_budget": None,
    },
}

FALLBACK_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]

PERSONAS = {
    "general": {
        "title": "🤖 دستیار عامیانه ابوالفضل (پیش‌فرض)",
        "prompt": (
            "تو دستیار هوش مصنوعی جمنای ابوالفضل هستی. "
            "لحنت باید کاملاً خودمانی، صمیمی، گرم، محاوره‌ای و عامیانه باشه. "
            "اصلاً کتابی، رسمی، خشک یا اداری حرف نزن. "
            "مثل یک دوست و رفیق کاربلد و باهوش جواب بده و هر وقت لازم بود شوخی‌های بجا و بامزه هم بکن."
        ),
    },
    "coder": {
        "title": "💻 برنامه‌نویس ارشد (Senior Dev)",
        "prompt": "تو مهندس نرم‌افزار ارشد هستی. با لحن صمیمی و فنی، بدون اضافه گویی بهترین و تمیزترین کدها رو بده.",
    },
    "translator": {
        "title": "✍️ مترجم و ویراستار روان",
        "prompt": "تو یک مترجم حرفه‌ای هستی. متون رو به طبیعی‌ترین و قشنگ‌ترین شکل عامیانه یا رسمی (بسته به متن) ترجمه کن.",
    },
    "tutor": {
        "title": "🇬🇧 رفیق و پارتنر زبان انگلیسی",
        "prompt": "You are a friendly, cool English partner. Chat naturally in English, help Abolfazl improve, and gently correct mistakes.",
    },
}

# ================= سیستم گزارش زنده و طبیعی (Progress Tracker) =================
class ProgressTracker:
    def __init__(self, message, steps: List[str], min_step_duration: float = 1.3, min_total_duration: float = 2.4):
        self.trigger_message = message
        self.steps = steps
        self.min_step_duration = min_step_duration
        self.min_total_duration = min_total_duration
        self.status_msg = None
        self.current_idx = 0
        self.start_time = None
        self._stopped = False
        self._task = None

    async def start(self):
        if not self.steps or not self.trigger_message:
            return
        self.start_time = time.time()
        try:
            self.status_msg = await self.trigger_message.reply_text(self.steps[0])
            self.current_idx = 1
            self._task = asyncio.create_task(self._ticker())
        except Exception as e:
            logging.debug(f"Tracker start error: {e}")

    async def _ticker(self):
        try:
            while not self._stopped and self.current_idx < len(self.steps):
                await asyncio.sleep(self.min_step_duration)
                if self._stopped:
                    break
                new_text = self.steps[self.current_idx]
                self.current_idx += 1
                if self.status_msg:
                    try:
                        await self.status_msg.edit_text(new_text)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def finish(self, final_text: str, update_obj: Update, reply_markup: Optional[InlineKeyboardMarkup] = None):
        if self.start_time:
            elapsed = time.time() - self.start_time
            if elapsed < self.min_total_duration:
                await asyncio.sleep(self.min_total_duration - elapsed)
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()

        if self.status_msg:
            if len(final_text) <= 4000:
                try:
                    await self.status_msg.edit_text(final_text, reply_markup=reply_markup, parse_mode="Markdown")
                    return
                except Exception:
                    try:
                        await self.status_msg.edit_text(final_text, reply_markup=reply_markup)
                        return
                    except Exception:
                        pass
            try:
                await self.status_msg.delete()
            except Exception:
                pass
        await send_long_message(update_obj, final_text, reply_markup=reply_markup)

# ================= توابع کمکی هوش مصنوعی (R1 & R6) =================

def format_response_with_code_execution(
    clean_text: str,
    executed_code: Optional[str] = None,
    execution_result: Optional[str] = None
) -> str:
    """
    Formats the model's answer alongside the executed Python code and verified
    numerical terminal output with transparent badges (Requirement R1).
    """
    if not executed_code:
        return clean_text

    parts = []
    if clean_text:
        parts.append(clean_text)

    parts.append("━━━━━━━━━━━━━━━━━━")
    parts.append(f"💻 **کد پایتون اجرا شده:**\n```python\n{executed_code.strip()}\n```")

    res_str = execution_result.strip() if execution_result else "*(بدون خروجی کنسول)*"
    parts.append(f"📊 **نتیجه تایید شده اجرا:**\n```\n{res_str}\n```")

    return "\n\n".join(parts)


class ChatResponse(NamedTuple):
    clean_text: str
    executed_code: Optional[str] = None
    execution_result: Optional[str] = None
    thought_text: Optional[str] = None

    @property
    def formatted_text(self) -> str:
        return format_response_with_code_execution(self.clean_text, self.executed_code, self.execution_result)


def parse_gemini_candidate_parts(response) -> ChatResponse:
    """
    Extracts text, executable code, execution result, and thinking process parts
    from a Gemini candidate (Requirements R1, R6).
    """
    if not response:
        return ChatResponse(clean_text="")

    clean_texts = []
    thought_texts = []
    executed_codes = []
    execution_results = []

    candidates = getattr(response, "candidates", None) or []
    if candidates and len(candidates) > 0:
        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            # R6: Thinking tokens inspection
            if getattr(part, "thought", False):
                t = getattr(part, "text", "")
                if t:
                    thought_texts.append(str(t))
            # R1: Executable code generated by model
            elif getattr(part, "executable_code", None) is not None:
                code_obj = part.executable_code
                code = getattr(code_obj, "code", "")
                if code:
                    executed_codes.append(str(code))
            # R1: Result of code execution in sandbox
            elif getattr(part, "code_execution_result", None) is not None:
                res_obj = part.code_execution_result
                out = getattr(res_obj, "output", "")
                if out:
                    execution_results.append(str(out))
            # Standard model text response
            elif getattr(part, "text", None):
                clean_texts.append(str(part.text))

    clean_text = "\n".join(clean_texts).strip()
    if not clean_text and getattr(response, "text", None):
        clean_text = str(response.text).strip()

    executed_code = "\n".join(executed_codes).strip() if executed_codes else None
    execution_result = "\n".join(execution_results).strip() if execution_results else None
    thought_text = "\n".join(thought_texts).strip() if thought_texts else None

    return ChatResponse(
        clean_text=clean_text,
        executed_code=executed_code,
        execution_result=execution_result,
        thought_text=thought_text,
    )


def build_gemini_session(
    user_id: str | int,
    chat_id: int,
    target_model: str,
    is_fallback: bool = False,
    enable_code_exec: bool = True,
):
    uid_str = str(user_id)
    u = db.get_or_create_user(uid_str)
    model_info = AVAILABLE_MODELS.get(u.get("model", "flash_38_med"), AVAILABLE_MODELS["flash_38_med"])
    persona_info = PERSONAS.get(u.get("persona", "general"), PERSONAS["general"])

    tools = []
    # R1: Native Python Code Execution
    if enable_code_exec:
        try:
            tools.append(types.Tool(code_execution=types.ToolCodeExecution()))
        except Exception as e:
            logging.warning(f"Could not initialize ToolCodeExecution: {e}")

    if u.get("search_enabled"):
        try:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        except Exception as e:
            logging.warning(f"Could not initialize GoogleSearch: {e}")

    # R6: ThinkingConfig with include_thoughts=True for transparent reasoning
    thinking_cfg = None
    if not is_fallback and model_info.get("thinking_budget") is not None:
        try:
            thinking_cfg = types.ThinkingConfig(
                thinking_budget=model_info["thinking_budget"],
                include_thoughts=True,
            )
        except Exception:
            try:
                thinking_cfg = types.ThinkingConfig(thinking_budget=model_info["thinking_budget"])
            except Exception:
                thinking_cfg = None

    gen_config = types.GenerateContentConfig(
        system_instruction=persona_info["prompt"],
        tools=tools if tools else None,
        thinking_config=thinking_cfg,
    )

    history_records = db.get_chat_messages(chat_id, limit=30)
    history = []
    for role, content in history_records:
        history.append(
            types.Content(
                role="user" if role == "user" else "model",
                parts=[types.Part.from_text(text=content)]
            )
        )

    return client.chats.create(
        model=target_model,
        config=gen_config,
        history=history
    )


async def generate_chat_response(
    user_id: str | int,
    chat_id: int,
    user_text: str,
    enable_code_exec: bool = True
) -> ChatResponse:
    uid_str = str(user_id)
    u = db.get_or_create_user(uid_str)
    model_info = AVAILABLE_MODELS.get(u.get("model", "flash_38_med"), AVAILABLE_MODELS["flash_38_med"])
    primary_model = model_info["model_id"]

    models_to_try = [primary_model]
    for fb in FALLBACK_MODELS:
        if fb not in models_to_try:
            models_to_try.append(fb)

    last_error = None
    for idx, target_model in enumerate(models_to_try):
        try:
            is_fallback = (idx > 0)
            chat_session = build_gemini_session(
                uid_str,
                chat_id,
                target_model=target_model,
                is_fallback=is_fallback,
                enable_code_exec=enable_code_exec,
            )
            response = chat_session.send_message(user_text)
            parsed_resp = parse_gemini_candidate_parts(response)
            if parsed_resp.clean_text or parsed_resp.executed_code:
                return parsed_resp
        except Exception as e:
            last_error = e
            logging.warning(f"Chat model {target_model} failed ({e}). Trying fallback...")
            if "503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e):
                await asyncio.sleep(0.6)

    raise last_error

async def generate_multimodal_response(user_id: str, contents: list) -> str:
    u = db.get_or_create_user(user_id)
    model_info = AVAILABLE_MODELS.get(u["model"], AVAILABLE_MODELS["flash_38_med"])
    primary_model = model_info["model_id"]

    models_to_try = [primary_model]
    for fb in FALLBACK_MODELS:
        if fb not in models_to_try:
            models_to_try.append(fb)

    last_error = None
    for idx, target_model in enumerate(models_to_try):
        try:
            response = client.models.generate_content(
                model=target_model,
                contents=contents
            )
            if response and response.text:
                return response.text
        except Exception as e:
            last_error = e
            logging.warning(f"Multimodal {target_model} failed ({e}). Trying fallback...")
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                await asyncio.sleep(0.6)

    raise last_error

def extract_docx_text(docx_bytes: bytes) -> Optional[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)
            texts = [node.text for node in tree.iter() if node.tag.endswith("}t") and node.text]
            return " ".join(texts)
    except Exception:
        return None

def check_access(user_id: str) -> bool:
    if not ALLOWED_USERS or "" in ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

async def send_long_message(update: Update, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    if not chunks:
        chunks = [text]
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        try:
            await update.message.reply_text(chunk, reply_markup=markup, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk, reply_markup=markup)

async def reply_with_optional_voice(update: Update, text: str, user_id: str, send_text: bool = True):
    if send_text:
        await send_long_message(update, text)
    u = db.get_or_create_user(user_id)
    if u["voice_reply"] and len(text) < 1500:
        try:
            tts = edge_tts.Communicate(text[:1000], "fa-IR-DilaraNeural")
            audio_buffer = io.BytesIO()
            async for chunk in tts.stream():
                if chunk["type"] == "audio":
                    audio_buffer.write(chunk["data"])
            audio_buffer.seek(0)
            await update.message.reply_voice(voice=audio_buffer)
        except Exception as e:
            logging.error(f"Voice error: {e}")

# ================= استخراج وب و ویدیو (R4: Web & YouTube Content Extraction) =================

def extract_urls(text: str) -> list[str]:
    """
    Detects and extracts all valid HTTP and HTTPS URLs from text (Requirement R4).
    Strips trailing sentence punctuation while preserving balanced parentheses.
    """
    if not text:
        return []
    raw_urls = re.findall(r'https?://[^\s<>"]+', text)
    cleaned_urls = []
    for raw in raw_urls:
        u = raw.rstrip('.,!?:;')
        if u.endswith(')') and u.count('(') < u.count(')'):
            u = u.rstrip(')')
        if u.endswith(']') and u.count('[') < u.count(']'):
            u = u.rstrip(']')
        if u and u not in cleaned_urls:
            cleaned_urls.append(u)
    return cleaned_urls


def extract_youtube_video_id(url: str) -> Optional[str]:
    """
    Extracts the 11-character YouTube video ID from standard watch, share, embed, shorts, or live URLs.
    Enforces strict host domain boundaries to isolate against domain spoofing.
    """
    if not url:
        return None
    patterns = [
        r'(?:^|https?:\/\/)(?:www\.|m\.|music\.)?youtube\.com\/watch\?.*v=([a-zA-Z0-9_-]{11})',
        r'(?:^|https?:\/\/)(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})',
        r'(?:^|https?:\/\/)(?:www\.|m\.|music\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})',
        r'(?:^|https?:\/\/)(?:www\.|m\.|music\.)?youtube\.com\/live\/([a-zA-Z0-9_-]{11})',
        r'(?:^|https?:\/\/)(?:www\.|m\.|music\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})',
        r'(?:^|https?:\/\/)(?:www\.|m\.|music\.)?youtube\.com\/v\/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def fetch_youtube_transcript(video_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches transcript/subtitles for a YouTube video using youtube-transcript-api.
    Supports multilingual priorities (Persian, English, auto-generated).
    Returns (transcript_text, None) on success, or (None, friendly_error_message).
    """
    try:
        api = YouTubeTranscriptApi()
        transcript_data = None
        # Handle instance .fetch (v1.x+)
        if hasattr(api, "fetch"):
            try:
                transcript_data = api.fetch(video_id, languages=("fa", "en"))
            except Exception:
                try:
                    transcript_list = api.list(video_id)
                    for t in transcript_list:
                        transcript_data = t.fetch()
                        break
                except Exception as inner_e:
                    return None, f"زیرنویس در دسترس نیست ({inner_e})"
        # Handle classmethod .get_transcript (legacy)
        elif hasattr(YouTubeTranscriptApi, "get_transcript"):
            try:
                transcript_data = YouTubeTranscriptApi.get_transcript(video_id, languages=["fa", "en"])
            except Exception:
                try:
                    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                    for t in transcript_list:
                        transcript_data = t.fetch()
                        break
                except Exception as inner_e:
                    return None, f"زیرنویس در دسترس نیست ({inner_e})"

        if transcript_data:
            lines = []
            for item in transcript_data:
                txt = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
                if txt and txt.strip():
                    lines.append(txt.strip())
            full_text = " ".join(lines)
            if full_text.strip():
                return full_text.strip(), None

        return None, "رونوشت یا زیرنویسی برای این ویدیو یافت نشد."
    except Exception as e:
        return None, f"خطا در دریافت زیرنویس ویدیوی یوتیوب: {e}"


async def fetch_web_page_content(url: str, timeout: float = 10.0) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Fetches readable article text, title, and metadata from a web page using httpx and BeautifulSoup4.
    Returns (result_dict, None) on success, or (None, friendly_error_message) on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa,en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as http_client:
            resp = await http_client.get(url)
            if resp.status_code == 404:
                return None, "⚠️ صفحه مورد نظر یافت نشد (خطای ۴۰۴). لطفاً آدرس را بررسی کنید."
            elif resp.status_code in (401, 403):
                return None, "⚠️ دسترسی به این صفحه مسدود است یا نیاز به اشتراک و ورود دارد (خطای دسترسی ۴۰۳)."
            elif resp.status_code >= 500:
                return None, f"⚠️ سرور مقصد با خطا مواجه شد (کد {resp.status_code})."
            elif resp.status_code != 200:
                return None, f"⚠️ دریافت صفحه با کد وضعیت {resp.status_code} متوقف شد."

            # Pre-filter non-HTML / binary payloads to conserve memory and CPU
            content_type = ""
            if hasattr(resp, "headers") and resp.headers:
                ct_raw = resp.headers.get("content-type", "")
                if isinstance(ct_raw, str):
                    content_type = ct_raw.lower()

            if content_type and not any(t in content_type for t in ("text", "html", "xml", "json", "javascript")):
                return None, "⚠️ این لینک به یک فایل باینری یا چندرسانه‌ای اشاره دارد و صفحه وب متنی نیست."

            clean_url_path = url.split("?")[0].lower()
            binary_exts = (".bin", ".zip", ".tar", ".gz", ".exe", ".iso", ".dmg", ".mp4", ".mp3", ".avi", ".mkv", ".wav", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
            if clean_url_path.endswith(binary_exts):
                return None, "⚠️ این لینک به یک فایل باینری یا چندرسانه‌ای اشاره دارد و صفحه وب متنی نیست."

            soup = bs4.BeautifulSoup(resp.text, "html.parser")

            # Extract title
            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            elif soup.find("meta", property="og:title"):
                title = soup.find("meta", property="og:title").get("content", "").strip()

            # Extract meta description
            meta_desc = ""
            desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
            if desc_tag and desc_tag.get("content"):
                meta_desc = desc_tag["content"].strip()

            # Remove unwanted tags
            for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
                tag.decompose()

            # Extract readable text from main container
            container = soup.find("article") or soup.find("main") or soup.find("div", class_=re.compile(r"content|article|post|body|entry", re.I)) or soup.body
            if container:
                paragraphs = container.find_all(["p", "h1", "h2", "h3", "h4", "li"])
                text_blocks = [p.get_text().strip() for p in paragraphs if p.get_text().strip()]
                text = "\n\n".join(text_blocks)
            else:
                text = soup.get_text(separator="\n", strip=True)

            if not text.strip() and soup.body:
                text = soup.body.get_text(separator="\n", strip=True)

            text = text[:60000].strip()
            if not text:
                return None, "⚠️ محتوای متنی قابل استخراجی در این صفحه یافت نشد."

            return {
                "url": url,
                "title": title or "بدون عنوان",
                "meta_description": meta_desc,
                "text": text,
            }, None

    except httpx.TimeoutException:
        return None, "⚠️ مهلت بارگذاری صفحه به پایان رسید (تایم‌اوت ۱۰ ثانیه). سرور مقصد در مهلت مقرر پاسخ نداد."
    except httpx.ConnectError:
        return None, "⚠️ اتصال به سرور مقصد برقرار نشد (خطای شبکه یا دامنه نامعتبر)."
    except Exception as e:
        return None, f"⚠️ خطا در خواندن صفحه وب: {str(e)[:150]}"


async def fetch_and_summarize_url(url: str, user_id: int | str = "default") -> str:
    """
    Fetches content from a web page or YouTube video, then prompts Gemini
    to synthesize a structured executive summary with key takeaways and bullet points (Requirement R4).
    """
    video_id = extract_youtube_video_id(url)
    if video_id:
        transcript, err = fetch_youtube_transcript(video_id)
        if not transcript:
            return (
                f"⚠️ استخراج زیرنویس یا رونوشت ویدیوی یوتیوب امکان‌پذیر نبود.\n"
                f"علت: {err or 'زیرنویس برای این ویدیو در دسترس نیست.'}\n"
                f"🔗 لینک: {url}"
            )

        prompt = (
            "تو یک دستیار هوش مصنوعی تحلیلگر و خلاصه‌ساز حرفه‌ای هستی. "
            "رونوشت زیر مربوط به یک ویدیوی یوتیوب است. "
            "لطفاً یک خلاصه‌ی اجرایی و ساختاریافته به زبان فارسی ارائه بده که شامل موارد زیر باشد:\n"
            "۱. 📌 **موضوع و ایده محوری ویدیو**\n"
            "۲. 💡 **نکات کلیدی و آموزه‌های اصلی (Key Takeaways)** به صورت بولت‌پوینت‌های مجزا\n"
            "۳. ⏱️ **سرفصل‌ها و مباحث مطرح شده**\n"
            "۴. 🎯 **جمع‌بندی نهایی**\n\n"
            f"🔗 آدرس ویدیو: {url}\n"
            f"🎥 شناسه ویدیو: {video_id}\n\n"
            f"📜 متن رونوشت ویدیو:\n{transcript[:45000]}"
        )
    else:
        page_info, err = await fetch_web_page_content(url, timeout=10.0)
        if not page_info:
            return f"{err or '⚠️ دریافت محتوای صفحه با خطا مواجه شد.'}\n🔗 آدرس: {url}"

        prompt = (
            "تو یک دستیار هوش مصنوعی تحلیلگر و خلاصه‌ساز حرفه‌ای هستی. "
            "متن زیر از یک صفحه وب/مقاله استخراج شده است. "
            "لطفاً یک خلاصه‌ی جامع، دقیق و ساختاریافته به زبان فارسی ارائه بده که شامل موارد زیر باشد:\n"
            "۱. 📌 **عنوان و موضوع اصلی**\n"
            "۲. 💡 **نکات کلیدی و آموزه‌های اصلی (Key Takeaways)** به صورت بولت‌پوینت\n"
            "۳. 🔍 **جزئیات و تحلیل بخش‌های مهم**\n"
            "۴. 🎯 **نتیجه‌گیری**\n\n"
            f"🔗 آدرس صفحه: {page_info['url']}\n"
            f"📌 عنوان استخراج شده: {page_info['title']}\n"
            f"📝 متادیتا: {page_info['meta_description']}\n\n"
            f"📄 متن کامل مقاله:\n{page_info['text'][:45000]}"
        )

    uid_str = str(user_id)
    u = db.get_or_create_user(uid_str)
    active_chat = db.get_active_chat(uid_str)
    chat_id = active_chat["id"] if active_chat else 1

    try:
        chat_resp = await generate_chat_response(uid_str, chat_id, prompt)
        clean_text = chat_resp.clean_text if hasattr(chat_resp, "clean_text") else chat_resp[0]
        if clean_text:
            return clean_text
    except Exception:
        try:
            summary = await generate_multimodal_response(uid_str, [prompt])
            if summary:
                return summary
        except Exception as e:
            return f"⚠️ خلاصه کردن محتوا با خطا مواجه شد: {str(e)[:150]}"

    return "⚠️ متأسفانه پاسخی برای خلاصه‌سازی محتوا از جمینای دریافت نشد."


# ================= ساخت و خروجی پویای اسناد (R5: Dynamic Document Export) =================

def get_persian_font_path() -> Optional[str]:
    """Finds available TrueType font supporting Persian/Arabic text."""
    candidate_paths = [
        os.path.join(os.path.dirname(__file__), "fonts", "tahoma.ttf"),
        os.path.join(os.path.dirname(__file__), "fonts", "arial.ttf"),
        os.path.join(os.path.dirname(__file__), "fonts", "Vazirmatn.ttf"),
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/ARIALUNI.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
    ]
    for p in candidate_paths:
        if os.path.exists(p):
            return p
    return None


def setup_pdf_fonts() -> str:
    """Registers Persian TTF font with ReportLab if available, returns registered font name."""
    font_name = "PersianFont"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    font_path = get_persian_font_path()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
            return font_name
        except Exception as e:
            logging.warning(f"Could not register font {font_path}: {e}")
    return "Helvetica"


def reshape_persian_text(text: str) -> str:
    """Reshapes and applies BiDi algorithm for Persian/Arabic RTL text."""
    if not text:
        return ""
    try:
        if re.search(r"[\u0600-\u06FF]", text):
            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
    except Exception:
        pass
    return text


def generate_excel_report(data: list[dict], title: str = "گزارش") -> io.BytesIO:
    """
    Generates a beautifully styled in-memory Excel spreadsheet (.xlsx) using openpyxl (Requirement R5).
    Includes dark-blue headers, zebra striping, auto-fit column widths, thin borders, and RTL view.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    clean_sheet_title = re.sub(r'[\\/*?:\[\]]', '_', title)[:30] or "Sheet1"
    ws.title = clean_sheet_title
    ws.sheet_view.rightToLeft = True

    if not data:
        ws.append(["پیام"])
        ws.append(["هیچ داده‌ای برای نمایش وجود ندارد."])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = f"{re.sub(r'[\\/*?:\[\]]', '_', title)[:40]}.xlsx"
        return buf

    raw_headers = []
    for row_dict in data:
        if isinstance(row_dict, dict):
            for k in row_dict.keys():
                if k not in raw_headers:
                    raw_headers.append(k)

    if not raw_headers:
        raw_headers = ["ستون_1"]

    headers = []
    for h in raw_headers:
        h_str = ILLEGAL_CHARACTERS_RE.sub('', str(h))
        if h_str.startswith(('=', '+', '-', '@')):
            h_str = "'" + h_str
        headers.append(h_str)

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1B4F72", end_color="1B4F72", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    even_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    odd_fill = PatternFill(start_color="EBF5FB", end_color="EBF5FB", fill_type="solid")
    row_font = Font(name="Calibri", size=10)

    thin_border = Border(
        left=Side(style="thin", color="D5D8DC"),
        right=Side(style="thin", color="D5D8DC"),
        top=Side(style="thin", color="D5D8DC"),
        bottom=Side(style="thin", color="D5D8DC")
    )

    ws.append(headers)
    ws.row_dimensions[1].height = 26
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    for row_idx, row_data in enumerate(data, start=2):
        row_vals = []
        for h in raw_headers:
            val = row_data.get(h, "") if isinstance(row_data, dict) else ""
            if val is None:
                row_vals.append("")
            elif isinstance(val, (int, float)):
                row_vals.append(val)
            elif isinstance(val, str):
                cleaned_val = ILLEGAL_CHARACTERS_RE.sub('', val)
                if cleaned_val.startswith(('=', '+', '-', '@')):
                    cleaned_val = "'" + cleaned_val
                row_vals.append(cleaned_val)
            else:
                cleaned_val = ILLEGAL_CHARACTERS_RE.sub('', str(val))
                if cleaned_val.startswith(('=', '+', '-', '@')):
                    cleaned_val = "'" + cleaned_val
                row_vals.append(cleaned_val)

        ws.append(row_vals)
        ws.row_dimensions[row_idx].height = 20
        fill_to_use = odd_fill if (row_idx % 2 == 1) else even_fill
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = row_font
            cell.fill = fill_to_use
            cell.border = thin_border
            val = cell.value
            if isinstance(val, (int, float)):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)

    for col_idx, col in enumerate(ws.columns, start=1):
        max_len = 0
        col_letter = get_column_letter(col_idx)
        for cell in list(col)[:100]:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = f"{re.sub(r'[\\/*?:\[\]]', '_', title)[:40]}.xlsx"
    return buf


def generate_pdf_document(title: str, text: str) -> io.BytesIO:
    """
    Generates a professionally formatted in-memory PDF document using ReportLab (Requirement R5).
    Applies arabic_reshaper and python-bidi for Persian/Arabic RTL text, standard TrueType fonts,
    and elegant document layout with title, divider, and paragraphs.
    """
    font_name = setup_pdf_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "PersianTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=15,
        leading=22,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#1B4F72"),
        spaceAfter=12
    )

    body_style = ParagraphStyle(
        "PersianBody",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=16,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#2C3E50"),
        spaceAfter=6
    )

    bullet_style = ParagraphStyle(
        "PersianBullet",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=16,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#1A5276"),
        leftIndent=12,
        spaceAfter=4
    )

    story = []
    reshaped_title = xml.sax.saxutils.escape(reshape_persian_text(title))
    story.append(Paragraph(reshaped_title, title_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1B4F72"), spaceAfter=14))

    lines = text.split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 6))
            continue

        if stripped.startswith(("#", "===")):
            clean_h = stripped.lstrip("#=").strip()
            story.append(Spacer(1, 8))
            escaped_h = xml.sax.saxutils.escape(reshape_persian_text(clean_h))
            story.append(Paragraph(escaped_h, title_style))
        elif stripped.startswith(("•", "-", "*", "▫️", "▪️", "🔹")):
            clean_b = stripped.lstrip("•-*▫️▪️🔹 ").strip()
            escaped_b = xml.sax.saxutils.escape(reshape_persian_text(clean_b))
            story.append(Paragraph(f"• {escaped_b}", bullet_style))
        else:
            escaped_s = xml.sax.saxutils.escape(reshape_persian_text(stripped))
            story.append(Paragraph(escaped_s, body_style))

    doc.build(story)
    buf.seek(0)
    buf.name = f"{re.sub(r'[\\/*?:\[\]]', '_', title)[:40]}.pdf"
    return buf


def generate_chart(data: dict, chart_type: str = "bar") -> io.BytesIO:
    """
    Generates an in-memory statistical chart (bar, line, or pie) using Matplotlib (Requirement R5).
    Applies RTL reshaping for Persian/Arabic labels and titles.
    Hardened against type coercion, numeric filtering, empty datasets, and pie chart negatives/zeros.
    """
    data = data or {}
    chart_type_clean = str(chart_type or "bar").lower().strip()
    title = data.get("title", "نمودار تحلیلی")

    raw_labels = []
    raw_values = []

    if "labels" in data and "values" in data:
        raw_labels = list(data.get("labels") or [])
        raw_values = list(data.get("values") or [])
    elif "series" in data and isinstance(data["series"], dict):
        raw_labels = list(data["series"].keys())
        raw_values = list(data["series"].values())
    else:
        filtered = {k: v for k, v in data.items() if k != "title"}
        raw_labels = list(filtered.keys())
        raw_values = list(filtered.values())

    clean_labels = []
    clean_values = []
    max_len = max(len(raw_labels), len(raw_values))
    for i in range(max_len):
        lbl = raw_labels[i] if i < len(raw_labels) else f"دسته {i+1}"
        val = raw_values[i] if i < len(raw_values) else 0.0
        try:
            val_float = float(val) if val is not None else 0.0
            if math.isnan(val_float) or math.isinf(val_float):
                val_float = 0.0
        except (ValueError, TypeError):
            val_float = 0.0
        clean_labels.append(str(lbl))
        clean_values.append(val_float)

    reshaped_labels = [reshape_persian_text(str(l)) for l in clean_labels]
    reshaped_title = reshape_persian_text(str(title))

    font_prop = None
    font_path = get_persian_font_path()
    if font_path and os.path.exists(font_path):
        try:
            font_prop = fm.FontProperties(fname=font_path)
        except Exception:
            font_prop = None

    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)

    # Validate pie chart conditions: non-empty, all non-negative, and sum > 0
    if chart_type_clean in ("pie", "دایره‌ای", "دایره"):
        if not clean_values or sum(clean_values) <= 0 or any(v < 0 for v in clean_values):
            chart_type_clean = "bar"

    if chart_type_clean in ("pie", "دایره‌ای", "دایره"):
        wedges, texts, autotexts = ax.pie(
            clean_values,
            labels=reshaped_labels,
            autopct="%1.1f%%",
            startangle=140,
            textprops={"fontproperties": font_prop} if font_prop else None
        )
        if font_prop:
            for t in texts + autotexts:
                t.set_fontproperties(font_prop)
    elif chart_type_clean in ("line", "خطی", "خط"):
        if clean_labels and clean_values:
            ax.plot(
                reshaped_labels,
                clean_values,
                marker="o",
                color="#2980B9",
                linewidth=2.5,
                markersize=6
            )
        else:
            ax.plot([], [])
        ax.grid(True, linestyle="--", alpha=0.5)
    else:  # default "bar"
        if clean_labels and clean_values:
            colors_list = ["#2E86C1", "#17A589", "#F39C12", "#D35400", "#8E44AD", "#27AE60", "#C0392B"]
            bar_colors = [colors_list[i % len(colors_list)] for i in range(len(clean_labels))]
            bars = ax.bar(reshaped_labels, clean_values, color=bar_colors, edgecolor="black", linewidth=0.5)
            ax.grid(axis="y", linestyle="--", alpha=0.5)
            for bar in bars:
                height = bar.get_height()
                ax.annotate(
                    f"{height:g}",
                    xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3 if height >= 0 else -10),
                    textcoords="offset points",
                    ha="center", va="bottom" if height >= 0 else "top",
                    fontsize=9
                )
        else:
            ax.bar([], [])
            ax.grid(axis="y", linestyle="--", alpha=0.5)

    if font_prop:
        ax.set_title(reshaped_title, fontproperties=font_prop, fontsize=14, pad=12)
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_fontproperties(font_prop)
    else:
        ax.set_title(reshaped_title, fontsize=14, pad=12)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    buf.name = f"chart_{chart_type_clean}.png"
    return buf


def extract_markdown_table(text: str) -> Optional[List[Dict[str, str]]]:
    """Extracts rows from a markdown table in text if present."""
    if not text:
        return None
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    table_lines = [l for l in lines if l.startswith("|") and l.endswith("|")]
    if len(table_lines) >= 3 and any("---" in l for l in table_lines):
        headers = [c.strip() for c in table_lines[0].strip("|").split("|")]
        data_rows = []
        for line in table_lines[1:]:
            if "---" in line:
                continue
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) == len(headers):
                row_dict = {headers[i]: cols[i] for i in range(len(headers))}
                data_rows.append(row_dict)
        if data_rows:
            return data_rows
    return None


def detect_export_intent(text: str) -> Optional[str]:
    """Detects if user is asking for an export in natural language."""
    if not text:
        return None
    lower_text = text.lower()
    if any(k in lower_text for k in ["خروجی اکسل", "دانلود اکسل", "فایل اکسل", "تبدیل به اکسل", "اکسل بده", "خروجی excel", "دانلود excel", "خروجی xlsx", "دانلود جدول"]):
        return "excel"
    if any(k in lower_text for k in ["خروجی pdf", "دانلود pdf", "خروجی پی دی اف", "خروجی پی‌دی‌اف", "دانلود پی دی اف", "دانلود پی‌دی‌اف", "فایل pdf", "فایل پی دی اف", "پی دی اف بده"]):
        return "pdf"
    if any(k in lower_text for k in ["خروجی نمودار", "رسم نمودار", "نمودار بکش", "دانلود نمودار", "نمودار بده", "خروجی chart", "رسم chart"]):
        return "chart"
    return None


async def perform_chat_export(message_obj, active_chat: dict, messages: list, export_format: str):
    """Executes file export and delivers document/photo to Telegram (Requirement R5)."""
    try:
        chat_title = active_chat.get("title", "گفتگو")

        if export_format == "excel":
            table_data = None
            for role, msg_text in reversed(messages):
                if role == "model":
                    table_data = extract_markdown_table(msg_text)
                    if table_data:
                        break

            if table_data:
                bio = generate_excel_report(table_data, title=f"جدول_{chat_title}")
                caption = f"📊 **خروجی اکسل جدول استخراج‌شده از گفتگوی «{chat_title}»**"
            else:
                rows = []
                for idx, (role, msg_text) in enumerate(messages, start=1):
                    sender = "کاربر" if role == "user" else "جمینای"
                    rows.append({
                        "ردیف": idx,
                        "فرستنده": sender,
                        "متن پیام": msg_text,
                    })
                bio = generate_excel_report(rows, title=f"چت_{chat_title}")
                caption = f"📊 **خروجی اکسل کامل گفتگوی «{chat_title}»**"

            await message_obj.reply_document(document=bio, caption=caption, parse_mode="Markdown")

        elif export_format == "pdf":
            pdf_lines = [f"# گفتگوی: {chat_title}\n"]
            for role, msg_text in messages:
                sender = "👤 کاربر (شما)" if role == "user" else "🤖 هوش مصنوعی جمینای"
                pdf_lines.append(f"### {sender}:")
                pdf_lines.append(msg_text)
                pdf_lines.append("────────────────────")

            full_pdf_text = "\n".join(pdf_lines)
            bio = generate_pdf_document(title=f"گفتگوی_{chat_title}", text=full_pdf_text)
            caption = f"📑 **خروجی PDF گفتگوی «{chat_title}» با چیدمان راست‌چین و فونت استاندارد**"
            await message_obj.reply_document(document=bio, caption=caption, parse_mode="Markdown")

        elif export_format == "chart":
            user_msgs = sum(1 for role, _ in messages if role == "user")
            model_msgs = sum(1 for role, _ in messages if role == "model")
            chart_data = {
                "labels": ["پیام‌های کاربر", "پاسخ‌های جمینای"],
                "values": [user_msgs, model_msgs],
                "title": f"تحلیل آماری پیام‌های {chat_title}"
            }
            bio = generate_chart(chart_data, chart_type="bar")
            caption = f"📈 **نمودار مقایسه پیام‌های گفتگوی «{chat_title}»**\n👤 کاربر: {user_msgs} پیام\n🤖 جمینای: {model_msgs} پاسخ"
            try:
                await message_obj.reply_photo(photo=bio, caption=caption, parse_mode="Markdown")
            except Exception:
                bio.seek(0)
                await message_obj.reply_document(document=bio, caption=caption, parse_mode="Markdown")

        elif export_format == "txt":
            lines = [f"=== تاریخچه گفتگوی: {chat_title} ==="]
            for role, msg in messages:
                sender = "ابوالفضل" if role == "user" else "جمینای"
                lines.append(f"\n[{sender}]:\n{msg}")
            content = "\n".join(lines)
            file_bytes = io.BytesIO(content.encode("utf-8"))
            file_bytes.name = f"chat_{active_chat.get('id', 'export')}.txt"
            await message_obj.reply_document(document=file_bytes, caption=f"📁 خروجی متنی گفتگوی «{chat_title}»")
    except Exception as e:
        await message_obj.reply_text(f"⚠️ متأسفانه در ساخت و ارسال فایل خروجی خطایی رخ داد ({e})")

# ================= هندلرهای دستورات تلگرام =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if context.args and context.args[0] == INVITE_CODE:
        ALLOWED_USERS.add(user_id)
        await update.message.reply_text("🎉 به جمع ما خوش اومدی رفیق! دسترسی برات باز شد.")

    if not check_access(user_id):
        await update.message.reply_text("⛔ شرمنده، برای استفاده از این ربات باید لینک دعوت داشته باشی.")
        return

    db.get_or_create_user(user_id)
    active_chat = db.get_active_chat(user_id)

    welcome_text = (
        "سلام! من دستیار هوش مصنوعی جمنای ابوالفضل هستم. چه کمکی از من برمیاد؟ 😉\n\n"
        f"💬 **گفتگوی فعال فعلی:** `{active_chat['title']}`\n"
        "🟡 **مدل فعال:** Gemini 3.8 Flash (تفکر استاندارد)\n"
        "🌐 **سرچ زنده اینترنت:** خاموش (با /search روشن می‌شه)\n"
        "🔊 **پاسخ صوتی:** خاموش (با /voice روشن می‌شه)\n\n"
        "🗂️ **مدیریت گفتگوها و تاریخچه:**\n"
        "➕ /new : شروع یک گفتگوی جدید بدون پاک شدن قبلی‌ها\n"
        "📂 /chats : دیدن لیست چت‌های قبلی و بازگشت به هرکدام\n"
        "✏️ /rename <اسم جدید> : تغییر نام چت فعلی\n"
        "🗑️ /delete : حذف این گفتگو\n"
        "🧹 /clear : پاک کردن پیام‌های چت فعلی\n"
        "📁 /export : دانلود فایل تاریخچه چت فعلی\n\n"
        "🎨 **خلق تصویر با هوش مصنوعی:**\n"
        "🖼️ /draw یا /image <توصیف> : ساخت تصویر با هوش مصنوعی Imagen 3\n\n"
        "⚙️ **تنظیمات هوش مصنوعی:**\n"
        "🔘 /model : انتخاب مدل و سطح تفکر\n"
        "🎭 /persona : تغییر لحن و تخصص من\n"
        "🌐 /search : سرچ زنده گوگل\n"
        "🔊 /voice : فعال‌سازی پاسخ با ویس فارسی\n\n"
        "🎬 *محاسبات ریاضی دقیق، اجرای خودکار کدهای پایتون، تحلیل تصاویر، ویس، ویدیو و اسناد با هوش مصنوعی جمینای.*"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    new_id = db.create_new_chat(user_id, "گفتگوی جدید")
    await update.message.reply_text(
        "➕ **یک گفتگوی کاملاً جدید برات باز شد!**\n\n"
        "چت‌های قبلی‌ات سر جاشونه و هر وقت خواستی با دستور /chats می‌تونی بهشون برگردی.\n"
        "خب بگو ببینم راجع به چی گپ بزنیم؟ 😉",
        parse_mode="Markdown"
    )

async def list_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    u = db.get_or_create_user(user_id)
    all_chats = db.get_user_chats(user_id)
    active_id = u["active_chat_id"]

    if not all_chats:
        await update.message.reply_text("هنوز هیچ گفتگویی ثبت نشده است.")
        return

    keyboard = []
    for c in all_chats:
        prefix = "👉 " if c["id"] == active_id else ""
        badge = " (فعال)" if c["id"] == active_id else ""
        btn_text = f"{prefix}💬 {c['title']} ({c['msg_count']} پیام){badge}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"switch_chat:{c['id']}")])

    action_row = [
        InlineKeyboardButton("➕ چت جدید", callback_data="action:new_chat"),
        InlineKeyboardButton("🗑️ حذف این چت", callback_data=f"action:delete_chat:{active_id}")
    ]
    keyboard.append(action_row)

    await update.message.reply_text(
        "📂 **لیست گفتگوهای شما:**\nبرای رفتن به هر گفتگو، رویش کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def rename_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    if not context.args:
        await update.message.reply_text("لطفاً نام جدید را بنویسید. مثلاً:\n`/rename ترجمه مقاله هوش مصنوعی`", parse_mode="Markdown")
        return

    new_name = " ".join(context.args)
    active_chat = db.get_active_chat(user_id)
    db.rename_chat(active_chat["id"], new_name)
    await update.message.reply_text(f"✏️ نام این گفتگو به «**{new_name}**» تغییر کرد.", parse_mode="Markdown")

async def delete_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    active_chat = db.get_active_chat(user_id)
    new_active_id = db.delete_chat(user_id, active_chat["id"])
    new_active = db.get_active_chat(user_id)
    await update.message.reply_text(
        f"🗑️ گفتگوی «{active_chat['title']}» حذف شد.\n"
        f"👉 اکنون در گفتگوی «**{new_active['title']}**» هستید.",
        parse_mode="Markdown"
    )

async def model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    u = db.get_or_create_user(user_id)
    current = u["model"]

    keyboard = []
    for k, v in AVAILABLE_MODELS.items():
        tick = "✅ " if k == current else ""
        keyboard.append([InlineKeyboardButton(f"{tick}{v['title']}", callback_data=f"set_model:{k}")])

    await update.message.reply_text("کدوم مدل رو انتخاب می‌کنی؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def persona_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    u = db.get_or_create_user(user_id)
    current = u["persona"]

    keyboard = []
    for k, v in PERSONAS.items():
        tick = "✅ " if k == current else ""
        keyboard.append([InlineKeyboardButton(f"{tick}{v['title']}", callback_data=f"set_persona:{k}")])

    await update.message.reply_text("دوست داری چه نقشی به خودم بگیرم؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    u = db.get_or_create_user(user_id)
    new_val = 0 if u["search_enabled"] else 1
    db.update_user_setting(user_id, "search_enabled", new_val)
    status = "روشن شد 🟢 (الان به اخبار و دیتای لحظه‌ای وصلم)" if new_val else "خاموش شد 🔴"
    await update.message.reply_text(f"جستجوی زنده گوگل: {status}")

async def toggle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    u = db.get_or_create_user(user_id)
    new_val = 0 if u["voice_reply"] else 1
    db.update_user_setting(user_id, "voice_reply", new_val)
    status = "روشن شد 🟢 (از الان با ویس هم جوابت رو می‌دم)" if new_val else "خاموش شد 🔴"
    await update.message.reply_text(f"پاسخ صوتی: {status}")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    active_chat = db.get_active_chat(user_id)
    db.clear_chat_messages(active_chat["id"])
    await update.message.reply_text(f"🧹 پیام‌های گفتگوی «{active_chat['title']}» پاک شد. راجع به چی گپ بزنیم؟")

async def export_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id):
        return

    active_chat = db.get_active_chat(user_id)
    messages = db.get_chat_messages(active_chat["id"], limit=200)
    if not messages:
        await update.message.reply_text("در این گفتگو هنوز پیامی برای خروجی گرفتن نیست!")
        return

    export_format = None
    if context.args:
        arg = context.args[0].lower().strip()
        if arg in ("excel", "xlsx", "اکسل"):
            export_format = "excel"
        elif arg in ("pdf", "پی‌دی‌اف", "پی_دی_اف"):
            export_format = "pdf"
        elif arg in ("chart", "graph", "نمودار"):
            export_format = "chart"
        elif arg in ("txt", "متن"):
            export_format = "txt"

    if not export_format:
        keyboard = [
            [
                InlineKeyboardButton("📊 خروجی اکسل (.xlsx)", callback_data="export_action:excel"),
                InlineKeyboardButton("📑 خروجی پی‌دی‌اف (.pdf)", callback_data="export_action:pdf")
            ],
            [
                InlineKeyboardButton("📈 نمودار تحلیلی پیام‌ها", callback_data="export_action:chart"),
                InlineKeyboardButton("📁 فایل متنی (.txt)", callback_data="export_action:txt")
            ]
        ]
        lines = [f"=== تاریخچه گفتگوی: {active_chat['title']} ==="]
        for role, msg in messages:
            sender = "ابوالفضل" if role == "user" else "جمینای"
            lines.append(f"\n[{sender}]:\n{msg}")

        content = "\n".join(lines)
        file_bytes = io.BytesIO(content.encode("utf-8"))
        file_bytes.name = f"chat_{active_chat['id']}.txt"
        await update.message.reply_document(
            document=file_bytes,
            caption=(
                f"📁 **خروجی گفتگوی «{active_chat['title']}»**\n\n"
                f"💡 برای دریافت مستقیم سایر فرمت‌ها، روی دکمه‌های زیر بزنید یا از دستورات زیر استفاده کنید:\n"
                f"▫️ `/export excel` : فایل اکسل با جدول‌بندی زیبا\n"
                f"▫️ `/export pdf` : فایل PDF خوانا و راست‌چین\n"
                f"▫️ `/export chart` : نمودار آماری گفتگو"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await perform_chat_export(update.message, active_chat, messages, export_format)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data

    if data.startswith("switch_chat:"):
        target_chat_id = int(data.split(":")[1])
        if db.switch_active_chat(user_id, target_chat_id):
            active_chat = db.get_active_chat(user_id)
            msgs = db.get_chat_messages(target_chat_id)
            await query.edit_message_text(
                f"✅ **وارد گفتگوی «{active_chat['title']}» شدید!**\n"
                f"📊 تعداد پیام‌های قبلی: {len(msgs)}\n\n"
                f"می‌تونید ادامه‌ش بدید یا سوال جدید بپرسید.",
                parse_mode="Markdown"
            )

    elif data == "action:new_chat":
        new_id = db.create_new_chat(user_id, "گفتگوی جدید")
        await query.edit_message_text("➕ گفتگوی جدید باز شد! پیام خودت رو بفرست.")

    elif data.startswith("action:delete_chat:"):
        chat_id_to_del = int(data.split(":")[2])
        db.delete_chat(user_id, chat_id_to_del)
        new_active = db.get_active_chat(user_id)
        await query.edit_message_text(f"🗑️ گفتگو حذف شد.\n👉 اکنون در گفتگوی «**{new_active['title']}**» هستید.", parse_mode="Markdown")

    elif data.startswith("set_model:"):
        key = data.split(":")[1]
        db.update_user_setting(user_id, "model", key)
        await query.edit_message_text(f"✅ مدل با موفقیت به `{AVAILABLE_MODELS[key]['title']}` تغییر یافت.", parse_mode="Markdown")

    elif data.startswith("set_persona:"):
        key = data.split(":")[1]
        db.update_user_setting(user_id, "persona", key)
        await query.edit_message_text(f"✅ تخصص و لحن با موفقیت به `{PERSONAS[key]['title']}` تغییر یافت.", parse_mode="Markdown")

    elif data.startswith("export_action:"):
        fmt = data.split(":")[1]
        active_chat = db.get_active_chat(user_id)
        messages = db.get_chat_messages(active_chat["id"], limit=200)
        if not messages:
            await query.message.reply_text("در این گفتگو هنوز پیامی برای خروجی گرفتن نیست!")
            return
        await perform_chat_export(query.message, active_chat, messages, fmt)

    elif data.startswith("show_thought:"):
        thought_id = data.split(":")[1]
        thought_content = db.get_thought(thought_id)
        if thought_content:
            chunk_size = 3500
            if len(thought_content) <= chunk_size:
                header = "🧠 **فرآیند و زنجیره تفکر و استدلال جمینای:**\n\n"
                try:
                    await query.message.reply_text(
                        f"{header}||{thought_content}||",
                        parse_mode="Markdown"
                    )
                except Exception:
                    await query.message.reply_text(f"{header}{thought_content}")
            else:
                chunks = [thought_content[i:i + chunk_size] for i in range(0, len(thought_content), chunk_size)]
                total_chunks = len(chunks)
                for idx, chunk in enumerate(chunks):
                    header = f"🧠 **فرآیند و زنجیره تفکر و استدلال جمینای (بخش {idx + 1} از {total_chunks}):**\n\n"
                    try:
                        await query.message.reply_text(
                            f"{header}||{chunk}||",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        await query.message.reply_text(f"{header}{chunk}")
        else:
            await query.message.reply_text("⚠️ استدلال ذخیره‌شده برای این پیام یافت نشد یا منقضی شده است.")

    elif data.startswith("snooze:"):
        parts = data.split(":")
        if len(parts) >= 3:
            minutes = int(parts[1])
            rem_id = int(parts[2])
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            new_due_utc = (now_utc + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.snooze_reminder(rem_id, new_due_utc)
            new_tehran = (now_utc + datetime.timedelta(minutes=minutes)).astimezone(TEHRAN_TZ).strftime("%H:%M - %Y/%m/%d")
            snooze_text = (
                f"⏰ **یادآور به مدت {minutes} دقیقه به تعویق افتاد.**\n"
                f"⏳ موعد جدید: `{new_tehran}` (به وقت تهران)"
            )
            try:
                await query.edit_message_text(snooze_text, parse_mode="Markdown")
            except Exception:
                await query.message.reply_text(snooze_text, parse_mode="Markdown")

    elif data.startswith("dismiss:"):
        parts = data.split(":")
        if len(parts) >= 2:
            rem_id = int(parts[1])
            db.update_reminder_status(rem_id, "dismissed")
            dismiss_text = "✅ **یادآور با موفقیت پایان یافت (انجام شد).**"
            try:
                await query.edit_message_text(dismiss_text, parse_mode="Markdown")
            except Exception:
                await query.message.reply_text(dismiss_text, parse_mode="Markdown")

# ================= هندلرهای پیام و چندرسانه‌ای =================

REMINDER_TRIGGER_RE = re.compile(
    r'(?:^|\s)(?:یادم\s*بنداز|یادآوری\s*(?:کن|کنید|بشه|شود)?|یادم\s*باشه|به\s*من\s*یادآوری\s*کن|remind\s*(?:me)?)(?:\s|$)',
    re.IGNORECASE
)
REMINDER_RELATIVE_RE = re.compile(
    r'(?:[0-9۰-۹]+(?:[.\u066b][0-9۰-۹]+)?|نیم|یک\s*ربع|دو|سه|چهار|پنج|شش|شیش|هفت|هشت|نه|ده)\s*(?:دقیقه|ساعت|روز|هفته|m|min|h|hr|d|w)\s+(?:دیگه|بعد|دیگر|later)\b',
    re.IGNORECASE
)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    if not update.message or not update.message.text: return
    user_text = update.message.text

    active_chat = db.get_active_chat(user_id)
    u = db.get_or_create_user(user_id)

    # 1. بررسی درخواست خروجی در زبان طبیعی (Requirement R5)
    export_intent = detect_export_intent(user_text)
    if export_intent:
        messages = db.get_chat_messages(active_chat["id"], limit=200)
        if not messages:
            await update.message.reply_text("در این گفتگو هنوز پیامی برای خروجی گرفتن نیست!")
            return
        await perform_chat_export(update.message, active_chat, messages, export_intent)
        return

    # 2. بررسی وجود لینک‌های وب و ویدیوهای یوتیوب (Requirement R4)
    urls = extract_urls(user_text)
    if urls:
        target_url = urls[0]
        is_youtube = extract_youtube_video_id(target_url) is not None
        if is_youtube:
            steps = [
                "🎥 در حال بررسی و شناسایی ویدیوی یوتیوب...",
                "📜 در حال دریافت و تحلیل رونوشت (Transcript)...",
                "🧠 در حال تدوین خلاصه‌ی نکات کلیدی توسط جمینای..."
            ]
        else:
            steps = [
                "🔗 در حال شناسایی و بررسی آدرس اینترنتی...",
                "🌐 در حال استخراج محتوای متنی صفحه...",
                "🧠 در حال تدوین خلاصه‌ی ساختاریافته توسط جمینای..."
            ]

        tracker = ProgressTracker(update.message, steps, min_step_duration=1.4, min_total_duration=2.5)
        await tracker.start()
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

        try:
            summary_text = await fetch_and_summarize_url(target_url, user_id=user_id)
            db.add_message(active_chat["id"], "user", user_text)
            db.add_message(active_chat["id"], "model", summary_text)
            await tracker.finish(summary_text, update)
            await reply_with_optional_voice(update, summary_text, user_id, send_text=False)
        except Exception as e:
            logging.error(f"URL extraction error: {e}")
            await tracker.finish(f"⚠️ خطا در تحلیل و خلاصه‌سازی لینک ({e})", update)
        return

    # 3. بررسی درخواست یادآوری و زمان‌بندی تسک‌ها (Requirement R3)
    is_remind_cmd = user_text.strip().startswith("/remind")
    has_explicit_trigger = bool(REMINDER_TRIGGER_RE.search(user_text))
    is_question = bool(re.search(r'[?؟]|\b(آیا|چرا|چطور|چگونه|کجا|چند|چقدر|چیست|کیست)\b', user_text))
    has_relative_future = bool(REMINDER_RELATIVE_RE.search(user_text)) and not is_question

    if is_remind_cmd or has_explicit_trigger or has_relative_future:
        parsed_rem = parse_reminder_input(user_text)
        if parsed_rem is not None and hasattr(parsed_rem, "__await__"):
            parsed_rem = await parsed_rem
        if parsed_rem:
            due_at_utc, reminder_text = parsed_rem
            chat_id = update.effective_chat.id
            msg_id = update.message.message_id
            rem_id = db.add_reminder(
                user_id=int(user_id) if str(user_id).isdigit() else user_id,
                chat_id=chat_id,
                message_id=msg_id,
                reminder_text=reminder_text,
                due_at_utc=due_at_utc
            )
            due_dt_utc = datetime.datetime.fromisoformat(due_at_utc.replace("Z", "+00:00"))
            due_dt_tehran = due_dt_utc.astimezone(TEHRAN_TZ)
            time_display = due_dt_tehran.strftime("%H:%M - %Y/%m/%d")

            confirm_msg = (
                f"⏰ **یادآور با موفقیت در سیستم ثبت شد!**\n\n"
                f"📝 **یادداشت:** {reminder_text}\n"
                f"⏳ **موعد یادآوری:** `{time_display}` (به وقت تهران)\n"
                f"💾 **وضعیت:** ماندگار در دیتابیس (شناسه: `#{rem_id}`)"
            )
            try:
                await update.message.reply_text(confirm_msg, parse_mode="Markdown", reply_to_message_id=msg_id)
            except Exception:
                await update.message.reply_text(confirm_msg, parse_mode="Markdown")
            return

    if u.get("search_enabled"):
        steps = [
            "🤔 در حال دریافت و بررسی پیام شما...",
            "🌐 در حال جستجو در گوگل و جمع‌آوری جدیدترین اطلاعات وب...",
            "✍️ در حال دسته‌بندی داده‌ها و نگارش پاسخ نهایی..."
        ]
    else:
        steps = [
            "🤔 در حال خواندن پیام و تحلیل موضوع...",
            "🧠 در حال تفکر عمیق و پردازش موضوع توسط جمینای...",
            "✍️ در حال جمع‌بندی نکات و نگارش پاسخ نهایی..."
        ]

    tracker = ProgressTracker(update.message, steps, min_step_duration=1.4, min_total_duration=2.5)
    await tracker.start()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        chat_resp = await generate_chat_response(user_id, active_chat["id"], user_text)
        clean_text, executed_code, execution_result, thought_text = chat_resp
        display_text = chat_resp.formatted_text

        reply_markup = None
        if thought_text and thought_text.strip():
            thought_id = uuid.uuid4().hex[:12]
            db.save_thought(thought_id, user_id, active_chat["id"], thought_text)
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 مشاهده فرآیند استدلال", callback_data=f"show_thought:{thought_id}")]
            ])

        db.add_message(active_chat["id"], "user", user_text)
        db.add_message(active_chat["id"], "model", display_text)
        await tracker.finish(display_text, update, reply_markup=reply_markup)
        await reply_with_optional_voice(update, clean_text, user_id, send_text=False)
    except Exception as e:
        logging.error(f"Text error: {e}")
        await tracker.finish("⚠️ سرورهای جمینای در حال حاضر مشغول هستند. لطفاً لحظاتی دیگر پیام دهید.", update)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    if not update.message or not update.message.photo: return

    steps = [
        "📸 در حال دریافت و دانلود باکیفیت تصویر از تلگرام...",
        "👁️ در حال اسکن دقیق و تشخیص عناصر بصری با هوش مصنوعی...",
        "✍️ در حال آماده‌سازی تحلیل و نگارش توضیحات تصویر..."
    ]
    tracker = ProgressTracker(update.message, steps, min_step_duration=1.4, min_total_duration=2.5)
    await tracker.start()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
    except Exception:
        await tracker.finish("⚠️ خطا در دانلود تصویر از تلگرام. لطفاً مجدداً ارسال کنید.", update)
        return

    caption = update.message.caption or "این عکس رو با لحن عامیانه تحلیل کن و بگو چیه."
    active_chat = db.get_active_chat(user_id)

    try:
        image_part = types.Part.from_bytes(data=bytes(img_bytes), mime_type="image/jpeg")
        reply_text = await generate_multimodal_response(user_id, [caption, image_part])
        db.add_message(active_chat["id"], "user", f"[ارسال تصویر]: {caption}")
        db.add_message(active_chat["id"], "model", reply_text)
        await tracker.finish(reply_text, update)
        await reply_with_optional_voice(update, reply_text, user_id, send_text=False)
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await tracker.finish("⚠️ خطا در پردازش و تحلیل تصویر.", update)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    if not update.message: return

    is_voice = bool(update.message.voice)
    audio_obj = update.message.voice if is_voice else update.message.audio
    if not audio_obj: return

    if audio_obj.file_size and audio_obj.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم این فایل صوتی بیشتر از ۲۰ مگابایت است! طبق قوانین تلگرام ربات‌ها فقط فایل‌های کمتر از ۲۰ مگابایت را می‌توانند دریافت کنند. لطفاً فایل را فشرده کنید یا بخش‌های کوتاه‌تر آن را بفرستید.")
        return

    file_title = getattr(audio_obj, "file_name", "پیام صوتی") or "پیام صوتی"
    caption = update.message.caption or (
        "این پیام صوتی را گوش بده و جوابش رو صمیمی و عامیانه به فارسی بگو:" if is_voice
        else f"این فایل صوتی ({file_title}) را کامل و با دقت گوش بده. اگر تمرین، لیسنینگ زبان انگلیسی، یا مکالمه است، متن کامل صحبت‌ها (Transcript)، ترجمه یا پاسخ سوالاتش را دقیق و کامل بگو:"
    )

    steps = [
        f"🎙️ در حال دانلود و دریافت فایل صوتی ({file_title}) از تلگرام...",
        "🎧 در حال گوش دادن و پردازش دقیق صدا توسط جمینای...",
        "🧠 در حال درک کلمات، لهجه و تحلیل مفاهیم گفتار...",
        "✍️ در حال استخراج متن و نگارش پاسخ نهایی..."
    ]
    tracker = ProgressTracker(update.message, steps, min_step_duration=1.4, min_total_duration=2.5)
    await tracker.start()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    try:
        audio_file = await audio_obj.get_file()
        audio_bytes = await audio_file.download_as_bytearray()
    except Exception as e:
        logging.error(f"Audio download error: {e}")
        await tracker.finish("⚠️ خطا در دانلود فایل صوتی از تلگرام.", update)
        return

    active_chat = db.get_active_chat(user_id)
    mime = "audio/ogg" if is_voice else (getattr(audio_obj, "mime_type", None) or "audio/mp3")

    try:
        audio_part = types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime)
        reply_text = await generate_multimodal_response(
            user_id,
            [caption, audio_part]
        )
        db.add_message(active_chat["id"], "user", f"[ارسال فایل صوتی {file_title}]: {caption}")
        db.add_message(active_chat["id"], "model", reply_text)
        await tracker.finish(reply_text, update)
        await reply_with_optional_voice(update, reply_text, user_id, send_text=False)
    except Exception as e:
        logging.error(f"Voice/Audio error: {e}")
        await tracker.finish("⚠️ خطا در شنیدن یا تحلیل فایل صوتی با هوش مصنوعی.", update)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    if not update.message: return

    vid_obj = update.message.video or update.message.video_note or update.message.animation
    if not vid_obj: return

    if vid_obj.file_size and vid_obj.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم این ویدیو بیشتر از ۲۰ مگابایت است! طبق قوانین تلگرام ربات‌ها فقط فایل‌های کمتر از ۲۰ مگابایت را می‌توانند دریافت کنند. لطفاً ویدیوی کوتاه‌تر یا کم‌حجم‌تر ارسال کنید.")
        return

    is_note = bool(update.message.video_note)
    file_title = getattr(vid_obj, "file_name", "پیام ویدیویی") or "ویدیو"
    caption = update.message.caption or (
        "این ویدیو مسیج دایره‌ای را کامل ببین و بشنو و جوابش رو صمیمی و عامیانه به فارسی بگو:" if is_note
        else f"این ویدیو ({file_title}) را کامل از نظر بصری و صوتی تماشا کن. اتفاقات رخ داده، صحبت‌ها و نکات مهم آن را به طور کامل و جذاب به فارسی توضیح بده:"
    )

    steps = [
        f"🎬 در حال دانلود و دریافت ویدیو ({file_title}) از تلگرام...",
        "🎞️ در حال آنالیز فریم‌های تصویر و تفکیک جریان صدا...",
        "🧠 در حال پردازش خط زمانی ویدیو و رویدادها با جمینای...",
        "✍️ در حال نگارش خلاصه و تحلیل جامع محتوای فیلم..."
    ]
    tracker = ProgressTracker(update.message, steps, min_step_duration=1.6, min_total_duration=3.0)
    await tracker.start()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VIDEO)

    try:
        vid_file = await vid_obj.get_file()
        vid_bytes = await vid_file.download_as_bytearray()
    except Exception as e:
        logging.error(f"Video download error: {e}")
        await tracker.finish("⚠️ خطا در دانلود ویدیو از تلگرام. لطفاً مجدداً ارسال کنید.", update)
        return

    active_chat = db.get_active_chat(user_id)
    mime = getattr(vid_obj, "mime_type", None) or "video/mp4"

    try:
        vid_part = types.Part.from_bytes(data=bytes(vid_bytes), mime_type=mime)
        reply_text = await generate_multimodal_response(
            user_id,
            [caption, vid_part]
        )
        db.add_message(active_chat["id"], "user", f"[ارسال ویدیو {file_title}]: {caption}")
        db.add_message(active_chat["id"], "model", reply_text)
        await tracker.finish(reply_text, update)
        await reply_with_optional_voice(update, reply_text, user_id, send_text=False)
    except Exception as e:
        logging.error(f"Video processing error: {e}")
        await tracker.finish("⚠️ خطا در تماشا و تحلیل ویدیو با هوش مصنوعی.", update)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    if not update.message or not update.message.document: return

    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم این فایل بیشتر از ۲۰ مگابایت است! طبق قوانین تلگرام ربات‌ها فقط فایل‌های کمتر از ۲۰ مگابایت را می‌توانند دریافت کنند. لطفاً حجم فایل را کم کنید یا صفحات مورد نظرتان را به صورت عکس بفرستید.")
        return

    steps = [
        f"📥 در حال دانلود و دریافت فایل ({doc.file_name or 'سند'}) از تلگرام...",
        "📑 در حال استخراج محتوا و تفکیک ساختار داده‌ها...",
        "🧠 در حال مطالعه عمیق و تحلیل دقیق متن با جمینای...",
        "✍️ در حال خلاصه‌سازی و نگارش گزارش نهایی..."
    ]
    tracker = ProgressTracker(update.message, steps, min_step_duration=1.5, min_total_duration=2.8)
    await tracker.start()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        doc_file = await doc.get_file()
        doc_bytes = await doc_file.download_as_bytearray()
    except Exception as e:
        await tracker.finish("⚠️ خطا در دانلود فایل از تلگرام. لطفاً مجدداً بفرستید.", update)
        return

    file_name = (doc.file_name or "file").lower()
    prompt = update.message.caption or f"این فایل ({doc.file_name or 'سند'}) رو کامل و دقیق بررسی کن و نکات، تحلیل‌ها، ترجمه یا پاسخ‌های مهمش رو به زبان عامیانه و شیوا بگو."
    active_chat = db.get_active_chat(user_id)

    try:
        # ۱. اگر فایل ورد (.docx) باشد: استخراج خودکار متن
        if file_name.endswith(".docx"):
            extracted = extract_docx_text(bytes(doc_bytes))
            if extracted:
                content_payload = f"{prompt}\n\n[متن استخراج‌شده از سند Word ({doc.file_name})]:\n{extracted[:120000]}"
                resp = await generate_chat_response(user_id, active_chat["id"], content_payload)
                reply_text = resp.formatted_text if hasattr(resp, "formatted_text") else resp[0]
            else:
                reply_text = "⚠️ فایل Word باز شد اما متنی داخل آن پیدا نشد یا فایل محافظت‌شده است."

        # ۲. اگر فایل‌های متنی یا کدهای برنامه‌نویسی باشد (.txt, .py, .csv, .json, .md, .html, .c, .cpp, .js, ...)
        elif any(file_name.endswith(ext) for ext in [
            ".txt", ".py", ".csv", ".json", ".md", ".html", ".css", ".js", ".ts", 
            ".c", ".cpp", ".h", ".java", ".sql", ".sh", ".xml", ".log", ".yaml", ".yml"
        ]):
            try:
                text_content = doc_bytes.decode("utf-8", errors="replace")
                content_payload = f"{prompt}\n\n[محتوای فایل {doc.file_name}]:\n```\n{text_content[:120000]}\n```"
                resp = await generate_chat_response(user_id, active_chat["id"], content_payload)
                reply_text = resp.formatted_text if hasattr(resp, "formatted_text") else resp[0]
            except Exception:
                file_part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type="text/plain")
                reply_text = await generate_multimodal_response(user_id, [prompt, file_part])

        # ۳. سایر فایل‌های چندرسانه‌ای و باینری (PDF, تصویر, صوت, ویدیو)
        else:
            mime = doc.mime_type or "application/pdf"
            if file_name.endswith(".pdf"): mime = "application/pdf"
            elif file_name.endswith((".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")):
                mime = "audio/mp3" if file_name.endswith(".mp3") else "audio/wav"
            elif file_name.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
                mime = "video/mp4"
            elif file_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".heic")):
                mime = "image/jpeg"

            file_part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type=mime)
            reply_text = await generate_multimodal_response(user_id, [prompt, file_part])

        db.add_message(active_chat["id"], "user", f"[ارسال فایل {doc.file_name}]: {prompt}")
        db.add_message(active_chat["id"], "model", reply_text)
        await tracker.finish(reply_text, update)
        await reply_with_optional_voice(update, reply_text, user_id, send_text=False)
    except Exception as e:
        logging.error(f"Doc error: {e}")
        await tracker.finish(f"⚠️ خطا در تحلیل فایل ({e})", update)

# ================= توابع و هندلرهای تولید تصویر (R2: Imagen 3) =================

async def enhance_image_prompt(raw_prompt: str) -> str:
    """
    Enriches and translates Persian/English prompts using Gemini Flash
    into an optimal, vivid, photorealistic prompt for Imagen 3.
    """
    instruction = (
        "You are an expert prompt engineer for Google Imagen 3. "
        "Translate and enrich the following user prompt (Persian or English) into a highly descriptive, "
        "photorealistic, vivid English prompt suitable for AI image generation. "
        "Include details about subject, environment, lighting, composition, mood, and style. "
        "Output ONLY the final English prompt, without introductory text, quotes, or markdown."
    )
    try:
        resp = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[instruction, raw_prompt]
        )
        if resp and resp.text:
            return resp.text.strip()
    except Exception as e:
        logging.warning(f"Prompt enhancement fallback due to: {e}")
    return raw_prompt


async def handle_draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /draw and /image commands by generating high-resolution images
    via Imagen 3 ('imagen-3.0-generate-002') with bilingual enrichment and aspect ratio options.
    """
    user_id = str(update.effective_user.id)
    if not check_access(user_id):
        await update.message.reply_text("⛔ شرمنده، برای استفاده از این ربات باید لینک دعوت داشته باشی.")
        return

    raw_text = update.message.text if (update.message and update.message.text) else ""
    parts = raw_text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""

    if not prompt:
        help_text = (
            "🎨 **راهنمای دستور ساخت تصویر هوش مصنوعی (/draw و /image):**\n\n"
            "برای تولید عکس، متن توصیف مورد نظرتان را به همراه دستور بنویسید.\n"
            "می‌توانید به زبان فارسی یا انگلیسی تایپ کنید:\n\n"
            "▫️ `/draw یک فضانورد شناور در کهکشان با سبک واقع‌گرایانه`\n"
            "▫️ `/image a cybernetic eagle flying over futuristic neon city, 8k, photorealistic`\n"
            "▫️ `/draw منظره غروب خورشید در کوهستان برفی --ar 16:9`\n\n"
            "📐 **نسبت‌های ابعاد تصویر قابل انتخاب (--ar):**\n"
            "`1:1` (مربعی، پیش‌فرض) | `16:9` (سینمایی عریض) | `9:16` (استوری عمودی) | `4:3` | `3:4`"
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")
        return

    # Extract aspect ratio flag if provided
    aspect_ratio = "1:1"
    ar_match = re.search(r"--ar\s+(1:1|16:9|9:16|4:3|3:4)\b", prompt, flags=re.IGNORECASE)
    if ar_match:
        aspect_ratio = ar_match.group(1)
        prompt = prompt[:ar_match.start()] + prompt[ar_match.end():]
        prompt = prompt.strip()

    status_msg = await update.message.reply_text(
        "🎨 در حال طراحی و ساخت تصویر با هوش مصنوعی Imagen 3...\n⏳ لطفاً چند لحظه شکیبا باشید."
    )
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    # Bilingual prompt enrichment (Persian to vivid English, or short prompt expansion)
    final_prompt = prompt
    if bool(re.search(r"[\u0600-\u06FF]", prompt)) or len(prompt) < 20:
        final_prompt = await enhance_image_prompt(prompt)

    try:
        response = client.models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=final_prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio=aspect_ratio,
            )
        )

        images = getattr(response, "generated_images", None) or []
        if not images:
            await status_msg.edit_text("⚠️ متأسفانه تصویری ایجاد نشد. لطفاً پرامپت دیگری را امتحان کنید.")
            return

        first_img = images[0]
        filtered_reason = getattr(first_img, "rai_filtered_reason", None)
        if filtered_reason:
            await status_msg.edit_text(
                f"⚠️ تصویر درخواستی با قوانین فیلتر ایمنی گوگل سازگار نبود ({filtered_reason}). لطفاً توضیحات تصویر را اصلاح کنید."
            )
            return

        img_obj = getattr(first_img, "image", None)
        img_bytes = getattr(img_obj, "image_bytes", None)
        if not img_bytes:
            await status_msg.edit_text("⚠️ داده‌های فایل تصویر خالی است.")
            return

        photo_io = io.BytesIO(img_bytes)
        photo_io.name = "generated_image.jpg"
        caption = (
            f"🎨 **تصویر تولید شده با Imagen 3**\n"
            f"📝 **توصیف:** `{prompt[:200]}`\n"
            f"📐 **نسبت تصویر:** `{aspect_ratio}`"
        )
        try:
            await update.message.reply_photo(
                photo=photo_io,
                caption=caption,
                parse_mode="Markdown"
            )
        except Exception:
            photo_io.seek(0)
            await update.message.reply_photo(
                photo=photo_io,
                caption=f"🎨 تصویر تولید شده با Imagen 3\nپرامپت: {prompt[:200]}"
            )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        err_str = str(e)
        logging.error(f"Image generation error: {e}")
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            msg = "⚠️ سهمیه درخواست تصویر گوگل موقتاً پر شده است (429 Rate Limit). لطفاً دقایقی دیگر تلاش کنید."
        elif "SAFETY" in err_str.upper() or "FILTER" in err_str.upper():
            msg = "⚠️ پرامپت شما توسط سیستم فیلتر ایمنی محتوای گوگل مسدود شد. لطفاً کلمات پرامپت را تغییر دهید."
        else:
            msg = f"⚠️ خطا در تولید تصویر با هوش مصنوعی:\n`{err_str[:200]}`"

        try:
            await status_msg.edit_text(msg, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(msg)

# ================= سیستم یادآورها و زمان‌بندی پایدار (R3: Persistent Reminders & Scheduler) =================

TEHRAN_TZ = datetime.timezone(datetime.timedelta(hours=3, minutes=30))

PERSIAN_TO_ASCII_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩\u066b",
    "01234567890123456789."
)

def normalize_digits(text: str) -> str:
    """Converts Persian and Arabic digits to ASCII digits."""
    if not text:
        return ""
    return text.translate(PERSIAN_TO_ASCII_DIGITS)


PERSIAN_WORD_NUMBERS = {
    "نیم": 0.5, "یک": 1, "یه": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5,
    "شش": 6, "شیش": 6, "هفت": 7, "هشت": 8, "نه": 9, "ده": 10,
    "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15, "پونزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19, "بیست": 20, "سی": 30
}

UNIT_MAP = {
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes", "دقیقه": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours", "ساعت": "hours",
    "d": "days", "day": "days", "days": "days", "روز": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks", "هفته": "weeks"
}

INTENT_PATTERNS = [
    r"^/?remind(?:\s+me(?:\s+to)?)?\b",
    r"(?:^|\s)لطف[اآً\u064b]*(?:\s|$)",
    r"\bبه\s+من\s+یادآوری\s+کن(?:\s+که)?\b",
    r"\bبه\s+من\s+بگو(?:\s+که)?\b",
    r"\bیادم\s+(?:بنداز|بندازی|باشه|بمونه|نره)(?:\s+که)?\b",
    r"\bیادآوری\s+(?:کن|کنید|شو|شود)(?:\s+که)?\b",
    r"\bیادآور\s+(?:شو|باش)(?:\s+که)?\b",
    r"\bیادآوری:\b",
    r"\bیادآوری\b",
    r"\bرو\s+یادم\s+بنداز\b",
    r"\bرا\s+یادم\s+بنداز\b",
]

REMINDER_INTENT_KEYWORDS = [
    "یادم بنداز",
    "یادم بندازی",
    "یادم باشه",
    "یادم بمونه",
    "یادم نره",
    "یادآوری",
    "یادآور",
    "یاداور",
    "remind",
    "reminder",
    "/remind"
]

def has_reminder_intent(text: str) -> bool:
    if not text:
        return False
    lower_t = text.lower()
    return any(k in lower_t for k in REMINDER_INTENT_KEYWORDS)


class AwaitableTuple(tuple):
    """
    Tuple subclass that is also an awaitable coroutine.
    Enables callers to use either `due, text = parse_reminder_input(...)`
    or `due, text = await parse_reminder_input(...)` seamlessly.
    """
    def __new__(cls, a: str, b: str):
        return super().__new__(cls, (a, b))

    def __await__(self):
        async def _coro():
            return self
        return _coro().__await__()


class Tier2AwaitableResult:
    """
    Awaitable result representing a deferred Tier 2 Gemini LLM parsing task.
    When unpacked synchronously (`due, text = result`), it evaluates synchronously.
    When awaited (`result = await result`), it runs via executor without blocking the event loop.
    """
    def __init__(self, text: str, base_dt: Optional[datetime.datetime]):
        self.text = text
        self.base_dt = base_dt
        self._cached_result: Optional[Tuple[str, str]] = None
        self._evaluated = False

    def _eval_sync(self) -> Optional[Tuple[str, str]]:
        if not self._evaluated:
            self._evaluated = True
            self._cached_result = _execute_gemini_reminder_fallback(self.text, self.base_dt)
        return self._cached_result

    def __iter__(self):
        res = self._eval_sync()
        if res is None:
            return iter(())
        return iter(res)

    def __getitem__(self, item):
        res = self._eval_sync()
        if res is None:
            raise IndexError("Reminder parsing failed")
        return res[item]

    def __bool__(self):
        res = self._eval_sync()
        return bool(res)

    def __len__(self):
        res = self._eval_sync()
        return len(res) if res is not None else 0

    def __await__(self):
        async def _coro():
            if not self._evaluated:
                self._evaluated = True
                try:
                    loop = asyncio.get_running_loop()
                    self._cached_result = await loop.run_in_executor(
                        None,
                        _execute_gemini_reminder_fallback,
                        self.text,
                        self.base_dt
                    )
                except RuntimeError:
                    self._cached_result = _execute_gemini_reminder_fallback(self.text, self.base_dt)
            return self._cached_result
        return _coro().__await__()


def clean_reminder_text(text: str) -> str:
    t = text
    for p in INTENT_PATTERNS:
        t = re.sub(p, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[\u064b-\u065f]", "", t)
    t = re.sub(r"^\s*(?:که|رو|را|برای|تا|to|in)\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(?:رو|را)\s*$", "", t)
    # Protect decimals and version numbers (e.g. 2.0) by only replacing dots not between digits
    t = re.sub(r"(?<!\d)\.+|\.+(?!\d)", " ", t)
    t = re.sub(r"[\s\-:،,]+", " ", t).strip()
    return t if t else "یادآوری"


def parse_reminder_input_tier1(
    raw_text: str,
    base_dt: Optional[datetime.datetime] = None
) -> Optional[Tuple[str, str]]:
    """
    Tier 1 Deterministic Regex Engine:
    Handles relative delays, clock times, Persian/Arabic numerals, Tehran UTC+03:30 timezone conversion.
    """
    if not raw_text or not raw_text.strip():
        return None

    # Past tense guard: if sentence talks about yesterday/past without future marker, skip
    if re.search(r"\b(دیروز|پریروز|گذشته|قبلی|yesterday|ago)\b", raw_text, re.IGNORECASE) and not re.search(r"\b(دیگه|بعد|آینده|next|later)\b", raw_text, re.IGNORECASE):
        return None

    text = normalize_digits(raw_text).strip()

    if base_dt is None:
        base_dt = datetime.datetime.now(TEHRAN_TZ)
    elif base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=TEHRAN_TZ)
    else:
        base_dt = base_dt.astimezone(TEHRAN_TZ)

    # Negative interval rejection: disallow negative durations (e.g. /remind -5m)
    if re.search(r"(?:^|[\s/])-\s*\d+(?:\.\d+)?\s*(?:m|min|mins|minute|minutes|دقیقه|h|hr|hrs|hour|hours|ساعت|d|day|days|روز|w|week|weeks|هفته)\b", text, re.IGNORECASE):
        return None

    # 1. Check relative delays
    rel_match = None
    m = re.search(r"\b(?:یک|یه)?\s*ربع(?:\s+ساعت)?\s+(?:دیگه|بعد|دیگر|آینده|later)\b", text)
    if m:
        rel_match = (15, "minutes", m.span())
    if not rel_match:
        m = re.search(r"\bنیم\s+ساعت\s+(?:دیگه|بعد|دیگر|آینده|later)\b", text)
        if m:
            rel_match = (30, "minutes", m.span())
    if not rel_match:
        m = re.search(r"\bنیم\s+روز\s+(?:دیگه|بعد|دیگر|آینده|later)\b", text)
        if m:
            rel_match = (12, "hours", m.span())
    if not rel_match:
        # Check command pattern first: e.g. "/remind 10m" or "remind 10m"
        is_cmd = bool(re.search(r"^(?:/remind|remind)\b", text, re.IGNORECASE))
        if is_cmd:
            pat_cmd = (
                r"(?:^|\s)/?remind\s+(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|دقیقه|h|hr|hrs|hour|hours|ساعت|d|day|days|روز|w|week|weeks|هفته)\b(?:\s+(?:دیگه|بعد|دیگر|آینده|later))?"
            )
            m = re.search(pat_cmd, text, re.IGNORECASE)
            if m:
                val_str = m.group(1).lower()
                unit_str = m.group(2).lower()
                val = float(val_str) if val_str[0].isdigit() else PERSIAN_WORD_NUMBERS.get(val_str, 1)
                unit = UNIT_MAP.get(unit_str, "minutes")
                rel_match = (val, unit, m.span())
        else:
            # Check shorthand starting with compact unit e.g. "10m test"
            m_compact = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\b", text, re.IGNORECASE)
            if m_compact:
                val_str = m_compact.group(1).lower()
                unit_str = m_compact.group(2).lower()
                val = float(val_str)
                unit = UNIT_MAP.get(unit_str, "minutes")
                rel_match = (val, unit, m_compact.span())
            else:
                # Natural language relative delay: REQUIRES future marker (دیگه/بعد/دیگر/آینده/later) or leading (in/ظرف)
                pat_nat = (
                    r"(?:(?:\b(?:in|ظرف)\s+)(\d+(?:\.\d+)?|"
                    + "|".join(PERSIAN_WORD_NUMBERS.keys())
                    + r")\s*(m|min|mins|minute|minutes|دقیقه|h|hr|hrs|hour|hours|ساعت|d|day|days|روز|w|week|weeks|هفته)(?:\s+(?:دیگه|بعد|دیگر|آینده|later))?\b)"
                    + r"|(?:\b(\d+(?:\.\d+)?|"
                    + "|".join(PERSIAN_WORD_NUMBERS.keys())
                    + r")\s*(m|min|mins|minute|minutes|دقیقه|h|hr|hrs|hour|hours|ساعت|d|day|days|روز|w|week|weeks|هفته)(?:\s+(?:دیگه|بعد|دیگر|آینده|later))\b)"
                )
                m = re.search(pat_nat, text, re.IGNORECASE)
                if m:
                    groups = [g for g in m.groups() if g is not None]
                    val_str = groups[0].lower()
                    unit_str = groups[1].lower()
                    val = float(val_str) if val_str[0].isdigit() else PERSIAN_WORD_NUMBERS.get(val_str, 1)
                    unit = UNIT_MAP.get(unit_str, "minutes")
                    rel_match = (val, unit, m.span())

    if rel_match:
        val, unit, span = rel_match
        try:
            delta = datetime.timedelta(**{unit: val})
            target_dt = base_dt + delta
        except (OverflowError, ValueError):
            return None
        target_utc = target_dt.astimezone(datetime.timezone.utc)
        due_at_utc = target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_text = text[:span[0]] + " " + text[span[1]:]
        return due_at_utc, clean_reminder_text(rem_text)

    # 2. Check specific clock times
    day_offset = 0
    span_day = None
    m_day = re.search(r"\b(پس‌فردا|پس فردا)\b", text)
    if m_day:
        day_offset = 2
        span_day = m_day.span()
    else:
        m_day = re.search(r"\b(فردا|tomorrow)\b", text, re.IGNORECASE)
        if m_day:
            day_offset = 1
            span_day = m_day.span()
        else:
            m_day = re.search(r"\b(امروز|today)\b", text, re.IGNORECASE)
            if m_day:
                day_offset = 0
                span_day = m_day.span()

    clock_pat = (
        r"(?:(?:ساعت|at)\s*)?(\b\d{1,2})(?::(\d{2}))?"
        r"(?:\s*(صبح|ق\.ظ|am|شب|عصر|بعد[\s\u200c]*از[\s\u200c]*ظهر|بعد[\u200c\s]*ازظهر|ب\.ظ|pm|ظهر|بامداد))?\b"
    )
    for m in re.finditer(clock_pat, text, re.IGNORECASE):
        h_str = m.group(1)
        m_str = m.group(2)
        ampm_str = m.group(3)
        matched_str = m.group(0).strip().lower()

        # If no colon and no am/pm, must be preceded by ساعت or at or have day indicator
        if not m_str and not ampm_str and not matched_str.startswith("ساعت") and not matched_str.startswith("at") and not span_day:
            continue

        hour = int(h_str)
        minute = int(m_str) if m_str else 0
        if hour > 23 or minute > 59:
            continue

        if ampm_str:
            ampm = re.sub(r"[\s\u200c]+", "", ampm_str.lower())
            if ampm in ("شب", "بامداد"):
                if hour == 12:
                    hour = 0
                elif hour < 12 and ampm == "شب":
                    hour += 12
            elif ampm in ("عصر", "بعدازظهر", "ب.ظ", "pm"):
                if hour < 12:
                    hour += 12
            elif ampm in ("صبح", "ق.ظ", "am"):
                if hour == 12:
                    hour = 0
            elif ampm == "ظهر":
                if hour < 12:
                    hour = 12

        try:
            target_dt = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day_offset > 0:
                target_dt += datetime.timedelta(days=day_offset)
                if hour == 0 and ampm_str and "شب" in ampm_str:
                    target_dt += datetime.timedelta(days=1)
            else:
                # Rollover to tomorrow if time has already passed today
                if target_dt <= base_dt:
                    target_dt += datetime.timedelta(days=1)
        except (OverflowError, ValueError):
            continue

        target_utc = target_dt.astimezone(datetime.timezone.utc)
        due_at_utc = target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        if span_day:
            rem_text = text
            spans = sorted([span_day, (m.start(), m.end())], key=lambda x: x[0], reverse=True)
            for s, e in spans:
                rem_text = rem_text[:s] + " " + rem_text[e:]
        else:
            rem_text = text[:m.start()] + " " + text[m.end():]

        return due_at_utc, clean_reminder_text(rem_text)

    return None


def _execute_gemini_reminder_fallback(
    text: str,
    base_dt: Optional[datetime.datetime] = None
) -> Optional[Tuple[str, str]]:
    """
    Tier 2: Invokes Gemini Flash with structured prompt requesting strict JSON.
    """
    if base_dt is None:
        base_dt = datetime.datetime.now(TEHRAN_TZ)
    elif base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=TEHRAN_TZ)
    else:
        base_dt = base_dt.astimezone(TEHRAN_TZ)

    now_tehran_str = base_dt.strftime("%Y-%m-%d %H:%M:%S")
    now_utc_str = base_dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    instruction = (
        "You are a precise multilingual reminder parser assistant.\n"
        f"Current UTC time: {now_utc_str}\n"
        f"Current Tehran local time (UTC+03:30): {now_tehran_str}\n"
        "Your task is to parse the user's reminder request into a strict JSON object with this exact structure:\n"
        '{"due_at_utc": "YYYY-MM-DDTHH:MM:SSZ", "reminder_text": "clean reminder note in original language"}\n'
        "Rules:\n"
        "1. 'due_at_utc' MUST be a valid ISO-8601 UTC timestamp ending in 'Z'.\n"
        "2. Accurately resolve relative terms like 'فردا پس‌فردا', 'آخر هفته', 'next Monday', etc.\n"
        "3. 'reminder_text' should have reminder intent keywords (e.g. 'یادم بنداز', 'remind me') stripped out.\n"
        "4. Output ONLY the JSON object, without markdown formatting, backticks, or explanation."
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[instruction, text]
        )
        if not response or not response.text:
            return None

        raw_resp = response.text.strip()
        json_match = re.search(r"\{.*\}", raw_resp, flags=re.DOTALL)
        if not json_match:
            return None

        data = json.loads(json_match.group(0))
        due_at_utc = data.get("due_at_utc", "").strip()
        reminder_text = data.get("reminder_text", "").strip()

        if not due_at_utc or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", due_at_utc):
            return None

        if not reminder_text:
            reminder_text = "یادآوری"

        return AwaitableTuple(due_at_utc, reminder_text)
    except Exception as e:
        logging.warning(f"Gemini reminder extraction fallback error: {e}")
        return None


def parse_reminder_input(
    text: str,
    base_dt: Optional[datetime.datetime] = None
):
    """
    2-Tier Bilingual NLP & Command Parser:
    - Normalizes Persian/Arabic digits.
    - Tier 1: Deterministic regex for relative delays, clock times, commands.
    - Tier 2: Gemini LLM fallback for complex natural language expressions.
    Returns AwaitableTuple or Tier2AwaitableResult (both usable synchronously or via `await`), or None.
    """
    if not text or not text.strip():
        return None

    # Tier 1
    t1 = parse_reminder_input_tier1(text, base_dt)
    if t1 is not None:
        return AwaitableTuple(t1[0], t1[1])

    # Check intent for Tier 2
    if not has_reminder_intent(text):
        return None

    return Tier2AwaitableResult(text, base_dt)


async def handle_remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram CommandHandler for /remind.
    Supports /remind 10m text, /remind 2h, /remind 14:30 text, or general guide.
    """
    user_id = str(update.effective_user.id)
    if not check_access(user_id):
        await update.message.reply_text("⛔ شرمنده، برای استفاده از این ربات باید لینک دعوت داشته باشی.")
        return

    raw_text = update.message.text if (update.message and update.message.text) else ""
    parts = raw_text.split(maxsplit=1)
    args = parts[1].strip() if len(parts) > 1 else ""

    if not args:
        help_text = (
            "⏰ **راهنمای ثبت یادآور هوشمند (/remind):**\n\n"
            "می‌توانید به صورت دستوری یا زبان طبیعی یادآوری تنظیم کنید:\n\n"
            "🔹 **نمونه‌های زمانی نسبی:**\n"
            "▫️ `/remind 10m پیام تست`\n"
            "▫️ `/remind 2h تماس با علی`\n"
            "▫️ `/remind 1d تحویل پروژه`\n\n"
            "🔹 **نمونه‌های به زبان طبیعی (فارسی و انگلیسی):**\n"
            "▫️ `۱۰ دقیقه دیگه آب بخورم`\n"
            "▫️ `فردا ساعت ۸ صبح یادم بنداز جزوه رو بخونم`\n"
            "▫️ `ساعت ۱۴:۳۰ جلسه با تیم فنی`\n"
            "▫️ `ساعت ۹ شب فوتبال`\n\n"
            "💡 تمام یادآورها در دیتابیس ماندگار ثبت شده و حتی در صورت ری‌استارت سرور فعال می‌مانند."
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")
        return

    parsed = parse_reminder_input(raw_text)
    if parsed is not None and hasattr(parsed, "__await__"):
        parsed = await parsed
    if not parsed:
        await update.message.reply_text(
            "⚠️ متوجه زمان یا تاریخ یادآوری نشدم!\n"
            "لطفاً زمان را مشخص کنید (مثال: `/remind 10m متن` یا `فردا ساعت ۱۰ متن`).",
            parse_mode="Markdown"
        )
        return

    due_at_utc, reminder_text = parsed
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id
    rem_id = db.add_reminder(
        user_id=int(user_id) if str(user_id).isdigit() else user_id,
        chat_id=chat_id,
        message_id=msg_id,
        reminder_text=reminder_text,
        due_at_utc=due_at_utc
    )

    due_dt_utc = datetime.datetime.fromisoformat(due_at_utc.replace("Z", "+00:00"))
    due_dt_tehran = due_dt_utc.astimezone(TEHRAN_TZ)
    time_display = due_dt_tehran.strftime("%H:%M - %Y/%m/%d")

    confirm_msg = (
        f"⏰ **یادآور با موفقیت در سیستم ثبت شد!**\n\n"
        f"📝 **یادداشت:** {reminder_text}\n"
        f"⏳ **موعد یادآوری:** `{time_display}` (به وقت تهران)\n"
        f"💾 **وضعیت:** ماندگار در دیتابیس (شناسه: `#{rem_id}`)"
    )
    try:
        await update.message.reply_text(confirm_msg, parse_mode="Markdown", reply_to_message_id=msg_id)
    except Exception:
        await update.message.reply_text(confirm_msg, parse_mode="Markdown")


async def reminder_scheduler_loop(application, single_pass: bool = False):
    """
    Autonomous background worker polling due reminders and delivering them.
    Includes interactive inline Snooze (+10m, +1h) and Dismiss buttons.
    """
    logging.info("Reminder scheduler worker started.")
    while True:
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            due_reminders = db.get_due_reminders(now_utc)
            for rem in due_reminders:
                rem_id = rem["id"]
                # Mark firing to prevent duplicate dispatch
                db.update_reminder_status(rem_id, "firing")

                chat_id = rem["chat_id"]
                rem_text = rem["reminder_text"]
                msg_id = rem.get("message_id")

                keyboard = [
                    [
                        InlineKeyboardButton("⏰ تعویق ۱۰ دقیقه", callback_data=f"snooze:10:{rem_id}"),
                        InlineKeyboardButton("⏰ تعویق ۱ ساعت", callback_data=f"snooze:60:{rem_id}")
                    ],
                    [
                        InlineKeyboardButton("✅ انجام شد", callback_data=f"dismiss:{rem_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                delivery_text = (
                    "⏰ *یادآوری!*\n\n"
                    f"📝 *متن:* {rem_text}"
                )

                delivered = False
                try:
                    if msg_id:
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=delivery_text,
                                parse_mode="Markdown",
                                reply_markup=reply_markup,
                                reply_to_message_id=msg_id
                            )
                            delivered = True
                        except BadRequest as bre:
                            if any(k in str(bre).lower() for k in ("entities", "markdown", "parse")):
                                plain_text = f"⏰ یادآوری!\n\n📝 متن: {rem_text}"
                                try:
                                    await application.bot.send_message(
                                        chat_id=chat_id,
                                        text=plain_text,
                                        parse_mode=None,
                                        reply_markup=reply_markup,
                                        reply_to_message_id=msg_id
                                    )
                                    delivered = True
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    if not delivered:
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=delivery_text,
                                parse_mode="Markdown",
                                reply_markup=reply_markup
                            )
                            delivered = True
                        except BadRequest as bre:
                            if any(k in str(bre).lower() for k in ("entities", "markdown", "parse")):
                                plain_text = f"⏰ یادآوری!\n\n📝 متن: {rem_text}"
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=plain_text,
                                    parse_mode=None,
                                    reply_markup=reply_markup
                                )
                                delivered = True
                            else:
                                raise

                    db.update_reminder_status(rem_id, "delivered")
                except Exception as e:
                    err_str = str(e).lower()
                    err_type = type(e).__name__.lower()
                    logging.warning(f"Error delivering reminder #{rem_id} to chat {chat_id}: {e}")
                    if "forbidden" in err_str or "blocked" in err_str or "forbidden" in err_type:
                        db.update_reminder_status(rem_id, "cancelled")
                    elif "chat not found" in err_str or "chatnotfound" in err_type or "user not found" in err_str or "badrequest" in err_type:
                        db.update_reminder_status(rem_id, "failed")
                    else:
                        db.update_reminder_status(rem_id, "pending")

        except asyncio.CancelledError:
            logging.info("Reminder scheduler worker cancelled.")
            break
        except Exception as e:
            logging.error(f"Unexpected error in reminder_scheduler_loop: {e}")

        if single_pass:
            break
        await asyncio.sleep(5)


async def recover_server_reboot_reminders(application):
    """
    On bot startup: recovers reminders that matured while the server was down.
    Overdue <= 24h: delivered immediately with a Persian delayed banner.
    Overdue > 24h: marked 'missed'.
    """
    try:
        recovery = db.recover_missed_reminders()
        recoverable = recovery.get("recoverable", [])
        missed = recovery.get("missed", [])
        logging.info(f"Reboot recovery: {len(recoverable)} overdue reminders to deliver, {len(missed)} marked missed (>24h).")

        for rem in recoverable:
            rem_id = rem["id"]
            chat_id = rem["chat_id"]
            rem_text = rem["reminder_text"]
            msg_id = rem.get("message_id")

            keyboard = [
                [
                    InlineKeyboardButton("⏰ تعویق ۱۰ دقیقه", callback_data=f"snooze:10:{rem_id}"),
                    InlineKeyboardButton("⏰ تعویق ۱ ساعت", callback_data=f"snooze:60:{rem_id}")
                ],
                [
                    InlineKeyboardButton("✅ انجام شد", callback_data=f"dismiss:{rem_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            delivery_text = (
                "⏰ *یادآوری با تأخیر* (به دلیل بازنشانی یا بروزرسانی سرور)\n\n"
                f"📝 *متن:* {rem_text}"
            )

            delivered = False
            try:
                if msg_id:
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=delivery_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup,
                            reply_to_message_id=msg_id
                        )
                        delivered = True
                    except Exception as bre:
                        if any(k in str(bre).lower() for k in ("entities", "markdown", "parse")):
                            plain_text = (
                                "⏰ یادآوری با تأخیر (به دلیل بازنشانی یا بروزرسانی سرور)\n\n"
                                f"📝 متن: {rem_text}"
                            )
                            try:
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=plain_text,
                                    parse_mode=None,
                                    reply_markup=reply_markup,
                                    reply_to_message_id=msg_id
                                )
                                delivered = True
                            except Exception:
                                pass

                if not delivered:
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=delivery_text,
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                        delivered = True
                    except Exception as bre:
                        if any(k in str(bre).lower() for k in ("entities", "markdown", "parse")):
                            plain_text = (
                                "⏰ یادآوری با تأخیر (به دلیل بازنشانی یا بروزرسانی سرور)\n\n"
                                f"📝 متن: {rem_text}"
                            )
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=plain_text,
                                parse_mode=None,
                                reply_markup=reply_markup
                            )
                            delivered = True
                        else:
                            raise
                db.update_reminder_status(rem_id, "delivered")
            except Exception as e:
                logging.warning(f"Error delivering recovered reminder #{rem_id}: {e}")
                db.update_reminder_status(rem_id, "delivered")
    except Exception as e:
        logging.error(f"Error in recover_server_reboot_reminders: {e}")


async def post_init(application):
    """
    PTB post_init hook: executes server reboot recovery and spawns autonomous scheduler.
    """
    await recover_server_reboot_reminders(application)
    asyncio.create_task(reminder_scheduler_loop(application))


# ================= استارت نهایی =================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_chat_command))
    app.add_handler(CommandHandler("chats", list_chats_command))
    app.add_handler(CommandHandler("rename", rename_chat_command))
    app.add_handler(CommandHandler("delete", delete_chat_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("export", export_chat))

    app.add_handler(CommandHandler("model", model_menu))
    app.add_handler(CommandHandler("persona", persona_menu))
    app.add_handler(CommandHandler("search", toggle_search))
    app.add_handler(CommandHandler("voice", toggle_voice))

    # R2: Command handlers for AI image generation
    app.add_handler(CommandHandler("draw", handle_draw_command))
    app.add_handler(CommandHandler("image", handle_draw_command))

    # R3: Command handler for reminders
    app.add_handler(CommandHandler("remind", handle_remind_command))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Gemini Multi-Session AI Bot is live and running...")
    app.run_polling()
