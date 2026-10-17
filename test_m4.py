"""
Comprehensive Unit & Concurrency Test Suite for Milestone 4 (test_m4.py)
Covering Requirement R3: Persistent Task Scheduling & Reminder Engine:
1. Persian and ASCII digit normalization in reminder parser.
2. Relative time parsing (10m, 2h, 1d, ۱۰ دقیقه, ۲ ساعت, etc.).
3. Specific clock times (14:30, ۸ صبح, ۹ شب, فردا).
4. Tehran UTC+03:30 timezone conversion to UTC ISO-8601.
5. Gemini fallback JSON parsing with mocked responses.
6. Database storage, retrieval, and status updates for reminders.
7. Background scheduler worker firing due reminders with inline buttons.
8. Snooze and dismiss callback execution.
9. Reboot recovery for missed reminders (<24h delivered, >24h marked missed).
10. Concurrency: multiple reminders scheduled simultaneously with zero SQLite lock errors.
11. Telegram /remind command and natural language intent detection in handle_message.

100% Offline, Zero Network Dependencies.
"""

import os
import sys
import io
import re
import uuid
import asyncio
import sqlite3
import datetime
import unittest
import threading
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Set offline environment variables
os.environ.setdefault("GEMINI_API_KEY", "dummy_test_key_m4")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy_token_m4")

import bot
from bot import (
    Database,
    TEHRAN_TZ,
    normalize_digits,
    has_reminder_intent,
    clean_reminder_text,
    parse_reminder_input_tier1,
    parse_reminder_input,
    _execute_gemini_reminder_fallback,
    handle_remind_command,
    handle_message,
    callback_handler,
    reminder_scheduler_loop,
    recover_server_reboot_reminders,
    post_init,
    AwaitableTuple,
    Tier2AwaitableResult
)


class TestM4DigitNormalization(unittest.TestCase):
    """Test 1: Persian and ASCII digit normalization."""

    def test_persian_to_ascii_digits(self):
        persian_input = "۰۱۲۳۴۵۶۷۸۹"
        self.assertEqual(normalize_digits(persian_input), "0123456789")

    def test_arabic_to_ascii_digits(self):
        arabic_input = "٠١٢٣٤٥٦٧٨٩"
        self.assertEqual(normalize_digits(arabic_input), "0123456789")

    def test_mixed_text_normalization(self):
        mixed = "ساعت ۱۰:۳۰ صبح در اتاق ۴"
        self.assertEqual(normalize_digits(mixed), "ساعت 10:30 صبح در اتاق 4")

    def test_ascii_and_empty_inputs(self):
        self.assertEqual(normalize_digits(""), "")
        self.assertEqual(normalize_digits("10m reminder"), "10m reminder")
        self.assertEqual(normalize_digits(None), "")


class TestM4RelativeTimeParsing(unittest.TestCase):
    """Test 2: Relative time parsing with various units and Persian numbers."""

    def setUp(self):
        # Fixed base time: 2026-10-16 14:00:00 Tehran time (+03:30) => 2026-10-16 10:30:00 UTC
        self.base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)
        self.base_utc = self.base_tehran.astimezone(datetime.timezone.utc)

    def test_command_relative_delays(self):
        # 10m
        res = parse_reminder_input_tier1("/remind 10m پیام تستی", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-16T10:40:00Z")
        self.assertEqual(text, "پیام تستی")

        # 2h
        res2 = parse_reminder_input_tier1("/remind 2h تماس با علی", self.base_tehran)
        self.assertIsNotNone(res2)
        due_utc2, text2 = res2
        self.assertEqual(due_utc2, "2026-10-16T12:30:00Z")
        self.assertEqual(text2, "تماس با علی")

        # 1d
        res3 = parse_reminder_input_tier1("/remind 1d تحویل پروژه", self.base_tehran)
        self.assertIsNotNone(res3)
        due_utc3, text3 = res3
        self.assertEqual(due_utc3, "2026-10-17T10:30:00Z")
        self.assertEqual(text3, "تحویل پروژه")

        # Default text when no note provided
        res4 = parse_reminder_input_tier1("/remind 2h", self.base_tehran)
        self.assertIsNotNone(res4)
        due_utc4, text4 = res4
        self.assertEqual(due_utc4, "2026-10-16T12:30:00Z")
        self.assertEqual(text4, "یادآوری")

    def test_persian_relative_phrases(self):
        # ۱۰ دقیقه دیگه آب بخورم
        res = parse_reminder_input_tier1("۱۰ دقیقه دیگه آب بخورم", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-16T10:40:00Z")
        self.assertEqual(text, "آب بخورم")

        # ۲ ساعت بعد زنگ بزن
        res2 = parse_reminder_input_tier1("۲ ساعت بعد زنگ بزن به شرکت", self.base_tehran)
        self.assertIsNotNone(res2)
        due_utc2, text2 = res2
        self.assertEqual(due_utc2, "2026-10-16T12:30:00Z")
        self.assertEqual(text2, "زنگ بزن به شرکت")

        # ۳ روز دیگه جلسه
        res3 = parse_reminder_input_tier1("۳ روز دیگه جلسه سهامداران", self.base_tehran)
        self.assertIsNotNone(res3)
        due_utc3, text3 = res3
        self.assertEqual(due_utc3, "2026-10-19T10:30:00Z")
        self.assertEqual(text3, "جلسه سهامداران")

    def test_special_persian_word_durations(self):
        # نیم ساعت دیگه
        res = parse_reminder_input_tier1("نیم ساعت دیگه آماده شو", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-16T11:00:00Z")
        self.assertEqual(text, "آماده شو")

        # یک ربع دیگه
        res2 = parse_reminder_input_tier1("یک ربع دیگه تماس بگیر", self.base_tehran)
        self.assertIsNotNone(res2)
        due_utc2, text2 = res2
        self.assertEqual(due_utc2, "2026-10-16T10:45:00Z")
        self.assertEqual(text2, "تماس بگیر")

        # یک ساعت بعد
        res3 = parse_reminder_input_tier1("یک ساعت بعد گزارش رو بفرست", self.base_tehran)
        self.assertIsNotNone(res3)
        due_utc3, text3 = res3
        self.assertEqual(due_utc3, "2026-10-16T11:30:00Z")
        self.assertEqual(text3, "گزارش رو بفرست")

    def test_sync_and_async_awaitable_tuple(self):
        # Both sync unpack and async await work seamlessly
        res = parse_reminder_input("10m تست همگام", self.base_tehran)
        self.assertIsInstance(res, tuple)
        due, text = res  # Sync unpack
        self.assertEqual(due, "2026-10-16T10:40:00Z")
        self.assertEqual(text, "تست همگام")

        # Async await test
        async def _test():
            r = await parse_reminder_input("10m تست ناهمگام", self.base_tehran)
            return r
        due_async, text_async = asyncio.run(_test())
        self.assertEqual(due_async, "2026-10-16T10:40:00Z")
        self.assertEqual(text_async, "تست ناهمگام")


class TestM4SpecificClockTimes(unittest.TestCase):
    """Test 3 & 4: Specific clock times, rollover, and Tehran UTC+03:30 timezone conversion."""

    def setUp(self):
        # Base time: 2026-10-16 14:00:00 Tehran time (+03:30) => 2026-10-16 10:30:00 UTC
        self.base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)

    def test_specific_time_today(self):
        # 14:30 today (in 30 mins) => 11:00 UTC
        res = parse_reminder_input_tier1("ساعت ۱۴:۳۰ جلسه با تیم فنی", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-16T11:00:00Z")
        self.assertEqual(text, "جلسه با تیم فنی")

        # ساعت ۹ شب (21:00 tonight) => 17:30 UTC
        res2 = parse_reminder_input_tier1("ساعت ۹ شب مسابقه فوتبال", self.base_tehran)
        self.assertIsNotNone(res2)
        due_utc2, text2 = res2
        self.assertEqual(due_utc2, "2026-10-16T17:30:00Z")
        self.assertEqual(text2, "مسابقه فوتبال")

    def test_rollover_when_time_passed_today(self):
        # Base is 14:00. 'ساعت ۸ صبح' has passed today, so must roll over to tomorrow 08:00
        # Tomorrow 08:00 Tehran => 04:30 UTC on 2026-10-17
        res = parse_reminder_input_tier1("ساعت ۸ صبح بیدار شو", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-17T04:30:00Z")
        self.assertEqual(text, "بیدار شو")

    def test_explicit_tomorrow_and_future_days(self):
        # فردا ساعت ۱۰ => tomorrow 10:00 Tehran => 06:30 UTC on 2026-10-17
        res = parse_reminder_input_tier1("فردا ساعت ۱۰ برم شرکت", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-17T06:30:00Z")
        self.assertEqual(text, "برم شرکت")

        # پس‌فردا ساعت ۱۱:۰۰ => day after tomorrow 11:00 Tehran => 07:30 UTC on 2026-10-18
        res2 = parse_reminder_input_tier1("پس‌فردا ساعت ۱۱:۰۰ تحویل نهایی", self.base_tehran)
        self.assertIsNotNone(res2)
        due_utc2, text2 = res2
        self.assertEqual(due_utc2, "2026-10-18T07:30:00Z")
        self.assertEqual(text2, "تحویل نهایی")

    def test_intent_keywords_stripped_cleanly(self):
        # یادم بنداز فردا ساعت ۸ صبح جزوه رو بخونم
        res = parse_reminder_input_tier1("فردا ساعت ۸ صبح یادم بنداز جزوه رو بخونم", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, text = res
        self.assertEqual(due_utc, "2026-10-17T04:30:00Z")
        self.assertEqual(text, "جزوه رو بخونم")

        # لطفاً ۲ ساعت بعد یادم بنداز فایل رو بفرستم
        res2 = parse_reminder_input_tier1("لطفاً ۲ ساعت بعد یادم بنداز فایل رو بفرستم", self.base_tehran)
        self.assertIsNotNone(res2)
        due_utc2, text2 = res2
        self.assertEqual(due_utc2, "2026-10-16T12:30:00Z")
        self.assertEqual(text2, "فایل رو بفرستم")


class TestM4GeminiFallback(unittest.TestCase):
    """Test 5: Gemini LLM extraction fallback with mocked responses."""

    def setUp(self):
        self.base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)

    def test_gemini_fallback_valid_json(self):
        # A complex natural language query where Tier 1 returns None, but reminder intent is present
        raw_llm_json = '{"due_at_utc": "2026-10-18T14:00:00Z", "reminder_text": "پرداخت قسط وام"}'
        mock_resp = MagicMock()
        mock_resp.text = raw_llm_json

        with patch.object(bot.client.models, "generate_content", return_value=mock_resp):
            res = parse_reminder_input("دوشنبه هفته آینده یادم بنداز پرداخت قسط وام", self.base_tehran)
            self.assertIsNotNone(res)
            # Evaluate Tier2
            due_utc, text = res
            self.assertEqual(due_utc, "2026-10-18T14:00:00Z")
            self.assertEqual(text, "پرداخت قسط وام")

    def test_gemini_fallback_markdown_codeblock_json(self):
        raw_llm_json = '```json\n{"due_at_utc": "2026-10-22T08:30:00Z", "reminder_text": "خرید کیک تولد"}\n```'
        mock_resp = MagicMock()
        mock_resp.text = raw_llm_json

        with patch.object(bot.client.models, "generate_content", return_value=mock_resp):
            async def _run():
                return await parse_reminder_input("آخر هفته یادم بنداز خرید کیک تولد", self.base_tehran)
            due_utc, text = asyncio.run(_run())
            self.assertEqual(due_utc, "2026-10-22T08:30:00Z")
            self.assertEqual(text, "خرید کیک تولد")

    def test_gemini_fallback_api_error_handling(self):
        # When Gemini raises an error (e.g. 503 or 429), it must handle gracefully and return None
        with patch.object(bot.client.models, "generate_content", side_effect=Exception("503 UNAVAILABLE")):
            res = parse_reminder_input("آخر هفته یادم بنداز بریم کوه", self.base_tehran)
            self.assertFalse(bool(res))

    def test_non_reminder_text_returns_none(self):
        # Pure greetings or non-reminder text should not trigger reminder
        self.assertIsNone(parse_reminder_input("سلام خسته نباشی چطوری؟", self.base_tehran))
        self.assertIsNone(parse_reminder_input("پایتون چیست و چه کاربردهایی دارد؟", self.base_tehran))


class TestM4DatabaseReminders(unittest.TestCase):
    """Test 6: Database storage, retrieval, status progression, and queries."""

    def setUp(self):
        self.db_path = f"test_reminders_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        # Cleanly delete temporary database
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            wal = self.db_path + "-wal"
            shm = self.db_path + "-shm"
            if os.path.exists(wal): os.remove(wal)
            if os.path.exists(shm): os.remove(shm)
        except Exception:
            pass

    def test_reminder_crud_lifecycle(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        due_now = (now_utc - datetime.timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        due_future = (now_utc + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Add reminders
        r1_id = self.db.add_reminder(user_id=123, chat_id=1001, message_id=45, reminder_text="کار ۱", due_at_utc=due_now)
        r2_id = self.db.add_reminder(user_id=123, chat_id=1001, message_id=46, reminder_text="کار ۲", due_at_utc=due_future)
        self.assertGreater(r1_id, 0)
        self.assertGreater(r2_id, 0)

        # 2. Get due reminders
        due_items = self.db.get_due_reminders()
        due_ids = [r["id"] for r in due_items]
        self.assertIn(r1_id, due_ids)
        self.assertNotIn(r2_id, due_ids)

        # 3. Update status to firing
        self.assertTrue(self.db.update_reminder_status(r1_id, "firing"))
        # Firing reminder should no longer appear in get_due_reminders
        due_items_after_firing = self.db.get_due_reminders()
        self.assertNotIn(r1_id, [r["id"] for r in due_items_after_firing])

        # 4. Deliver reminder
        self.assertTrue(self.db.update_reminder_status(r1_id, "delivered"))
        rows = self.db.get_user_reminders(123, status="delivered")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "delivered")
        self.assertIsNotNone(rows[0]["notified_at"])

        # 5. Snooze reminder
        new_due = (now_utc + datetime.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(self.db.snooze_reminder(r1_id, new_due))
        pending_rows = self.db.get_user_reminders(123, status="pending")
        self.assertEqual(len(pending_rows), 2)  # r1 and r2


class TestM4SchedulerWorker(unittest.IsolatedAsyncioTestCase):
    """Test 7: Background scheduler worker firing due reminders with interactive inline buttons."""

    def setUp(self):
        self.db_path = f"test_sched_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.db_path + ext):
                    os.remove(self.db_path + ext)
        except Exception:
            pass

    async def test_scheduler_loop_single_pass_delivery(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        due_str = (now_utc - datetime.timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Add due reminder
        rem_id = self.db.add_reminder(
            user_id=888,
            chat_id=999,
            message_id=77,
            reminder_text="جلسه بررسی عملکرد",
            due_at_utc=due_str
        )

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        # Run one iteration of the scheduler worker
        await reminder_scheduler_loop(mock_app, single_pass=True)

        # Verify send_message was invoked
        mock_app.bot.send_message.assert_called_once()
        call_kwargs = mock_app.bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], 999)
        self.assertIn("جلسه بررسی عملکرد", call_kwargs["text"])
        self.assertEqual(call_kwargs["reply_to_message_id"], 77)

        # Verify inline keyboard buttons
        reply_markup = call_kwargs["reply_markup"]
        keyboard = reply_markup.inline_keyboard
        self.assertEqual(len(keyboard), 2)
        # Row 1: Snooze buttons
        self.assertEqual(keyboard[0][0].callback_data, f"snooze:10:{rem_id}")
        self.assertEqual(keyboard[0][1].callback_data, f"snooze:60:{rem_id}")
        # Row 2: Dismiss button
        self.assertEqual(keyboard[1][0].callback_data, f"dismiss:{rem_id}")

        # Verify reminder status in database is updated to delivered
        rows = self.db.get_user_reminders(888, status="delivered")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], rem_id)


class TestM4SnoozeAndDismissCallbacks(unittest.IsolatedAsyncioTestCase):
    """Test 8: Interactive inline Snooze and Dismiss callback query handlers."""

    def setUp(self):
        self.db_path = f"test_cb_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.db_path + ext):
                    os.remove(self.db_path + ext)
        except Exception:
            pass

    async def test_snooze_callback(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        due_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(777, 888, 10, "مصرف قرص", due_str)
        self.db.update_reminder_status(rem_id, "delivered")

        # Mock Update and CallbackQuery
        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.data = f"snooze:10:{rem_id}"
        mock_query.from_user.id = 777
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        await callback_handler(mock_update, MagicMock())

        mock_query.answer.assert_called_once()
        mock_query.edit_message_text.assert_called_once()
        edit_text = mock_query.edit_message_text.call_args[0][0]
        self.assertIn(f"{10} دقیقه به تعویق افتاد", edit_text)

        # Verify DB updated back to pending with future due date
        pending = self.db.get_user_reminders(777, status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], rem_id)
        self.assertGreater(pending[0]["due_at_utc"], due_str)

    async def test_dismiss_callback(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        due_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(777, 888, 10, "خاموش کردن گاز", due_str)

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.data = f"dismiss:{rem_id}"
        mock_query.from_user.id = 777
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        await callback_handler(mock_update, MagicMock())

        mock_query.answer.assert_called_once()
        mock_query.edit_message_text.assert_called_once()
        edit_text = mock_query.edit_message_text.call_args[0][0]
        self.assertIn("با موفقیت پایان یافت", edit_text)

        # Verify DB updated to dismissed
        dismissed = self.db.get_user_reminders(777, status="dismissed")
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["id"], rem_id)


class TestM4ServerRebootRecovery(unittest.IsolatedAsyncioTestCase):
    """Test 9: Server reboot recovery logic (matured <24h delivered, >24h marked missed)."""

    def setUp(self):
        self.db_path = f"test_recov_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.db_path + ext):
                    os.remove(self.db_path + ext)
        except Exception:
            pass

    async def test_reboot_recovery(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        # Reminder 1: Matured 2 hours ago (< 24h) => Recoverable
        recent_due = (now_utc - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r_rec_id = self.db.add_reminder(101, 202, 1, "یادآوری ۲ ساعت قبل", recent_due)

        # Reminder 2: Matured 30 hours ago (> 24h) => Expired/Missed
        stale_due = (now_utc - datetime.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r_stale_id = self.db.add_reminder(101, 202, 2, "یادآوری ۳۰ ساعت قبل", stale_due)

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        # Run recovery
        await recover_server_reboot_reminders(mock_app)

        # Recent reminder must be delivered immediately with delayed banner
        mock_app.bot.send_message.assert_called_once()
        call_kwargs = mock_app.bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], 202)
        self.assertIn("یادآوری با تأخیر", call_kwargs["text"])
        self.assertIn("یادآوری ۲ ساعت قبل", call_kwargs["text"])

        # In DB, recent reminder is delivered
        all_rems = self.db.get_user_reminders(101, status="all")
        rec_item = next(r for r in all_rems if r["id"] == r_rec_id)
        self.assertEqual(rec_item["status"], "delivered")

        # In DB, stale reminder (>24h) is marked 'missed'
        stale_item = next(r for r in all_rems if r["id"] == r_stale_id)
        self.assertEqual(stale_item["status"], "missed")


class TestM4ConcurrencyStress(unittest.TestCase):
    """Test 10: Multi-threaded concurrent database writes with zero SQLite lock errors."""

    def setUp(self):
        self.db_path = f"test_concur_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.db_path + ext):
                    os.remove(self.db_path + ext)
        except Exception:
            pass

    def test_concurrent_reminder_inserts_and_queries(self):
        errors = []
        threads = []
        num_threads = 10
        ops_per_thread = 20

        def worker(tid: int):
            try:
                for i in range(ops_per_thread):
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    due = (now_utc + datetime.timedelta(minutes=i + tid)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    rem_id = self.db.add_reminder(
                        user_id=tid,
                        chat_id=1000 + tid,
                        message_id=i,
                        reminder_text=f"Thread {tid} Reminder {i}",
                        due_at_utc=due
                    )
                    # Read back
                    due_list = self.db.get_due_reminders(due)
                    self.db.update_reminder_status(rem_id, "firing")
            except Exception as e:
                errors.append(e)

        for t in range(num_threads):
            th = threading.Thread(target=worker, args=(t,))
            threads.append(th)
            th.start()

        for th in threads:
            th.join(timeout=15.0)

        self.assertEqual(len(errors), 0, f"Encountered concurrency errors: {errors}")
        total_rows = self.db.execute("SELECT COUNT(*) as cnt FROM reminders")
        self.assertEqual(total_rows[0]["cnt"], num_threads * ops_per_thread)


class TestM4TelegramCommandAndIntent(unittest.IsolatedAsyncioTestCase):
    """Test 11: Telegram /remind command and natural language intent integration."""

    def setUp(self):
        self.db_path = f"test_tg_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.db_path + ext):
                    os.remove(self.db_path + ext)
        except Exception:
            pass

    async def test_remind_command_help(self):
        mock_update = MagicMock()
        mock_update.effective_user.id = "99999"
        mock_update.message.text = "/remind"
        mock_update.message.reply_text = AsyncMock()

        await handle_remind_command(mock_update, MagicMock())
        mock_update.message.reply_text.assert_called_once()
        help_text = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("راهنمای ثبت یادآور هوشمند", help_text)

    async def test_remind_command_execution(self):
        mock_update = MagicMock()
        mock_update.effective_user.id = "99999"
        mock_update.effective_chat.id = 555
        mock_update.message.message_id = 1234
        mock_update.message.text = "/remind 10m پیام ثبت دستوری"
        mock_update.message.reply_text = AsyncMock()

        await handle_remind_command(mock_update, MagicMock())
        mock_update.message.reply_text.assert_called_once()
        confirm_text = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("یادآور با موفقیت در سیستم ثبت شد", confirm_text)
        self.assertIn("پیام ثبت دستوری", confirm_text)

        # Check DB
        rems = self.db.get_user_reminders("99999", status="pending")
        self.assertEqual(len(rems), 1)
        self.assertEqual(rems[0]["reminder_text"], "پیام ثبت دستوری")

    async def test_natural_language_intent_in_handle_message(self):
        mock_update = MagicMock()
        mock_update.effective_user.id = "99999"
        mock_update.effective_chat.id = 555
        mock_update.message.message_id = 5678
        mock_update.message.text = "۱۰ دقیقه دیگه آب بخورم"
        mock_update.message.reply_text = AsyncMock()

        with patch("bot.check_access", return_value=True):
            await handle_message(mock_update, MagicMock())

        mock_update.message.reply_text.assert_called_once()
        confirm_text = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("یادآور با موفقیت در سیستم ثبت شد", confirm_text)
        self.assertIn("آب بخورم", confirm_text)

        # Check DB
        rems = self.db.get_user_reminders("99999", status="pending")
        self.assertEqual(len(rems), 1)
        self.assertEqual(rems[0]["reminder_text"], "آب بخورم")


if __name__ == "__main__":
    unittest.main()
