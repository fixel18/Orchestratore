from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from orch.codex_limits import (
    CodexError, detect_usage_limit, is_usage_limit, reset_from_rollout, reset_from_text,
)


TEST_ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone(timedelta(hours=2)))


class ResetTextTests(unittest.TestCase):
    def test_limit_codes_and_text(self):
        for code in ("usage_limit_reached", "rate_limit_reached", "quota_exceeded"):
            with self.subTest(code=code):
                self.assertTrue(is_usage_limit("Limite", code))
                self.assertTrue(is_usage_limit(code.upper()))
        self.assertTrue(is_usage_limit("You've hit your usage limit. Please wait."))
        self.assertTrue(is_usage_limit("You’ve hit your usage limit."))
        self.assertFalse(is_usage_limit("Network error", "connection_failed"))
        self.assertTrue(is_usage_limit("You've hit your weekly limit."))
        self.assertTrue(is_usage_limit("Limite settimanale raggiunto"))

    def test_12_hour_and_24_hour_formats(self):
        for text, hour in (("3:05 PM", 15), ("15:05", 15), ("3:05 pm", 15)):
            with self.subTest(text=text):
                actual = reset_from_text("You've hit your usage limit. Try again at " + text + ".", now=NOW)
                self.assertEqual(actual, NOW.replace(hour=hour, minute=5))

    def test_midnight_noon_and_next_day(self):
        for text, expected in (
            ("12:00 AM", NOW + timedelta(minutes=2)),
            ("12:00 PM", NOW + timedelta(minutes=2)),
            ("11:59 AM", NOW + timedelta(minutes=2)),
            ("00:15", NOW + timedelta(minutes=2)),
        ):
            with self.subTest(text=text):
                self.assertEqual(reset_from_text("try again at " + text, now=NOW), expected)

    def test_exact_real_jsonl_message_and_null_rollout(self):
        from orch.codex import parse_codex_jsonl
        raw = ('{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro '
               '(https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage '
               'to purchase more credits or try again at Oct 23rd, 2026 12:49 AM."}\n'
               '{"type":"turn.failed","error":{"message":"...stesso testo..."}}')
        output = parse_codex_jsonl(raw)
        now = NOW.replace(hour=23, minute=35)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            path = Path(folder) / "2026" / "10" / "22"
            path.mkdir(parents=True)
            (path / "rollout-test-thread.jsonl").write_text(
                '{"rate_limits":{"limit_id":"premium","primary":null,"secondary":null}}',
                encoding="utf-8")
            limit = detect_usage_limit(output.errors, "", sessions=folder, thread_id="thread", now=now)
        self.assertEqual(limit.retry_at, NOW.replace(day=23, hour=0, minute=49))
        self.assertEqual(limit.source, "testo")
        self.assertFalse(limit.weekly)

    def test_recent_past_time_does_not_become_tomorrow(self):
        now = NOW.replace(day=23, hour=0, minute=49, second=6)
        retry = reset_from_text("try again at 12:49 AM.", now=now)
        self.assertEqual(retry, now + timedelta(minutes=2))
        self.assertEqual(reset_from_text("try again at 12:49 AM.", now=NOW,
                                        recent_hours=6),
                         (NOW + timedelta(days=1)).replace(hour=0, minute=49))

    def test_date_first_ordinals_and_invalid_date(self):
        for day, suffix in ((1, "st"), (2, "nd"), (3, "rd"), (23, "rd"), (24, "th")):
            self.assertEqual(reset_from_text(f"try again at Oct {day}{suffix}, 2026 12:49 PM.", now=NOW),
                             NOW.replace(day=day, minute=49))
        self.assertIsNone(reset_from_text("try again at Feb 31st, 2026 12:49 AM.", now=NOW))

    def test_english_date_without_year(self):
        for time in ("3:05 PM", "15:05"):
            with self.subTest(time=time):
                self.assertEqual(
                    reset_from_text(f"or try again at {time} on 24 Oct.", now=NOW),
                    NOW.replace(day=24, hour=15, minute=5),
                )

    def test_english_date_with_year_and_past_date(self):
        self.assertEqual(
            reset_from_text("try again at 3:05 PM on 24 Oct 2027.", now=NOW),
            NOW.replace(year=2027, day=24, hour=15, minute=5),
        )
        self.assertEqual(
            reset_from_text("try again at 01:02 on 1 Jan 2020.", now=NOW),
            NOW.replace(year=2020, month=1, day=1, hour=1, minute=2),
        )

    def test_bad_or_missing_time_does_not_guess(self):
        for text in (
            "You've hit your usage limit.", "try again at 25:00",
            "try again at 3:99 PM", "try again at 13:00 PM",
            "try again at 0:30 AM", "try again at 15:05 on 31 Feb 2026",
            "try again at 15:05 on 24 Foo",
        ):
            with self.subTest(text=text):
                self.assertIsNone(reset_from_text(text, now=NOW))


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.sessions = Path(self.temp.name)
        self.folder = self.sessions / "2026" / "10" / "22"
        self.folder.mkdir(parents=True)
        self.thread_id = "12345678-1234-1234-1234-123456789abc"
        self.path = self.folder / f"rollout-2026-10-22T12-00-00-{self.thread_id}.jsonl"

    def write_rates(self, rates):
        self.path.write_text(json.dumps({"rate_limits": rates}) + "\n", encoding="utf-8")

    def bucket(self, used, delta):
        return {"used_percent": used, "window_minutes": 300,
                "resets_at": (NOW + timedelta(minutes=delta)).timestamp()}

    def test_exhausted_window_overrides_closer_non_exhausted_window(self):
        self.write_rates({"primary": self.bucket(100, 90), "secondary": self.bucket(35, 10)})
        self.assertEqual(reset_from_rollout(self.sessions, self.thread_id, now=NOW), NOW + timedelta(minutes=90))

    def test_both_exhausted_waits_until_both_reset(self):
        self.write_rates({"primary": self.bucket(100, 30), "secondary": self.bucket(105, 90)})
        self.assertEqual(reset_from_rollout(self.sessions, self.thread_id, now=NOW), NOW + timedelta(minutes=90))

    def test_weekly_classification_rollout_text_and_threshold(self):
        message = "You've hit your usage limit."
        for rates, text, expected in (
            ({"primary": self.bucket(100, 300)}, message, False),
            ({"secondary": {"used_percent": 100}}, message, True),
            ({"custom": {"used_percent": 100, "window_minutes": 1440}}, message, True),
            ({"secondary": self.bucket(99, 300)}, message, False),
            ({}, message + " weekly", True),
            ({}, message + " settimanale", True),
            ({"primary": self.bucket(100, 721)}, message, True),
            ({"primary": self.bucket(100, 720)}, message, False),
        ):
            with self.subTest(rates=rates, text=text):
                self.write_rates(rates)
                limit = detect_usage_limit([CodexError(text)], "", sessions=self.sessions,
                                           thread_id=self.thread_id, now=NOW)
                self.assertEqual(limit.weekly, expected)
        self.write_rates({"primary": self.bucket(100, 721)})
        limit = detect_usage_limit([CodexError(message)], "", sessions=self.sessions,
                                   thread_id=self.thread_id, now=NOW, threshold_hours=24)
        self.assertFalse(limit.weekly)

    def test_no_exhausted_window_uses_nearest_future_reset(self):
        self.write_rates({"primary": self.bucket(99, 90), "secondary": self.bucket(25, 10)})
        self.assertEqual(reset_from_rollout(self.sessions, self.thread_id, now=NOW), NOW + timedelta(minutes=10))

    def test_exhausted_window_in_past_allows_immediate_retry(self):
        self.write_rates({"primary": self.bucket(100, -5), "secondary": self.bucket(10, 300)})
        self.assertEqual(reset_from_rollout(self.sessions, self.thread_id, now=NOW), NOW - timedelta(minutes=5))

    def test_nested_payload_and_latest_snapshot_with_truncated_tail(self):
        first = {"rate_limits": {"primary": self.bucket(100, 10)}}
        last = {"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"secondary": self.bucket(100, 45)},
        }}
        self.path.write_text(
            "\n".join((json.dumps(first), 'not json', json.dumps(last), '{"payload":')),
            encoding="utf-8",
        )
        self.assertEqual(reset_from_rollout(self.sessions, self.thread_id, now=NOW), NOW + timedelta(minutes=45))

    def test_missing_wrong_thread_or_unsafe_id(self):
        self.assertIsNone(reset_from_rollout(self.sessions, self.thread_id, now=NOW))
        self.write_rates({"primary": self.bucket(100, 30)})
        for thread in (None, "another-thread", "../*", "[abc]"):
            with self.subTest(thread=thread):
                self.assertIsNone(reset_from_rollout(self.sessions, thread, now=NOW))

    def test_invalid_buckets_and_no_future_reset(self):
        self.write_rates({
            "primary": {"used_percent": 100, "resets_at": "tomorrow"},
            "secondary": None, "bool": {"resets_at": True},
            "overflow": {"resets_at": 1e300}, "past": self.bucket(5, -20),
        })
        self.assertIsNone(reset_from_rollout(self.sessions, self.thread_id, now=NOW))

    def test_rollout_is_authoritative_text_is_fallback(self):
        message = "You've hit your usage limit. or try again at 3:05 PM."
        errors = [CodexError(message)]
        self.write_rates({"primary": self.bucket(100, 30)})
        limit = detect_usage_limit(errors, "", sessions=self.sessions, thread_id=self.thread_id, now=NOW)
        self.assertEqual(limit.source, "rollout")
        self.assertEqual(limit.retry_at, NOW + timedelta(minutes=30))
        self.path.unlink()
        limit = detect_usage_limit(errors, "", sessions=self.sessions, thread_id=self.thread_id, now=NOW)
        self.assertEqual(limit.source, "testo")
        self.assertEqual(limit.retry_at, NOW.replace(hour=15, minute=5))

    def test_code_without_reset_and_non_limit_error(self):
        limit = detect_usage_limit(
            [CodexError("Riprova più tardi", "quota_exceeded")], "",
            sessions=self.sessions, thread_id=self.thread_id, now=NOW,
        )
        self.assertEqual(limit.source, "sconosciuto")
        self.assertIsNone(limit.retry_at)
        self.assertIsNone(detect_usage_limit(
            [CodexError("Errore connessione")], "", sessions=self.sessions,
            thread_id=self.thread_id, now=NOW,
        ))

    def test_limit_can_be_reported_only_on_stderr(self):
        limit = detect_usage_limit(
            [], "You've hit your usage limit. try again at 15:05",
            sessions=self.sessions, thread_id=None, now=NOW,
        )
        self.assertEqual(limit.retry_at, NOW.replace(hour=15, minute=5))


if __name__ == "__main__":
    unittest.main()
