"""
Adversarial Stress Test Suite for Milestone 4 Requirement R3:
Persistent Task Scheduling & Reminder Parser (test_challenger_m4_1.py)
Empirical Challenger M4-1

Focus Areas:
1. Persian, Arabic, and mixed numeral strings (۰۱۲۳۴۵۶۷۸۹, ٠١٢٣٤٥٦٧٨٩, mixed ASCII/Persian).
2. Extreme relative durations (0m, 999999h, fractional minutes/hours, negative intervals, integer overflow).
3. Clock time edge cases (midnight 00:00, noon 12:00, 24-hour boundaries, 12-hour AM/PM markers: صبح, عصر, شب, بامداد).
4. Same-day elapsed times rollover (scheduling past time today -> rolls over to tomorrow).
5. Conversational text with complex syntax, quotes, emojis, Persian ZWNJ (\u200c), and multiple numbers.
6. Polymorphic return handling: verify both synchronous and asynchronous evaluation of parse_reminder_input.
7. Concurrency: rapid multi-threaded and asyncio concurrent parser invocations.

100% Offline, Zero External Network Dependencies.
"""

import os
import sys
import re
import asyncio
import datetime
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure offline dummy environment variables
os.environ.setdefault("GEMINI_API_KEY", "dummy_challenger_m4_key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy_challenger_m4_token")

import bot
from bot import (
    TEHRAN_TZ,
    normalize_digits,
    has_reminder_intent,
    clean_reminder_text,
    parse_reminder_input_tier1,
    parse_reminder_input,
    AwaitableTuple,
    Tier2AwaitableResult,
)


class TestM4PersianArabicNumerals(unittest.TestCase):
    """Category 1: Persian, Arabic, and mixed numeral strings."""

    def test_01_persian_and_arabic_full_digits_table(self):
        """Verify full 0-9 normalization for Persian, Arabic, and mixed scripts."""
        persian_str = "۰۱۲۳۴۵۶۷۸۹"
        arabic_str = "٠١٢٣٤٥٦٧٨٩"
        self.assertEqual(normalize_digits(persian_str), "0123456789")
        self.assertEqual(normalize_digits(arabic_str), "0123456789")

        # Mixed Persian + Arabic + ASCII
        mixed_str = "ساعت ۱۰:۳۰ با ۳ تماس و ٥ ایمیل"
        expected = "ساعت 10:30 با 3 تماس و 5 ایمیل"
        self.assertEqual(normalize_digits(mixed_str), expected)

        # Idempotency
        self.assertEqual(normalize_digits(normalize_digits(mixed_str)), expected)

        # Edge cases: None, empty, whitespace
        self.assertEqual(normalize_digits(""), "")
        self.assertEqual(normalize_digits(None), "")
        self.assertEqual(normalize_digits("   "), "   ")

    def test_02_persian_momayyez_decimal_separator(self):
        """
        Adversarial test: Persian decimal separator Momayyez (٫ - U+066B).
        Example: '۲٫۵ ساعت بعد جلسه' should parse as 2.5 hours (150 minutes),
        not fail or misinterpret as 5 hours (300 minutes).
        """
        base_tehran = datetime.datetime(2026, 10, 16, 10, 0, 0, tzinfo=TEHRAN_TZ)
        input_text = "۲\u066b۵ ساعت بعد جلسه"
        res = parse_reminder_input_tier1(input_text, base_tehran)
        self.assertIsNotNone(res, "Failed to parse Persian decimal momayyez")
        due_utc, note = res
        # 10:00 + 2.5 hours = 12:30 Tehran => 09:00 UTC
        self.assertEqual(due_utc, "2026-10-16T09:00:00Z",
                         f"Momayyez 2.5h parsed incorrectly as {due_utc}")
        self.assertNotIn("2٫", note, "Dangling decimal prefix left in reminder text")
        self.assertIn("جلسه", note)


class TestM4ExtremeRelativeDurations(unittest.TestCase):
    """Category 2: Extreme relative durations (0m, 999999h, fractions, negatives, overflows)."""

    def setUp(self):
        # 2026-10-16 14:00:00 Tehran (+03:30) => 2026-10-16 10:30:00 UTC
        self.base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)

    def test_03_zero_duration_relative_delays(self):
        """0m or 0h delay should evaluate to current base time without throwing."""
        res_m = parse_reminder_input_tier1("/remind 0m پیام تست فوری", self.base_tehran)
        self.assertIsNotNone(res_m)
        due_utc_m, note_m = res_m
        self.assertEqual(due_utc_m, "2026-10-16T10:30:00Z")
        self.assertEqual(note_m, "پیام تست فوری")

        res_h = parse_reminder_input_tier1("۰ ساعت دیگه اعلام وضعیت", self.base_tehran)
        self.assertIsNotNone(res_h)
        due_utc_h, note_h = res_h
        self.assertEqual(due_utc_h, "2026-10-16T10:30:00Z")
        self.assertEqual(note_h, "اعلام وضعیت")

    def test_04_fractional_relative_durations(self):
        """Fractional durations: 1.5m, 2.5h, 0.5d."""
        # 1.5 minutes = +90 seconds => 10:31:30 UTC
        res1 = parse_reminder_input_tier1("/remind 1.5m چای دم کشید", self.base_tehran)
        self.assertIsNotNone(res1)
        self.assertEqual(res1[0], "2026-10-16T10:31:30Z")
        self.assertEqual(res1[1], "چای دم کشید")

        # 2.5 hours = +150 minutes => 13:00:00 UTC
        res2 = parse_reminder_input_tier1("2.5 ساعت بعد پایان آزمون", self.base_tehran)
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "2026-10-16T13:00:00Z")
        self.assertEqual(res2[1], "پایان آزمون")

    def test_05_astronomical_duration_within_datetime_range(self):
        """999999h (~114 years) is within datetime.MAXYEAR (9999). Should succeed."""
        res = parse_reminder_input_tier1("/remind 999999h کپسول زمان", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertTrue(due_utc.startswith("2140-"))
        self.assertEqual(note, "کپسول زمان")

    def test_06_overflow_protection_against_unhandled_crash(self):
        """
        Adversarial test: Gigantic duration (e.g. 9999999999d) must NOT crash
        with uncaught OverflowError. It should return None or handle safely.
        """
        try:
            res = parse_reminder_input_tier1("/remind 9999999999d تست سرریز", self.base_tehran)
            # If handled safely, returns None
            self.assertIsNone(res)
        except OverflowError:
            self.fail("parse_reminder_input_tier1 crashed with unhandled OverflowError on huge duration")

    def test_07_negative_intervals_rejection(self):
        """Negative intervals like '/remind -5m' must not silently be scheduled as future."""
        # A negative interval should not be scheduled as +5m
        res = parse_reminder_input_tier1("/remind -5m تست منفی", self.base_tehran)
        if res is not None:
            # If matched, it should not have stripped the minus and scheduled +5m in the future
            self.assertNotEqual(res[0], "2026-10-16T10:35:00Z",
                                "Negative duration '-5m' was incorrectly scheduled as +5 minutes in future")


class TestM4ClockTimeEdgeCases(unittest.TestCase):
    """Category 3: Clock time edge cases (midnight, noon, 24h boundaries, Persian AM/PM markers)."""

    def setUp(self):
        # 2026-10-16 14:00:00 Tehran (+03:30) => 2026-10-16 10:30:00 UTC
        self.base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)

    def test_08_clock_midnight_00_00(self):
        """Midnight 00:00 requested at 14:00 should roll over to tonight at 00:00 (2026-10-16 20:30 UTC)."""
        res = parse_reminder_input_tier1("ساعت 00:00 خاموشی سیستم", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertEqual(due_utc, "2026-10-16T20:30:00Z")
        self.assertEqual(note, "خاموشی سیستم")

    def test_09_clock_noon_12_00(self):
        """Noon 12:00 requested at 14:00 should roll over to tomorrow at 12:00 (2026-10-17 08:30 UTC)."""
        res = parse_reminder_input_tier1("ساعت ۱۲ ظهر ناهار", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertEqual(due_utc, "2026-10-17T08:30:00Z")
        self.assertEqual(note, "ناهار")

    def test_10_persian_twelve_night_midnight_bug(self):
        """
        Adversarial test: In Persian, 'ساعت ۱۲ شب' means midnight (00:00 tonight)!
        It must resolve to 2026-10-16T20:30:00Z (tonight midnight),
        NOT 2026-10-17T08:30:00Z (tomorrow 12:00 noon)!
        """
        res = parse_reminder_input_tier1("ساعت ۱۲ شب خوابیدن", self.base_tehran)
        self.assertIsNotNone(res, "Failed to parse 'ساعت ۱۲ شب'")
        due_utc, note = res
        # 12 شب is 00:00 Tehran tonight => 2026-10-16 20:30:00 UTC
        self.assertEqual(due_utc, "2026-10-16T20:30:00Z",
                         f"'ساعت ۱۲ شب' was incorrectly parsed as noon ({due_utc}) instead of midnight!")
        self.assertEqual(note, "خوابیدن")

    def test_11_persian_twelve_thirty_night_bug(self):
        """
        Adversarial test: 'ساعت ۱۲:۳۰ شب' means 00:30 tonight (half hour after midnight).
        Must resolve to 2026-10-16T21:00:00Z, NOT 2026-10-17T09:00:00Z (12:30 PM tomorrow).
        """
        res = parse_reminder_input_tier1("ساعت ۱۲:۳۰ شب مسواک", self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertEqual(due_utc, "2026-10-16T21:00:00Z",
                         f"'ساعت ۱۲:۳۰ شب' was incorrectly parsed as {due_utc}")
        self.assertEqual(note, "مسواک")

    def test_12_persian_bamdad_marker_recognition_bug(self):
        """
        Adversarial test: 'بامداد' is standard Persian for AM / early morning.
        1. 'ساعت ۱ بامداد مطالعه' must be recognized as 01:00 AM, and 'بامداد' stripped from note.
        2. 'ساعت ۱۲ بامداد خواب' must resolve to midnight 00:00, not 12:00 noon.
        """
        res1 = parse_reminder_input_tier1("ساعت ۱ بامداد مطالعه", self.base_tehran)
        self.assertIsNotNone(res1, "Failed to parse 'ساعت ۱ بامداد'")
        self.assertEqual(res1[0], "2026-10-16T21:30:00Z")  # 01:00 tomorrow Tehran => 21:30Z
        self.assertEqual(res1[1], "مطالعه", f"'بامداد' was not cleaned from reminder text: '{res1[1]}'")

        res2 = parse_reminder_input_tier1("ساعت ۱۲ بامداد خواب", self.base_tehran)
        self.assertIsNotNone(res2, "Failed to parse 'ساعت ۱۲ بامداد'")
        self.assertEqual(res2[0], "2026-10-16T20:30:00Z",
                         f"'ساعت ۱۲ بامداد' was parsed as {res2[0]} instead of midnight 20:30Z")
        self.assertEqual(res2[1], "خواب")

    def test_13_clock_invalid_hours_and_minutes_rejected(self):
        """Invalid clock times (hour > 23, minute > 59) must be rejected by Tier 1."""
        self.assertIsNone(parse_reminder_input_tier1("ساعت 25:00 جلسه", self.base_tehran))
        self.assertIsNone(parse_reminder_input_tier1("ساعت 14:60 جلسه", self.base_tehran))
        self.assertIsNone(parse_reminder_input_tier1("ساعت 99:99 جلسه", self.base_tehran))


class TestM4ElapsedTimesRollover(unittest.TestCase):
    """Category 4: Same-day elapsed times rollover."""

    def test_14_morning_time_requested_in_afternoon_rolls_over(self):
        """At 14:00, scheduling 08:00 morning must schedule for tomorrow at 08:00."""
        base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)
        res = parse_reminder_input_tier1("ساعت ۰۸:۰۰ صبح ورزش صبحگاهی", base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        # Tomorrow 08:00 Tehran => 2026-10-17 04:30:00 UTC
        self.assertEqual(due_utc, "2026-10-17T04:30:00Z")
        self.assertEqual(note, "ورزش صبحگاهی")

    def test_15_exact_same_minute_rolls_over(self):
        """At 14:00:00, scheduling 14:00 has already arrived, must roll over to tomorrow."""
        base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)
        res = parse_reminder_input_tier1("ساعت ۱۴:۰۰ بررسی شاخص‌ها", base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertEqual(due_utc, "2026-10-17T10:30:00Z")
        self.assertEqual(note, "بررسی شاخص‌ها")

    def test_16_future_same_day_stays_today(self):
        """At 10:00, scheduling 16:30 must stay today at 16:30 (13:00 UTC)."""
        base_tehran = datetime.datetime(2026, 10, 16, 10, 0, 0, tzinfo=TEHRAN_TZ)
        res = parse_reminder_input_tier1("ساعت ۱۶:۳۰ جلسه هماهنگی", base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertEqual(due_utc, "2026-10-16T13:00:00Z")
        self.assertEqual(note, "جلسه هماهنگی")

    def test_17_explicit_day_indicator_overrides_same_day_rollover(self):
        """Explicit day: 'فردا ساعت ۱۰' vs 'پس‌فردا ساعت ۱۰'."""
        base_tehran = datetime.datetime(2026, 10, 16, 14, 0, 0, tzinfo=TEHRAN_TZ)
        # Tomorrow at 10:00
        res_ferda = parse_reminder_input_tier1("فردا ساعت ۱۰:۰۰ تماس با بانک", base_tehran)
        self.assertIsNotNone(res_ferda)
        self.assertEqual(res_ferda[0], "2026-10-17T06:30:00Z")

        # Day after tomorrow at 10:00
        res_pasferda = parse_reminder_input_tier1("پس‌فردا ساعت ۱۰:۰۰ تحویل مدارک", base_tehran)
        self.assertIsNotNone(res_pasferda)
        self.assertEqual(res_pasferda[0], "2026-10-18T06:30:00Z")


class TestM4ConversationalSyntaxAndZWNJ(unittest.TestCase):
    """Category 5: Conversational text, emojis, ZWNJ, and multiple numbers."""

    def setUp(self):
        self.base_tehran = datetime.datetime(2026, 10, 16, 8, 0, 0, tzinfo=TEHRAN_TZ)

    def test_18_phone_number_or_id_before_saat_bug(self):
        """
        Adversarial test: Phone number or numeric ID appearing before 'ساعت':
        'یادم بنداز با شماره ۰۹۱۲۳۴۵۶۷۸۹ ساعت ۱۰ تماس بگیرم 📞'
        Must NOT be misinterpreted as 9123456789 hours and crash with OverflowError!
        Must schedule at 10:00 AM (06:30Z) and keep phone number in text.
        """
        input_text = "یادم بنداز با شماره ۰۹۱۲۳۴۵۶۷۸۹ ساعت ۱۰ تماس بگیرم 📞"
        try:
            res = parse_reminder_input_tier1(input_text, self.base_tehran)
        except OverflowError:
            self.fail("parse_reminder_input_tier1 crashed with OverflowError on phone number before 'ساعت'")

        self.assertIsNotNone(res, "Failed to parse reminder with phone number before 'ساعت'")
        due_utc, note = res
        # 10:00 AM Tehran today => 06:30 UTC
        self.assertEqual(due_utc, "2026-10-16T06:30:00Z",
                         f"Phone number caused wrong schedule time: {due_utc}")
        self.assertIn("09123456789", note, "Phone number was destroyed from reminder note")
        self.assertIn("تماس بگیرم 📞", note)

    def test_19_zwnj_in_bad_az_zohr_bug(self):
        """
        Adversarial test: Persian Zero-Width Non-Joiner (ZWNJ \u200c) in 'بعد‌از‌ظهر':
        'ساعت ۵ بعد\u200cاز\u200cظهر تمرین باشگاه'
        Must parse as 17:00 (5 PM today, 13:30Z), NOT 05:00 AM tomorrow (01:30Z)!
        """
        input_text = "ساعت ۵ بعد\u200cاز\u200cظهر تمرین باشگاه"
        res = parse_reminder_input_tier1(input_text, self.base_tehran)
        self.assertIsNotNone(res, "Failed to parse 'ساعت ۵ بعد‌از‌ظهر' with ZWNJ")
        due_utc, note = res
        # 5 PM = 17:00 Tehran => 13:30 UTC
        self.assertEqual(due_utc, "2026-10-16T13:30:00Z",
                         f"ZWNJ 'بعد‌از‌ظهر' misinterpreted as AM tomorrow ({due_utc}) instead of PM today!")
        self.assertNotIn("بعد", note, "ZWNJ marker 'بعد‌از‌ظهر' leaked into reminder note")
        self.assertIn("تمرین باشگاه", note)

    def test_20_conversational_quotes_brackets_emojis(self):
        """Complex conversational text with quotes, brackets, and emojis."""
        text = 'لطفاً فردا ساعت ۱۵:۳۰ یادم بنداز "فایل پروژه (نسخه 2.0)" رو بفرستم! 🚀'
        res = parse_reminder_input_tier1(text, self.base_tehran)
        self.assertIsNotNone(res)
        due_utc, note = res
        self.assertEqual(due_utc, "2026-10-17T12:00:00Z")
        self.assertIn('"فایل پروژه (نسخه 2.0)"', note)
        self.assertIn("🚀", note)

    def test_21_non_reminder_conversational_messages_ignored(self):
        """Conversational chat messages without reminder semantics must return None."""
        cases = [
            "سلام چطوری؟",
            "یک تابع پایتون برای ساعت کاری بنویس",
            "فاصله زمین تا ماه ۳۸۴۴۰۰ کیلومتر است",
            "دیروز ساعت ۴ رفتم دکتر و دارو گرفتم",
        ]
        for msg in cases:
            res = parse_reminder_input(msg, self.base_tehran)
            self.assertIsNone(res, f"Non-reminder message wrongly parsed: '{msg}'")


class TestM4PolymorphicReturns(unittest.TestCase):
    """Category 6: Polymorphic return handling (sync unpacking vs async awaiting)."""

    def setUp(self):
        self.base_tehran = datetime.datetime(2026, 10, 16, 10, 0, 0, tzinfo=TEHRAN_TZ)

    def test_22_tier1_awaitable_tuple_properties(self):
        """AwaitableTuple can be unpacked synchronously or awaited asynchronously."""
        res = parse_reminder_input("10m تست چندریختی", self.base_tehran)
        self.assertIsInstance(res, tuple)
        self.assertTrue(bool(res))
        self.assertEqual(len(res), 2)

        # Synchronous unpacking
        due, text = res
        self.assertEqual(due, "2026-10-16T06:40:00Z")
        self.assertEqual(text, "تست چندریختی")

        # Indexing and slicing
        self.assertEqual(res[0], due)
        self.assertEqual(res[1], text)
        self.assertEqual(res[:], (due, text))

        # Asynchronous awaiting
        async def _async_eval():
            r = await parse_reminder_input("10m تست چندریختی", self.base_tehran)
            return r

        due_a, text_a = asyncio.run(_async_eval())
        self.assertEqual(due_a, due)
        self.assertEqual(text_a, text)

    def test_23_tier2_awaitable_result_sync_and_async_with_mock(self):
        """Tier2AwaitableResult evaluates correctly both synchronously and asynchronously."""
        fake_llm_json = '{"due_at_utc": "2026-10-19T06:30:00Z", "reminder_text": "قسط وام"}'
        mock_resp = MagicMock()
        mock_resp.text = fake_llm_json

        # Test sync evaluation
        with patch.object(bot.client.models, "generate_content", return_value=mock_resp):
            res_sync = parse_reminder_input("دوشنبه یادم بنداز قسط وام رو بدم", self.base_tehran)
            self.assertIsInstance(res_sync, Tier2AwaitableResult)
            self.assertTrue(bool(res_sync))
            due_s, note_s = res_sync
            self.assertEqual(due_s, "2026-10-19T06:30:00Z")
            self.assertEqual(note_s, "قسط وام")

        # Test async evaluation
        with patch.object(bot.client.models, "generate_content", return_value=mock_resp):
            async def _async_test():
                res_async = parse_reminder_input("دوشنبه یادم بنداز قسط وام رو بدم", self.base_tehran)
                return await res_async

            res_a = asyncio.run(_async_test())
            self.assertIsNotNone(res_a)
            self.assertEqual(res_a[0], "2026-10-19T06:30:00Z")
            self.assertEqual(res_a[1], "قسط وام")


class TestM4ConcurrencyStress(unittest.TestCase):
    """Category 7: Rapid multi-threaded and asyncio concurrent parser invocations."""

    def test_24_rapid_multi_threaded_parser_invocations(self):
        """Stress-test: 50 concurrent threads parsing varied inputs simultaneously."""
        test_inputs = [
            "۱۰ دقیقه دیگه آب بخورم",
            "ساعت ۱۴:۳۰ جلسه فنی",
            "۲ ساعت بعد تماس با شرکت",
            "/remind 1d ارسال گزارش نهایی",
            "ساعت ۹ شب فوتبال",
            "نیم ساعت دیگه آماده شو",
            "فردا ساعت ۱۰ صبح دادگاه",
            "سلام چطوری پیام عادی",
        ]
        base = datetime.datetime(2026, 10, 16, 12, 0, 0, tzinfo=TEHRAN_TZ)
        results = []
        errors = []

        def worker(thread_idx):
            for i, text in enumerate(test_inputs):
                try:
                    res = parse_reminder_input(text, base)
                    results.append((thread_idx, i, res is not None))
                except Exception as e:
                    errors.append((thread_idx, text, str(e)))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Encountered thread errors during stress test: {errors}")
        self.assertEqual(len(results), 50 * len(test_inputs))

    def test_25_concurrent_asyncio_evaluations(self):
        """Stress-test: 50 concurrent asyncio tasks awaiting parser evaluations."""
        base = datetime.datetime(2026, 10, 16, 12, 0, 0, tzinfo=TEHRAN_TZ)

        async def _eval_one(idx):
            text = f"/{'remind'} {idx % 60 + 1}m تست همزمانی {idx}"
            r = await parse_reminder_input(text, base)
            return r

        async def _main():
            tasks = [_eval_one(i) for i in range(50)]
            return await asyncio.gather(*tasks)

        all_res = asyncio.run(_main())
        self.assertEqual(len(all_res), 50)
        for r in all_res:
            self.assertIsNotNone(r)
            self.assertTrue(r[0].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
