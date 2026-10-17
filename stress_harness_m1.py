"""
Adversarial Stress Test Harness for Milestone 1: Database Concurrency & Windows File Locks
Empirical Challenger: M1-1
"""

import os
import sys
import time
import random
import datetime
import threading
import traceback
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set mock API key before importing bot.py to satisfy module-level client instantiation
os.environ.setdefault("GEMINI_API_KEY", "mock_gemini_key_for_stress_test")

# Ensure UTF-8 stdout
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import bot

STRESS_DB_PATH = "stress_test_m1.db"


def clean_db_files(path: str):
    """Clean up db and its WAL/SHM artifacts."""
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                print(f"[CLEANUP WARNING] Failed to remove {p}: {e}")


def test_concurrency_single_instance(thread_count: int = 50, ops_per_thread: int = 20):
    """
    Test 1: High concurrency against a single shared Database instance.
    thread_count threads simultaneously performing mixed reads, writes, updates,
    snoozes, and recoveries.
    """
    print(f"\n--- [Test 1] Concurrency Test: 1 Database Instance, {thread_count} Threads x {ops_per_thread} Ops ---")
    clean_db_files(STRESS_DB_PATH)
    db = bot.Database(STRESS_DB_PATH)

    # Initialize a test user and chat
    user_info = db.get_or_create_user("user_stress_single")
    chat_id = user_info["active_chat_id"]
    user_id = user_info["user_id"]

    errors = []
    reminders_added = []
    reminders_lock = threading.Lock()

    def worker_func(worker_id: int):
        try:
            for op_idx in range(ops_per_thread):
                op = random.choice([
                    "add_msg",
                    "add_rem",
                    "get_due",
                    "snooze_rem",
                    "update_status",
                    "get_user_rems",
                    "raw_query",
                    "recover_missed"
                ])

                now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                if op == "add_msg":
                    db.add_message(chat_id, "user", f"Worker {worker_id} op {op_idx}: {random.random()}")
                elif op == "add_rem":
                    # Random due date from -2 hours to +2 hours
                    delta = datetime.timedelta(minutes=random.randint(-120, 120))
                    due_str = (datetime.datetime.now(datetime.timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
                    r_id = db.add_reminder(user_id, chat_id, 1000 + op_idx, f"Rem from {worker_id}-{op_idx}", due_str)
                    with reminders_lock:
                        reminders_added.append(r_id)
                elif op == "get_due":
                    db.get_due_reminders(now_utc_str, limit=20)
                elif op == "snooze_rem":
                    with reminders_lock:
                        target_id = random.choice(reminders_added) if reminders_added else None
                    if target_id:
                        new_due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        db.snooze_reminder(target_id, new_due)
                elif op == "update_status":
                    with reminders_lock:
                        target_id = random.choice(reminders_added) if reminders_added else None
                    if target_id:
                        db.update_reminder_status(target_id, random.choice(["delivered", "missed", "cancelled"]))
                elif op == "get_user_rems":
                    db.get_user_reminders(user_id, status="all", limit=25)
                elif op == "raw_query":
                    db.execute("SELECT COUNT(*) as c FROM reminders WHERE user_id = ?", (user_id,))
                elif op == "recover_missed":
                    db.recover_missed_reminders(now_utc_str, max_delay_hours=1.0)

        except Exception as e:
            tb = traceback.format_exc()
            errors.append((worker_id, e, tb))

    start_time = time.perf_counter()
    threads = [threading.Thread(target=worker_func, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration = time.perf_counter() - start_time

    total_ops = thread_count * ops_per_thread
    print(f"Executed {total_ops} operations across {thread_count} threads in {duration:.2f}s ({total_ops/duration:.1f} ops/sec)")

    if errors:
        print(f"FAILED: Encountered {len(errors)} errors during single-instance concurrency test:")
        for w_id, err, tb in errors[:5]:
            print(f"Worker {w_id}: {type(err).__name__}: {err}\n{tb}")
        assert False, f"Encountered {len(errors)} concurrency errors"
    else:
        print("PASSED: Zero concurrency errors or database locks observed.")

    # Drop db reference and test Windows file removal immediately
    del db
    print("Testing immediate Windows file removal for single instance...")
    time.sleep(0.05)
    for ext in ["", "-wal", "-shm"]:
        p = STRESS_DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)
    assert not os.path.exists(STRESS_DB_PATH), "STRESS_DB_PATH still exists after removal!"
    print("PASSED: Database files removed cleanly with zero WinError 32.")


def test_concurrency_multi_instance(thread_count: int = 30, ops_per_thread: int = 15):
    """
    Test 2: High concurrency with EACH thread creating its own bot.Database instance
    pointing to the same SQLite database file.
    Tests independent connection pool lifecycle and inter-instance locking.
    """
    print(f"\n--- [Test 2] Multi-Instance Concurrency: {thread_count} Threads x {ops_per_thread} Ops (Each creates Database instance) ---")
    clean_db_files(STRESS_DB_PATH)

    # Initialize master
    init_db = bot.Database(STRESS_DB_PATH)
    init_db.get_or_create_user("user_multi")
    del init_db

    errors = []
    def multi_instance_worker(worker_id: int):
        try:
            # Instantiate thread-local Database
            local_db = bot.Database(STRESS_DB_PATH)
            user_info = local_db.get_or_create_user(f"user_thread_{worker_id}")
            chat_id = user_info["active_chat_id"]
            user_id = user_info["user_id"]

            for op_idx in range(ops_per_thread):
                local_db.add_message(chat_id, "user", f"Multi msg from {worker_id}-{op_idx}")
                r_id = local_db.add_reminder(
                    user_id, chat_id, op_idx, f"Rem from multi {worker_id}-{op_idx}",
                    datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
                local_db.get_due_reminders()
                local_db.snooze_reminder(r_id, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                local_db.get_user_chats(user_id)
        except Exception as e:
            tb = traceback.format_exc()
            errors.append((worker_id, e, tb))

    start_time = time.perf_counter()
    threads = [threading.Thread(target=multi_instance_worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration = time.perf_counter() - start_time

    total_ops = thread_count * ops_per_thread * 5
    print(f"Executed {total_ops} operations across {thread_count} distinct Database instances in {duration:.2f}s ({total_ops/duration:.1f} ops/sec)")

    if errors:
        print(f"FAILED: Encountered {len(errors)} errors in multi-instance test:")
        for w_id, err, tb in errors[:5]:
            print(f"Worker {w_id}: {type(err).__name__}: {err}\n{tb}")
        assert False, f"Encountered {len(errors)} multi-instance errors"
    else:
        print("PASSED: Zero multi-instance concurrency errors or locks observed.")

    # Test Windows file removal
    time.sleep(0.05)
    for ext in ["", "-wal", "-shm"]:
        p = STRESS_DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)
    assert not os.path.exists(STRESS_DB_PATH), "STRESS_DB_PATH still exists after multi-instance removal!"
    print("PASSED: Multi-instance database files removed cleanly with zero WinError 32.")


def test_exception_rollback_and_lock_release():
    """
    Test 3: Verify that when queries fail or raise exceptions, rollback occurs
    and conn.close() in finally block ALWAYS runs, preventing lock leaks on Windows.
    """
    print("\n--- [Test 3] Exception Rollback & Lock Release Integrity ---")
    clean_db_files(STRESS_DB_PATH)
    db = bot.Database(STRESS_DB_PATH)

    # 1. Deliberate syntax error in execute()
    caught_syntax_err = False
    try:
        db.execute("THIS IS COMPLETELY INVALID SQL STATEMENT;")
    except Exception as e:
        caught_syntax_err = True
        print(f"Expected syntax exception caught cleanly: {e}")
    assert caught_syntax_err, "Database.execute did not raise syntax error!"

    # 2. Deliberate constraint violation
    # Inserting duplicate into users table where user_id is PRIMARY KEY
    db.get_or_create_user("user_unique_1")
    caught_integrity_err = False
    try:
        with db._get_connection() as conn:
            conn.execute("INSERT INTO users (user_id) VALUES (?)", ("user_unique_1",))
    except sqlite3.IntegrityError as e:
        caught_integrity_err = True
        print(f"Expected IntegrityError caught cleanly: {e}")
    assert caught_integrity_err, "Duplicate PRIMARY KEY did not raise IntegrityError!"

    # 3. Immediately after exceptions, verify subsequent operations succeed without lock
    user = db.get_or_create_user("user_after_error")
    assert user["user_id"] == "user_after_error"
    print("Subsequent database operation succeeded immediately after handled exceptions.")

    # 4. Immediate file deletion check
    del db
    time.sleep(0.05)
    for ext in ["", "-wal", "-shm"]:
        p = STRESS_DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)
    assert not os.path.exists(STRESS_DB_PATH)
    print("PASSED: Exception rollback and lock release verified OK.")


def test_data_integrity_accounting(thread_count: int = 20, items_per_thread: int = 25):
    """
    Test 4: Strict data accounting test.
    thread_count threads concurrently insert exact count of messages and reminders.
    Verify total counts match exactly (no lost updates or dropped writes).
    """
    print(f"\n--- [Test 4] Data Integrity & Accounting: {thread_count} Threads x {items_per_thread} Writes ---")
    clean_db_files(STRESS_DB_PATH)
    db = bot.Database(STRESS_DB_PATH)

    user = db.get_or_create_user("accounting_user")
    chat_id = user["active_chat_id"]

    def write_batch(tid: int):
        for i in range(items_per_thread):
            db.add_message(chat_id, "user", f"msg_tid_{tid}_idx_{i}")
            db.add_reminder("accounting_user", chat_id, i, f"rem_tid_{tid}_idx_{i}", "2026-12-31T23:59:59Z")

    threads = [threading.Thread(target=write_batch, args=(t,)) for t in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Query counts
    msg_res = db.execute("SELECT COUNT(*) as cnt FROM messages WHERE chat_id = ?", (chat_id,))
    total_msgs = msg_res[0]["cnt"]

    rem_res = db.execute("SELECT COUNT(*) as cnt FROM reminders WHERE user_id = ?", ("accounting_user",))
    total_rems = rem_res[0]["cnt"]

    expected_total = thread_count * items_per_thread
    print(f"Messages count: {total_msgs} (expected: {expected_total})")
    print(f"Reminders count: {total_rems} (expected: {expected_total})")

    assert total_msgs == expected_total, f"Message count mismatch: {total_msgs} != {expected_total}"
    assert total_rems == expected_total, f"Reminder count mismatch: {total_rems} != {expected_total}"
    print("PASSED: 100% data accounting accuracy achieved under heavy concurrency.")

    del db
    clean_db_files(STRESS_DB_PATH)


def test_concurrency_heavy_100_threads(thread_count: int = 100, ops_per_thread: int = 20):
    """
    Test 5: Extreme heavy stress: 100 simultaneous threads performing 2,000 transactions.
    Verifies that WAL mode + busy_timeout=5000ms handles extreme contention without starvation.
    """
    print(f"\n--- [Test 5] Extreme Concurrency: {thread_count} Threads x {ops_per_thread} Ops (2,000 Total Transactions) ---")
    clean_db_files(STRESS_DB_PATH)
    db = bot.Database(STRESS_DB_PATH)
    user = db.get_or_create_user("heavy_stress_user")
    chat_id = user["active_chat_id"]

    errors = []
    rem_ids = []
    lock = threading.Lock()

    def heavy_worker(worker_id: int):
        try:
            for i in range(ops_per_thread):
                action = i % 4
                if action == 0:
                    db.add_message(chat_id, "user", f"Heavy msg {worker_id}-{i}")
                elif action == 1:
                    rid = db.add_reminder("heavy_stress_user", chat_id, i, f"Heavy rem {worker_id}-{i}", "2026-10-16T12:00:00Z")
                    with lock:
                        rem_ids.append(rid)
                elif action == 2:
                    with lock:
                        target = rem_ids[-1] if rem_ids else None
                    if target:
                        db.update_reminder_status(target, "delivered")
                elif action == 3:
                    db.get_due_reminders("2026-10-16T13:00:00Z", limit=10)
        except Exception as e:
            errors.append((worker_id, e, traceback.format_exc()))

    start_time = time.perf_counter()
    threads = [threading.Thread(target=heavy_worker, args=(t,)) for t in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration = time.perf_counter() - start_time

    total_ops = thread_count * ops_per_thread
    print(f"Executed {total_ops} operations across {thread_count} threads in {duration:.2f}s ({total_ops/duration:.1f} ops/sec)")

    if errors:
        print(f"FAILED: Encountered {len(errors)} errors during 100-thread concurrency test:")
        for w_id, err, tb in errors[:5]:
            print(f"Worker {w_id}: {type(err).__name__}: {err}\n{tb}")
        assert False, f"Encountered {len(errors)} errors"
    else:
        print("PASSED: Zero database locks or errors under 100 concurrent threads.")

    del db
    clean_db_files(STRESS_DB_PATH)


def main():
    print("================================================================")
    print("EMPIRICAL STRESS TEST SUITE FOR M1: DATABASE & FILE LOCKS")
    print("Testing bot.py Database implementation")
    print("================================================================")

    test_concurrency_single_instance(thread_count=50, ops_per_thread=20)
    test_concurrency_multi_instance(thread_count=30, ops_per_thread=15)
    test_exception_rollback_and_lock_release()
    test_data_integrity_accounting(thread_count=20, items_per_thread=25)
    test_concurrency_heavy_100_threads(thread_count=100, ops_per_thread=20)

    print("\n================================================================")
    print("ALL 5 EMPIRICAL ADVERSARIAL STRESS SUITES PASSED 100%!")
    print("Zero SQLite database locks. Zero Windows file lock leaks.")
    print("================================================================")


if __name__ == "__main__":
    main()
