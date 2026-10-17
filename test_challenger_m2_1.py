"""
Adversarial Stress Test Suite for Milestone 2 (R1 & R6)
Empirical Challenger: M2-1
Coverage:
- R1: Python Code Execution (Scientific Notation, Multiline Traces, Syntax Errors, Unicode, Triple Backticks, Multi-parts)
- R6: Transparent Reasoning (Massive 10k+ Thought Chains, Database Integrity, Spoiler Display, Chunking/Truncation Behavior, Markdown Collisions)
- Concurrency: Simultaneous Thought Logging and Code Execution Handling
"""

import os
import sys
import io
import re
import uuid
import asyncio
import threading
import datetime
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure UTF-8 stdout
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure offline environment variables
os.environ.setdefault("GEMINI_API_KEY", "mock_key_challenger_m2")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:mock_token_challenger_m2")

import bot
from bot import (
    Database,
    ChatResponse,
    format_response_with_code_execution,
    parse_gemini_candidate_parts,
    build_gemini_session,
    generate_chat_response,
    callback_handler,
    handle_message,
)
from google.genai import types
from telegram import Update, Message, User, Chat, CallbackQuery, InlineKeyboardMarkup


class TestMilestone2AdversarialStress(unittest.TestCase):
    def setUp(self):
        self.test_db_path = f"stress_m2_test_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.test_db_path)
        bot.db = self.db
        self.user_id = "99887766"
        self.db.get_or_create_user(self.user_id)
        self.active_chat = self.db.get_active_chat(self.user_id)

    def tearDown(self):
        for ext in ["", "-wal", "-shm"]:
            p = self.test_db_path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # =========================================================================
    # Category 1: R1 Code Execution Parsing — Scientific Notation & Math Edge Cases
    # =========================================================================

    def test_r1_scientific_notation_small_floats(self):
        """Verifies parsing and badge formatting of extremely small scientific notation floats (e.g. 1.234567e-15)."""
        code = "import scipy.constants\nprint(1.234567e-15)"
        output = "1.234567e-15\n"
        clean = "مقدار ثابت بسیار کوچک محاسبه شد."

        p_code = MagicMock(thought=False, executable_code=MagicMock(code=code), code_execution_result=None, text=None)
        p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=output, outcome="OUTCOME_OK"), text=None)
        p_txt = MagicMock(thought=False, executable_code=None, code_execution_result=None, text=clean)

        mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_code, p_res, p_txt]))], text=clean)

        parsed = parse_gemini_candidate_parts(mock_resp)
        self.assertEqual(parsed.clean_text, clean)
        self.assertEqual(parsed.executed_code, code)
        self.assertEqual(parsed.execution_result, "1.234567e-15")

        formatted = parsed.formatted_text
        self.assertIn("1.234567e-15", formatted)
        self.assertIn("💻 **کد پایتون اجرا شده:**", formatted)
        self.assertIn("📊 **نتیجه تایید شده اجرا:**", formatted)

    def test_r1_scientific_notation_large_floats_and_special_numbers(self):
        """Verifies parsing of massive floats (9.87654321e+50), negative exponents (-3.14e-20), inf, -inf, nan."""
        test_cases = [
            ("9.87654321e+50", "9.87654321e+50"),
            ("-3.1415926535e-20", "-3.1415926535e-20"),
            ("inf", "inf"),
            ("-inf", "-inf"),
            ("nan", "nan"),
            ("(3.5+4.2j)", "(3.5+4.2j)"),
        ]

        for raw_out, expected_res in test_cases:
            p_code = MagicMock(thought=False, executable_code=MagicMock(code=f"print({raw_out})"), code_execution_result=None, text=None)
            p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=raw_out + "\n"), text=None)
            mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_code, p_res]))], text="نتیجه محاسبه")

            parsed = parse_gemini_candidate_parts(mock_resp)
            self.assertEqual(parsed.execution_result, expected_res)
            self.assertIn(expected_res, parsed.formatted_text)

    # =========================================================================
    # Category 2: R1 Code Execution Parsing — Multiline Traces & Syntax Errors
    # =========================================================================

    def test_r1_multiline_python_traceback(self):
        """Verifies proper capture and formatting of complex multiline Python stack tracebacks."""
        traceback_output = (
            "Traceback (most recent call last):\n"
            "  File \"<stdin>\", line 5, in <module>\n"
            "  File \"<stdin>\", line 3, in recursive_factorial\n"
            "  File \"<stdin>\", line 3, in recursive_factorial\n"
            "RecursionError: maximum recursion depth exceeded in comparison"
        )
        code = "def recursive_factorial(n):\n    return n * recursive_factorial(n - 1)\nrecursive_factorial(10000)"

        p_code = MagicMock(thought=False, executable_code=MagicMock(code=code), code_execution_result=None, text=None)
        p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=traceback_output, outcome="OUTCOME_FAILED"), text=None)
        p_txt = MagicMock(thought=False, executable_code=None, code_execution_result=None, text="خطای حداکثر عمق بازگشت رخ داد.")

        mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_code, p_res, p_txt]))], text="خطای حداکثر عمق بازگشت رخ داد.")

        parsed = parse_gemini_candidate_parts(mock_resp)
        self.assertEqual(parsed.clean_text, "خطای حداکثر عمق بازگشت رخ داد.")
        self.assertEqual(parsed.executed_code, code)
        self.assertEqual(parsed.execution_result, traceback_output)

        formatted = parsed.formatted_text
        self.assertIn("RecursionError", formatted)
        self.assertIn("Traceback (most recent call last):", formatted)
        self.assertIn("```\n" + traceback_output + "\n```", formatted)

    def test_r1_syntax_error_with_caret(self):
        """Verifies syntax error formatting containing caret positioning characters (^) and quotes."""
        syntax_err = (
            "  File \"<stdin>\", line 1\n"
            "    def broken_func(\n"
            "                   ^\n"
            "SyntaxError: unexpected EOF while parsing"
        )
        code = "def broken_func("

        p_code = MagicMock(thought=False, executable_code=MagicMock(code=code), code_execution_result=None, text=None)
        p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=syntax_err), text=None)

        mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_code, p_res]))], text="")
        parsed = parse_gemini_candidate_parts(mock_resp)
        # Note: bot.py line 761 calls .strip() on execution_results, which strips leading indentation on first line
        self.assertEqual(parsed.execution_result, syntax_err.strip())
        self.assertIn(syntax_err.strip(), parsed.formatted_text)

    # =========================================================================
    # Category 3: R1 Code Execution Parsing — Unicode, Emojis & Markdown Collisions
    # =========================================================================

    def test_r1_unicode_and_persian_stdout(self):
        """Verifies bidirectional Persian text, emojis, and special symbols in code stdout."""
        persian_output = "✨ محاسبه کامل شد: ریشه دوم ۶۲۵ برابر است با ۲۵.۰! 🚀\nداده‌ها: [۱۰, ۲۰, ۳۰]"
        code = "# تست یونیکد\nprint('✨ محاسبه کامل شد: ریشه دوم ۶۲۵ برابر است با ۲۵.۰! 🚀')\nprint('داده‌ها: [۱۰, ۲۰, ۳۰]')"

        p_code = MagicMock(thought=False, executable_code=MagicMock(code=code), code_execution_result=None, text=None)
        p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=persian_output), text=None)

        mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_code, p_res]))], text="پاسخ نهایی")
        parsed = parse_gemini_candidate_parts(mock_resp)

        self.assertIn("✨", parsed.execution_result)
        self.assertIn("ریشه دوم ۶۲۵", parsed.execution_result)
        self.assertIn("🚀", parsed.formatted_text)

    def test_r1_triple_backticks_in_code_or_output(self):
        """Stress-tests code or output containing triple backticks (```) which would break markdown fencing."""
        code_with_backticks = 's = "```python\\nprint(1)\\n```"\nprint(s)'
        output_with_backticks = '```python\nprint(1)\n```'

        formatted = format_response_with_code_execution("پاسخ تست", code_with_backticks, output_with_backticks)
        self.assertIn("💻 **کد پایتون اجرا شده:**", formatted)
        self.assertIn("📊 **نتیجه تایید شده اجرا:**", formatted)
        # Verify the content is preserved
        self.assertIn(code_with_backticks.strip(), formatted)
        self.assertIn(output_with_backticks.strip(), formatted)

    def test_r1_multiple_code_executions_in_single_candidate(self):
        """Verifies candidate with multiple code snippets and execution results (multi-step solver)."""
        p_c1 = MagicMock(thought=False, executable_code=MagicMock(code="x = 10 / 0"), code_execution_result=None, text=None)
        p_r1 = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output="ZeroDivisionError: division by zero"), text=None)
        p_c2 = MagicMock(thought=False, executable_code=MagicMock(code="x = 10 / 2\nprint(x)"), code_execution_result=None, text=None)
        p_r2 = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output="5.0"), text=None)
        p_ans = MagicMock(thought=False, executable_code=None, code_execution_result=None, text="پاسخ اصلاح شده ۵.۰ است.")

        mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_c1, p_r1, p_c2, p_r2, p_ans]))], text="پاسخ اصلاح شده ۵.۰ است.")
        parsed = parse_gemini_candidate_parts(mock_resp)

        self.assertIn("10 / 0", parsed.executed_code)
        self.assertIn("10 / 2", parsed.executed_code)
        self.assertIn("ZeroDivisionError", parsed.execution_result)
        self.assertIn("5.0", parsed.execution_result)
        self.assertEqual(parsed.clean_text, "پاسخ اصلاح شده ۵.۰ است.")

    def test_r1_empty_and_corrupted_candidate_parts(self):
        """Verifies boundary cases: None response, empty candidate parts, None code/output values."""
        # 1. None response
        p0 = parse_gemini_candidate_parts(None)
        self.assertEqual(p0.clean_text, "")
        self.assertIsNone(p0.executed_code)

        # 2. Empty candidates list
        p1 = parse_gemini_candidate_parts(MagicMock(candidates=[], text=""))
        self.assertEqual(p1.clean_text, "")

        # 3. None content or empty parts
        p2 = parse_gemini_candidate_parts(MagicMock(candidates=[MagicMock(content=None)], text=""))
        self.assertEqual(p2.clean_text, "")

        # 4. executable_code with code=None
        part_bad = MagicMock(thought=False, executable_code=MagicMock(code=None), code_execution_result=None, text=None)
        p3 = parse_gemini_candidate_parts(MagicMock(candidates=[MagicMock(content=MagicMock(parts=[part_bad]))], text=""))
        self.assertIsNone(p3.executed_code)

        # 5. code_execution_result with output=None
        part_bad_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=None), text=None)
        p4 = parse_gemini_candidate_parts(MagicMock(candidates=[MagicMock(content=MagicMock(parts=[part_bad_res]))], text=""))
        self.assertIsNone(p4.execution_result)

    # =========================================================================
    # Category 4: R6 Massive Thought Chains (10,000+ characters) & Database Storage
    # =========================================================================

    def test_r6_massive_10k_thought_database_integrity(self):
        """Verifies that a 10,000+ character thought chain persists in SQLite without loss or corruption."""
        # Construct 12,000-character thought chain with Persian, code, and markdown
        thought_segments = [
            f"مرحله {i}: تحلیل فرضیات هندسی مسئله و اثبات قضیه شماره {i}. "
            f"متغیر x برابر با {i * 1.5:.2f} است و باید با تابع f(x) ترکیب شود.\n"
            for i in range(1, 150)
        ]
        massive_thought = "".join(thought_segments)
        self.assertGreater(len(massive_thought), 10000)

        thought_id = "test_10k_" + uuid.uuid4().hex[:8]
        self.db.save_thought(thought_id, self.user_id, self.active_chat["id"], massive_thought)

        retrieved = self.db.get_thought(thought_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(len(retrieved), len(massive_thought))
        self.assertEqual(retrieved, massive_thought)

    def test_r6_massive_50k_thought_database_integrity(self):
        """Verifies extreme 50,000+ character thought storage in SQLite reasoning_logs table."""
        extreme_thought = ("🧠 تفکر عمیق گام به گام مدل: " + "A" * 100 + "\n") * 450
        self.assertGreater(len(extreme_thought), 50000)

        t_id = "test_50k_" + uuid.uuid4().hex[:8]
        self.db.save_thought(t_id, self.user_id, self.active_chat["id"], extreme_thought)

        retrieved = self.db.get_thought(t_id)
        self.assertEqual(retrieved, extreme_thought)

    def test_r6_thought_with_sql_injection_payload(self):
        """Verifies resilience against SQL injection attempts inside thought content and IDs."""
        injection_id = "id_'; DROP TABLE reasoning_logs;--"
        injection_content = "Normal thought content'); DELETE FROM users;--"

        self.db.save_thought(injection_id, self.user_id, self.active_chat["id"], injection_content)
        retrieved = self.db.get_thought(injection_id)
        self.assertEqual(retrieved, injection_content)

        # Verify table still exists and is intact
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM reasoning_logs")
            count = cursor.fetchone()[0]
            self.assertGreaterEqual(count, 1)

    # =========================================================================
    # Category 5: R6 Callback Handler & Thought Truncation / Chunking Analysis
    # =========================================================================

    def test_r6_callback_handler_massive_thought_delivery_behavior(self):
        """
        Adversarial test inspecting the delivery behavior of 10,000+ character thoughts via callback_handler.
        Verifies that massive thoughts are delivered without truncation across sequential spoiler messages.
        """
        massive_thought = "🧠 گام تفکر: " + ("این یک متن استدلال طولانی برای آزمایش است. " * 300)
        self.assertGreater(len(massive_thought), 10000)

        t_id = "thought_stress_10k"
        self.db.save_thought(t_id, self.user_id, self.active_chat["id"], massive_thought)

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = int(self.user_id)
        mock_query.data = f"show_thought:{t_id}"
        mock_query.answer = AsyncMock()
        mock_query.message.reply_text = AsyncMock()
        mock_update.callback_query = mock_query

        asyncio.run(callback_handler(mock_update, MagicMock()))

        mock_query.answer.assert_called_once()

        # Verify that multiple sequential messages were delivered
        call_count = mock_query.message.reply_text.call_count
        expected_chunks = (len(massive_thought) + 3499) // 3500
        self.assertEqual(call_count, expected_chunks, f"Expected {expected_chunks} chunks for length {len(massive_thought)}")

        delivered_chunks = []
        for idx, call in enumerate(mock_query.message.reply_text.call_args_list):
            sent_msg = call[0][0]
            parse_mode = call[1].get("parse_mode")
            self.assertEqual(parse_mode, "Markdown", "Chunk should be formatted with Markdown")
            self.assertIn("فرآیند و زنجیره تفکر", sent_msg)
            self.assertIn(f"بخش {idx + 1} از {expected_chunks}", sent_msg)
            self.assertLessEqual(len(sent_msg), 4096, "Sent message must not exceed Telegram 4096 character limit")

            # Extract chunk content from spoiler ||...||
            pattern = rf"^🧠 \*\*فرآیند و زنجیره تفکر و استدلال جمینای \(بخش {idx + 1} از {expected_chunks}\):\*\*\n\n\|\|([\s\S]*)\|\|$"
            m = re.match(pattern, sent_msg)
            self.assertIsNotNone(m, f"Message {idx+1} format did not match expected spoiler wrapper: {sent_msg[:100]}...")
            delivered_chunks.append(m.group(1))

        # Reconstructed full thought must match original byte-for-byte with 100% fidelity
        reconstructed = "".join(delivered_chunks)
        print(f"\n[EMPIRICAL AUDIT] Massive thought original length: {len(massive_thought)} chars")
        print(f"[EMPIRICAL AUDIT] Total delivered length across {call_count} chunks: {len(reconstructed)} chars (100% delivered, zero truncation)")

        self.assertEqual(len(reconstructed), len(massive_thought))
        self.assertEqual(reconstructed, massive_thought)

    def test_r6_callback_handler_markdown_spoiler_error_fallback(self):
        """Verifies that if thought contains broken spoiler tags or invalid markdown, fallback sends plain text."""
        # Unmatched spoiler and markdown formatting that causes Telegram Markdown parser to fail
        broken_markdown_thought = "Check || unmatched spoiler *unclosed bold `unclosed code"
        t_id = "broken_md_" + uuid.uuid4().hex[:8]
        self.db.save_thought(t_id, self.user_id, self.active_chat["id"], broken_markdown_thought)

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = int(self.user_id)
        mock_query.data = f"show_thought:{t_id}"
        mock_query.answer = AsyncMock()

        # Simulate Telegram API rejecting the first reply_text with BadRequest: Can't parse entities
        async def reply_side_effect(*args, **kwargs):
            if kwargs.get("parse_mode") == "Markdown":
                raise Exception("BadRequest: Can't find end of spoiler entity")
            return MagicMock()

        mock_query.message.reply_text = AsyncMock(side_effect=reply_side_effect)
        mock_update.callback_query = mock_query

        asyncio.run(callback_handler(mock_update, MagicMock()))

        # Verify fallback was invoked
        self.assertEqual(mock_query.message.reply_text.call_count, 2)
        # Second call does not have parse_mode
        second_call_kwargs = mock_query.message.reply_text.call_args_list[1][1]
        self.assertNotIn("parse_mode", second_call_kwargs)

    # =========================================================================
    # Category 6: High-Concurrency Stress Workload
    # =========================================================================

    def test_concurrency_realistic_multi_user_workload(self):
        """
        Stress-tests 20 concurrent threads simultaneously saving/reading 10k thoughts
        and parsing candidate code executions with realistic multi-user pacing (5ms jitter).
        Verifies zero SQLite database locking collisions or data corruption under normal WAL load.
        """
        import time
        num_threads = 20
        ops_per_thread = 5
        errors = []

        def worker(worker_idx: int):
            try:
                for op in range(ops_per_thread):
                    t_id = f"concur_{worker_idx}_{op}_{uuid.uuid4().hex[:6]}"
                    thought_data = f"Worker {worker_idx} Op {op}: " + ("استدلال همزمان تستی " * 200)

                    # 1. Save thought
                    self.db.save_thought(t_id, str(worker_idx), 1000 + worker_idx, thought_data)

                    # 2. Retrieve thought
                    fetched = self.db.get_thought(t_id)
                    if fetched != thought_data:
                        errors.append(f"Data mismatch in worker {worker_idx} op {op}")

                    # 3. Simulate R1 candidate parsing concurrently
                    p_code = MagicMock(thought=False, executable_code=MagicMock(code=f"print({worker_idx} * {op})"), code_execution_result=None, text=None)
                    p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output=f"{worker_idx * op}"), text=None)
                    mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_code, p_res]))], text="ok")
                    parsed = parse_gemini_candidate_parts(mock_resp)
                    if parsed.execution_result != str(worker_idx * op):
                        errors.append(f"Candidate parsing mismatch in worker {worker_idx} op {op}")

                    time.sleep(0.005)

            except Exception as e:
                errors.append(f"Worker {worker_idx} failed with: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Encountered concurrency errors under realistic workload: {errors}")

    def test_concurrency_extreme_write_lock_contention_audit(self):
        """
        Empirically audits lock contention boundary when 50 threads perform unspaced concurrent writes.
        Documents SQLite busy_timeout saturation behavior on Windows NTFS.
        """
        num_threads = 50
        ops_per_thread = 3
        lock_errors = []
        successful_ops = 0
        lock_obj = threading.Lock()

        def unspaced_worker(w_idx: int):
            nonlocal successful_ops
            for op in range(ops_per_thread):
                try:
                    t_id = f"extreme_{w_idx}_{op}_{uuid.uuid4().hex[:6]}"
                    content = f"Extreme {w_idx}-{op}: " + ("X" * 2000)
                    self.db.save_thought(t_id, str(w_idx), 2000 + w_idx, content)
                    with lock_obj:
                        successful_ops += 1
                except Exception as e:
                    with lock_obj:
                        lock_errors.append(type(e).__name__ + ": " + str(e))

        threads = [threading.Thread(target=unspaced_worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"\n[EMPIRICAL CONCURRENCY AUDIT] Unspaced 50 threads x 3 ops (150 total writes):")
        print(f"  Successful writes: {successful_ops}")
        print(f"  Lock error count: {len(lock_errors)}")
        if lock_errors:
            print(f"  Sample error: {lock_errors[0]}")
            # All errors must be standard SQLite locking exceptions (no data corruption or unhandled crashes)
            for err in lock_errors:
                self.assertIn("database is locked", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
