"""
Adversarial Stress-Test Suite for Milestone 2 R2 (Imagen 3 AI Image Generation)
Author: Challenger M2-2 (Empirical Challenger)
Target: bot.py (handle_draw_command, enhance_image_prompt)
"""

import os
import sys
import io
import re
import uuid
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure UTF-8 output encoding
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key_for_offline")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test_token_for_offline")

import bot
from bot import (
    handle_draw_command,
    enhance_image_prompt,
    check_access,
)
from google.genai import types
from telegram.error import BadRequest, Forbidden


class TestR2AdversarialStress(unittest.TestCase):
    def setUp(self):
        # Create a fresh mock update and context for each test
        self.mock_update = MagicMock()
        self.mock_update.effective_user.id = 12345678
        self.mock_update.effective_chat.id = 98765432
        self.mock_update.message = MagicMock()

        self.mock_status = MagicMock()
        self.mock_status.edit_text = AsyncMock()
        self.mock_status.delete = AsyncMock()

        self.mock_update.message.reply_text = AsyncMock(return_value=self.mock_status)
        self.mock_update.message.reply_photo = AsyncMock()

        self.mock_context = MagicMock()
        self.mock_context.bot.send_chat_action = AsyncMock()

        # Mock image bytes
        self.fake_jpeg = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB\x00\x43\x00\x08"

    def _create_mock_gen_resp(self, img_bytes=None, rai_reason=None):
        mock_resp = MagicMock()
        if img_bytes is not None or rai_reason is not None:
            mock_img = MagicMock()
            mock_img.rai_filtered_reason = rai_reason
            mock_img.image.image_bytes = img_bytes
            mock_resp.generated_images = [mock_img]
        else:
            mock_resp.generated_images = []
        return mock_resp

    # =========================================================================
    # 1. Aspect Ratio Variations & Boundary Conditions
    # =========================================================================

    def test_adv_ar_valid_ratios_extraction(self):
        """Tests all supported aspect ratios (1:1, 16:9, 9:16, 4:3, 3:4) are passed to Imagen 3."""
        ratios = ["1:1", "16:9", "9:16", "4:3", "3:4"]
        for ar in ratios:
            with self.subTest(aspect_ratio=ar):
                self.mock_update.message.text = f"/draw majestic lion on mountain cliff --ar {ar}"
                mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

                with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
                     patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)):

                    asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

                    mock_gen.assert_called_once()
                    cfg = mock_gen.call_args[1]["config"]
                    self.assertEqual(cfg.aspect_ratio, ar)
                    self.mock_update.message.reply_photo.assert_called_once()
                    caption = self.mock_update.message.reply_photo.call_args[1]["caption"]
                    self.assertIn(f"`{ar}`", caption)

                self.mock_update.message.reply_photo.reset_mock()

    def test_adv_ar_case_insensitivity(self):
        """Verifies uppercase --AR and mixed-case --Ar are correctly parsed."""
        for ar_flag in ["--AR 16:9", "--Ar 9:16", "--aR 4:3"]:
            with self.subTest(flag=ar_flag):
                expected_ar = ar_flag.split()[1]
                self.mock_update.message.text = f"/draw futuristic car {ar_flag}"
                mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

                with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
                     patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)):

                    asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

                    mock_gen.assert_called_once()
                    cfg = mock_gen.call_args[1]["config"]
                    self.assertEqual(cfg.aspect_ratio, expected_ar)

    def test_adv_ar_invalid_ratio_graceful_fallback(self):
        """Verifies unsupported aspect ratios (--ar 99:99, --ar 21:9, --ar abc) fallback to 1:1 without crash."""
        invalid_flags = ["--ar 99:99", "--ar 21:9", "--ar 2:3", "--ar widescreen", "--ar", "--ar -1:-1"]
        for flag in invalid_flags:
            with self.subTest(flag=flag):
                self.mock_update.message.text = f"/draw cool wallpaper {flag}"
                mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

                with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
                     patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)):

                    asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

                    mock_gen.assert_called_once()
                    cfg = mock_gen.call_args[1]["config"]
                    self.assertEqual(cfg.aspect_ratio, "1:1", "Invalid aspect ratio must safely fallback to 1:1")

    def test_adv_ar_only_without_prompt_text(self):
        """Adversarial: User sends only '/draw --ar 16:9' with no description."""
        self.mock_update.message.text = "/draw --ar 16:9"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="A generic artistic landscape")):

            # Crucially: it must NOT raise an unhandled exception or crash the process
            try:
                asyncio.run(handle_draw_command(self.mock_update, self.mock_context))
            except Exception as e:
                self.fail(f"handle_draw_command crashed on '--ar 16:9' only prompt: {e}")

    def test_adv_ar_multiple_flags_in_single_prompt(self):
        """Adversarial: User passes multiple --ar flags like '/draw cat --ar 16:9 --ar 9:16'."""
        self.mock_update.message.text = "/draw cat --ar 16:9 --ar 9:16"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
             patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            # Must pick the first valid one (16:9) and clean it up
            mock_gen.assert_called_once()
            self.assertEqual(mock_gen.call_args[1]["config"].aspect_ratio, "16:9")

    def test_adv_ar_embedded_in_middle_of_sentence(self):
        """Verifies aspect ratio flag embedded in the middle of prompt is extracted cleanly."""
        self.mock_update.message.text = "/draw sunset --ar 16:9 over Persian Gulf waves"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
             patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)) as mock_enh:

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            mock_gen.assert_called_once()
            cfg = mock_gen.call_args[1]["config"]
            self.assertEqual(cfg.aspect_ratio, "16:9")
            call_prompt = mock_gen.call_args[1]["prompt"]
            self.assertNotIn("--ar 16:9", call_prompt)
            self.assertIn("sunset", call_prompt)
            self.assertIn("over Persian Gulf", call_prompt)

    # =========================================================================
    # 2. Extreme Prompt Inputs & Unicode Resilience
    # =========================================================================

    def test_adv_empty_and_whitespace_prompts(self):
        """Verifies empty, space-only, tab-only, newline-only commands reply with help guide."""
        test_inputs = [
            "/draw",
            "/image",
            "/draw   ",
            "/image \t\t",
            "/draw \n\n",
            "/draw     \t  \n  ",
        ]
        for cmd in test_inputs:
            with self.subTest(cmd=repr(cmd)):
                self.mock_update.message.text = cmd
                self.mock_update.message.reply_text.reset_mock()

                asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

                self.mock_update.message.reply_text.assert_called_once()
                sent_msg = self.mock_update.message.reply_text.call_args[0][0]
                self.assertIn("راهنمای دستور ساخت تصویر", sent_msg)

    def test_adv_persian_poetry_with_zwnj_and_diacritics(self):
        """Verifies complex Persian literature with ZWNJ, Tashdid, and Tanvin executes flawlessly."""
        poem = (
            "بیا تا گل برافشانیم و مَی در ساغر اندازیم / "
            "فلک را سقف بشکافیم و طرحی نو دراندازیم / "
            "اگر غم لشکر انگیزد که خون عاشقان ریزد / "
            "من و ساقی به هم تازیم و بنیادش براندازیم --ar 16:9"
        )
        self.mock_update.message.text = f"/draw {poem}"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        mock_enhanced_english = (
            "An ethereal Persian miniature style painting depicting celestial lovers, "
            "wine poured into golden chalices, swirling rose petals, mystical atmosphere, 8k"
        )

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
             patch("bot.enhance_image_prompt", AsyncMock(return_value=mock_enhanced_english)) as mock_enh:

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            mock_enh.assert_called_once()
            mock_gen.assert_called_once()
            self.assertEqual(mock_gen.call_args[1]["prompt"], mock_enhanced_english)
            self.assertEqual(mock_gen.call_args[1]["config"].aspect_ratio, "16:9")
            self.mock_update.message.reply_photo.assert_called_once()

    def test_adv_extreme_length_2000_char_prompt(self):
        """Verifies 2000+ character prompt is handled without overflowing Telegram photo caption limits."""
        long_prompt = "یک شهر باستانی پر از ستون‌های مرمر سفید و کاخ‌های باشکوه " * 40
        self.assertGreater(len(long_prompt), 2000)
        self.mock_update.message.text = f"/draw {long_prompt}"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="Ancient marble city")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_update.message.reply_photo.assert_called_once()
            caption = self.mock_update.message.reply_photo.call_args[1]["caption"]
            # Telegram photo caption limit is 1024 characters. Verify caption is safe.
            self.assertLess(len(caption), 1024)
            self.assertIn("Imagen 3", caption)

    def test_adv_rtl_override_and_control_chars(self):
        """Verifies Right-To-Left Override (U+202E), zero-width characters, and non-printable control chars."""
        malicious_unicode = "\u202e\u200b\ufeffPersian test with \x00\x08 hidden bytes and reverse order"
        self.mock_update.message.text = f"/draw {malicious_unicode}"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="Sanitized unicode prompt")):

            # Must process without UnicodeDecodeError or crash
            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))
            self.mock_update.message.reply_photo.assert_called_once()

    def test_adv_emojis_and_multilingual_unicode(self):
        """Verifies emojis, RTL/LTR markers, and multi-script symbols."""
        emoji_prompt = "🦁👑 Golden Cyber-Lion with glowing eyes 🌌✨🪐 in ancient Persepolis تخت جمشید 🏛️"
        self.mock_update.message.text = f"/draw {emoji_prompt} --ar 9:16"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp) as mock_gen, \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="Cybernetic golden lion in Persepolis")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            mock_gen.assert_called_once()
            self.assertEqual(mock_gen.call_args[1]["config"].aspect_ratio, "9:16")
            self.mock_update.message.reply_photo.assert_called_once()

    def test_adv_unclosed_markdown_in_prompt_triggers_safe_fallback(self):
        """
        Adversarial: Prompt contains unclosed markdown entities like ``` or * or `
        which causes Telegram's Markdown parser to raise BadRequest('Can't parse entities').
        Verifies handle_draw_command catches BadRequest and falls back to plain text delivery.
        """
        bad_markdown_prompt = "``` unclosed code block * bold _ italic ` backtick"
        self.mock_update.message.text = f"/draw {bad_markdown_prompt}"
        mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)

        # First reply_photo raises BadRequest because of broken Markdown
        # Second reply_photo (fallback) succeeds
        self.mock_update.message.reply_photo.side_effect = [
            BadRequest("Can't parse entities: can't find end of the entity starting at byte-offset 50"),
            MagicMock()
        ]

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="safe enhanced")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            # reply_photo should have been called twice (1: failed markdown, 2: fallback plain text)
            self.assertEqual(self.mock_update.message.reply_photo.call_count, 2)
            second_call_kwargs = self.mock_update.message.reply_photo.call_args_list[1][1]
            self.assertNotIn("parse_mode", second_call_kwargs)

    # =========================================================================
    # 3. Transient API & Error Simulations (503, 429, Safety, Network)
    # =========================================================================

    def test_adv_imagen3_transient_503_unavailable_simulation(self):
        """Simulates 503 UNAVAILABLE service outage from Google Imagen 3 API."""
        self.mock_update.message.text = "/draw sunset over sea"
        exc_503 = Exception("503 UNAVAILABLE: The service is currently overloaded. Please try again later.")

        with patch.object(bot.client.models, "generate_images", side_effect=exc_503), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="sunset over sea")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_status.edit_text.assert_called_once()
            err_msg = self.mock_status.edit_text.call_args[0][0]
            self.assertIn("خطا در تولید تصویر", err_msg)
            self.assertIn("503", err_msg)
            self.mock_update.message.reply_photo.assert_not_called()

    def test_adv_imagen3_rate_limit_429_exhausted_simulation(self):
        """Simulates 429 RESOURCE_EXHAUSTED quota spike from Google Imagen 3 API."""
        self.mock_update.message.text = "/draw rapid fire images"
        exc_429 = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Generate Images requests'")

        with patch.object(bot.client.models, "generate_images", side_effect=exc_429), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="rapid fire images")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_status.edit_text.assert_called_once()
            err_msg = self.mock_status.edit_text.call_args[0][0]
            self.assertIn("سهمیه درخواست تصویر گوگل موقتاً پر شده است", err_msg)
            self.assertIn("429 Rate Limit", err_msg)
            self.mock_update.message.reply_photo.assert_not_called()

    def test_adv_imagen3_safety_filter_exception_simulation(self):
        """Simulates Google API raising an explicit SAFETY / FILTER exception."""
        self.mock_update.message.text = "/draw violent scene"
        exc_safety = Exception("GoogleGenAIError: Request was blocked by SAFETY filters.")

        with patch.object(bot.client.models, "generate_images", side_effect=exc_safety), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="violent scene")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_status.edit_text.assert_called_once()
            err_msg = self.mock_status.edit_text.call_args[0][0]
            self.assertIn("فیلتر ایمنی", err_msg)
            self.mock_update.message.reply_photo.assert_not_called()

    def test_adv_imagen3_rai_filtered_reason_in_image_object(self):
        """Simulates Imagen 3 returning 200 OK but with rai_filtered_reason in image."""
        self.mock_update.message.text = "/draw sensitive subject"
        mock_resp = self._create_mock_gen_resp(rai_reason="IMAGE_SAFETY_POLICY_VIOLATION")

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="sensitive subject")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_status.edit_text.assert_called_once()
            err_msg = self.mock_status.edit_text.call_args[0][0]
            self.assertIn("قوانین فیلتر ایمنی گوگل", err_msg)
            self.assertIn("IMAGE_SAFETY_POLICY_VIOLATION", err_msg)
            self.mock_update.message.reply_photo.assert_not_called()

    def test_adv_imagen3_empty_image_list_returned(self):
        """Simulates Imagen 3 returning an empty generated_images list."""
        self.mock_update.message.text = "/draw strange prompt"
        mock_resp = self._create_mock_gen_resp()  # empty images

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="strange prompt")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_status.edit_text.assert_called_once()
            err_msg = self.mock_status.edit_text.call_args[0][0]
            self.assertIn("متأسفانه تصویری ایجاد نشد", err_msg)
            self.mock_update.message.reply_photo.assert_not_called()

    def test_adv_imagen3_empty_image_bytes_returned(self):
        """Simulates Imagen 3 returning an image object with None or empty image_bytes."""
        self.mock_update.message.text = "/draw zero byte image"
        mock_resp = self._create_mock_gen_resp(img_bytes=b"")

        with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
             patch("bot.enhance_image_prompt", AsyncMock(return_value="zero byte image")):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            self.mock_status.edit_text.assert_called_once()
            err_msg = self.mock_status.edit_text.call_args[0][0]
            self.assertIn("داده‌های فایل تصویر خالی است", err_msg)
            self.mock_update.message.reply_photo.assert_not_called()

    def test_adv_enhance_prompt_503_fallback(self):
        """Verifies enhance_image_prompt degrades gracefully to raw prompt when Flash hits 503."""
        with patch.object(bot.client.models, "generate_content", side_effect=Exception("503 Service Unavailable")):
            result = asyncio.run(enhance_image_prompt("تست پرامپت فارسی"))
            self.assertEqual(result, "تست پرامپت فارسی", "Must return raw prompt when enhancer fails")

    def test_adv_status_msg_edit_text_markdown_failure_fallback(self):
        """Verifies that if status_msg.edit_text fails (e.g. Markdown syntax error in error message), it falls back to reply_text."""
        self.mock_update.message.text = "/draw prompt with bad error"
        exc_with_backticks = Exception("Internal Error: `corrupted `unclosed markdown `")

        # edit_text throws BadRequest
        self.mock_status.edit_text.side_effect = BadRequest("Can't parse entities in error message")

        with patch.object(bot.client.models, "generate_images", side_effect=exc_with_backticks), \
             patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)):

            asyncio.run(handle_draw_command(self.mock_update, self.mock_context))

            # When status_msg.edit_text failed, fallback reply_text was invoked
            self.assertGreaterEqual(self.mock_update.message.reply_text.call_count, 2)

    # =========================================================================
    # 4. Concurrency & High Load Stress
    # =========================================================================

    def test_adv_concurrent_draw_invocations(self):
        """Adversarial: Simulates 25 concurrent /draw calls with mixed success and failure."""
        async def run_concurrent():
            tasks = []
            for i in range(25):
                up = MagicMock()
                up.effective_user.id = 1000 + i
                up.effective_chat.id = 2000 + i
                up.message = MagicMock()
                st = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
                up.message.reply_text = AsyncMock(return_value=st)
                up.message.reply_photo = AsyncMock()
                ctx = MagicMock()
                ctx.bot.send_chat_action = AsyncMock()

                if i % 3 == 0:
                    up.message.text = f"/draw mountain landscape #{i} --ar 16:9"
                elif i % 3 == 1:
                    up.message.text = f"/image portrait #{i} --ar 9:16"
                else:
                    up.message.text = f"/draw invalid --ar 99:99 #{i}"

                tasks.append(handle_draw_command(up, ctx))

            mock_resp = self._create_mock_gen_resp(img_bytes=self.fake_jpeg)
            with patch.object(bot.client.models, "generate_images", return_value=mock_resp), \
                 patch("bot.enhance_image_prompt", AsyncMock(side_effect=lambda p: p)):
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for idx, r in enumerate(results):
                    if isinstance(r, Exception):
                        self.fail(f"Concurrent task {idx} raised exception: {r}")

        asyncio.run(run_concurrent())


if __name__ == "__main__":
    unittest.main()
