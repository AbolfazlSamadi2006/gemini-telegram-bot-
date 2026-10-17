"""
Adversarial Stress Test Suite for Milestone 4 (test_challenger_m4_2.py)
Focus: R3 Persistence, Scheduler Loop Resilience, Reboot Recovery, and Concurrency.

Tested Dimensions:
1. High-concurrency reminder insertion and polling (50+ and 60+ concurrent threads).
2. Simulated server crash / power failure reboot recovery (<=24h delivered, >24h marked missed).
3. Rapid repeated snooze and dismiss cycles across multiple reminders.
4. Scheduler worker loop stability under simulated Telegram network timeouts and rate-limiting (RetryAfter, Forbidden, TimedOut).
5. Monotonic ordering of UTC ISO-8601 timestamps in SQLite queries with covering index verification.

100% Offline, Zero External Network Calls.
"""

import os
import sys
import uuid
import time
import random
import asyncio
import sqlite3
import datetime
import threading
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure test credentials are set
os.environ.setdefault("GEMINI_API_KEY", "dummy_challenger_m4_2_key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy_challenger_m4_2_token")

from telegram.error import TimedOut, RetryAfter, Forbidden, NetworkError
import bot
from bot import (
    Database,
    TEHRAN_TZ,
    reminder_scheduler_loop,
    recover_server_reboot_reminders,
    callback_handler,
)


def cleanup_db(db_path: str):
    """Safely cleans up temporary database files including WAL and SHM."""
    try:
        if os.path.exists(db_path):
            os.remove(db_path)
        for ext in ("-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass


class TestChallengerM42HighConcurrency(unittest.TestCase):
    """
    Category 1: High-Concurrency Reminder Insertion and Polling.
    Evaluates 50+ and 60+ simultaneous threads hammering SQLite to verify zero
    'sqlite3.OperationalError: database is locked' or database corruption.
    """

    def setUp(self):
        self.db_path = f"test_ch42_concur_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        cleanup_db(self.db_path)

    def test_01_fifty_threads_concurrent_inserts_and_queries(self):
        """Stress-test 50 concurrent threads executing 1,000 total operations with zero lock errors."""
        num_threads = 50
        ops_per_thread = 20
        errors = []
        barrier = threading.Barrier(num_threads)

        def worker(tid: int):
            try:
                barrier.wait(timeout=10.0)
                for i in range(ops_per_thread):
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    due_iso = (now_utc + datetime.timedelta(minutes=i + tid)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    rem_id = self.db.add_reminder(
                        user_id=1000 + tid,
                        chat_id=2000 + tid,
                        message_id=i,
                        reminder_text=f"Stress reminder {tid}-{i}",
                        due_at_utc=due_iso
                    )
                    # Concurrent read
                    due_rems = self.db.get_due_reminders(due_iso)
                    self.assertIsInstance(due_rems, list)

                    # Concurrent status update
                    self.db.update_reminder_status(rem_id, "firing")
            except Exception as e:
                errors.append((tid, e))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30.0)

        self.assertEqual(len(errors), 0, f"Encountered concurrency errors in 50 threads: {errors}")
        total = self.db.execute("SELECT COUNT(*) as cnt FROM reminders")[0]["cnt"]
        self.assertEqual(total, num_threads * ops_per_thread)

    def test_02_sixty_threads_mixed_workload_extreme_contention(self):
        """Stress-test 60 threads performing heterogeneous operations (insert, query, snooze, update, recover)."""
        num_threads = 60
        ops_per_thread = 15
        errors = []
        barrier = threading.Barrier(num_threads)

        def worker(tid: int):
            try:
                barrier.wait(timeout=10.0)
                for i in range(ops_per_thread):
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    future_iso = (now_utc + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

                    role = tid % 4
                    if role == 0:
                        # Writer: Add & Snooze
                        r_id = self.db.add_reminder(tid, 5000 + tid, i, f"Note {tid}_{i}", now_iso)
                        self.db.snooze_reminder(r_id, future_iso)
                    elif role == 1:
                        # Writer: Add & Deliver
                        r_id = self.db.add_reminder(tid, 5000 + tid, i, f"Note {tid}_{i}", now_iso)
                        self.db.update_reminder_status(r_id, "firing")
                        self.db.update_reminder_status(r_id, "delivered")
                    elif role == 2:
                        # Reader/Writer: Polling & Recovery
                        self.db.get_due_reminders(now_iso)
                        self.db.recover_missed_reminders(now_iso, max_delay_hours=24.0)
                    else:
                        # Reader: User Reminders
                        self.db.get_user_reminders(tid, status="all")
                        self.db.get_user_reminders(tid, status="pending")
            except Exception as e:
                errors.append((tid, e))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30.0)

        self.assertEqual(len(errors), 0, f"Encountered errors in 60-thread mixed workload: {errors}")

    def test_03_concurrent_snooze_and_dismiss_race(self):
        """Concurrent threads snoozing and dismissing the same and different reminders."""
        # Create 10 target reminders
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_ids = [self.db.add_reminder(99, 999, i, f"Target {i}", now_iso) for i in range(10)]

        errors = []
        num_threads = 20

        def race_worker(tid: int):
            try:
                for rem_id in rem_ids:
                    if tid % 2 == 0:
                        new_due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        self.db.snooze_reminder(rem_id, new_due)
                    else:
                        self.db.update_reminder_status(rem_id, "dismissed")
            except Exception as e:
                errors.append((tid, e))

        threads = [threading.Thread(target=race_worker, args=(t,)) for t in range(num_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15.0)

        self.assertEqual(len(errors), 0, f"Errors in concurrent snooze/dismiss race: {errors}")
        # Every reminder must have a valid final status (either pending or dismissed)
        rows = self.db.get_user_reminders(99, status="all")
        for row in rows:
            self.assertIn(row["status"], ("pending", "dismissed"))


class TestChallengerM42SimulatedRebootPowerFailure(unittest.IsolatedAsyncioTestCase):
    """
    Category 2: Simulated Server Crash & Power Failure (Reboot Recovery).
    Verifies that upon sudden restart:
    - Reminders matured <= 24h ago in both 'pending' and 'firing' states are delivered.
    - Reminders matured > 24h ago are marked 'missed'.
    - Future and completed reminders remain unaffected.
    - Idempotency and recovery stability under Telegram errors.
    """

    def setUp(self):
        self.db_path = f"test_ch42_reboot_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        cleanup_db(self.db_path)

    async def test_04_reboot_recovery_pending_and_firing_within_24h(self):
        """Reminders overdue <= 24h in pending and firing states delivered with delayed banner."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        # Pending 2h overdue
        id_pen = self.db.add_reminder(101, 201, 11, "جلسه مهم", (now - datetime.timedelta(hours=2)).strftime(fmt))
        # Firing 30m overdue (server died mid-dispatch!)
        id_fir = self.db.add_reminder(101, 201, 12, "خرید نان", (now - datetime.timedelta(minutes=30)).strftime(fmt))
        self.db.update_reminder_status(id_fir, "firing")

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        await recover_server_reboot_reminders(mock_app)

        self.assertEqual(mock_app.bot.send_message.call_count, 2)
        sent_texts = [call.kwargs["text"] for call in mock_app.bot.send_message.call_args_list]
        for text in sent_texts:
            self.assertIn("یادآوری با تأخیر", text)
            self.assertIn("به دلیل بازنشانی یا بروزرسانی سرور", text)

        # Both must be marked delivered in database
        all_rems = {r["id"]: r for r in self.db.get_user_reminders(101, status="all")}
        self.assertEqual(all_rems[id_pen]["status"], "delivered")
        self.assertEqual(all_rems[id_fir]["status"], "delivered")

    async def test_05_reboot_recovery_expired_past_24h(self):
        """Reminders overdue > 24h (pending and firing) marked 'missed' and NOT delivered."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        # Pending 26h overdue
        id_pen_stale = self.db.add_reminder(102, 202, 21, "یادآوری ۲۶ ساعت پیش", (now - datetime.timedelta(hours=26)).strftime(fmt))
        # Firing 72h overdue
        id_fir_stale = self.db.add_reminder(102, 202, 22, "یادآوری ۳ روز پیش", (now - datetime.timedelta(days=3)).strftime(fmt))
        self.db.update_reminder_status(id_fir_stale, "firing")

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        await recover_server_reboot_reminders(mock_app)

        # Zero messages delivered
        mock_app.bot.send_message.assert_not_called()

        # Both marked missed with notified_at set
        all_rems = {r["id"]: r for r in self.db.get_user_reminders(102, status="all")}
        self.assertEqual(all_rems[id_pen_stale]["status"], "missed")
        self.assertIsNotNone(all_rems[id_pen_stale]["notified_at"])
        self.assertEqual(all_rems[id_fir_stale]["status"], "missed")
        self.assertIsNotNone(all_rems[id_fir_stale]["notified_at"])

    async def test_06_reboot_recovery_preserves_future_and_completed(self):
        """Future reminders remain pending; already delivered/dismissed/cancelled remain unchanged."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        # Future reminder
        id_future = self.db.add_reminder(103, 203, 31, "آینده", (now + datetime.timedelta(hours=5)).strftime(fmt))
        # Delivered reminder
        id_deliv = self.db.add_reminder(103, 203, 32, "قبلی تحویل‌شده", (now - datetime.timedelta(hours=2)).strftime(fmt))
        self.db.update_reminder_status(id_deliv, "delivered")
        # Dismissed reminder
        id_dism = self.db.add_reminder(103, 203, 33, "قبلی لغو شده", (now - datetime.timedelta(hours=3)).strftime(fmt))
        self.db.update_reminder_status(id_dism, "dismissed")

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        await recover_server_reboot_reminders(mock_app)

        mock_app.bot.send_message.assert_not_called()
        all_rems = {r["id"]: r for r in self.db.get_user_reminders(103, status="all")}
        self.assertEqual(all_rems[id_future]["status"], "pending")
        self.assertEqual(all_rems[id_deliv]["status"], "delivered")
        self.assertEqual(all_rems[id_dism]["status"], "dismissed")

    async def test_07_reboot_recovery_exact_24h_boundaries(self):
        """Boundary test: 23 hours 59 minutes (delivered) vs 24 hours 01 minutes (missed)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        id_under_24 = self.db.add_reminder(104, 204, 41, "زیر ۲۴ ساعت", (now - datetime.timedelta(hours=23, minutes=59)).strftime(fmt))
        id_over_24 = self.db.add_reminder(104, 204, 42, "بالای ۲۴ ساعت", (now - datetime.timedelta(hours=24, minutes=1)).strftime(fmt))

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        await recover_server_reboot_reminders(mock_app)

        # Only the under-24h reminder is sent
        self.assertEqual(mock_app.bot.send_message.call_count, 1)
        self.assertIn("زیر ۲۴ ساعت", mock_app.bot.send_message.call_args.kwargs["text"])

        all_rems = {r["id"]: r for r in self.db.get_user_reminders(104, status="all")}
        self.assertEqual(all_rems[id_under_24]["status"], "delivered")
        self.assertEqual(all_rems[id_over_24]["status"], "missed")

    async def test_08_reboot_recovery_corrupt_due_date_handling(self):
        """Corrupt or unparseable due_at_utc timestamps tested for graceful handling."""
        with self.db._get_connection() as conn:
            c = conn.cursor()
            # 1. Past-collated corrupt timestamp (due <= now_utc_str):
            # Picked up by SQL query and marked 'missed' via the delay_hours = 999.0 fallback
            c.execute(
                "INSERT INTO reminders (user_id, chat_id, message_id, reminder_text, due_at_utc, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (105, 205, 51, "خراب گذشته", "2020-INVALID-DATE", "pending", "2026-10-16T12:00:00Z")
            )
            past_corrupt_id = c.lastrowid

            # 2. Future-collated corrupt timestamp (due > now_utc_str in lexicographical comparison):
            # Excluded by SQL 'WHERE due_at_utc <= ?' filter, remaining 'pending'
            c.execute(
                "INSERT INTO reminders (user_id, chat_id, message_id, reminder_text, due_at_utc, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (105, 205, 52, "خراب آینده", "9999-INVALID-DATE", "pending", "2026-10-16T12:00:00Z")
            )
            future_corrupt_id = c.lastrowid

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        # Recovery executes without throwing exceptions
        await recover_server_reboot_reminders(mock_app)

        all_rems = {r["id"]: r for r in self.db.get_user_reminders(105, status="all")}
        self.assertEqual(all_rems[past_corrupt_id]["status"], "missed", "Past-collated corrupt date must be marked missed")
        self.assertEqual(all_rems[future_corrupt_id]["status"], "pending", "Future-collated corrupt date remains pending")

    async def test_09_reboot_recovery_idempotency(self):
        """Consecutive runs of recover_server_reboot_reminders produce zero duplicate deliveries."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        self.db.add_reminder(106, 206, 61, "تست تکرار", (now - datetime.timedelta(hours=1)).strftime(fmt))

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        # Pass 1
        await recover_server_reboot_reminders(mock_app)
        self.assertEqual(mock_app.bot.send_message.call_count, 1)

        # Pass 2: Must be 0
        mock_app.bot.send_message.reset_mock()
        await recover_server_reboot_reminders(mock_app)
        self.assertEqual(mock_app.bot.send_message.call_count, 0)

    async def test_10_reboot_recovery_telegram_failure_resilience(self):
        """Telegram send failure during reboot recovery does not crash and updates status to delivered."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        r_id = self.db.add_reminder(107, 207, 71, "خطای تلگرام", (now - datetime.timedelta(hours=1)).strftime(fmt))

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=Exception("Simulated Telegram Disconnect"))

        # Should log warning and proceed without unhandled exception
        await recover_server_reboot_reminders(mock_app)

        all_rems = {r["id"]: r for r in self.db.get_user_reminders(107, status="all")}
        self.assertEqual(all_rems[r_id]["status"], "delivered")


class TestChallengerM42SnoozeAndDismissLifecycle(unittest.IsolatedAsyncioTestCase):
    """
    Category 3: Rapid Repeated Snooze and Dismiss Cycles.
    Validates button interaction routing, database updates, and state persistence.
    """

    def setUp(self):
        self.db_path = f"test_ch42_snooze_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        cleanup_db(self.db_path)

    async def test_11_repeated_snooze_cycle(self):
        """Repeated sequential snoozing (+10m, +60m, +10m) properly postpones due_at_utc and resets status."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        due_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(301, 401, 1, "دارو", due_str)
        self.db.update_reminder_status(rem_id, "delivered")

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = 301
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        # Snooze 1: +10m
        mock_query.data = f"snooze:10:{rem_id}"
        await callback_handler(mock_update, MagicMock())
        r1 = self.db.get_user_reminders(301, status="all")[0]
        self.assertEqual(r1["status"], "pending")
        self.assertIsNone(r1["notified_at"])
        due1 = r1["due_at_utc"]
        self.assertGreater(due1, due_str)

        # Snooze 2: +60m
        mock_query.data = f"snooze:60:{rem_id}"
        await callback_handler(mock_update, MagicMock())
        r2 = self.db.get_user_reminders(301, status="all")[0]
        self.assertEqual(r2["status"], "pending")
        due2 = r2["due_at_utc"]
        self.assertGreater(due2, due1)

        # Snooze 3: +10m
        mock_query.data = f"snooze:10:{rem_id}"
        await callback_handler(mock_update, MagicMock())
        r3 = self.db.get_user_reminders(301, status="all")[0]
        self.assertEqual(r3["status"], "pending")

    async def test_12_dismiss_terminates_reminder(self):
        """Dismiss callback permanently finalizes reminder with status='dismissed' and notified_at."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        due_str = (now_utc - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(302, 402, 2, "خاموش کردن گاز", due_str)

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = 302
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        mock_query.data = f"dismiss:{rem_id}"
        await callback_handler(mock_update, MagicMock())

        r = self.db.get_user_reminders(302, status="all")[0]
        self.assertEqual(r["status"], "dismissed")
        self.assertIsNotNone(r["notified_at"])

        # Scheduler must not fetch dismissed reminders
        due = self.db.get_due_reminders(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(len([d for d in due if d["id"] == rem_id]), 0)

    async def test_13_snooze_dismiss_on_nonexistent_reminder(self):
        """Non-existent reminder IDs handled gracefully in snooze and dismiss callbacks."""
        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = 303
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        # Should not raise exception
        mock_query.data = "snooze:10:99999999"
        await callback_handler(mock_update, MagicMock())

        mock_query.data = "dismiss:99999999"
        await callback_handler(mock_update, MagicMock())

    async def test_14_adversarial_malformed_callback_data(self):
        """Adversarial callback payloads (non-numeric parameters) tested for graceful handling."""
        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = 304
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        # Insufficient parts
        mock_query.data = "snooze"
        await callback_handler(mock_update, MagicMock())

        mock_query.data = "dismiss"
        await callback_handler(mock_update, MagicMock())

        # Non-numeric parameter causes ValueError in integer casting
        mock_query.data = "snooze:not_a_number:123"
        with self.assertRaises(ValueError):
            await callback_handler(mock_update, MagicMock())


class TestChallengerM42SchedulerWorkerResilience(unittest.IsolatedAsyncioTestCase):
    """
    Category 4: Scheduler Worker Loop Stability Under Network Timeouts & Rate-Limiting.
    Tests:
    - Network timeout resets status from 'firing' to 'pending'.
    - RetryAfter resets status to 'pending'.
    - Forbidden marks reminder as 'cancelled'.
    - Mixed batch fault isolation: one failure does not halt remaining deliveries.
    - Clean cancellation handling.
    - Outer exception survivability.
    """

    def setUp(self):
        self.db_path = f"test_ch42_sched_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)
        bot.db = self.db

    def tearDown(self):
        bot.db = Database()
        cleanup_db(self.db_path)

    async def test_15_scheduler_telegram_timeout_retry(self):
        """When send_message raises TimedOut, reminder is reverted to 'pending' for future retry."""
        now = datetime.datetime.now(datetime.timezone.utc)
        past = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(501, 601, None, "Timeout test", past)

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=TimedOut("Request timed out"))

        await reminder_scheduler_loop(mock_app, single_pass=True)

        rem = self.db.get_user_reminders(501, status="all")[0]
        self.assertEqual(rem["status"], "pending", "Timed out reminder must revert to pending")

    async def test_16_scheduler_telegram_rate_limiting_retry_after(self):
        """When send_message raises RetryAfter, reminder is reverted to 'pending'."""
        now = datetime.datetime.now(datetime.timezone.utc)
        past = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(502, 602, None, "Rate limit test", past)

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=RetryAfter(10))

        await reminder_scheduler_loop(mock_app, single_pass=True)

        rem = self.db.get_user_reminders(502, status="all")[0]
        self.assertEqual(rem["status"], "pending", "Rate limited reminder must revert to pending")

    async def test_17_scheduler_telegram_forbidden_cancels_reminder(self):
        """When send_message raises Forbidden (user blocked bot), status is set to 'cancelled'."""
        now = datetime.datetime.now(datetime.timezone.utc)
        past = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rem_id = self.db.add_reminder(503, 603, None, "Blocked user test", past)

        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock(side_effect=Forbidden("Forbidden: bot was blocked by the user"))

        await reminder_scheduler_loop(mock_app, single_pass=True)

        rem = self.db.get_user_reminders(503, status="all")[0]
        self.assertEqual(rem["status"], "cancelled", "Forbidden reminder must be marked cancelled")

    async def test_18_scheduler_mixed_batch_fault_isolation(self):
        """Mixed batch of 5 reminders: Timeout, RetryAfter, Forbidden, and 2 Successes."""
        now = datetime.datetime.now(datetime.timezone.utc)
        past = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        id_to = self.db.add_reminder(1, 10, None, "Timeout", past)
        id_ra = self.db.add_reminder(2, 20, None, "RetryAfter", past)
        id_fb = self.db.add_reminder(3, 30, None, "Forbidden", past)
        id_ok1 = self.db.add_reminder(4, 40, None, "Success 1", past)
        id_ok2 = self.db.add_reminder(5, 50, None, "Success 2", past)

        mock_app = MagicMock()
        async def mock_dispatch(chat_id, **kwargs):
            if chat_id == 10:
                raise TimedOut("Timed out")
            elif chat_id == 20:
                raise RetryAfter(5)
            elif chat_id == 30:
                raise Forbidden("Forbidden: bot was kicked from group")
            elif chat_id in (40, 50):
                return True
            raise Exception("Unknown chat")

        mock_app.bot.send_message = AsyncMock(side_effect=mock_dispatch)

        await reminder_scheduler_loop(mock_app, single_pass=True)

        all_r = {r["id"]: r for r in self.db.execute("SELECT * FROM reminders")}
        self.assertEqual(all_r[id_to]["status"], "pending")
        self.assertEqual(all_r[id_ra]["status"], "pending")
        self.assertEqual(all_r[id_fb]["status"], "cancelled")
        self.assertEqual(all_r[id_ok1]["status"], "delivered")
        self.assertEqual(all_r[id_ok2]["status"], "delivered")

    async def test_19_scheduler_loop_clean_cancellation(self):
        """Scheduler loop cleanly terminates on asyncio.CancelledError."""
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        task = asyncio.create_task(reminder_scheduler_loop(mock_app, single_pass=False))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertTrue(task.done())

    async def test_20_scheduler_outer_exception_survivability(self):
        """Outer exception (e.g. database error) is logged and does not kill scheduler loop."""
        mock_app = MagicMock()
        call_count = 0

        # Simulate transient error on first get_due_reminders call
        real_get = self.db.get_due_reminders
        def flappy_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise sqlite3.OperationalError("Simulated transient query error")
            return []

        with patch.object(self.db, "get_due_reminders", side_effect=flappy_get):
            # Run single pass; loop catches error and does not raise
            await reminder_scheduler_loop(mock_app, single_pass=True)
            self.assertEqual(call_count, 1)


class TestChallengerM42MonotonicISO8601Ordering(unittest.TestCase):
    """
    Category 5: Monotonic Ordering of UTC ISO-8601 Timestamps in SQLite Queries.
    Evaluates:
    - 1,000 randomized ISO-8601 timestamps spanning years 2024 to 2030, leap days, and boundaries.
    - Equivalence between SQLite 'ORDER BY due_at_utc ASC' and Python sorted().
    - Exact range filtering (due_at_utc <= ?) matching Python datetimes.
    - SQLite covering index utilization.
    - Standard formatting consistency (%Y-%m-%dT%H:%M:%SZ).
    """

    def setUp(self):
        self.db_path = f"test_ch42_mono_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        cleanup_db(self.db_path)

    def test_21_monotonic_order_equivalence_1000_samples(self):
        """1,000 randomized UTC ISO-8601 timestamps sort identically in SQLite and Python."""
        base_dt = datetime.datetime(2024, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        timestamps = []
        for i in range(1000):
            delta = datetime.timedelta(
                days=random.randint(0, 2200),
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
                seconds=random.randint(0, 59)
            )
            dt = base_dt + delta
            timestamps.append(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

        timestamps = sorted(list(set(timestamps)))

        shuffled = list(timestamps)
        random.shuffle(shuffled)

        for idx, ts in enumerate(shuffled):
            self.db.add_reminder(1, 10, idx, f"Rem_{idx}", ts)

        with self.db._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT due_at_utc FROM reminders ORDER BY due_at_utc ASC")
            db_sorted = [r["due_at_utc"] for r in c.fetchall()]

        self.assertEqual(db_sorted, timestamps, "SQLite string sort must strictly match Python sorted timestamps")

    def test_22_covering_index_usage(self):
        """SQLite query optimizer employs covering index idx_reminders_status_due."""
        with self.db._get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "EXPLAIN QUERY PLAN SELECT due_at_utc FROM reminders WHERE status = ? AND due_at_utc <= ? ORDER BY due_at_utc ASC",
                ("pending", "2026-10-16T12:00:00Z")
            )
            plan = [dict(r) for r in c.fetchall()]

        details = " ".join(p["detail"] for p in plan)
        self.assertIn("idx_reminders_status_due", details)

    def test_23_exact_range_filtering_monotonicity(self):
        """Boundary range queries across leap days and year rollovers match Python <= filtering."""
        boundary_dates = [
            "2024-02-28T23:59:59Z",
            "2024-02-29T00:00:00Z",
            "2024-02-29T23:59:59Z",
            "2024-03-01T00:00:00Z",
            "2025-12-31T23:59:59Z",
            "2026-01-01T00:00:00Z",
            "2026-06-30T23:59:59Z",
            "2026-07-01T00:00:00Z"
        ]

        shuffled = list(boundary_dates)
        random.shuffle(shuffled)
        for idx, bd in enumerate(shuffled):
            self.db.add_reminder(2, 20, idx, f"Bound_{idx}", bd)

        for cutoff in boundary_dates:
            with self.db._get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT due_at_utc FROM reminders WHERE status = 'pending' AND due_at_utc <= ? ORDER BY due_at_utc ASC",
                    (cutoff,)
                )
                db_results = [r["due_at_utc"] for r in c.fetchall()]

            expected = sorted([d for d in boundary_dates if d <= cutoff])
            self.assertEqual(db_results, expected, f"Mismatch at boundary cutoff {cutoff}")


if __name__ == "__main__":
    unittest.main()
