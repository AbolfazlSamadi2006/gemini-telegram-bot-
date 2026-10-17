"""
Comprehensive Offline Test Suite for Milestone 3 (test_m3.py)
Covering:
- R4: Web Page & YouTube Video Extraction & Summarization
- R5: Dynamic File Generation & Export (Excel, PDF with Persian BiDi, Matplotlib Charts)
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
    extract_urls,
    extract_youtube_video_id,
    fetch_youtube_transcript,
    fetch_web_page_content,
    fetch_and_summarize_url,
    get_persian_font_path,
    setup_pdf_fonts,
    reshape_persian_text,
    generate_excel_report,
    generate_pdf_document,
    generate_chart,
    extract_markdown_table,
    detect_export_intent,
    perform_chat_export,
    export_chat,
    callback_handler,
    handle_message,
)
import openpyxl
from telegram import Update, Message, User, Chat, CallbackQuery, InlineKeyboardMarkup


class TestMilestone3(unittest.TestCase):
    def setUp(self):
        self.test_db_path = f"test_m3_{uuid.uuid4().hex[:8]}.db"
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
    # R4: Web Page & YouTube Video Extraction & Summarization Tests
    # =========================================================================

    def test_r4_extract_urls_various_formats(self):
        """Verifies robust extraction of HTTP and HTTPS URLs across diverse formats."""
        # Standard web and YouTube links
        text1 = "لطفاً این دو لینک رو چک کن: https://example.com/article?id=42 و https://youtu.be/dQw4w9WgXcQ ممنون!"
        urls1 = extract_urls(text1)
        self.assertEqual(len(urls1), 2)
        self.assertEqual(urls1[0], "https://example.com/article?id=42")
        self.assertEqual(urls1[1], "https://youtu.be/dQw4w9WgXcQ")

        # URLs followed by punctuation
        text2 = "مقاله عالی در https://en.wikipedia.org/wiki/Python_(programming_language). همچنین وبسایت http://test.org:8080/path/to/page, حتما ببین!"
        urls2 = extract_urls(text2)
        self.assertIn("https://en.wikipedia.org/wiki/Python_(programming_language)", urls2)
        self.assertIn("http://test.org:8080/path/to/page", urls2)

        # Unbalanced trailing parenthesis stripped
        text3 = "لینک مستندات: (https://docs.python.org/3/)"
        urls3 = extract_urls(text3)
        self.assertEqual(urls3[0], "https://docs.python.org/3/")

        # Empty or text with no URLs
        self.assertEqual(extract_urls("این یک پیام بدون لینک است."), [])
        self.assertEqual(extract_urls(""), [])
        self.assertEqual(extract_urls(None), [])

    def test_r4_extract_youtube_video_id(self):
        """Verifies accurate extraction of 11-char YouTube video ID across all URL styles."""
        cases = [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=30s", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("http://youtu.be/1234567890a", "1234567890a"),
        ]
        for url, expected_id in cases:
            self.assertEqual(extract_youtube_video_id(url), expected_id, f"Failed on URL: {url}")

        # Non-YouTube or invalid URLs
        self.assertIsNone(extract_youtube_video_id("https://vimeo.com/12345678"))
        self.assertIsNone(extract_youtube_video_id("https://google.com"))
        self.assertIsNone(extract_youtube_video_id(""))
        self.assertIsNone(extract_youtube_video_id(None))

    def test_r4_fetch_web_page_content_success(self):
        """Verifies HTML text, title, and meta description extraction with script removal."""
        fake_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>هوش مصنوعی و یادگیری ماشین</title>
            <meta name="description" content="راهنمای جامع هوش مصنوعی و کاربردهای آن">
            <script>var x = 10; alert('spam');</script>
            <style>body { color: red; }</style>
        </head>
        <body>
            <header><nav>منو</nav></header>
            <article>
                <h1>مقدمه به هوش مصنوعی</h1>
                <p>هوش مصنوعی شاخه‌ای گسترده از علوم رایانه است که سیستم‌های هوشمند می‌سازد.</p>
                <p>یادگیری ماشین زیرمجموعه‌ای اساسی از هوش مصنوعی محسوب می‌شود.</p>
            </article>
            <footer>کپی‌رایت ۲۰۲۶</footer>
        </body>
        </html>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = fake_html

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res, err = asyncio.run(fetch_web_page_content("https://example.com/ai-article"))
            self.assertIsNone(err)
            self.assertIsNotNone(res)
            self.assertEqual(res["title"], "هوش مصنوعی و یادگیری ماشین")
            self.assertEqual(res["meta_description"], "راهنمای جامع هوش مصنوعی و کاربردهای آن")
            self.assertIn("هوش مصنوعی شاخه‌ای گسترده", res["text"])
            self.assertIn("یادگیری ماشین زیرمجموعه‌ای اساسی", res["text"])
            # Ensure script, style, header, footer contents were scrubbed
            self.assertNotIn("var x = 10", res["text"])
            self.assertNotIn("کپی‌رایت", res["text"])

    def test_r4_fetch_web_page_content_resilience_404_and_paywall(self):
        """Verifies graceful handling of HTTP 404 and 403 paywalls without crashing."""
        # 404 Not Found
        mock_404 = MagicMock(status_code=404)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_404):
            res, err = asyncio.run(fetch_web_page_content("https://example.com/not-found"))
            self.assertIsNone(res)
            self.assertIn("۴۰۴", err)

        # 403 Forbidden / Paywall
        mock_403 = MagicMock(status_code=403)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_403):
            res, err = asyncio.run(fetch_web_page_content("https://example.com/paywalled"))
            self.assertIsNone(res)
            self.assertIn("۴۰۳", err)

    def test_r4_fetch_web_page_content_timeout_and_network_error(self):
        """Verifies graceful resilience against HTTP timeouts and connection drops."""
        import httpx

        # Timeout Exception
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.TimeoutException("Timeout")):
            res, err = asyncio.run(fetch_web_page_content("https://slow-site.com"))
            self.assertIsNone(res)
            self.assertIn("تایم‌اوت", err)

        # Connect Error
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.ConnectError("Connection refused")):
            res, err = asyncio.run(fetch_web_page_content("https://dead-domain.xyz"))
            self.assertIsNone(res)
            self.assertIn("اتصال به سرور مقصد برقرار نشد", err)

    def test_r4_fetch_youtube_transcript_success(self):
        """Verifies YouTube transcript retrieval and concatenation via youtube-transcript-api."""
        fake_snippets = [
            {"text": "سلام به همه بینندگان عزیز.", "start": 0.0, "duration": 2.5},
            {"text": "در این ویدیو می‌خواهیم پایتون را آموزش دهیم.", "start": 2.5, "duration": 3.0},
            {"text": "پایتون یک زبان بسیار قدرتمند و آسان است.", "start": 5.5, "duration": 3.2},
        ]
        # Mock instance fetch method
        mock_api_instance = MagicMock()
        mock_api_instance.fetch.return_value = fake_snippets

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api_instance):
            transcript_text, err = fetch_youtube_transcript("dQw4w9WgXcQ")
            self.assertIsNone(err)
            self.assertIsNotNone(transcript_text)
            self.assertIn("سلام به همه بینندگان عزیز", transcript_text)
            self.assertIn("پایتون یک زبان بسیار قدرتمند", transcript_text)

    def test_r4_fetch_youtube_transcript_no_transcripts(self):
        """Verifies friendly error handling when video has disabled or unavailable captions."""
        mock_api_instance = MagicMock()
        mock_api_instance.fetch.side_effect = Exception("TranscriptsDisabled: Subtitles are disabled for this video")
        mock_api_instance.list.side_effect = Exception("NoTranscriptFound")

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api_instance):
            transcript_text, err = fetch_youtube_transcript("no_subs_video")
            self.assertIsNone(transcript_text)
            self.assertIn("زیرنویس", err)

    def test_r4_fetch_and_summarize_url_web(self):
        """Verifies end-to-end fetch_and_summarize_url on web URLs invoking Gemini synthesis."""
        fake_page = {
            "url": "https://tech.io/deep-learning",
            "title": "راهنمای شبکه‌های عصبی عمیق",
            "meta_description": "بررسی جامع یادگیری عمیق",
            "text": "شبکه‌های عصبی عمیق تحول عظیمی در پردازش تصویر و هوش مصنوعی ایجاد کرده‌اند."
        }
        fake_summary = (
            "📌 **خلاصه اجرایی مقاله: راهنمای شبکه‌های عصبی عمیق**\n\n"
            "💡 **نکات کلیدی (Key Takeaways):**\n"
            "• شبکه‌های عصبی عمیق اساس هوش مصنوعی مدرن هستند.\n"
            "• تحول بنیادین در بینایی ماشین و پردازش زبان پدید آورده‌اند.\n\n"
            "🎯 **نتیجه‌گیری:** یادگیری عمیق آینده صنایع را هدایت می‌کند."
        )

        with patch("bot.fetch_web_page_content", new_callable=AsyncMock, return_value=(fake_page, None)), \
             patch("bot.generate_chat_response", new_callable=AsyncMock, return_value=(fake_summary, None, None, None)):
            summary = asyncio.run(fetch_and_summarize_url("https://tech.io/deep-learning", user_id=self.user_id))
            self.assertIn("خلاصه اجرایی مقاله", summary)
            self.assertIn("نکات کلیدی", summary)
            self.assertIn("Key Takeaways", summary)

    def test_r4_fetch_and_summarize_url_youtube(self):
        """Verifies end-to-end fetch_and_summarize_url on YouTube URLs invoking Gemini synthesis."""
        fake_transcript = "در این ویدیوی آموزشی، ۵ نکته طلایی برای یادگیری برنامه‌نویسی پایتون بیان می‌شود..."
        fake_summary = (
            "🎥 **خلاصه ویدیوی یوتیوب**\n\n"
            "💡 **نکات کلیدی و آموزه‌های اصلی:**\n"
            "• تمرین روزانه کدنویسی کلید موفقیت است.\n"
            "• پروژه‌محور یاد بگیرید."
        )

        with patch("bot.fetch_youtube_transcript", return_value=(fake_transcript, None)), \
             patch("bot.generate_chat_response", new_callable=AsyncMock, return_value=(fake_summary, None, None, None)):
            summary = asyncio.run(fetch_and_summarize_url("https://youtu.be/dQw4w9WgXcQ", user_id=self.user_id))
            self.assertIn("خلاصه ویدیوی یوتیوب", summary)
            self.assertIn("نکات کلیدی", summary)

    def test_r4_handle_message_url_detection(self):
        """Verifies handle_message automatically detects URLs and delivers structured summaries."""
        mock_msg = AsyncMock(spec=Message)
        mock_msg.text = "این مقاله رو برام خلاصه کن: https://wikipedia.org/wiki/AI"
        mock_msg.reply_text = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.effective_user = MagicMock(spec=User, id=12345678)
        mock_update.effective_chat = MagicMock(spec=Chat, id=999)
        mock_update.message = mock_msg

        mock_ctx = MagicMock()
        mock_ctx.bot.send_chat_action = AsyncMock()

        fake_summary = "📌 خلاصه مقاله هوش مصنوعی: سیستم‌های هوشمند..."
        with patch("bot.fetch_and_summarize_url", new_callable=AsyncMock, return_value=fake_summary):
            asyncio.run(handle_message(mock_update, mock_ctx))
            # ProgressTracker and final message should have been called
            self.assertTrue(mock_msg.reply_text.called)

    # =========================================================================
    # R5: Dynamic File Generation & Export (Excel, PDF, Charts) Tests
    # =========================================================================

    def test_r5_generate_excel_report_structure_and_styling(self):
        """Verifies in-memory Excel .xlsx generation via openpyxl with zebra striping and RTL view."""
        data = [
            {"ردیف": 1, "نام ابزار": "Gemini 3.8", "امتیاز": 98, "وضعیت": "فعال"},
            {"ردیف": 2, "نام ابزار": "Python Code Execution", "امتیاز": 99, "وضعیت": "فعال"},
            {"ردیف": 3, "نام ابزار": "Imagen 3", "امتیاز": 95, "وضعیت": "فعال"},
        ]
        bio = generate_excel_report(data, title="گزارش_تست_هوش_مصنوعی")
        self.assertIsInstance(bio, io.BytesIO)
        self.assertTrue(bio.name.endswith(".xlsx"))

        # Verify ZIP/XLSX magic bytes (PK\x03\x04)
        raw_bytes = bio.getvalue()
        self.assertTrue(raw_bytes.startswith(b"PK\x03\x04"), "Excel file must have valid ZIP/XLSX header")

        # Load back workbook and verify contents and styling
        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        self.assertTrue(ws.sheet_view.rightToLeft, "Sheet must be RTL for Persian")

        # Verify headers
        header_vals = [cell.value for cell in ws[1]]
        self.assertEqual(header_vals, ["ردیف", "نام ابزار", "امتیاز", "وضعیت"])

        # Verify header fill (Dark slate blue #1B4F72)
        h_cell = ws.cell(row=1, column=1)
        self.assertEqual(h_cell.fill.start_color.rgb, "001B4F72")
        self.assertTrue(h_cell.font.bold)

        # Verify data rows count
        self.assertEqual(ws.max_row, 4)
        self.assertEqual(ws.cell(row=2, column=2).value, "Gemini 3.8")
        self.assertEqual(ws.cell(row=3, column=2).value, "Python Code Execution")

        # Verify zebra striping: row 2 (even) vs row 3 (odd)
        cell_r2 = ws.cell(row=2, column=1)
        cell_r3 = ws.cell(row=3, column=1)
        self.assertNotEqual(cell_r2.fill.start_color.rgb, cell_r3.fill.start_color.rgb)

        # Verify column auto-fit
        col_b_width = ws.column_dimensions["B"].width
        self.assertGreater(col_b_width, 15)

    def test_r5_generate_excel_report_empty_data(self):
        """Verifies generate_excel_report handles empty datasets gracefully."""
        bio = generate_excel_report([], title="خالی")
        self.assertIsInstance(bio, io.BytesIO)
        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        self.assertIn("پیام", [cell.value for cell in ws[1]])

    def test_r5_generate_pdf_document_persian_bidi(self):
        """Verifies in-memory PDF generation with ReportLab, font registration, and Persian RTL text."""
        title = "گزارش پیشرفت هوش مصنوعی جمینای"
        body = (
            "این یک مستند رسمی برای تست تولید فایل‌های PDF در بات تلگرام است.\n"
            "• نکته اول: پشتیبانی کامل از رسم‌الخط و متون فارسی راست‌چین (RTL).\n"
            "• نکته دوم: ساختار چندپاراگرافی بدون به‌هم‌ریختگی حروف یا کرش فونت.\n\n"
            "### جمع‌بندی نهایی:\n"
            "تمام تست‌های ماژول با موفقیت کامل پاس شدند."
        )
        bio = generate_pdf_document(title, body)
        self.assertIsInstance(bio, io.BytesIO)
        self.assertTrue(bio.name.endswith(".pdf"))

        pdf_bytes = bio.getvalue()
        # Verify PDF magic bytes (%PDF)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"), "Output must start with valid PDF header")
        self.assertGreater(len(pdf_bytes), 1000, "PDF file size must be non-trivial")

    def test_r5_generate_chart_bar_line_pie(self):
        """Verifies in-memory chart generation (bar, line, pie) returning valid PNG images."""
        chart_data = {
            "title": "آمار عملکرد سیستم",
            "labels": ["پیام‌های دریافتی", "پاسخ‌های جمینای", "کدهای پایتون", "تصاویر ساخته‌شده"],
            "values": [45, 45, 18, 12]
        }

        # 1. Bar Chart
        bar_io = generate_chart(chart_data, chart_type="bar")
        self.assertIsInstance(bar_io, io.BytesIO)
        self.assertTrue(bar_io.getvalue().startswith(b"\x89PNG"), "Must have PNG magic header")

        # 2. Line Chart
        line_io = generate_chart(chart_data, chart_type="line")
        self.assertIsInstance(line_io, io.BytesIO)
        self.assertTrue(line_io.getvalue().startswith(b"\x89PNG"))

        # 3. Pie Chart
        pie_io = generate_chart(chart_data, chart_type="pie")
        self.assertIsInstance(pie_io, io.BytesIO)
        self.assertTrue(pie_io.getvalue().startswith(b"\x89PNG"))

    def test_r5_extract_markdown_table(self):
        """Verifies extraction and parsing of markdown tables into structured dict rows."""
        text_with_table = (
            "در ادامه جدول مقایسه مدل‌ها را مشاهده می‌کنید:\n\n"
            "| نام مدل | سرعت | دقت |\n"
            "| :--- | :---: | ---: |\n"
            "| Gemini 3.8 | عالی | ۹۹٪ |\n"
            "| Gemini 3.6 | بسیار بالا | ۹۵٪ |\n\n"
            "امیدوارم این اطلاعات مفید باشد."
        )
        table = extract_markdown_table(text_with_table)
        self.assertIsNotNone(table)
        self.assertEqual(len(table), 2)
        self.assertEqual(table[0]["نام مدل"], "Gemini 3.8")
        self.assertEqual(table[0]["سرعت"], "عالی")
        self.assertEqual(table[0]["دقت"], "۹۹٪")
        self.assertEqual(table[1]["نام مدل"], "Gemini 3.6")

        # Text without markdown table
        self.assertIsNone(extract_markdown_table("یک متن معمولی بدون هیچ جدولی."))
        self.assertIsNone(extract_markdown_table(""))

    def test_r5_detect_export_intent(self):
        """Verifies natural language export intent classifier across diverse Persian/English phrases."""
        excel_phrases = [
            "خروجی اکسل این گفتگو رو بهم بده",
            "دانلود فایل اکسل لطفا",
            "این جدول رو تبدیل به اکسل کن",
            "دانلود جدول",
            "خروجی excel",
        ]
        for p in excel_phrases:
            self.assertEqual(detect_export_intent(p), "excel", f"Failed for phrase: {p}")

        pdf_phrases = [
            "خروجی pdf لطفا",
            "دانلود فایل پی دی اف",
            "خروجی پی‌دی‌اف این متن رو بده",
            "پی دی اف بده",
        ]
        for p in pdf_phrases:
            self.assertEqual(detect_export_intent(p), "pdf", f"Failed for phrase: {p}")

        chart_phrases = [
            "خروجی نمودار رو برام بفرست",
            "یک نمودار بکش",
            "رسم نمودار آمار",
            "دانلود نمودار",
        ]
        for p in chart_phrases:
            self.assertEqual(detect_export_intent(p), "chart", f"Failed for phrase: {p}")

        # Normal text
        self.assertIsNone(detect_export_intent("سلام چطوری؟ وضعیت آب و هوا چطوره؟"))

    def test_r5_export_chat_command_subcommands(self):
        """Verifies /export command with subcommands: excel, pdf, chart, and interactive default."""
        self.db.add_message(self.active_chat["id"], "user", "سلام")
        self.db.add_message(self.active_chat["id"], "model", "درود! چطور می‌تونم کمکت کنم؟")

        mock_msg = AsyncMock(spec=Message)
        mock_msg.reply_document = AsyncMock()
        mock_msg.reply_photo = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.effective_user = MagicMock(spec=User, id=int(self.user_id))
        mock_update.message = mock_msg

        # 1. /export excel
        mock_ctx_excel = MagicMock(args=["excel"])
        asyncio.run(export_chat(mock_update, mock_ctx_excel))
        self.assertTrue(mock_msg.reply_document.called)
        doc_arg = mock_msg.reply_document.call_args[1]["document"]
        self.assertTrue(doc_arg.name.endswith(".xlsx"))

        # 2. /export pdf
        mock_msg.reset_mock()
        mock_ctx_pdf = MagicMock(args=["pdf"])
        asyncio.run(export_chat(mock_update, mock_ctx_pdf))
        self.assertTrue(mock_msg.reply_document.called)
        doc_arg = mock_msg.reply_document.call_args[1]["document"]
        self.assertTrue(doc_arg.name.endswith(".pdf"))

        # 3. /export chart
        mock_msg.reset_mock()
        mock_ctx_chart = MagicMock(args=["chart"])
        asyncio.run(export_chat(mock_update, mock_ctx_chart))
        self.assertTrue(mock_msg.reply_photo.called or mock_msg.reply_document.called)

        # 4. /export (no args) -> sends text with interactive inline keyboard
        mock_msg.reset_mock()
        mock_ctx_default = MagicMock(args=[])
        asyncio.run(export_chat(mock_update, mock_ctx_default))
        self.assertTrue(mock_msg.reply_document.called)
        markup_arg = mock_msg.reply_document.call_args[1].get("reply_markup")
        self.assertIsInstance(markup_arg, InlineKeyboardMarkup)

    def test_r5_callback_handler_export_actions(self):
        """Verifies callback_handler executes export_action callbacks for excel, pdf, chart."""
        self.db.add_message(self.active_chat["id"], "user", "تست پیام")
        self.db.add_message(self.active_chat["id"], "model", "پاسخ تست")

        mock_query_msg = AsyncMock(spec=Message)
        mock_query_msg.reply_document = AsyncMock()
        mock_query_msg.reply_photo = AsyncMock()

        mock_query = AsyncMock(spec=CallbackQuery)
        mock_query.from_user = MagicMock(id=int(self.user_id))
        mock_query.message = mock_query_msg
        mock_query.answer = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.callback_query = mock_query

        # Test export_action:excel
        mock_query.data = "export_action:excel"
        asyncio.run(callback_handler(mock_update, MagicMock()))
        self.assertTrue(mock_query_msg.reply_document.called)

        # Test export_action:pdf
        mock_query_msg.reset_mock()
        mock_query.data = "export_action:pdf"
        asyncio.run(callback_handler(mock_update, MagicMock()))
        self.assertTrue(mock_query_msg.reply_document.called)

        # Test export_action:chart
        mock_query_msg.reset_mock()
        mock_query.data = "export_action:chart"
        asyncio.run(callback_handler(mock_update, MagicMock()))
        self.assertTrue(mock_query_msg.reply_photo.called or mock_query_msg.reply_document.called)

    def test_r5_handle_message_nlp_export_trigger(self):
        """Verifies natural language message trigger like 'خروجی اکسل' generates and delivers document."""
        self.db.add_message(self.active_chat["id"], "user", "یک جدول بده")
        self.db.add_message(self.active_chat["id"], "model", "| زبان | رتبه |\n| --- | --- |\n| پایتون | ۱ |")

        mock_msg = AsyncMock(spec=Message)
        mock_msg.text = "لطفاً خروجی اکسل این گفتگو رو بده"
        mock_msg.reply_document = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.effective_user = MagicMock(spec=User, id=int(self.user_id))
        mock_update.effective_chat = MagicMock(spec=Chat, id=999)
        mock_update.message = mock_msg

        asyncio.run(handle_message(mock_update, MagicMock()))
        self.assertTrue(mock_msg.reply_document.called)
        doc = mock_msg.reply_document.call_args[1]["document"]
        self.assertTrue(doc.name.endswith(".xlsx"))

    def test_r5_handle_message_nlp_pdf_and_chart_triggers(self):
        """Verifies natural language triggers for PDF ('خروجی pdf') and Chart ('رسم نمودار')."""
        self.db.add_message(self.active_chat["id"], "user", "پیام ۱")
        self.db.add_message(self.active_chat["id"], "model", "پاسخ ۱")

        mock_msg = AsyncMock(spec=Message)
        mock_msg.reply_document = AsyncMock()
        mock_msg.reply_photo = AsyncMock()

        mock_update = MagicMock(spec=Update)
        mock_update.effective_user = MagicMock(spec=User, id=int(self.user_id))
        mock_update.effective_chat = MagicMock(spec=Chat, id=999)
        mock_update.message = mock_msg

        # 1. NLP PDF trigger
        mock_msg.text = "لطفاً خروجی پی‌دی‌اف بده"
        asyncio.run(handle_message(mock_update, MagicMock()))
        self.assertTrue(mock_msg.reply_document.called)
        pdf_doc = mock_msg.reply_document.call_args[1]["document"]
        self.assertTrue(pdf_doc.name.endswith(".pdf"))

        # 2. NLP Chart trigger
        mock_msg.reset_mock()
        mock_msg.text = "رسم نمودار پیام‌ها"
        asyncio.run(handle_message(mock_update, MagicMock()))
        self.assertTrue(mock_msg.reply_photo.called or mock_msg.reply_document.called)

    def test_r4_fetch_and_summarize_url_unreachable_web_resilience(self):
        """Verifies fetch_and_summarize_url handles 404 or connection failures gracefully."""
        mock_404 = MagicMock(status_code=404)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_404):
            summary = asyncio.run(fetch_and_summarize_url("https://example.com/missing-page", user_id=self.user_id))
            self.assertIn("۴۰۴", summary)
            self.assertIn("https://example.com/missing-page", summary)

    def test_r4_fetch_and_summarize_url_youtube_no_transcript_resilience(self):
        """Verifies fetch_and_summarize_url provides clear explanation when YouTube video lacks transcripts."""
        mock_api_instance = MagicMock()
        mock_api_instance.fetch.side_effect = Exception("NoTranscriptFound")
        mock_api_instance.list.side_effect = Exception("NoTranscriptFound")

        with patch("bot.YouTubeTranscriptApi", return_value=mock_api_instance):
            summary = asyncio.run(fetch_and_summarize_url("https://www.youtube.com/watch?v=1234567890a", user_id=self.user_id))
            self.assertIn("استخراج زیرنویس یا رونوشت ویدیوی یوتیوب امکان‌پذیر نبود", summary)
            self.assertIn("1234567890a", summary)

    def test_r5_generate_chart_with_direct_dict(self):
        """Verifies generate_chart accepts direct dict of {label: value} without 'labels'/'values' wrapper."""
        direct_data = {
            "title": "فروش فصل بهار",
            "فروردین": 150,
            "اردیبهشت": 220,
            "خرداد": 310
        }
        bio = generate_chart(direct_data, chart_type="bar")
        self.assertIsInstance(bio, io.BytesIO)
        self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))

    def test_r5_generate_excel_alignment_and_dimensions(self):
        """Verifies numeric columns are centered and text columns are right-aligned in Excel report."""
        mixed_data = [
            {"ردیف": 1, "عنوان": "پروژه آلفا", "امتیاز": 95.5},
            {"ردیف": 2, "عنوان": "پروژه بتا", "امتیاز": 88.0},
        ]
        bio = generate_excel_report(mixed_data, title="پروژه‌ها")
        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        # Cell A2 is integer 1 -> centered
        self.assertEqual(ws.cell(row=2, column=1).alignment.horizontal, "center")
        # Cell B2 is string "پروژه آلفا" -> right
        self.assertEqual(ws.cell(row=2, column=2).alignment.horizontal, "right")
        # Cell C2 is float 95.5 -> centered
        self.assertEqual(ws.cell(row=2, column=3).alignment.horizontal, "center")


if __name__ == "__main__":
    unittest.main()
