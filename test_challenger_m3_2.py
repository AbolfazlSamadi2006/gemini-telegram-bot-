"""
Adversarial Stress Test Suite for Milestone 3 Requirement R5:
Dynamic File Generation & Export (Excel, PDF, Matplotlib Charts)
Empirical Challenger M3-2
"""

import io
import os
import sys
import time
import math
import unittest
import openpyxl
import pymupdf

# Import targets from bot
import bot
from bot import (
    generate_excel_report,
    generate_pdf_document,
    generate_chart,
    reshape_persian_text,
    setup_pdf_fonts,
    get_persian_font_path,
)

class TestR5AdversarialExcel(unittest.TestCase):
    """Adversarial stress-testing of generate_excel_report"""

    def test_01_empty_dataset(self):
        """Test with completely empty list."""
        bio = generate_excel_report([], title="گزارش خالی")
        self.assertIsInstance(bio, io.BytesIO)
        self.assertEqual(bio.tell(), 0, "Buffer seek position must be 0")
        content = bio.getvalue()
        self.assertTrue(len(content) > 0, "Buffer must not be empty")
        self.assertTrue(content.startswith(b"PK\x03\x04"), "Must be a valid zip / xlsx file")
        self.assertTrue(bio.name.endswith(".xlsx"))
        
        # Load and verify structure
        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        self.assertTrue(ws.sheet_view.rightToLeft, "Sheet must be RTL")
        self.assertEqual(ws.cell(row=1, column=1).value, "پیام")

    def test_02_empty_dict_row(self):
        """Test with a row containing an empty dict: [{}]"""
        try:
            bio = generate_excel_report([{}], title="دیکت خالی")
            self.assertIsInstance(bio, io.BytesIO)
            self.assertEqual(bio.tell(), 0)
            wb = openpyxl.load_workbook(bio)
            ws = wb.active
            self.assertTrue(ws.sheet_view.rightToLeft)
        except Exception as e:
            self.fail(f"generate_excel_report([{{}}]) crashed with: {e}")

    def test_03_formula_injections_and_unicode(self):
        """Test with formula injections, emojis, RTL unicode, ZWNJ, and verify cell data_types."""
        injection_data = [
            {
                "نام": "کاربر ۱",
                "فرمول_۱": "=cmd|' /C calc'!A0",
                "فرمول_۲": "-5+10",
                "فرمول_۳": "+cmd|' /C notepad'!A0",
                "فرمول_۴": "@SUM(10, 20)",
                "هایپرلینک": '=HYPERLINK("http://attacker.com", "کلیک کنید")',
                "کاراکترهای_خاص": "<>&\"'\\/\n\r\t!@#$%^&*()",
                "متن_فارسی_پیچیده": "تست نیم‌فاصله\u200cها و حرکات: اعراب‌گذاری عَلَیْهِ",
                "ایموجی": "🚀📊🔥💡💻🎉",
                "مقدار_عددی": 123456.789,
                "تهی": None
            }
        ]

        bio = generate_excel_report(injection_data, title="گزارش_تزریق_فرمول")
        self.assertIsInstance(bio, io.BytesIO)
        self.assertEqual(bio.tell(), 0)
        self.assertTrue(bio.getvalue().startswith(b"PK\x03\x04"))

        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        self.assertTrue(ws.sheet_view.rightToLeft)
        # Check how openpyxl stored the formula injection strings:
        formula_cell = ws.cell(row=2, column=2)
        print(f"\n[VULN CHECK EXCEL] Cell formula_1 value: {formula_cell.value!r}, data_type: {formula_cell.data_type!r}")
        # Note: if data_type == 'f', Excel executes it as a formula. If data_type == 's', it is safe string.

    def test_03b_control_characters_resilience(self):
        """Test with ASCII control characters (\x00, \x01, \x02) which openpyxl rejects unless sanitized."""
        ctrl_data = [
            {"پیام": "متن با بایت خالی \x00\x01\x02 و تب \t"}
        ]
        bio = generate_excel_report(ctrl_data, title="کنترل_بایت")
        self.assertEqual(bio.tell(), 0)

    def test_04_missing_and_disjoint_keys(self):
        """Test with rows that have inconsistent keys."""
        data = [
            {"الف": 1, "ب": 2},
            {"الف": 10},
            {"ج": 30, "ب": 20},
            {}
        ]
        bio = generate_excel_report(data, title="ناهمگون")
        self.assertEqual(bio.tell(), 0)
        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        self.assertEqual(ws.max_row, 5) # 1 header + 4 rows

    def test_05_massive_dataset_10k_rows_50_cols(self):
        """Stress-test generate_excel_report with 10,000 rows and 50 columns (500,000 cells)."""
        num_rows = 10000
        num_cols = 50
        headers = [f"ستون_{i}" for i in range(num_cols)]
        
        print(f"\n[STRESS EXCEL] Generating dataset: {num_rows} rows x {num_cols} cols = {num_rows*num_cols} cells...")
        start_t = time.time()
        
        # Build generator / row template
        row_template = {h: f"داده_{idx}" for idx, h in enumerate(headers)}
        row_template[headers[0]] = 12345
        row_template[headers[1]] = "تست فارسی"
        
        # Pre-allocate rows list
        data = [dict(row_template) for _ in range(num_rows)]
        gen_time = time.time() - start_t
        print(f"[STRESS EXCEL] Dataset prepped in {gen_time:.2f}s. Calling generate_excel_report...")

        start_call = time.time()
        bio = generate_excel_report(data, title="کلان_داده_اکسل")
        elapsed = time.time() - start_call
        print(f"[STRESS EXCEL] generate_excel_report completed in {elapsed:.2f}s. Buffer size: {len(bio.getvalue())/1024/1024:.2f} MB")

        self.assertEqual(bio.tell(), 0)
        self.assertTrue(len(bio.getvalue()) > 100000, "Output file must be substantial in size")
        self.assertTrue(bio.getvalue().startswith(b"PK\x03\x04"))
        
        # Verify openpyxl can read it back without crashing
        start_read = time.time()
        wb = openpyxl.load_workbook(bio)
        ws = wb.active
        self.assertEqual(ws.sheet_view.rightToLeft, True)
        print(f"[STRESS EXCEL] Read-back verified in {time.time() - start_read:.2f}s.")


class TestR5AdversarialPDF(unittest.TestCase):
    """Adversarial stress-testing of generate_pdf_document"""

    def test_01_empty_and_whitespace_input(self):
        """Test with empty string and whitespace-only text."""
        bio1 = generate_pdf_document(title="", text="")
        self.assertIsInstance(bio1, io.BytesIO)
        self.assertEqual(bio1.tell(), 0)
        self.assertTrue(bio1.getvalue().startswith(b"%PDF-"))
        doc1 = pymupdf.open(stream=bio1.getvalue(), filetype="pdf")
        self.assertGreaterEqual(len(doc1), 1)

        bio2 = generate_pdf_document(title="   ", text="   \n\n\t\n   ")
        self.assertEqual(bio2.tell(), 0)
        self.assertTrue(bio2.getvalue().startswith(b"%PDF-"))

    def test_02_xml_special_characters_and_code_blocks(self):
        """Test with HTML/XML reserved chars (<, >, &, \", ') which often break ReportLab Paragraphs."""
        malicious_text = (
            "### بررسی امنیت کد:\n"
            "اگر شرط `x < 10 && y > 20` برقرار باشد، خروجی `<script>alert('XSS')</script>` خواهد بود.\n"
            "فرمول ریاضی: a < b و c > d و e & f\n"
            "تگ‌های نامعتبر: <bold> <invalid_tag> <123>\n"
            "تگ‌های بدون بسته شدن: <b>این متن بسته نمی‌شود\n"
            "کاراکترهای خاص: & &amp; &lt; &gt; &quot; &#39;\n"
            "```python\n"
            "def test(a < b):\n"
            "    return a & b\n"
            "```\n"
        )
        try:
            bio = generate_pdf_document(title="تست کاراکترهای XML & HTML < >", text=malicious_text)
            self.assertIsInstance(bio, io.BytesIO)
            self.assertEqual(bio.tell(), 0)
            self.assertTrue(bio.getvalue().startswith(b"%PDF-"))
            doc = pymupdf.open(stream=bio.getvalue(), filetype="pdf")
            self.assertGreaterEqual(len(doc), 1)
        except Exception as e:
            self.fail(f"generate_pdf_document failed on XML/HTML special characters: {e}")

    def test_03_persian_poetry_with_zwnj_and_complex_diacritics(self):
        """Test with Persian poetry, ZWNJ, complex Arabic harakat and Quranic symbols."""
        poetry = (
            "# غزلیات شمس و حافظ با اعراب و نیم‌فاصله\n"
            "بشنو این نِی چون حکایت می‌کند\n"
            "از جُدایی‌ها شکایت می‌کند:\n"
            "«کَزْ نِیِسْتان تا مَرا بُبْریده‌اند\n"
            "دَر نَفیرَم مَرْد و زَن نالیده‌اند»\n\n"
            "### بیت با نشانه‌های ویژه:\n"
            "• کلماتی مانند: بی‌خانمان، خانه‌به‌دوش، دانش‌آموزان، می‌روند، آمده‌اند\n"
            "• حرکات ترکیبی: تَشْدیدّ، تَنْوینِ نَصْبْ ً، تَنْوینِ رَفْعْ ٌ، تَنْوینِ جَرّ ٍ\n"
            "• نمادهای قرآنی: ۖ ۗ ۚ ۛ ۜ ۞ ۟ ۠ ۡ ۢ ۣ\n"
            "• اعداد فارسی: ۰۱۲۳۴۵۶۷۸۹ و ارزشی: ۱,۲۳۴,۵۶۷ ریال\n"
        )
        bio = generate_pdf_document(title="دیوان اشعار و اعراب‌گذاری", text=poetry)
        self.assertEqual(bio.tell(), 0)
        self.assertTrue(bio.getvalue().startswith(b"%PDF-"))
        doc = pymupdf.open(stream=bio.getvalue(), filetype="pdf")
        self.assertGreaterEqual(len(doc), 1)

    def test_04_mixed_bidi_technical_document(self):
        """Test with heavily mixed BiDi (Persian sentences embedded with English identifiers, URLs, code)."""
        mixed_text = (
            "# مستند فنی سیستم API Gateway\n"
            "برای اتصال به سرور از آدرس https://api.example.com/v1/auth/token?client_id=12345&redirect_uri=https://bot.me استفاده کنید.\n"
            "• متغیر `MAX_CONNECTIONS = 5000` و `TIMEOUT = 10.5` ثانیه تنظیم شود.\n"
            "• دستور اجرا: `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4`\n"
            "خطای ۵۰۳ یعنی `HTTP 503 SERVICE UNAVAILABLE` که باید به مدت ۳۰ ثانیه retry شود.\n"
            "آدرس IP کلاینت: 192.168.1.100:8080 در شبکه داخلی.\n"
        )
        bio = generate_pdf_document(title="مستند BiDi", text=mixed_text)
        self.assertEqual(bio.tell(), 0)
        self.assertTrue(bio.getvalue().startswith(b"%PDF-"))
        doc = pymupdf.open(stream=bio.getvalue(), filetype="pdf")
        self.assertGreaterEqual(len(doc), 1)

    def test_05_long_continuous_unbroken_string(self):
        """Test with 500+ unbroken characters without whitespace (testing wrapping)."""
        unbroken_text = (
            "یک_رشته_خیلی_خیلی_طولانی_بدون_فاصله_" * 30 + "\n"
            "A" * 500 + "\n"
        )
        try:
            bio = generate_pdf_document(title="رشته ممتد", text=unbroken_text)
            self.assertEqual(bio.tell(), 0)
            self.assertTrue(bio.getvalue().startswith(b"%PDF-"))
        except Exception as e:
            self.fail(f"generate_pdf_document failed on unbroken long string: {e}")

    def test_06_massive_100_page_pdf(self):
        """Stress-test generate_pdf_document with massive text creating 100+ pages."""
        print("\n[STRESS PDF] Generating 100+ page PDF document text...")
        lines = []
        for i in range(1, 4500):
            if i % 40 == 0:
                lines.append(f"# سرفصل بخش {i // 40}: تحلیل پیشرفته و هوش مصنوعی")
            elif i % 5 == 0:
                lines.append(f"• نکته کلیدی شماره {i}: بررسی الگوریتم با مقادیر x={i*2.5} و مدل BiDi فارسی.")
            else:
                lines.append(f"این سطر آزمایشی شماره {i} برای اعتبارسنجی مقیاس‌پذیری ساخت مستندات پی‌دی‌اف چندصفحه‌ای در سیستم است.")

        full_text = "\n".join(lines)
        print(f"[STRESS PDF] Total input characters: {len(full_text):,}. Invoking generate_pdf_document...")
        start_t = time.time()
        bio = generate_pdf_document(title="کتابچه راهنمای جامع ۱۰۰ صفحه‌ای", text=full_text)
        elapsed = time.time() - start_t
        print(f"[STRESS PDF] generate_pdf_document finished in {elapsed:.2f}s. Buffer size: {len(bio.getvalue())/1024/1024:.2f} MB")

        self.assertEqual(bio.tell(), 0)
        self.assertTrue(bio.getvalue().startswith(b"%PDF-"))
        
        doc = pymupdf.open(stream=bio.getvalue(), filetype="pdf")
        page_count = len(doc)
        print(f"[STRESS PDF] Verified PDF page count: {page_count} pages.")
        self.assertGreaterEqual(page_count, 100, f"Expected at least 100 pages, got {page_count}")


class TestR5AdversarialChart(unittest.TestCase):
    """Adversarial stress-testing of generate_chart"""

    def test_01_empty_data(self):
        """Test generate_chart with empty dict: {}"""
        try:
            bio_bar = generate_chart({}, chart_type="bar")
            self.assertEqual(bio_bar.tell(), 0)
            self.assertTrue(bio_bar.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart({{}}, 'bar') crashed with: {e}")

        try:
            bio_pie = generate_chart({}, chart_type="pie")
            self.assertEqual(bio_pie.tell(), 0)
            self.assertTrue(bio_pie.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart({{}}, 'pie') crashed with: {e}")

        try:
            bio_line = generate_chart({}, chart_type="line")
            self.assertEqual(bio_line.tell(), 0)
            self.assertTrue(bio_line.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart({{}}, 'line') crashed with: {e}")

    def test_02_invalid_chart_types_fallback(self):
        """Test fallback on unsupported chart types."""
        data = {"A": 10, "B": 20}
        invalid_types = ["scatter", "histogram", "radar", "donut", "3D_surface", "", None, 12345]
        for ctype in invalid_types:
            try:
                bio = generate_chart(data, chart_type=ctype)
                self.assertEqual(bio.tell(), 0)
                self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))
            except Exception as e:
                self.fail(f"generate_chart with invalid chart_type '{ctype}' crashed with: {e}")

    def test_03_negative_values(self):
        """Test bar, line, and pie charts with negative values."""
        neg_data = {"فروردین": -150, "اردیبهشت": 200, "خرداد": -50, "تیر": 80}
        
        # Bar chart with negatives
        try:
            bio_bar = generate_chart(neg_data, chart_type="bar")
            self.assertEqual(bio_bar.tell(), 0)
            self.assertTrue(bio_bar.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart bar with negative values crashed: {e}")

        # Line chart with negatives
        try:
            bio_line = generate_chart(neg_data, chart_type="line")
            self.assertEqual(bio_line.tell(), 0)
            self.assertTrue(bio_line.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart line with negative values crashed: {e}")

        # Pie chart with negatives (Matplotlib pie() does not support negative values natively)
        try:
            bio_pie = generate_chart(neg_data, chart_type="pie")
            self.assertEqual(bio_pie.tell(), 0)
            self.assertTrue(bio_pie.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart pie with negative values crashed: {e}")

    def test_04_100_plus_categories(self):
        """Stress-test chart with 100+ categories."""
        huge_cat_data = {f"دسته‌بندی_{i}": i * 1.5 for i in range(120)}
        huge_cat_data["title"] = "نمودار با ۱۲۰ دسته‌بندی"
        
        print("\n[STRESS CHART] Testing 120 categories in bar, line, pie...")
        for ctype in ["bar", "line", "pie"]:
            start_t = time.time()
            bio = generate_chart(huge_cat_data, chart_type=ctype)
            elapsed = time.time() - start_t
            self.assertEqual(bio.tell(), 0)
            self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))
            print(f"[STRESS CHART] {ctype} with 120 categories rendered in {elapsed:.2f}s.")

    def test_05_extreme_values(self):
        """Test with extreme numbers: 1e12, 1e-12, all zeros."""
        data_huge = {"الف": 1e12, "ب": 5.5e12, "ج": 2e11}
        bio = generate_chart(data_huge, chart_type="bar")
        self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))

        data_tiny = {"الف": 1e-9, "ب": 2.5e-9, "ج": 0.5e-9}
        bio = generate_chart(data_tiny, chart_type="line")
        self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))

        data_zeros = {"الف": 0, "ب": 0, "ج": 0}
        bio = generate_chart(data_zeros, chart_type="bar")
        self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))

    def test_06_non_numeric_and_malformed_values(self):
        """Test behavior when labels and non-numeric values are passed."""
        malformed = {
            "labels": ["الف", "ب", "ج"],
            "values": ["غیر_عددی", None, "100"]
        }
        try:
            bio = generate_chart(malformed, chart_type="bar")
            self.assertTrue(bio.getvalue().startswith(b"\x89PNG"))
        except Exception as e:
            self.fail(f"generate_chart crashed on non-numeric values: {e}")


if __name__ == "__main__":
    unittest.main()
