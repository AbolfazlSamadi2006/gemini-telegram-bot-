import os
import sys
import sqlite3
import datetime
import contextlib
import threading
from typing import List, Tuple, Optional, Dict

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class Database:
    def __init__(self, db_path: str = "bot_database.db"):
        self.db_path = db_path
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
                LIMIT 20
                """,
                (user_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_chat(self, user_id: str) -> Optional[Dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.* FROM chats c
                JOIN users u ON c.id = u.active_chat_id
                WHERE u.user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

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
            cursor.execute("UPDATE chats SET title = ? WHERE id = ?", (new_title[:50], chat_id))
            conn.commit()

    def delete_chat(self, user_id: str, chat_id: int) -> Optional[int]:
        """Deletes chat and switches to another active chat if this one was active."""
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
            return None

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
            user_msg_cnt = cursor.fetchone()["cnt"]
            if user_msg_cnt == 1 and role == 'user':
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
        if notified_at is None and status in ("delivered", "missed", "cancelled"):
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
                WHERE status = 'pending' AND due_at_utc <= ?
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


# Unit tests to verify 100% functionality
if __name__ == "__main__":
    test_db = "test_bot.db"
    for extra in ["", "-wal", "-shm"]:
        p = test_db + extra
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    db = Database(test_db)
    user_id = "123456"

    # 1. User creation
    u = db.get_or_create_user(user_id)
    assert u["user_id"] == user_id
    assert u["active_chat_id"] is not None
    chat1_id = u["active_chat_id"]
    print("Test 1: User & initial chat created OK. Chat ID:", chat1_id)

    # 2. Add message & auto-title
    db.add_message(chat1_id, "user", "چجوری در پایتون کد بنویسیم؟")
    db.add_message(chat1_id, "model", "پایتون خیلی ساده و قدرتمنده!")
    active_chat = db.get_active_chat(user_id)
    print("Test 2: Auto title generated:", active_chat["title"])
    assert "پایتون" in active_chat["title"]

    # 3. Create second chat
    chat2_id = db.create_new_chat(user_id, "چت بدنسازی")
    u = db.get_or_create_user(user_id)
    assert u["active_chat_id"] == chat2_id
    db.add_message(chat2_id, "user", "برنامه تمرینی برای ۴ روز در هفته بده")
    db.add_message(chat2_id, "model", "روز اول سینه و پشت بازو...")
    print("Test 3: Second chat created OK. Active is Chat 2:", chat2_id)

    # 4. List chats
    chats = db.get_user_chats(user_id)
    assert len(chats) == 2
    print("Test 4: User chats listed OK:")
    for c in chats:
        print(f" - [{c['id']}] {c['title']} ({c['msg_count']} msgs)")

    # 5. Switch back to chat 1
    switched = db.switch_active_chat(user_id, chat1_id)
    assert switched is True
    active_now = db.get_active_chat(user_id)
    assert active_now["id"] == chat1_id
    print("Test 5: Switch back to chat 1 successful. Active title:", active_now["title"])

    # 6. Verify history of chat 1 is intact
    msgs = db.get_chat_messages(chat1_id)
    assert len(msgs) == 2
    assert msgs[0][0] == "user"
    assert msgs[1][0] == "model"
    print("Test 6: Chat history retrieved accurately:", len(msgs), "messages.")

    # 7. Delete chat 1 and auto-switch
    db.delete_chat(user_id, chat1_id)
    chats_after = db.get_user_chats(user_id)
    assert len(chats_after) == 1
    active_after_del = db.get_active_chat(user_id)
    assert active_after_del["id"] == chat2_id
    print("Test 7: Deletion and fallback auto-switch to Chat 2 verified OK.")

    # ================= REMINDER TESTS =================
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    future_str = (now_utc + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    due_recent_str = (now_utc - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    due_yesterday_str = (now_utc - datetime.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 8. Add reminders
    r_future_id = db.add_reminder(user_id=user_id, chat_id=999, message_id=101, reminder_text="یادآوری آینده", due_at_utc=future_str)
    r_due_id = db.add_reminder(user_id=user_id, chat_id=999, message_id=102, reminder_text="یادآوری موعد رسیده", due_at_utc=due_recent_str)
    r_stale_id = db.add_reminder(user_id=user_id, chat_id=999, message_id=103, reminder_text="یادآوری ۳۰ ساعت قبل", due_at_utc=due_yesterday_str)
    assert r_future_id > 0 and r_due_id > 0 and r_stale_id > 0
    print(f"Test 8: Reminders added OK. IDs: {r_future_id}, {r_due_id}, {r_stale_id}")

    # 9. Get due reminders
    due_reminders = db.get_due_reminders(now_str)
    due_ids = [r["id"] for r in due_reminders]
    assert r_future_id not in due_ids, "Future reminder should not be due"
    assert r_due_id in due_ids, "Recent overdue reminder must be due"
    assert r_stale_id in due_ids, "Stale overdue reminder must be due"
    assert due_reminders[0]["due_at_utc"] <= due_reminders[1]["due_at_utc"]
    print("Test 9: Due reminders filtered and sorted chronologically OK.")

    # 10. Update reminder status
    upd_ok = db.update_reminder_status(r_due_id, "delivered")
    assert upd_ok is True
    due_after_update = db.get_due_reminders(now_str)
    assert r_due_id not in [r["id"] for r in due_after_update]
    print("Test 10: Reminder status updated to 'delivered' and removed from due queue OK.")

    # 11. Snooze reminder
    snooze_str = (now_utc + datetime.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snz_ok = db.snooze_reminder(r_due_id, snooze_str)
    assert snz_ok is True
    rem_rows = db.get_user_reminders(user_id, status="pending")
    snoozed_item = next(r for r in rem_rows if r["id"] == r_due_id)
    assert snoozed_item["status"] == "pending"
    assert snoozed_item["due_at_utc"] == snooze_str
    assert snoozed_item["notified_at"] is None
    print("Test 11: Reminder snoozed to new UTC time and reset to pending OK.")

    # 12. List user reminders
    pending_list = db.get_user_reminders(user_id, status="pending")
    assert len(pending_list) >= 2
    all_list = db.get_user_reminders(user_id, status="all")
    assert len(all_list) >= 3
    print(f"Test 12: User reminders listed OK: {len(pending_list)} pending, {len(all_list)} total.")

    # 13. Missed reminders recovery (>24h marked 'missed', <=24h recoverable)
    r_recov_id = db.add_reminder(user_id=user_id, chat_id=999, message_id=104, reminder_text="۵ ساعت قبل", due_at_utc=(now_utc - datetime.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    rec_result = db.recover_missed_reminders(now_str, max_delay_hours=24.0)
    assert rec_result["missed_count"] >= 1
    missed_ids = [m["id"] for m in rec_result["missed"]]
    assert r_stale_id in missed_ids, "30-hour overdue reminder must be marked missed"
    recoverable_ids = [rc["id"] for rc in rec_result["recoverable"]]
    assert r_recov_id in recoverable_ids, "5-hour overdue reminder must be recoverable"
    print(f"Test 13: Recovery logic verified OK. {len(rec_result['missed'])} marked missed, {len(rec_result['recoverable'])} recoverable.")

    # 14. Execute helper method
    res = db.execute("SELECT COUNT(*) as c FROM reminders")
    assert res[0]["c"] >= 4
    print("Test 14: Database.execute helper returned:", res[0]["c"], "reminders.")

    # 15. Concurrency & Connection Closing Stress Test
    errors = []
    def concurrent_worker(worker_id):
        try:
            for i in range(10):
                db.add_message(chat2_id, "user", f"پیام همزمانی {worker_id}-{i}")
                db.get_user_chats(user_id)
                db.get_due_reminders(now_str)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=concurrent_worker, args=(t,)) for t in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 0, f"Concurrent workers encountered errors: {errors}"
    print("Test 15: Concurrency stress test (5 threads x 10 cycles) completed with ZERO errors.")

    # 16. OS File Lock Release & Clean Database Deletion (No WinError 32)
    for extra in ["", "-wal", "-shm"]:
        p = test_db + extra
        if os.path.exists(p):
            os.remove(p)
    assert not os.path.exists(test_db)
    print("Test 16: test_bot.db deleted cleanly with 0 file locks (No WinError 32)!")

    print("\n==========================================")
    print("ALL 16 TESTS PASSED FLAWLESSLY 100%!")
    print("==========================================")
