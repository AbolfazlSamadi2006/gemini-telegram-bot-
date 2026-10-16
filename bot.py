import os
import io
import logging
import sqlite3
import datetime
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
    "flash_38_low": {
        "model_id": "gemini-3.8-flash",
        "fallback_id": "gemini-3.5-flash",
        "title": "🟢 3.8 Flash (Low Effort)",
        "thinking_budget": 1024,
    },
    "flash_38_med": {
        "model_id": "gemini-3.8-flash",
        "fallback_id": "gemini-3.5-flash",
        "title": "🟡 3.8 Flash (Medium Effort) [پیش‌فرض]",
        "thinking_budget": 4096,
    },
    "flash_38_high": {
        "model_id": "gemini-3.8-flash",
        "fallback_id": "gemini-3.5-flash",
        "title": "🔴 3.8 Flash (High Effort - تفکر عمیق)",
        "thinking_budget": 8192,
    },
    "pro_model": {
        "model_id": "gemini-3.5-flash",
        "fallback_id": "gemini-3.5-flash",
        "title": "💎 Gemini Flash 3.5 (فوق‌العاده سریع و پایدار)",
        "thinking_budget": None,
    },
    "argon_4": {
        "model_id": "gemini-4-argon",
        "fallback_id": "gemini-3.5-flash",
        "title": "🚀 Gemini 4 Argon",
        "thinking_budget": None,
    },
}

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

# ================= توابع کمکی هوش مصنوعی =================
def build_gemini_session(user_id: str, chat_id: int, use_fallback: bool = False):
    u = db.get_or_create_user(user_id)
    model_info = AVAILABLE_MODELS.get(u["model"], AVAILABLE_MODELS["flash_38_med"])
    persona_info = PERSONAS.get(u["persona"], PERSONAS["general"])

    tools = []
    if u["search_enabled"]:
        tools.append(types.Tool(google_search=types.GoogleSearch()))

    thinking_cfg = None
    if model_info.get("thinking_budget") is not None and not use_fallback:
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

    target_model = model_info["fallback_id"] if use_fallback else model_info["model_id"]
    return client.chats.create(
        model=target_model,
        config=gen_config,
        history=history
    )

def check_access(user_id: str) -> bool:
    if not ALLOWED_USERS or "" in ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

async def send_long_message(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])

async def reply_with_optional_voice(update: Update, text: str, user_id: str):
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
        "🟡 **مدل فعال:** Gemini 3.8 Flash (Medium Effort)\n"
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
        "📸 *راستی! هر عکس، ویس یا فایل PDF هم بفرستی با کمال میل برات تحلیلش می‌کنم.*"
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
    user_text = update.message.text
    if not user_text: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    active_chat = db.get_active_chat(user_id)

    try:
        try:
            chat_session = build_gemini_session(user_id, active_chat["id"], use_fallback=False)
            response = chat_session.send_message(user_text)
        except Exception as e_first:
            logging.warning(f"Primary model busy ({e_first}), switching to fallback 3.5...")
            chat_session = build_gemini_session(user_id, active_chat["id"], use_fallback=True)
            response = chat_session.send_message(user_text)

        db.add_message(active_chat["id"], "user", user_text)
        db.add_message(active_chat["id"], "model", response.text)
        await reply_with_optional_voice(update, response.text, user_id)
    except Exception as e:
        logging.error(f"Text error: {e}")
        await update.message.reply_text("⚠️ ارتباط با هوش مصنوعی موقتاً قطع شد، لطفاً دوباره ارسال کنید.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo_file = await update.message.photo[-1].get_file()
    img_bytearray = await photo_file.download_as_bytearray()
    caption = update.message.caption or "این عکس رو با لحن عامیانه تحلیل کن و بگو چیه."

    active_chat = db.get_active_chat(user_id)
    u = db.get_or_create_user(user_id)
    model_info = AVAILABLE_MODELS.get(u["model"], AVAILABLE_MODELS["flash_38_med"])

    try:
        image_part = types.Part.from_bytes(data=bytes(img_bytearray), mime_type="image/jpeg")
        try:
            response = client.models.generate_content(
                model=model_info["model_id"],
                contents=[caption, image_part]
            )
        except Exception:
            response = client.models.generate_content(
                model=model_info["fallback_id"],
                contents=[caption, image_part]
            )
        db.add_message(active_chat["id"], "user", f"[ارسال تصویر]: {caption}")
        db.add_message(active_chat["id"], "model", response.text)
        await reply_with_optional_voice(update, response.text, user_id)
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text("⚠️ خطا در پردازش و تحلیل تصویر.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
    voice_file = await update.message.voice.get_file()
    voice_bytes = await voice_file.download_as_bytearray()

    active_chat = db.get_active_chat(user_id)
    u = db.get_or_create_user(user_id)
    model_info = AVAILABLE_MODELS.get(u["model"], AVAILABLE_MODELS["flash_38_med"])

    try:
        audio_part = types.Part.from_bytes(data=bytes(voice_bytes), mime_type="audio/ogg")
        try:
            response = client.models.generate_content(
                model=model_info["model_id"],
                contents=["این پیام صوتی را گوش بده و جوابش رو صمیمی و عامیانه به فارسی بگو:", audio_part]
            )
        except Exception:
            response = client.models.generate_content(
                model=model_info["fallback_id"],
                contents=["این پیام صوتی را گوش بده و جوابش رو صمیمی و عامیانه به فارسی بگو:", audio_part]
            )
        db.add_message(active_chat["id"], "user", "[ارسال پیام صوتی]")
        db.add_message(active_chat["id"], "model", response.text)
        await reply_with_optional_voice(update, response.text, user_id)
    except Exception as e:
        logging.error(f"Voice error: {e}")
        await update.message.reply_text("⚠️ خطا در شنیدن یا پردازش ویس.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not check_access(user_id): return

    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم این فایل بیشتر از ۲۰ مگابایت است! طبق قوانین تلگرام ربات‌ها فقط فایل‌های کمتر از ۲۰ مگابایت را می‌توانند دریافت کنند. لطفاً حجم فایل را کم کنید یا صفحات مورد نظرتان را به صورت عکس بفرستید.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        doc_file = await doc.get_file()
        doc_bytes = await doc_file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text("⚠️ خطا در دانلود فایل از تلگرام. لطفاً مجدداً بفرستید.")
        return

    prompt = update.message.caption or "این فایل رو کامل بخون و نکات مهمش رو به زبان عامیانه خلاصه کن."
    active_chat = db.get_active_chat(user_id)
    u = db.get_or_create_user(user_id)
    model_info = AVAILABLE_MODELS.get(u["model"], AVAILABLE_MODELS["flash_38_med"])

    try:
        mime = doc.mime_type or "application/pdf"
        file_part = types.Part.from_bytes(data=bytes(doc_bytes), mime_type=mime)
        try:
            response = client.models.generate_content(
                model=model_info["model_id"],
                contents=[prompt, file_part]
            )
        except Exception:
            response = client.models.generate_content(
                model=model_info["fallback_id"],
                contents=[prompt, file_part]
            )
        db.add_message(active_chat["id"], "user", f"[ارسال فایل {doc.file_name}]: {prompt}")
        db.add_message(active_chat["id"], "model", response.text)
        await reply_with_optional_voice(update, response.text, user_id)
    except Exception as e:
        logging.error(f"Doc error: {e}")
        await update.message.reply_text("⚠️ خطا در خواندن یا تحلیل فایل.")

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
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Gemini Multi-Session AI Bot is live and running...")
    app.run_polling()
