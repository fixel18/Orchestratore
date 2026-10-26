from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from orch.claude_limits import claude_reset_time, is_authentication_error, is_claude_limit
from orch.claude_limits import is_weekly_limit


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone(timedelta(hours=2)))


class ClaudeLimitTests(unittest.TestCase):
    def test_weekly_text_and_reset_threshold(self):
        for text in ("weekly limit", "week limit", "limite settimanale"):
            self.assertTrue(is_weekly_limit(text, None, now=NOW))
        self.assertFalse(is_weekly_limit("usage limit", NOW + timedelta(hours=5), now=NOW))
        self.assertFalse(is_weekly_limit("usage limit", NOW + timedelta(hours=12), now=NOW))
        self.assertTrue(is_weekly_limit("usage limit", NOW + timedelta(hours=13), now=NOW))
        self.assertFalse(is_weekly_limit("usage limit", NOW + timedelta(hours=13),
                                        now=NOW, threshold_hours=24))

    def test_limit_patterns_and_error_flag(self):
        for text in (
            "usage limit", "You've hit your limit", "LIMIT REACHED",
            "Claude AI usage limit reached|1792670400", "resets 5pm",
            "resets 5:30pm (Europe/Rome)", "resets at 17:00",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_claude_limit(text))
        self.assertTrue(is_claude_limit("Errore sconosciuto", is_error=True))
        self.assertFalse(is_claude_limit("Revisione completata"))

    def test_epoch_including_past_reset_is_authoritative(self):
        for reset in (NOW + timedelta(hours=2), NOW - timedelta(hours=2)):
            with self.subTest(reset=reset):
                actual = claude_reset_time(f"Claude AI usage limit reached|{reset.timestamp()}", now=NOW)
                self.assertEqual(actual, reset)

    def test_hour_formats_and_next_occurrence(self):
        for text, expected in (
            ("resets 5pm", NOW.replace(hour=17)),
            ("resets 5:30pm", NOW.replace(hour=17, minute=30)),
            ("resets at 17:00", NOW.replace(hour=17)),
            ("resets 5am", (NOW + timedelta(days=1)).replace(hour=5)),
            ("resets 12am", (NOW + timedelta(days=1)).replace(hour=0)),
            ("resets 12pm", NOW + timedelta(days=1)),
        ):
            with self.subTest(text=text):
                self.assertEqual(claude_reset_time(text, now=NOW), expected)

    def test_named_zone_uses_zoneinfo_and_converts_reference_clock(self):
        zone = timezone(timedelta(hours=-4))
        with patch("orch.claude_limits.ZoneInfo", return_value=zone) as lookup:
            reset = claude_reset_time("resets 5:30pm (America/New_York)", now=NOW)
        lookup.assert_called_once_with("America/New_York")
        self.assertEqual(reset, datetime(2026, 10, 22, 17, 30, tzinfo=zone))

    def test_missing_timezone_database_returns_unknown_reset(self):
        with patch("orch.claude_limits.ZoneInfo", side_effect=ZoneInfoNotFoundError("Europe/Rome")):
            self.assertIsNone(claude_reset_time("resets 5pm (Europe/Rome)", now=NOW))

    def test_bad_or_missing_reset(self):
        for text in ("usage limit", "resets 25:00", "resets 13pm", "resets 5:99pm", "resets 1700",
                     "Claude AI usage limit reached|999999999999999999999999999999"):
            with self.subTest(text=text):
                self.assertIsNone(claude_reset_time(text, now=NOW))

    def test_authentication_keywords_are_distinct(self):
        for text in ("OAuth token expired", "Please authenticate", "Run login", "Please log in"):
            with self.subTest(text=text):
                self.assertTrue(is_authentication_error(text))
        self.assertFalse(is_authentication_error("usage limit reached"))


if __name__ == "__main__":
    unittest.main()
