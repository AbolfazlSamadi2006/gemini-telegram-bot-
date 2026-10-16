import os
import io
import time
import asyncio
import logging
import sqlite3
import datetime
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
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

# ================= تنظیمات لاگ =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
ALLOWED_USERS = set(filter(None, os.environ.get("ALLOWED_USERS", "").split(",")))
INVITE_CODE = os.environ.get("INVITE_CODE", "vip2026")

client = genai.Client(api_key=GEMINI_KEY)

# ================= دیتابیس ماندگار (SQLite) =================
class Database:
    def __init__(self, db_path: str = "bot_database.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
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
            conn.commit()

    def get_or_create_user(self, user_id: str) -> Dict:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                cursor.execute(
                    "INSERT INTO users (user_id, model, persona, search_enabled, voice_reply) VALUES (?, 'flash_38_med', 'general', 0, 0)",
                    (user_id,)
                )
                conn.commit()
                chat_id = self.create_new_chat(user_id, "گفتگوی اول")
                cursor.execute("UPDATE users SET active_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = cursor.fetchone()
            elif not user["active_chat_id"]:
                chat_id = self.create_new_chat(user_id, "گفتگوی جدید")
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
                chat_id = self.create_new_chat(user_id, "گفتگوی جدید")
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
                new_active = latest["id"] if latest else self.create_new_chat(user_id, "گفتگوی جدید")
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

    async def finish(self, final_text: str, update_obj: Update):
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
                    await self.status_msg.edit_text(final_text)
                    return
                except Exception:
                    pass
            try:
                await self.status_msg.delete()
            except Exception:
                pass
        await send_long_message(update_obj, final_text)

# ================= توابع کمکی هوش مصنوعی =================
def build_gemini_session(user_id: str, chat_id: int, target_model: str, is_fallback: bool = False):
    u = db.get_or_create_user(user_id)
    model_info = AVAILABLE_MODELS.get(u["model"], AVAILABLE_MODELS["flash_38_med"])
    persona_info = PERSONAS.get(u["persona"], PERSONAS["general"])

    tools = []
    if u["search_enabled"]:
        tools.append(types.Tool(google_search=types.GoogleSearch()))

    thinking_cfg = None
    if not is_fallback and model_info.get("thinking_budget") is not None:
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

async def generate_chat_response(user_id: str, chat_id: int, user_text: str) -> str:
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
            is_fallback = (idx > 0)
            chat_session = build_gemini_session(user_id, chat_id, target_model=target_model, is_fallback=is_fallback)
            response = chat_session.send_message(user_text)
            if response and response.text:
                return response.text
        except Exception as e:
            last_error = e
            logging.warning(f"Chat model {target_model} failed ({e}). Trying fallback...")
            if "503" in str(e) or "UNAVAILABLE" in str(e):
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

async def send_long_message(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])

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
        "⚙️ **تنظیمات هوش مصنوعی:**\n"
        "🔘 /model : انتخاب مدل و سطح تفکر\n"
        "🎭 /persona : تغییر لحن و تخصص من\n"
        "🌐 /search : سرچ زنده گوگل\n"
        "🔊 /voice : فعال‌سازی پاسخ با ویس فارسی\n\n"
        "🎬 *هر نوع فایلی (عکس، ویس، آهنگ، ویدیو، فیلم، PDF، اسناد Word و کدهای برنامه‌نویسی) بفرستی با کمال میل برات تحلیل و ترجمه‌ش می‌کنم.*"
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
    active_chat = db.get_active_chat(user_id)
    messages = db.get_chat_messages(active_chat["id"], limit=200)
    if not messages:
        await update.message.reply_text("در این گفتگو هنوز پیامی برای خروجی گرفتن نیست!")
        return

    lines = [f"=== تاریخچه گفتگوی: {active_chat['title']} ==="]
    for role, msg in messages:
        sender = "ابوالفضل" if role == "user" else "جمینای"
        lines.append(f"\n[{sender}]:\n{msg}")

    content = "\n".join(lines)
    file_bytes = io.BytesIO(content.encode("utf-8"))
    file_bytes.name = f"chat_{active_chat['id']}.txt"
    await update.message.reply_document(document=file_bytes, caption=f"📁 خروجی گفتگوی «{active_chat['title']}»")

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

# ================= هندلرهای پیام و چندرسانه‌ای =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return
    if not update.message or not update.message.text: return
    user_text = update.message.text

    active_chat = db.get_active_chat(user_id)
    u = db.get_or_create_user(user_id)

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
        reply_text = await generate_chat_response(user_id, active_chat["id"], user_text)
        db.add_message(active_chat["id"], "user", user_text)
        db.add_message(active_chat["id"], "model", reply_text)
        await tracker.finish(reply_text, update)
        await reply_with_optional_voice(update, reply_text, user_id, send_text=False)
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
        await update.message.reply_text("⚠️ حجم این فایل صوتی بیشتر از ۲۰ مگابایت است! تلگرام به ربات‌ها اجازه دانلود فایل‌های بالای ۲۰ مگابایت را نمی‌دهد. لطفاً فایل را فشرده کنید یا بخش‌های کوتاه‌تر آن را بفرستید.")
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
        "این ویدیو مسیج دایره‌ای را کامل ببین و بشنو و جوابش رو صمیمی و عامیانه بگو:" if is_note
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
                reply_text = await generate_chat_response(user_id, active_chat["id"], content_payload)
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
                reply_text = await generate_chat_response(user_id, active_chat["id"], content_payload)
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

# ================= استارت نهایی =================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

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

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Gemini Multi-Session AI Bot is live and running...")
    app.run_polling()
