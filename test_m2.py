"""
Comprehensive Offline Test Suite for Milestone 2 (test_m2.py)
Covering:
- R1: Python Code Execution & Mathematical Solving
- R2: AI Image Generation (/draw & /image via Imagen 3)
- R6: Transparent Reasoning & Thinking Process Display
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
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure offline environment variables
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key_for_offline")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test_token_for_offline")

# Import components from bot.py
import bot
from bot import (
    Database,
    ChatResponse,
    format_response_with_code_execution,
    parse_gemini_candidate_parts,
    build_gemini_session,
    generate_chat_response,
    enhance_image_prompt,
    handle_draw_command,
    handle_message,
    callback_handler,
    send_long_message,
    AVAILABLE_MODELS,
    FALLBACK_MODELS,
)
from google.genai import types
from telegram import Update, Message, User, Chat, CallbackQuery, InlineKeyboardMarkup


class TestMilestone2(unittest.TestCase):
    def setUp(self):
        self.test_db_path = f"test_m2_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.test_db_path)
        bot.db = self.db
        self.user_id = "12345678"
        self.db.get_or_create_user(self.user_id)
        self.active_chat = self.db.get_active_chat(self.user_id)

    def tearDown(self):
        for extra in ["", "-wal", "-shm"]:
            p = self.test_db_path + extra
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # =========================================================================
    # R1: Python Code Execution & Mathematical Solving Tests
    # =========================================================================

    def test_r1_chat_response_structure_and_namedtuple(self):
        """Verifies ChatResponse NamedTuple properties, unpacking, and formatted_text."""
        resp = ChatResponse(
            clean_text="پاسخ نهایی ۲۴ است.",
            executed_code="def fact(n):\n    return 1 if n<=1 else n*fact(n-1)\nprint(fact(4))",
            execution_result="24\n",
            thought_text="باید فاکتوریل ۴ را محاسبه کنم."
        )
        self.assertEqual(resp.clean_text, "پاسخ نهایی ۲۴ است.")
        self.assertEqual(resp[0], "پاسخ نهایی ۲۴ است.")
        self.assertIn("fact(4)", resp.executed_code)
        self.assertEqual(resp.execution_result.strip(), "24")
        self.assertEqual(resp.thought_text, "باید فاکتوریل ۴ را محاسبه کنم.")

        # Test tuple unpacking
        clean, code, result, thought = resp
        self.assertEqual(clean, "پاسخ نهایی ۲۴ است.")
        self.assertTrue(code.startswith("def fact"))
        self.assertEqual(result.strip(), "24")
        self.assertEqual(thought, "باید فاکتوریل ۴ را محاسبه کنم.")

        # Test formatted_text property
        formatted = resp.formatted_text
        self.assertIn("پاسخ نهایی ۲۴ است.", formatted)
        self.assertIn("💻 **کد پایتون اجرا شده:**", formatted)
        self.assertIn("def fact(n):", formatted)
        self.assertIn("📊 **نتیجه تایید شده اجرا:**", formatted)
        self.assertIn("24", formatted)

    def test_r1_format_response_with_code_execution_badge(self):
        """Verifies code execution badge formatting with exact output and no code branch."""
        # Case A: When no code was executed
        simple_res = format_response_with_code_execution("پاسخ ساده بدون کد")
        self.assertEqual(simple_res, "پاسخ ساده بدون کد")

        # Case B: When code executed with stdout
        code = "import math\nprint(math.sqrt(144))"
        output = "12.0\n"
        formatted = format_response_with_code_execution("جذر ۱۴۴ برابر ۱۲ است.", code, output)
        self.assertIn("جذر ۱۴۴ برابر ۱۲ است.", formatted)
        self.assertIn("```python\nimport math\nprint(math.sqrt(144))\n```", formatted)
        self.assertIn("```\n12.0\n```", formatted)
        self.assertIn("💻 **کد پایتون اجرا شده:**", formatted)
        self.assertIn("📊 **نتیجه تایید شده اجرا:**", formatted)

        # Case C: When code executed but produced no console stdout
        formatted_no_out = format_response_with_code_execution("محاسبه انجام شد.", "x = 42", "")
        self.assertIn("*(بدون خروجی کنسول)*", formatted_no_out)

    def test_r1_parse_gemini_candidate_parts_code_exec(self):
        """Verifies extraction of executable_code and code_execution_result from candidate parts."""
        # Mock Part with executable code
        part_code = MagicMock()
        part_code.thought = False
        part_code.executable_code = MagicMock()
        part_code.executable_code.code = "import math\nprint(math.factorial(6))"
        part_code.executable_code.language = "PYTHON"
        part_code.code_execution_result = None
        part_code.text = None

        # Mock Part with execution result
        part_res = MagicMock()
        part_res.thought = False
        part_res.executable_code = None
        part_res.code_execution_result = MagicMock()
        part_res.code_execution_result.output = "720\n"
        part_res.code_execution_result.outcome = "OUTCOME_OK"
        part_res.text = None

        # Mock Part with text answer
        part_text = MagicMock()
        part_text.thought = False
        part_text.executable_code = None
        part_text.code_execution_result = None
        part_text.text = "فاکتوریل ۶ برابر ۷۲۰ است."

        # Mock Candidate and Response
        candidate = MagicMock()
        candidate.content.parts = [part_code, part_res, part_text]
        mock_response = MagicMock()
        mock_response.candidates = [candidate]
        mock_response.text = "فاکتوریل ۶ برابر ۷۲۰ است."

        parsed = parse_gemini_candidate_parts(mock_response)
        self.assertEqual(parsed.clean_text, "فاکتوریل ۶ برابر ۷۲۰ است.")
        self.assertEqual(parsed.executed_code, "import math\nprint(math.factorial(6))")
        self.assertEqual(parsed.execution_result, "720")
        self.assertIsNone(parsed.thought_text)

    def test_r1_build_gemini_session_includes_tool_code_execution(self):
        """Verifies build_gemini_session adds ToolCodeExecution by default, and respects flag."""
        with patch("google.genai.chats.Chats.create") as mock_create:
            mock_create.return_value = MagicMock()

            # 1. Default enable_code_exec=True
            session = build_gemini_session(self.user_id, self.active_chat["id"], "gemini-3.8-flash")
            self.assertIsNotNone(session)
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args[1]
            gen_cfg = call_kwargs["config"]
            self.assertIsNotNone(gen_cfg.tools)

            # Check for code_execution tool
            has_code_exec = any(getattr(t, "code_execution", None) is not None for t in gen_cfg.tools)
            self.assertTrue(has_code_exec, "ToolCodeExecution must be present in tools list")

        # 2. When enable_code_exec=False
        with patch("google.genai.chats.Chats.create") as mock_create:
            mock_create.return_value = MagicMock()
            build_gemini_session(self.user_id, self.active_chat["id"], "gemini-3.8-flash", enable_code_exec=False)
            call_kwargs = mock_create.call_args[1]
            gen_cfg = call_kwargs["config"]
            tools = gen_cfg.tools or []
            has_code_exec = any(getattr(t, "code_execution", None) is not None for t in tools)
            self.assertFalse(has_code_exec, "ToolCodeExecution must be omitted when enable_code_exec=False")

    def test_r1_generate_chat_response_end_to_end_math(self):
        """Tests async generate_chat_response returning verified math code execution."""
        mock_chat_session = MagicMock()
        mock_response = MagicMock()

        part_code = MagicMock()
        part_code.thought = False
        part_code.executable_code = MagicMock(code="x = sum(i**2 for i in range(1, 11))\nprint(x)")
        part_code.code_execution_result = None
        part_code.text = None

        part_res = MagicMock()
        part_res.thought = False
        part_res.executable_code = None
        part_res.code_execution_result = MagicMock(output="385\n", outcome="OUTCOME_OK")
        part_res.text = None

        part_text = MagicMock()
        part_text.thought = False
        part_text.executable_code = None
        part_text.code_execution_result = None
        part_text.text = "مجموع مربعات اعداد ۱ تا ۱۰ برابر ۳۸۵ می‌شود."

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [part_code, part_res, part_text]
        mock_response.candidates = [mock_candidate]
        mock_response.text = "مجموع مربعات اعداد ۱ تا ۱۰ برابر ۳۸۵ می‌شود."

        mock_chat_session.send_message.return_value = mock_response

        with patch("bot.build_gemini_session", return_value=mock_chat_session):
            res = asyncio.run(generate_chat_response(self.user_id, self.active_chat["id"], "مجموع مربعات ۱ تا ۱۰ چی میشه؟"))
            self.assertIsInstance(res, ChatResponse)
            self.assertIn("۳۸۵", res.clean_text)
            self.assertIn("sum(i**2", res.executed_code)
            self.assertEqual(res.execution_result.strip(), "385")
            self.assertIn("💻 **کد پایتون اجرا شده:**", res.formatted_text)
            self.assertIn("📊 **نتیجه تایید شده اجرا:**", res.formatted_text)

    # =========================================================================
    # R2: AI Image Generation (/draw & /image via Imagen 3) Tests
    # =========================================================================

    def test_r2_enhance_image_prompt_persian_and_english(self):
        """Verifies bilingual prompt enhancement via Gemini Flash."""
        # 1. Persian prompt translates and enriches
        mock_resp = MagicMock()
        mock_resp.text = "A hyperrealistic astronaut floating in Mars orbit at sunset, cinematic lighting, 8k resolution"
        with patch.object(bot.client.models, "generate_content", return_value=mock_resp) as mock_gen:
            enhanced = asyncio.run(enhance_image_prompt("یک فضانورد در مدار مریخ هنگام غروب"))
            self.assertEqual(enhanced, mock_resp.text)
            mock_gen.assert_called_once()
            call_contents = mock_gen.call_args[1]["contents"]
            self.assertEqual(call_contents[1], "یک فضانورد در مدار مریخ هنگام غروب")

        # 2. Fallback on API exception returns raw prompt cleanly
        with patch.object(bot.client.models, "generate_content", side_effect=Exception("API Error")):
            raw = asyncio.run(enhance_image_prompt("plain prompt"))
            self.assertEqual(raw, "plain prompt")

    def test_r2_handle_draw_command_empty_prompt_sends_help(self):
        """Verifies /draw with no prompt delivers clear bilingual help guide."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 123456
        mock_update.message.text = "/draw"
        mock_update.message.reply_text = AsyncMock()
        mock_context = MagicMock()

        asyncio.run(handle_draw_command(mock_update, mock_context))

        mock_update.message.reply_text.assert_called_once()
        sent_text = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("راهنمای دستور ساخت تصویر", sent_text)
        self.assertIn("/draw", sent_text)
        self.assertIn("/image", sent_text)
        self.assertIn("--ar", sent_text)

    def test_r2_handle_draw_command_aspect_ratio_extraction(self):
        """Verifies parsing of aspect ratio flags: 16:9, 9:16, 4:3, 1:1."""
        test_prompts = [
            ("/draw منظره کوهستان --ar 16:9", "16:9", "منظره کوهستان"),
            ("/image futuristic city --ar 9:16", "9:16", "futuristic city"),
            ("/draw پرتره قدیمی --ar 4:3", "4:3", "پرتره قدیمی"),
            ("/draw گربه بامزه", "1:1", "گربه بامزه"),
        ]

        for cmd, expected_ar, expected_cleaned in test_prompts:
            ar_match = re.search(r"--ar\s+(1:1|16:9|9:16|4:3|3:4)\b", cmd, flags=re.IGNORECASE)
            ar = "1:1"
            cleaned = cmd.split(maxsplit=1)[1]
            if ar_match:
                ar = ar_match.group(1)
                cleaned = cleaned[:ar_match.start()] + cleaned[ar_match.end():]
                cleaned = cleaned.strip()
            self.assertEqual(ar, expected_ar)
            self.assertIn(expected_cleaned, cleaned)

    def test_r2_handle_draw_command_success_delivers_photo(self):
        """Verifies successful Imagen 3 generation sends photo with correct metadata and caption."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 123456
        mock_update.message.text = "/draw شیر بالدار طلایی در تخت جمشید --ar 16:9"
        mock_update.effective_chat.id = 998877

        # Status message mock
        mock_status = MagicMock()
        mock_status.edit_text = AsyncMock()
        mock_status.delete = AsyncMock()
        mock_update.message.reply_text = AsyncMock(return_value=mock_status)
        mock_update.message.reply_photo = AsyncMock()

        mock_context = MagicMock()
        mock_context.bot.send_chat_action = AsyncMock()

        # Mock image output
        fake_jpeg = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB"
        mock_generated_image = MagicMock()
        mock_generated_image.rai_filtered_reason = None
        mock_generated_image.image.image_bytes = fake_jpeg

        mock_gen_resp = MagicMock()
        mock_gen_resp.generated_images = [mock_generated_image]

        with patch.object(bot.client.models, "generate_images", return_value=mock_gen_resp) as mock_img_call, \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="A golden winged lion in Persepolis, 8k")):

            asyncio.run(handle_draw_command(mock_update, mock_context))

            # Verify generate_images was called with model imagen-3.0-generate-002
            mock_img_call.assert_called_once()
            call_kw = mock_img_call.call_args[1]
            self.assertEqual(call_kw["model"], "imagen-3.0-generate-002")
            self.assertEqual(call_kw["config"].aspect_ratio, "16:9")
            self.assertEqual(call_kw["config"].number_of_images, 1)

            # Verify photo delivery
            mock_update.message.reply_photo.assert_called_once()
            photo_arg = mock_update.message.reply_photo.call_args[1]["photo"]
            self.assertIsInstance(photo_arg, io.BytesIO)
            self.assertEqual(photo_arg.getvalue(), fake_jpeg)

            caption_arg = mock_update.message.reply_photo.call_args[1]["caption"]
            self.assertIn("Imagen 3", caption_arg)
            self.assertIn("16:9", caption_arg)

            # Verify status message cleanup
            mock_status.delete.assert_called_once()

    def test_r2_handle_draw_command_safety_filter_rejection(self):
        """Verifies graceful Persian error reporting when prompt triggers safety filter."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 123456
        mock_update.message.text = "/draw تصویر ممنوعه فیلترشده"
        mock_update.effective_chat.id = 998877

        mock_status = MagicMock()
        mock_status.edit_text = AsyncMock()
        mock_update.message.reply_text = AsyncMock(return_value=mock_status)
        mock_update.message.reply_photo = AsyncMock()

        mock_context = MagicMock()
        mock_context.bot.send_chat_action = AsyncMock()

        # Mock image flagged by safety
        mock_flagged_image = MagicMock()
        mock_flagged_image.rai_filtered_reason = "EXPLICIT_CONTENT"
        mock_gen_resp = MagicMock(generated_images=[mock_flagged_image])

        with patch.object(bot.client.models, "generate_images", return_value=mock_gen_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="blocked prompt")):

            asyncio.run(handle_draw_command(mock_update, mock_context))

            mock_status.edit_text.assert_called_once()
            err_text = mock_status.edit_text.call_args[0][0]
            self.assertIn("فیلتر ایمنی", err_text)
            mock_update.message.reply_photo.assert_not_called()

    def test_r2_handle_draw_command_rate_limit_429(self):
        """Verifies friendly Persian error response when Google API hits 429 quota limit."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 123456
        mock_update.message.text = "/image test heavy load"
        mock_update.effective_chat.id = 998877

        mock_status = MagicMock()
        mock_status.edit_text = AsyncMock()
        mock_update.message.reply_text = AsyncMock(return_value=mock_status)

        mock_context = MagicMock()
        mock_context.bot.send_chat_action = AsyncMock()

        with patch.object(bot.client.models, "generate_images", side_effect=Exception("429 RESOURCE_EXHAUSTED")), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="test prompt")):
            asyncio.run(handle_draw_command(mock_update, mock_context))

            mock_status.edit_text.assert_called_once()
            msg = mock_status.edit_text.call_args[0][0]
            self.assertIn("سهمیه درخواست تصویر", msg)
            self.assertIn("429", msg)

    def test_r2_handle_image_command_alias_and_options(self):
        """Verifies that /image works identically to /draw and strips command prefix."""
        mock_update = MagicMock()
        mock_update.effective_user.id = 123456
        mock_update.message.text = "/image a cute robot painter --ar 4:3"
        mock_update.effective_chat.id = 998877

        mock_status = MagicMock()
        mock_status.edit_text = AsyncMock()
        mock_status.delete = AsyncMock()
        mock_update.message.reply_text = AsyncMock(return_value=mock_status)
        mock_update.message.reply_photo = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.send_chat_action = AsyncMock()

        mock_gen_resp = MagicMock()
        mock_img = MagicMock()
        mock_img.rai_filtered_reason = None
        mock_img.image.image_bytes = b"fake_bytes_4_3"
        mock_gen_resp.generated_images = [mock_img]

        with patch.object(bot.client.models, "generate_images", return_value=mock_gen_resp) as mock_img_call, \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="a cute robot painter")):

            asyncio.run(handle_draw_command(mock_update, mock_context))

            mock_img_call.assert_called_once()
            call_kw = mock_img_call.call_args[1]
            self.assertEqual(call_kw["config"].aspect_ratio, "4:3")
            mock_update.message.reply_photo.assert_called_once()

    def test_r1_and_r6_mixed_candidate_parts(self):
        """Verifies candidate with thoughts, code execution, AND clean text in single payload."""
        p_thought = MagicMock(thought=True, text="فکر اولیه: بررسی فرمول دنباله فیبوناچی", executable_code=None, code_execution_result=None)
        p_code = MagicMock(thought=False, executable_code=MagicMock(code="a, b = 0, 1\nfor _ in range(10): a, b = b, a + b\nprint(a)"), code_execution_result=None, text=None)
        p_res = MagicMock(thought=False, executable_code=None, code_execution_result=MagicMock(output="55\n", outcome="OUTCOME_OK"), text=None)
        p_ans = MagicMock(thought=False, executable_code=None, code_execution_result=None, text="جمله دهم دنباله فیبوناچی برابر ۵۵ است.")

        mock_resp = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[p_thought, p_code, p_res, p_ans]))], text="جمله دهم دنباله فیبوناچی برابر ۵۵ است.")

        parsed = parse_gemini_candidate_parts(mock_resp)
        self.assertEqual(parsed.thought_text, "فکر اولیه: بررسی فرمول دنباله فیبوناچی")
        self.assertIn("range(10)", parsed.executed_code)
        self.assertEqual(parsed.execution_result.strip(), "55")
        self.assertEqual(parsed.clean_text, "جمله دهم دنباله فیبوناچی برابر ۵۵ است.")

        # Verify formatted text contains both badges
        formatted = parsed.formatted_text
        self.assertIn("💻 **کد پایتون اجرا شده:**", formatted)
        self.assertIn("📊 **نتیجه تایید شده اجرا:**", formatted)
        self.assertIn("55", formatted)

    def test_r6_database_reasoning_logs_schema(self):
        """Verifies reasoning_logs table schema and idx_reasoning_logs_user index exist."""
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reasoning_logs'")
            self.assertIsNotNone(cursor.fetchone(), "reasoning_logs table must exist")

            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_reasoning_logs_user'")
            self.assertIsNotNone(cursor.fetchone(), "idx_reasoning_logs_user index must exist")

    def test_r6_build_gemini_session_configures_include_thoughts(self):
        """Verifies ThinkingConfig is initialized with include_thoughts=True and thinking_budget."""
        with patch("google.genai.chats.Chats.create") as mock_create:
            mock_create.return_value = MagicMock()
            build_gemini_session(self.user_id, self.active_chat["id"], "gemini-3.8-flash")

            mock_create.assert_called_once()
            gen_cfg = mock_create.call_args[1]["config"]
            self.assertIsNotNone(gen_cfg.thinking_config)
            self.assertTrue(gen_cfg.thinking_config.include_thoughts)
            self.assertEqual(gen_cfg.thinking_config.thinking_budget, 4096)

    def test_r6_parse_gemini_candidate_parts_extracts_thoughts(self):
        """Verifies thinking tokens are separated from final clean response text."""
        part_thought = MagicMock()
        part_thought.thought = True
        part_thought.text = "مرحله ۱: تحلیل فرضیات مسئله. مرحله ۲: محاسبه مقدار x."
        part_thought.executable_code = None
        part_thought.code_execution_result = None

        part_answer = MagicMock()
        part_answer.thought = False
        part_answer.text = "مقدار نهایی x برابر ۴۲ است."
        part_answer.executable_code = None
        part_answer.code_execution_result = None

        mock_cand = MagicMock()
        mock_cand.content.parts = [part_thought, part_answer]
        mock_resp = MagicMock(candidates=[mock_cand], text="مقدار نهایی x برابر ۴۲ است.")

        parsed = parse_gemini_candidate_parts(mock_resp)
        self.assertEqual(parsed.clean_text, "مقدار نهایی x برابر ۴۲ است.")
        self.assertEqual(parsed.thought_text, "مرحله ۱: تحلیل فرضیات مسئله. مرحله ۲: محاسبه مقدار x.")
        self.assertIsNone(parsed.executed_code)

    def test_r6_database_reasoning_logs_persistence(self):
        """Verifies database saving and retrieval of thought content with UUID."""
        t_id = uuid.uuid4().hex[:12]
        sample_thought = "این یک تفکر عمیق چندمرحله‌ای برای حل مسئله است."
        self.db.save_thought(t_id, self.user_id, self.active_chat["id"], sample_thought)

        retrieved = self.db.get_thought(t_id)
        self.assertEqual(retrieved, sample_thought)

        # Non-existent thought returns None
        self.assertIsNone(self.db.get_thought("non_existent_id"))

    def test_r6_handle_message_attaches_thought_button_when_thoughts_present(self):
        """Verifies handle_message creates inline button [🧠 مشاهده فرآیند استدلال] when thought is captured."""
        mock_update = MagicMock()
        mock_update.effective_user.id = int(self.user_id)
        mock_update.effective_chat.id = 1234
        mock_update.message.text = "یک مسئله منطقی پیچیده را حل کن"
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.bot.send_chat_action = AsyncMock()

        # Mock generate_chat_response returning thought
        mock_resp = ChatResponse(
            clean_text="پاسخ مسئله: گزینه B صحیح است.",
            executed_code=None,
            execution_result=None,
            thought_text="در حال بررسی گزینه A... رد شد. گزینه B شرایط را ارضا می‌کند."
        )

        with patch("bot.generate_chat_response", AsyncMock(return_value=mock_resp)), \
             patch("bot.ProgressTracker.finish", AsyncMock()) as mock_finish:

            asyncio.run(handle_message(mock_update, mock_context))

            # ProgressTracker.finish must receive reply_markup with show_thought callback
            mock_finish.assert_called_once()
            call_kwargs = mock_finish.call_args[1]
            markup = call_kwargs.get("reply_markup")
            self.assertIsNotNone(markup)
            self.assertIsInstance(markup, InlineKeyboardMarkup)

            button = markup.inline_keyboard[0][0]
            self.assertIn("مشاهده فرآیند استدلال", button.text)
            self.assertTrue(button.callback_data.startswith("show_thought:"))

            # Verify thought saved in database
            thought_id = button.callback_data.split(":")[1]
            saved_content = self.db.get_thought(thought_id)
            self.assertEqual(saved_content, "در حال بررسی گزینه A... رد شد. گزینه B شرایط را ارضا می‌کند.")

    def test_r6_callback_handler_delivers_reasoning_spoiler(self):
        """Verifies tapping show_thought button fetches thought from DB and sends formatted spoiler."""
        t_id = uuid.uuid4().hex[:12]
        self.db.save_thought(t_id, self.user_id, self.active_chat["id"], "تفکر مدل: گام ۱ -> گام ۲")

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = int(self.user_id)
        mock_query.data = f"show_thought:{t_id}"
        mock_query.answer = AsyncMock()
        mock_query.message.reply_text = AsyncMock()
        mock_update.callback_query = mock_query

        mock_context = MagicMock()

        asyncio.run(callback_handler(mock_update, mock_context))

        mock_query.answer.assert_called_once()
        mock_query.message.reply_text.assert_called_once()
        reply_content = mock_query.message.reply_text.call_args[0][0]
        self.assertIn("فرآیند و زنجیره تفکر", reply_content)
        self.assertIn("||تفکر مدل: گام ۱ -> گام ۲||", reply_content)

    def test_r6_callback_handler_massive_thought_chunking(self):
        """Verifies thoughts exceeding 3500 chars are chunked across sequential spoiler messages with part indicators."""
        t_id = uuid.uuid4().hex[:12]
        large_thought = "این یک تفکر طولانی است.\n" * 200  # ~4800 chars
        self.db.save_thought(t_id, self.user_id, self.active_chat["id"], large_thought)

        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = int(self.user_id)
        mock_query.data = f"show_thought:{t_id}"
        mock_query.answer = AsyncMock()
        mock_query.message.reply_text = AsyncMock()
        mock_update.callback_query = mock_query

        asyncio.run(callback_handler(mock_update, MagicMock()))

        mock_query.answer.assert_called_once()
        expected_chunks = (len(large_thought) + 3499) // 3500
        self.assertEqual(mock_query.message.reply_text.call_count, expected_chunks)

        delivered = []
        for idx, call in enumerate(mock_query.message.reply_text.call_args_list):
            msg_text = call[0][0]
            self.assertIn(f"بخش {idx + 1} از {expected_chunks}", msg_text)
            self.assertIn("||", msg_text)
            body = msg_text.split("||")[1]
            delivered.append(body)

        self.assertEqual("".join(delivered), large_thought)

    def test_r6_callback_handler_missing_thought_graceful_response(self):
        """Verifies tapping an expired or missing thought ID returns informative Persian message."""
        mock_update = MagicMock()
        mock_query = MagicMock()
        mock_query.from_user.id = int(self.user_id)
        mock_query.data = "show_thought:unknown_uuid"
        mock_query.answer = AsyncMock()
        mock_query.message.reply_text = AsyncMock()
        mock_update.callback_query = mock_query

        asyncio.run(callback_handler(mock_update, MagicMock()))

        mock_query.message.reply_text.assert_called_once()
        self.assertIn("یافت نشد", mock_query.message.reply_text.call_args[0][0])

    # =========================================================================
    # Fallback, Resilience & Edge-Cases Tests
    # =========================================================================

    def test_resilience_multi_tier_fallback_on_503(self):
        """Verifies fallback cascade from primary model to fallback models upon 503 UNAVAILABLE."""
        call_models = []

        def fake_build_gemini_session(user_id, chat_id, target_model, is_fallback=False, enable_code_exec=True):
            call_models.append(target_model)
            sess = MagicMock()
            if target_model == "gemini-3.8-flash":
                sess.send_message.side_effect = Exception("503 UNAVAILABLE: Model overloaded")
            else:
                resp = MagicMock()
                resp.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(thought=False, executable_code=None, code_execution_result=None, text="پاسخ از مدل فال‌بک")]))]
                resp.text = "پاسخ از مدل فال‌بک"
                sess.send_message.return_value = resp
            return sess

        with patch("bot.build_gemini_session", side_effect=fake_build_gemini_session), \
             patch("asyncio.sleep", AsyncMock()):

            res = asyncio.run(generate_chat_response(self.user_id, self.active_chat["id"], "تست پایداری"))
            self.assertEqual(res.clean_text, "پاسخ از مدل فال‌بک")
            self.assertIn("gemini-3.8-flash", call_models)
            self.assertTrue(len(call_models) >= 2)

    def test_resilience_send_long_message_markdown_error_fallback(self):
        """Verifies that send_long_message falls back to plain text if Telegram Markdown parsing fails."""
        mock_update = MagicMock()
        # First call with parse_mode='Markdown' raises BadRequest, second call without parse_mode succeeds
        mock_update.message.reply_text = AsyncMock(side_effect=[Exception("BadRequest: Can't find end of entity"), MagicMock()])

        asyncio.run(send_long_message(mock_update, "متن با فرمت خراب Markdown: *تست_نامعتبر"))
        self.assertEqual(mock_update.message.reply_text.call_count, 2)
        # Second call does not have parse_mode
        self.assertNotIn("parse_mode", mock_update.message.reply_text.call_args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
