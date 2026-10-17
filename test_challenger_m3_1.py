"""
Adversarial Stress Test Suite for Milestone 3 Requirement R4:
Web Page & YouTube Video Extraction & Summarization
Empirical Challenger M3-1

Focus Areas:
1. Malformed, exotic, and adversarial URLs (schemes, 250+ params, ReDoS, length).
2. Server errors: 404, 403, 401, 500, 502, 503, 504, 418, timeouts, DNS/connect errors, SSL errors.
3. Slow streaming & payload stress: 50MB raw binary payloads, null bytes, deeply nested HTML, SPA/empty text.
4. YouTube transcripts: TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, TooManyRequests, malformed API objects.
5. End-to-end bot stability: handle_message and fetch_and_summarize_url under all adverse conditions.
"""

import os
import sys
import io
import re
import uuid
import asyncio
import sqlite3
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure offline dummy environment variables
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key_for_offline")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test_token_for_offline")

import httpx
import bot
from bot import (
    Database,
    extract_urls,
    extract_youtube_video_id,
    fetch_youtube_transcript,
    fetch_web_page_content,
    fetch_and_summarize_url,
    handle_message,
)
from telegram import Update, Message, User, Chat


class TestAdversarialURLExtraction(unittest.TestCase):
    """Stress-tests for extract_urls and extract_youtube_video_id with hostile inputs."""

    def test_01_malformed_and_forbidden_schemes(self):
        """Ensure non-HTTP/HTTPS schemes (javascript, ftp, file, data, blob, mailto) are rejected."""
        adversarial_inputs = [
            "javascript:alert(document.cookie)",
            "ftp://files.example.com/dump.zip",
            "file:///C:/Windows/System32/calc.exe",
            "file:///etc/passwd",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "blob:http://example.com/550e8400-e29b-41d4-a716-446655440000",
            "mailto:admin@example.com?subject=hack",
            "gopher://gopher.floodgap.com",
            "git@github.com:user/repo.git",
            "ssh://root@192.168.1.1:22",
        ]
        for hostile_str in adversarial_inputs:
            urls = extract_urls(hostile_str)
            # Must either return empty or only valid http/https URLs
            for u in urls:
                self.assertTrue(u.startswith("http://") or u.startswith("https://"),
                                f"Forbidden scheme extracted from '{hostile_str}': {u}")

    def test_02_relative_paths_and_path_traversal(self):
        """Relative URLs and path traversal strings must not be extracted as URLs."""
        inputs = [
            "/api/v1/users",
            "../../etc/passwd",
            "..\\..\\Windows\\System32\\config\\SAM",
            "./script.sh",
            "index.html",
        ]
        for item in inputs:
            urls = extract_urls(item)
            self.assertEqual(urls, [], f"Relative path wrongly recognized as URL: {item}")

    def test_03_query_params_stress_and_redos_resistance(self):
        """Test URL with 250+ query parameters and 15,000 characters for ReDoS and memory safety."""
        params = "&".join([f"param_{i}=val_{i}_{'x'*20}" for i in range(250)])
        huge_url = f"https://example.com/api/v2/search?{params}"
        text = f"لطفاً این لینک طولانی را پردازش کن: {huge_url} و نتیجه را بگو."

        urls = extract_urls(text)
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0], huge_url)
        self.assertTrue(len(urls[0]) > 7000)

    def test_04_complex_punctuation_and_parentheses_balancing(self):
        """Verify proper stripping of trailing punctuation without stripping valid Wikipedia parentheses."""
        cases = [
            ("ببین https://en.wikipedia.org/wiki/Python_(programming_language). جالب بود.",
             "https://en.wikipedia.org/wiki/Python_(programming_language)"),
            ("مستندات در (https://docs.python.org/3/) قرار دارد.",
             "https://docs.python.org/3/"),
            ("لینک: [https://github.com/project]!",
             "https://github.com/project"),
            ("آدرس سایت https://example.com/test?a=1&b=2;",
             "https://example.com/test?a=1&b=2"),
            ("آدرس سایت https://example.com/test?a=1&b=2,",
             "https://example.com/test?a=1&b=2"),
            ("واقعاً؟ https://example.com/question?",
             "https://example.com/question"),
            ("دیدیش؟ https://example.com/exclamation!",
             "https://example.com/exclamation"),
        ]
        for raw_text, expected_url in cases:
            extracted = extract_urls(raw_text)
            self.assertIn(expected_url, extracted, f"Failed on raw text: {raw_text}")

    def test_05_encoded_urls_and_ip_addresses(self):
        """Test extraction of URL-encoded Persian characters and direct IP addresses."""
        text = (
            "صفحه اول: https://fa.wikipedia.org/wiki/%D9%87%D9%88%D8%B4_%D9%85%D8%B5%D9%86%D9%88%D8%B9%DB%8C "
            "سرور محلی: http://192.168.1.100:8080/dashboard?token=xyz123 "
            "لوکال‌هاست: http://127.0.0.1:3000/#section"
        )
        urls = extract_urls(text)
        self.assertEqual(len(urls), 3)
        self.assertIn("https://fa.wikipedia.org/wiki/%D9%87%D9%88%D8%B4_%D9%85%D8%B5%D9%86%D9%88%D8%B9%DB%8C", urls)
        self.assertIn("http://192.168.1.100:8080/dashboard?token=xyz123", urls)
        self.assertIn("http://127.0.0.1:3000/#section", urls)

    def test_06_youtube_url_variations(self):
        """Test all valid YouTube URL formats extract the exact 11-char video ID."""
        valid_cases = [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("http://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ?t=120", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?feature=youtu.be&v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/v/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ]
        for url, expected_id in valid_cases:
            vid = extract_youtube_video_id(url)
            self.assertEqual(vid, expected_id, f"Failed on valid YouTube URL: {url}")

    def test_07_youtube_non_video_urls(self):
        """Test YouTube non-video URLs (channels, playlists, home) return None and do not crash."""
        non_video_cases = [
            "https://www.youtube.com/@Veritasium",
            "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA",
            "https://www.youtube.com/playlist?list=PL1234567890abcdef",
            "https://www.youtube.com/",
            "https://www.youtube.com/feed/trending",
            "https://www.youtube.com/watch?v=short",  # < 11 chars
            "https://www.youtube.com/watch?v=12345$#@!abc",  # invalid chars
        ]
        for url in non_video_cases:
            vid = extract_youtube_video_id(url)
            self.assertIsNone(vid, f"Expected None for non-video URL: {url}, got: {vid}")

    def test_08_youtube_domain_spoofing_isolation(self):
        """
        Adversarial test: Third-party domains containing 'youtube.com' or 'youtu.be'
        must NOT be recognized as YouTube videos, otherwise web scraping is bypassed.
        """
        spoofed_domains = [
            "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
            "https://fakeyoutu.be/dQw4w9WgXcQ",
            "https://evil-youtube.com/shorts/dQw4w9WgXcQ",
        ]
        for url in spoofed_domains:
            vid = extract_youtube_video_id(url)
            self.assertIsNone(vid, f"Spoofed domain '{url}' was incorrectly parsed as YouTube video: {vid}")

    def test_09_youtube_live_url_extraction(self):
        """
        Adversarial test: Standard YouTube Live URLs (youtube.com/live/<video_id>)
        must extract the 11-char video ID.
        """
        live_url = "https://www.youtube.com/live/dQw4w9WgXcQ"
        vid = extract_youtube_video_id(live_url)
        self.assertEqual(vid, "dQw4w9WgXcQ", f"YouTube Live URL failed to extract video ID: {live_url}")

    def test_10_oversized_youtube_id_behavior(self):
        """Document regex behavior on >11 char ID: extracts 11 chars without crashing or throwing."""
        url = "https://www.youtube.com/watch?v=toolongvideoidentifiertest"
        vid = extract_youtube_video_id(url)
        # Verify it either returns 11-char slice or None, and never crashes
        self.assertTrue(vid is None or len(vid) == 11)


class TestAdversarialWebScraping(unittest.TestCase):
    """Stress-tests for fetch_web_page_content with server errors, slow streams, and hostile payloads."""

    def test_01_http_error_codes(self):
        """Verify resilient error handling for 404, 403, 401, 500, 502, 503, 504, 418."""
        error_statuses = [
            (404, "۴۰۴"),
            (403, "۴۰۳"),
            (401, "۴۰۳"),  # 401 is grouped with 403 in bot.py
            (500, "500"),
            (502, "502"),
            (503, "503"),
            (504, "504"),
            (418, "418"),
            (429, "429"),
        ]
        for code, expected_fragment in error_statuses:
            mock_resp = MagicMock(status_code=code)
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
                res, err = asyncio.run(fetch_web_page_content(f"https://example.com/status/{code}"))
                self.assertIsNone(res, f"Status {code} should return None for result")
                self.assertIsNotNone(err, f"Status {code} should return an error message")
                self.assertTrue(
                    expected_fragment in err or str(code) in err,
                    f"Status {code} error message should mention code or fragment: {err}"
                )

    def test_02_network_timeouts_and_connection_failures(self):
        """Verify TimeoutException, ConnectError, ReadTimeout, and SSLError do not crash bot."""
        exceptions_to_test = [
            (httpx.TimeoutException("Client timed out after 10.0s"), "تایم‌اوت"),
            (httpx.ConnectTimeout("Connection timed out"), "تایم‌اوت"),
            (httpx.ReadTimeout("Read timed out"), "تایم‌اوت"),
            (httpx.ConnectError("Failed to resolve domain name"), "اتصال به سرور مقصد برقرار نشد"),
            (Exception("SSL: CERTIFICATE_VERIFY_FAILED"), "خطا در خواندن صفحه وب"),
        ]
        for exc, expected_msg_fragment in exceptions_to_test:
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=exc):
                res, err = asyncio.run(fetch_web_page_content("https://problematic-server.com"))
                self.assertIsNone(res)
                self.assertIsNotNone(err)
                self.assertIn(expected_msg_fragment, err)

    def test_03_non_html_50mb_raw_binary_payload(self):
        """Verify 50MB raw binary file does not crash BeautifulSoup or raise unhandled exceptions."""
        # 50MB of binary garbage
        # Use pseudo-random repeating pattern to avoid allocating 50MB of actual random bytes
        chunk = b"\x00\x01\x02\xFF\xFE\xFD\xAA\x55" * 1024  # 8 KB
        binary_50mb = (chunk * (50 * 1024 // 8)).decode("latin1")  # 50 MB string

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = binary_50mb

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res, err = asyncio.run(fetch_web_page_content("https://example.com/largefile.bin"))
            # Either it extracts text (if any readable characters) or returns a clean friendly error
            # Most important: it MUST NOT crash or raise an unhandled exception.
            self.assertTrue(res is not None or err is not None)
            if res:
                self.assertTrue(len(res["text"]) <= 60000, "Text must be truncated to <= 60000 chars")

    def test_04_empty_html_and_pure_javascript_spa(self):
        """Verify empty pages or JavaScript-only pages return friendly 'no readable text' error."""
        js_only_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Single Page App</title><script src="bundle.js"></script></head>
        <body><div id="root"></div></body>
        </html>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = js_only_html

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res, err = asyncio.run(fetch_web_page_content("https://spa-app.com"))
            self.assertIsNone(res)
            self.assertIn("محتوای متنی قابل استخراجی در این صفحه یافت نشد", err)

    def test_05_deeply_nested_and_malformed_html(self):
        """Verify deeply nested tags (500 levels) and broken HTML tags are handled safely."""
        nested_html = "<div>" * 500 + "<h1>تست تودرتو</h1><p>متن در عمق ۵۰۰ تگ</p>" + "</div>" * 500
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = nested_html

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res, err = asyncio.run(fetch_web_page_content("https://nested.com"))
            self.assertIsNone(err)
            self.assertIsNotNone(res)
            self.assertIn("متن در عمق ۵۰۰ تگ", res["text"])

    def test_06_json_payload_instead_of_html(self):
        """Verify API returning raw JSON string is processed cleanly without unhandled exceptions."""
        json_str = '{"status": "success", "message": "این یک پاسخ وب‌سرویس جیسون است", "items": [1, 2, 3]}'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json_str

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res, err = asyncio.run(fetch_web_page_content("https://api.example.com/data.json"))
            self.assertIsNone(err)
            self.assertIsNotNone(res)
            self.assertIn("این یک پاسخ وب‌سرویس جیسون است", res["text"])


class TestAdversarialYouTubeTranscripts(unittest.TestCase):
    """Stress-tests for fetch_youtube_transcript under YouTube API failure modes."""

    def test_01_transcripts_disabled(self):
        """Verify TranscriptsDisabled exception returns friendly Persian message."""
        mock_api = MagicMock()
        mock_api.fetch.side_effect = Exception("TranscriptsDisabled: Subtitles are disabled for this video.")
        mock_api.list.side_effect = Exception("TranscriptsDisabled: Subtitles are disabled for this video.")

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api):
            text, err = fetch_youtube_transcript("disabled1234")
            self.assertIsNone(text)
            self.assertIn("زیرنویس", err)

    def test_02_video_unavailable_or_private(self):
        """Verify VideoUnavailable / Private video returns clean error."""
        mock_api = MagicMock()
        mock_api.fetch.side_effect = Exception("VideoUnavailable: The video is no longer available.")
        mock_api.list.side_effect = Exception("VideoUnavailable: The video is no longer available.")

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api):
            text, err = fetch_youtube_transcript("unavail1234")
            self.assertIsNone(text)
            self.assertIn("زیرنویس", err)

    def test_03_rate_limited_or_ip_blocked(self):
        """Verify TooManyRequests / IP block is caught without crash."""
        mock_api = MagicMock()
        mock_api.fetch.side_effect = Exception("TooManyRequests: YouTube is blocking requests from this IP.")
        mock_api.list.side_effect = Exception("TooManyRequests: YouTube is blocking requests from this IP.")

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api):
            text, err = fetch_youtube_transcript("ratelimit12")
            self.assertIsNone(text)
            self.assertIn("زیرنویس", err)

    def test_04_corrupted_transcript_items(self):
        """Verify non-standard / corrupted transcript item formats don't raise KeyError or AttributeError."""
        # Mix of dict with text, dict without text, string, object with text, None
        class FakeSnippet:
            def __init__(self, text):
                self.text = text

        corrupted_data = [
            {"text": "بخش اول متن", "start": 0.0},
            {"no_text_key": "some value"},
            FakeSnippet("بخش سوم شیء"),
            None,
            "",
            {"text": ""},
            {"text": "   "},
            {"text": "بخش نهایی متن"},
        ]
        mock_api = MagicMock()
        mock_api.fetch.return_value = corrupted_data

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api):
            text, err = fetch_youtube_transcript("corrupted12")
            self.assertIsNone(err)
            self.assertIsNotNone(text)
            self.assertIn("بخش اول متن", text)
            self.assertIn("بخش سوم شیء", text)
            self.assertIn("بخش نهایی متن", text)


class TestAdversarialEndToEndBotResilience(unittest.TestCase):
    """Stress-tests for fetch_and_summarize_url and handle_message under adverse conditions."""

    def setUp(self):
        self.test_db_path = f"test_challenger_m3_{uuid.uuid4().hex[:8]}.db"
        self.db = Database(self.test_db_path)
        bot.db = self.db
        self.user_id = "987654321"
        self.db.get_or_create_user(self.user_id)
        self.chat = self.db.get_active_chat(self.user_id)

    def tearDown(self):
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
        except Exception:
            pass

    def test_01_fetch_and_summarize_url_with_404_page(self):
        """fetch_and_summarize_url on 404 returns clean Persian error message with URL reference."""
        mock_404 = MagicMock(status_code=404)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_404):
            summary = asyncio.run(fetch_and_summarize_url("https://example.com/not-existing-page", user_id=self.user_id))
            self.assertIn("۴۰۴", summary)
            self.assertIn("https://example.com/not-existing-page", summary)

    def test_02_fetch_and_summarize_url_with_disabled_youtube_subtitles(self):
        """fetch_and_summarize_url on video with disabled subs returns clean Persian explanation."""
        mock_api = MagicMock()
        mock_api.fetch.side_effect = Exception("TranscriptsDisabled")
        mock_api.list.side_effect = Exception("NoTranscriptFound")

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api):
            summary = asyncio.run(fetch_and_summarize_url("https://youtu.be/no_subtitles", user_id=self.user_id))
            self.assertIn("استخراج زیرنویس یا رونوشت ویدیوی یوتیوب امکان‌پذیر نبود", summary)
            self.assertIn("https://youtu.be/no_subtitles", summary)

    def test_03_fetch_and_summarize_url_gemini_503_recovery(self):
        """When Gemini API completely fails on summarization, returns friendly error instead of crashing."""
        fake_page = {
            "url": "https://example.com/article",
            "title": "مقاله نمونه",
            "meta_description": "توضیح",
            "text": "متن تستی برای خلاصه سازی",
        }
        with patch("bot.fetch_web_page_content", new_callable=AsyncMock, return_value=(fake_page, None)), \
             patch("bot.generate_chat_response", new_callable=AsyncMock, side_effect=Exception("503 UNAVAILABLE: Model overloaded")), \
             patch("bot.generate_multimodal_response", new_callable=AsyncMock, side_effect=Exception("503 UNAVAILABLE: Model overloaded")):
            summary = asyncio.run(fetch_and_summarize_url("https://example.com/article", user_id=self.user_id))
            self.assertIn("⚠️ خلاصه کردن محتوا با خطا مواجه شد", summary)
            self.assertIn("503 UNAVAILABLE", summary)

    def test_04_handle_message_adverse_url_integration(self):
        """Verify handle_message processes adverse URLs and replies cleanly without raising exceptions."""
        mock_msg = AsyncMock(spec=Message)
        mock_msg.text = "این صفحه ۴۰۴ رو چک کن: https://example.com/broken-page"
        mock_msg.reply_text = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.effective_user = MagicMock(spec=User, id=self.user_id)
        mock_update.effective_chat = MagicMock(spec=Chat, id=self.user_id)
        mock_update.message = mock_msg

        mock_ctx = MagicMock()
        mock_ctx.bot.send_chat_action = AsyncMock()

        mock_404 = MagicMock(status_code=404)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_404):
            asyncio.run(handle_message(mock_update, mock_ctx))
            self.assertTrue(mock_msg.reply_text.called)

    def test_05_handle_message_multiple_urls(self):
        """Message containing multiple URLs processes the first without crashing or hanging."""
        mock_msg = AsyncMock(spec=Message)
        mock_msg.text = "دو تا لینک دارم: https://site1.com/a و https://site2.com/b هر دو رو ببین."
        mock_msg.reply_text = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.effective_user = MagicMock(spec=User, id=self.user_id)
        mock_update.effective_chat = MagicMock(spec=Chat, id=self.user_id)
        mock_update.message = mock_msg

        mock_ctx = MagicMock()
        mock_ctx.bot.send_chat_action = AsyncMock()

        fake_summary = "📌 خلاصه مقاله اول: نکات مهم..."
        with patch("bot.fetch_and_summarize_url", new_callable=AsyncMock, return_value=fake_summary):
            asyncio.run(handle_message(mock_update, mock_ctx))
            self.assertTrue(mock_msg.reply_text.called)


if __name__ == "__main__":
    unittest.main()
