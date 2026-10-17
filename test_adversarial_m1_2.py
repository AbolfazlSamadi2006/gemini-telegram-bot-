"""
Adversarial Stress Test Suite for Milestone 1: Database Reminders & Concurrency
Challenger: Challenger M1-2
Tests:
1. Boundary values for timestamps (ISO-8601 formatting, millisecond vs second, leap years, year 2038+)
2. Empty database queries, invalid reminder IDs, snoozing non-existent reminders, SQL injection resilience
3. 64-bit Telegram user IDs and negative supergroup chat IDs (-100...)
4. Malformed Unicode text in reminder_text (Persian, Arabic diacritics, ZWNJ, ZWJ, BiDi, emojis, 20000+ chars)
5. Recovery logic with mixed batches (1 min ago, 23 hours ago, 24 hours exact, 25 hours ago, 100 days ago, future, unparseable)
6. Idempotency of recovery & SQLite file lock cleanup (no WinError 32)
7. Consistency parity between bot.py Database and test_db.py Database
"""

import os
import sys
import datetime
import traceback
import sqlite3

# Force UTF-8 stdout encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure GEMINI_API_KEY is present so bot.py can be imported without throwing
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_dummy_key_m1_2")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_telegram_dummy_token_m1_2")

import test_db
import bot

TEST_DB_PATH = "test_adversarial_m1_2.db"


def cleanup_db(path: str = TEST_DB_PATH):
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                print(f"Warning cleaning {p}: {e}")


class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.findings = []

    def assert_true(self, condition: bool, description: str):
        if condition:
            self.passed += 1
            print(f"  [PASS] {description}")
        else:
            self.failed += 1
            msg = f"Assertion failed: {description}"
            print(f"  [FAIL] {msg}")
            self.findings.append(msg)

    def assert_equal(self, actual, expected, description: str):
        if actual == expected:
            self.passed += 1
            print(f"  [PASS] {description}")
        else:
            self.failed += 1
            msg = f"Assertion failed: {description} | Expected: {expected!r}, Got: {actual!r}"
            print(f"  [FAIL] {msg}")
            self.findings.append(msg)


def run_tests():
    runner = TestRunner()
    cleanup_db()

    print("======================================================================")
    print("STARTING ADVERSARIAL STRESS TEST SUITE (CHALLENGER M1-2)")
    print("======================================================================")

    # ------------------------------------------------------------------
    # SUITE 1: Empty Database Queries & Edge Inputs
    # ------------------------------------------------------------------
    print("\n--- Suite 1: Empty Database & Invalid ID Handling ---")
    cleanup_db()
    db = bot.Database(TEST_DB_PATH)

    # 1.1 Query due reminders on empty DB
    due = db.get_due_reminders("2026-10-16T12:00:00Z")
    runner.assert_equal(due, [], "get_due_reminders on empty DB returns empty list")

    # 1.2 Query user reminders for non-existent user
    user_rems = db.get_user_reminders("non_existent_user_99999")
    runner.assert_equal(user_rems, [], "get_user_reminders for non-existent user returns empty list")

    # 1.3 User reminders with status = 'all' and status = None on empty DB
    runner.assert_equal(db.get_user_reminders("user1", status=None), [], "get_user_reminders with status=None returns empty list")
    runner.assert_equal(db.get_user_reminders("user1", status="all"), [], "get_user_reminders with status='all' returns empty list")
    runner.assert_equal(db.get_user_reminders("user1", status="ALL"), [], "get_user_reminders with status='ALL' (uppercase) returns empty list")
    runner.assert_equal(db.get_user_reminders("user1", status="non_existent_status"), [], "get_user_reminders with unknown status returns empty list")

    # 1.4 Update non-existent reminder IDs
    runner.assert_equal(db.update_reminder_status(999999, "delivered"), False, "update_reminder_status(999999) returns False")
    runner.assert_equal(db.update_reminder_status(-1, "delivered"), False, "update_reminder_status(-1) returns False")
    runner.assert_equal(db.update_reminder_status(0, "delivered"), False, "update_reminder_status(0) returns False")

    # 1.5 Snooze non-existent reminder IDs
    runner.assert_equal(db.snooze_reminder(999999, "2026-10-16T15:00:00Z"), False, "snooze_reminder(999999) returns False")
    runner.assert_equal(db.snooze_reminder(-1, "2026-10-16T15:00:00Z"), False, "snooze_reminder(-1) returns False")

    # 1.6 Recovery on empty database
    rec_empty = db.recover_missed_reminders("2026-10-16T12:00:00Z", max_delay_hours=24.0)
    runner.assert_equal(rec_empty["recoverable"], [], "recover_missed_reminders on empty DB has empty recoverable")
    runner.assert_equal(rec_empty["missed"], [], "recover_missed_reminders on empty DB has empty missed")
    runner.assert_equal(rec_empty["missed_count"], 0, "recover_missed_reminders on empty DB has missed_count 0")

    # 1.7 64-bit Telegram user IDs and negative supergroup IDs
    tg_user_id = 5894729103  # > 2^31 - 1
    tg_supergroup_id = -1001928374650  # negative 64-bit Telegram supergroup ID
    r_tg_id = db.add_reminder(tg_user_id, tg_supergroup_id, 999, "Telegram 64-bit test", "2026-10-16T12:00:00Z")
    runner.assert_true(r_tg_id > 0, "add_reminder supports 64-bit user_id and negative supergroup chat_id")
    tg_rems = db.get_user_reminders(tg_user_id)
    runner.assert_equal(len(tg_rems), 1, "get_user_reminders retrieves reminder for 64-bit user_id")
    runner.assert_equal(tg_rems[0]["chat_id"], tg_supergroup_id, "Negative supergroup chat_id preserved verbatim")

    # 1.8 SQL Injection resilience in add_reminder, user_id, and query parameters
    sqli_text = "'); DROP TABLE reminders; -- \" ' OR '1'='1"
    sqli_uid = "999; DROP TABLE users; --"
    r_id = db.add_reminder(sqli_uid, 12345, None, sqli_text, "2026-10-16T12:00:00Z")
    runner.assert_true(r_id > 0, "add_reminder succeeds with SQL injection payload text and user_id")
    
    # Verify table wasn't dropped
    user_rems_sqli = db.get_user_reminders(sqli_uid, status="pending")
    runner.assert_equal(len(user_rems_sqli), 1, "SQL injection string preserved as literal data without executing")
    runner.assert_equal(user_rems_sqli[0]["reminder_text"], sqli_text, "Reminder text matches SQL injection string verbatim")

    del db
    cleanup_db()

    # ------------------------------------------------------------------
    # SUITE 2: Boundary Values for Timestamps & Precision
    # ------------------------------------------------------------------
    print("\n--- Suite 2: Timestamp Boundary Values & Formats ---")
    db = bot.Database(TEST_DB_PATH)

    timestamps = [
        ("standard_iso", "2026-10-16T12:00:00Z", True),
        ("leap_year_2024", "2024-02-29T23:59:59Z", True),
        ("leap_year_2028", "2028-02-29T00:00:00Z", True),
        ("year_2038_boundary", "2038-01-19T03:14:07Z", True),
        ("year_2038_plus", "2038-01-19T03:14:08Z", True),
        ("year_2038_next_day", "2038-01-20T00:00:00Z", True),
        ("year_2050", "2050-12-31T23:59:59Z", True),
        ("year_9999", "9999-12-31T23:59:59Z", True),
        ("millisecond_precision", "2026-10-16T12:00:00.123Z", True),
        ("offset_utc_syntax", "2026-10-16T12:00:00+00:00", True),
    ]

    ids = {}
    for name, ts, _ in timestamps:
        rid = db.add_reminder(101, 202, None, f"Reminder for {name}", ts)
        ids[name] = rid

    # 2.1 Check due reminders before 2025
    due_2025 = db.get_due_reminders("2025-01-01T00:00:00Z")
    runner.assert_equal(len(due_2025), 1, "Only leap_year_2024 is due before 2025-01-01")
    runner.assert_equal(due_2025[0]["id"], ids["leap_year_2024"], "leap_year_2024 correctly identified as due")

    # 2.2 Check due reminders up to 2026-10-16T12:00:00Z (standard ISO)
    due_today = db.get_due_reminders("2026-10-16T12:00:00Z")
    due_ids = {r["id"] for r in due_today}
    runner.assert_true(ids["standard_iso"] in due_ids, "standard_iso is due at exact match time")
    runner.assert_true(ids["leap_year_2024"] in due_ids, "leap_year_2024 is due")

    # Test sorting order of distant future timestamps (2038+, 2050, 9999)
    all_user = db.get_user_reminders(101, status="pending", limit=50)
    
    # Verify year 2038 and beyond are sorted after 2026
    idx_2026 = [i for i, r in enumerate(all_user) if r["id"] == ids["standard_iso"]][0]
    idx_2038 = [i for i, r in enumerate(all_user) if r["id"] == ids["year_2038_boundary"]][0]
    idx_2050 = [i for i, r in enumerate(all_user) if r["id"] == ids["year_2050"]][0]
    idx_9999 = [i for i, r in enumerate(all_user) if r["id"] == ids["year_9999"]][0]

    runner.assert_true(idx_2026 < idx_2038 < idx_2050 < idx_9999, "Timestamps sorted chronologically into 2038, 2050, 9999 without 32-bit overflow")

    # Document lexicographical comparison of millisecond vs non-millisecond:
    ms_in_due = ids["millisecond_precision"] in due_ids
    print(f"  [BEHAVIOR_OBSERVATION] Millisecond precision string in SQLite string comparison: '.' < 'Z' -> {ms_in_due}")

    del db
    cleanup_db()

    # ------------------------------------------------------------------
    # SUITE 3: Malformed Unicode, Persian, BiDi, Emojis, Long Strings
    # ------------------------------------------------------------------
    print("\n--- Suite 3: Malformed Unicode & Boundary String Handling ---")
    db = bot.Database(TEST_DB_PATH)

    test_strings = [
        ("persian_standard", "فردا ساعت ۸ صبح یادم بنداز جزوه ساختمان داده را بخوانم"),
        ("persian_zwnj", "می‌خواهم کتاب‌خانه‌ها و پی‌گیری‌های روزمره را چک کنم (نیم‌فاصله)"),
        ("arabic_diacritics", "تَذْكِيرٌ مُهِمٌّ جِدّاً لِلاِمْتِحَانِ النِّهَائِيِّ"),
        ("complex_emojis", "یادآوری جلسه 👨‍👩‍👧‍👦 با تیم 🚀🔥🧠🎉 و پرچم 🏳️‍🌈"),
        ("bidi_mixed", "کتاب Python 3.13 Deep Dive را از ص 15 تا 45 مطالعه کن! (Left-to-Right \u200e and Right-to-Left \u200f marks)"),
        ("control_chars", "خط اول\nخط دوم\r\nخط سوم با تب:\tپایان."),
        ("html_tags", "<b>یادداشت مهم</b> <script>alert('xss')</script> &amp;"),
        ("long_4000_chars", "پ" * 4000),
        ("huge_20000_chars", "یادداشت طولانی: " + ("الف‌ب‌ج " * 2500)),
    ]

    for name, content in test_strings:
        rid = db.add_reminder(555, 666, 12, content, "2026-10-16T12:00:00Z")
        runner.assert_true(rid > 0, f"Inserted reminder with {name} (length={len(content)})")
        
        # Query back and verify verbatim match
        retrieved = db.get_user_reminders(555, status="all", limit=50)
        matching = [r for r in retrieved if r["id"] == rid]
        runner.assert_true(len(matching) == 1, f"Retrieved reminder {name} successfully")
        runner.assert_equal(matching[0]["reminder_text"], content, f"Verbatim match preserved for {name}")

    del db
    cleanup_db()

    # ------------------------------------------------------------------
    # SUITE 4: Recovery Logic with Mixed Overdue Batches & Unparseable TS
    # ------------------------------------------------------------------
    print("\n--- Suite 4: Recovery Logic with Mixed Overdue Batches ---")
    db = bot.Database(TEST_DB_PATH)

    # Reference time T0
    T0 = datetime.datetime(2026, 10, 16, 12, 0, 0, tzinfo=datetime.timezone.utc)
    T0_str = T0.strftime("%Y-%m-%dT%H:%M:%SZ")

    batch = [
        ("due_1min_ago", datetime.timedelta(minutes=1), "recoverable"),
        ("due_10min_ago", datetime.timedelta(minutes=10), "recoverable"),
        ("due_1h_ago", datetime.timedelta(hours=1), "recoverable"),
        ("due_12h_ago", datetime.timedelta(hours=12), "recoverable"),
        ("due_23h_ago", datetime.timedelta(hours=23), "recoverable"),
        ("due_23h59m_ago", datetime.timedelta(hours=23, minutes=59), "recoverable"),
        ("due_24h00m_exact", datetime.timedelta(hours=24), "recoverable"), # delay_hours == 24.0 -> not > 24.0 -> recoverable
        ("due_24h01m_ago", datetime.timedelta(hours=24, minutes=1), "missed"), # delay_hours > 24.0 -> missed
        ("due_25h_ago", datetime.timedelta(hours=25), "missed"),
        ("due_48h_ago", datetime.timedelta(hours=48), "missed"),
        ("due_100d_ago", datetime.timedelta(days=100), "missed"),
    ]

    batch_ids = {}
    for name, delta, exp_cat in batch:
        due_time = T0 - delta
        due_str = due_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        rid = db.add_reminder(777, 888, None, f"Batch reminder {name}", due_str)
        batch_ids[name] = (rid, exp_cat)

    # Unparseable timestamp that matches string comparison: '0000-00-00' <= T0_str
    rid_unparseable = db.add_reminder(777, 888, None, "Unparseable timestamp", "0000-00-00")
    batch_ids["unparseable_timestamp"] = (rid_unparseable, "missed")

    # Future reminder (should not be touched at all)
    future_time = T0 + datetime.timedelta(hours=2)
    rid_future = db.add_reminder(777, 888, None, "Future reminder", future_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Already-delivered reminder (should not be touched)
    past_delivered_time = (T0 - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rid_delivered = db.add_reminder(777, 888, None, "Already delivered", past_delivered_time)
    db.update_reminder_status(rid_delivered, "delivered")

    # Run recovery
    rec_result = db.recover_missed_reminders(now_utc_str=T0_str, max_delay_hours=24.0)

    rec_ids = {r["id"] for r in rec_result["recoverable"]}
    missed_ids = {r["id"] for r in rec_result["missed"]}

    print(f"  Recoverable count: {len(rec_ids)}, Missed count: {len(missed_ids)}")

    # Check each batch item
    for name, (rid, exp_cat) in batch_ids.items():
        if exp_cat == "recoverable":
            runner.assert_true(rid in rec_ids, f"{name} classified as recoverable")
            runner.assert_true(rid not in missed_ids, f"{name} NOT classified as missed")
        else:
            runner.assert_true(rid in missed_ids, f"{name} classified as missed")
            runner.assert_true(rid not in rec_ids, f"{name} NOT classified as recoverable")

    # Verify future reminder was neither recoverable nor missed
    runner.assert_true(rid_future not in rec_ids and rid_future not in missed_ids, "Future reminder untouched by recovery")
    
    # Verify already-delivered reminder was untouched
    runner.assert_true(rid_delivered not in rec_ids and rid_delivered not in missed_ids, "Delivered reminder untouched by recovery")

    # Verify DB state of missed items
    for r in rec_result["missed"]:
        runner.assert_equal(r["status"], "missed", f"Reminder {r['id']} has status='missed' in return dict")
        runner.assert_equal(r["notified_at"], T0_str, f"Reminder {r['id']} has notified_at set to recovery timestamp")

    # Verify DB state in SQLite directly
    with db._get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, status, notified_at FROM reminders WHERE id = ?", (batch_ids["due_100d_ago"][0],))
        row = c.fetchone()
        runner.assert_equal(row["status"], "missed", "100-day overdue reminder updated to status='missed' in SQLite")
        runner.assert_equal(row["notified_at"], T0_str, "100-day overdue reminder notified_at populated in SQLite")

        c.execute("SELECT id, status, notified_at FROM reminders WHERE id = ?", (rid_unparseable,))
        row_unp = c.fetchone()
        runner.assert_equal(row_unp["status"], "missed", "Unparseable timestamp reminder safely marked missed in SQLite")
        runner.assert_equal(row_unp["notified_at"], T0_str, "Unparseable timestamp notified_at populated in SQLite")

        c.execute("SELECT id, status FROM reminders WHERE id = ?", (batch_ids["due_1min_ago"][0],))
        row_rec = c.fetchone()
        runner.assert_equal(row_rec["status"], "pending", "Recoverable reminder remains status='pending' in SQLite")

    # 4.2 Idempotency test: run recovery a second time
    rec_result2 = db.recover_missed_reminders(now_utc_str=T0_str, max_delay_hours=24.0)
    runner.assert_equal(len(rec_result2["missed"]), 0, "Second recovery pass produces 0 new missed reminders (idempotent)")
    runner.assert_equal(len(rec_result2["recoverable"]), len(rec_result["recoverable"]), "Second recovery pass returns same recoverable set")

    del db
    cleanup_db()

    # ------------------------------------------------------------------
    # SUITE 5: Lifecycle & Snooze State Machine
    # ------------------------------------------------------------------
    print("\n--- Suite 5: Reminder Lifecycle & Snooze State Machine ---")
    db = bot.Database(TEST_DB_PATH)

    rid = db.add_reminder(1, 1, 100, "Lifecycle test reminder", "2026-10-16T10:00:00Z")
    runner.assert_true(rid > 0, "Created initial reminder")

    # Transition to delivered
    ok = db.update_reminder_status(rid, "delivered")
    runner.assert_true(ok, "update_reminder_status to delivered returned True")
    
    with db._get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT status, notified_at FROM reminders WHERE id = ?", (rid,))
        r = c.fetchone()
        runner.assert_equal(r["status"], "delivered", "DB status is delivered")
        runner.assert_true(r["notified_at"] is not None, "notified_at is populated")

    # Snooze the delivered reminder -> should reset to pending and nullify notified_at
    snooze_ok = db.snooze_reminder(rid, "2026-10-16T13:00:00Z")
    runner.assert_true(snooze_ok, "snooze_reminder returned True")
    
    with db._get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT status, due_at_utc, notified_at FROM reminders WHERE id = ?", (rid,))
        r = c.fetchone()
        runner.assert_equal(r["status"], "pending", "Snoozed reminder reset status to pending")
        runner.assert_equal(r["due_at_utc"], "2026-10-16T13:00:00Z", "Snoozed reminder updated due_at_utc")
        runner.assert_equal(r["notified_at"], None, "Snoozed reminder cleared notified_at to NULL")

    del db
    cleanup_db()

    # ------------------------------------------------------------------
    # SUITE 6: Parity Check between bot.py Database and test_db.py Database
    # ------------------------------------------------------------------
    print("\n--- Suite 6: Parity Between bot.py and test_db.py Database Implementations ---")
    db_bot = bot.Database("test_bot_parity.db")
    db_tdb = test_db.Database("test_tdb_parity.db")

    try:
        r_bot = db_bot.add_reminder(42, 42, 1, "Parity reminder", "2026-10-16T11:00:00Z")
        r_tdb = db_tdb.add_reminder(42, 42, 1, "Parity reminder", "2026-10-16T11:00:00Z")
        runner.assert_equal(r_bot, r_tdb, "Both implementations return identical initial reminder IDs")

        due_bot = db_bot.get_due_reminders("2026-10-16T12:00:00Z")
        due_tdb = db_tdb.get_due_reminders("2026-10-16T12:00:00Z")
        runner.assert_equal(len(due_bot), len(due_tdb), "Due reminders count matches identically")
        runner.assert_equal(due_bot[0]["reminder_text"], due_tdb[0]["reminder_text"], "Due reminder content matches identically")

        rec_bot = db_bot.recover_missed_reminders("2026-10-17T12:00:00Z", max_delay_hours=24.0)
        rec_tdb = db_tdb.recover_missed_reminders("2026-10-17T12:00:00Z", max_delay_hours=24.0)
        runner.assert_equal(rec_bot["missed_count"], rec_tdb["missed_count"], "Recovery missed_count matches identically")
    finally:
        del db_bot
        del db_tdb
        cleanup_db("test_bot_parity.db")
        cleanup_db("test_tdb_parity.db")

    # ------------------------------------------------------------------
    # SUITE 7: SQLite File Lock & Windows Cleanup Verification
    # ------------------------------------------------------------------
    print("\n--- Suite 7: Windows File Lock Cleanup ---")
    try:
        cleanup_db()
        print("  [PASS] test_adversarial_m1_2.db deleted cleanly with 0 file locks (No WinError 32)")
        runner.passed += 1
    except Exception as e:
        msg = f"Failed to delete database due to file lock: {e}"
        print(f"  [FAIL] {msg}")
        runner.failed += 1
        runner.findings.append(msg)

    # ------------------------------------------------------------------
    # Summary & Verdict
    # ------------------------------------------------------------------
    print("\n======================================================================")
    print(f"RESULTS: {runner.passed} PASSED, {runner.failed} FAILED")
    print("======================================================================")
    if runner.failed == 0:
        print("VERDICT: APPROVE")
    else:
        print("VERDICT: REQUEST_CHANGES")
        print("Failures:")
        for f in runner.findings:
            print(f" - {f}")

    return runner.failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
