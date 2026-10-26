from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from orch.claude import build_claude_command, DISALLOWED_TOOLS, parse_claude_response
from orch.claude_stream import parse_claude_stream
from orch.codex_limits import osservazione_da_rollout, osservazione_vuota
from orch.config import load_config
from orch.controllori import revisiona
from orch.esecutori import invia


ROOT = Path(__file__).resolve().parent
FAKE = ROOT / "finti" / "strumento_finto.py"
SESSION = "11111111-2222-3333-4444-555555555555"
RESET = 1792760400
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
CLAUDE = {"modello": "claude-sonnet-5-5", "ragionamento": "high"}
CODEX = {"modello": "modello-dalle-impostazioni", "ragionamento": "ultra"}


def event(**info):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


class StreamTests(unittest.TestCase):
    def test_rejection_overrides_successful_verdict_even_with_zero_exit(self):
        raw = event(status="rejected", rateLimitType="five_hour", resetsAt=RESET) + "\n" + json.dumps(
            {"type": "result", "result": "ESITO: OK", "is_error": False})
        verdict = parse_claude_response(raw, now=NOW)
        self.assertEqual(verdict.kind, "limite")
        self.assertEqual(verdict.retry_at.timestamp(), RESET)

    def test_stream_generic_errors_do_not_become_usage_limits_and_auth_has_priority(self):
        raw = json.dumps({"type": "result", "result": "Errore temporaneo", "is_error": True})
        self.assertEqual(parse_claude_response(raw).kind, "errore")
        raw = event(status="rejected", rateLimitType="five_hour", resetsAt=RESET) + "\n" + json.dumps(
            {"type": "result", "result": "OAuth expired: please login", "is_error": True})
        self.assertEqual(parse_claude_response(raw).kind, "errore_autenticazione")

    def test_stream_verdict_can_discuss_login_and_limits(self):
        raw = json.dumps({"type": "result", "result": "ESITO: OK\nTest login e usage limit", "is_error": False})
        self.assertEqual(parse_claude_response(raw).kind, "ok")

    def test_nested_windows_win_over_top_level_and_convert_fractions(self):
        data = parse_claude_stream(event(status="allowed_warning", utilization=0.99,
            rateLimitType="seven_day", resetsAt=RESET,
            unifiedWindows={"five_hour": {"utilization": 0.47, "resetsAt": RESET},
                            "seven_day": {"utilization": 0.95, "resetsAt": RESET + 1}}))
        self.assertEqual(data.usage, {"finestra_5h": 47, "resets_5h": datetime.fromtimestamp(RESET, timezone.utc),
            "settimana": 95, "resets_settimana": datetime.fromtimestamp(RESET + 1, timezone.utc)})
        self.assertIsNone(data.usage_limit)

    def test_simple_windows_rejected_and_weekly_priority(self):
        raw = "\n".join([
            event(status="rejected", rateLimitType="seven_day", utilization=1, resetsAt=RESET + 20),
            event(status="rejected", rateLimitType="five_hour", utilization=1, resetsAt=RESET),
            event(status="allowed", rateLimitType="five_hour", utilization=0.2, resetsAt=RESET + 1)])
        data = parse_claude_stream(raw)
        self.assertTrue(data.usage_limit.weekly)
        self.assertEqual(data.usage_limit.source, "rate_limit_event")
        self.assertEqual(data.usage_limit.retry_at.timestamp(), RESET + 20)
        self.assertEqual(data.usage["finestra_5h"], 20)

    def test_invalid_usage_is_unknown_including_boolean_nan_and_infinity(self):
        for value in (None, True, "0.5", -1, 2, float("nan"), float("inf")):
            with self.subTest(value=value):
                data = parse_claude_stream(event(status="allowed", rateLimitType="five_hour",
                                                 utilization=value, resetsAt=value))
                self.assertIsNone(data.usage["finestra_5h"])
        self.assertIsNone(parse_claude_stream(event(status="rejected", resetsAt="bad")).usage_limit.retry_at)

    def test_unknown_latest_window_clears_previous_observation(self):
        raw = event(rateLimitType="five_hour", utilization=0.8, resetsAt=RESET) + "\n" + event(
            unifiedWindows={"five_hour": None})
        self.assertEqual(parse_claude_stream(raw).usage, osservazione_vuota())

    def test_session_callback_once_and_mismatch_or_bad_events_fail(self):
        callback = Mock()
        raw = "\ufeff" + json.dumps({"type": "system", "session_id": SESSION}) + "\n" + json.dumps(
            {"type": "result", "session_id": SESSION, "result": "caffè", "is_error": False})
        data = parse_claude_stream(raw, on_session=callback)
        callback.assert_called_once_with(SESSION)
        self.assertEqual(data.text, "caffè")
        self.assertTrue(data.result_received)
        self.assertTrue(parse_claude_stream(raw, session_id="other").errors)
        for line in ("{", "[]", '{"type":"system","session_id":"../bad"}',
                     '{"type":"result","result":1,"is_error":false}'):
            with self.subTest(line=line):
                self.assertTrue(parse_claude_stream(line).errors)
        with self.assertRaisesRegex(RuntimeError, "callback"):
            parse_claude_stream(raw, on_session=Mock(side_effect=RuntimeError("callback")))


class AdattatoriTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        config_path = self.base / "config.json"
        config_path.write_text('{"cartella_sessioni_codex":"sessioni"}', encoding="utf-8")
        self.config = load_config(config_path)
        self.project = self.base / "progetto"
        self.project.mkdir()
        self.receipt = self.base / "ricevuta.json"

    def configured(self, tool, case="success"):
        command = [sys.executable, str(FAKE), tool, case, str(self.receipt)]
        return replace(self.config, **{f"comando_{tool}": command})

    def receipt_data(self):
        return json.loads(self.receipt.read_text(encoding="utf-8"))

    def rollout(self, rates):
        folder = self.config.cartella_sessioni_codex / "2026" / "10" / "23"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-test-{SESSION}.jsonl"
        path.write_text("{bad\n" + json.dumps({"payload": {"rate_limits": rates}}) + "\n", encoding="utf-8")
        return path

    def test_codex_uses_selected_model_resumes_and_observes_rollout(self):
        self.rollout({"primary": {"used_percent": 80, "resets_at": RESET, "window_minutes": 300},
                      "secondary": {"used_percent": 90, "resets_at": RESET + 86400}})
        callback = Mock()
        config = self.configured("codex")
        result = invia("codex", config, CODEX, self.project, "Primo caffè", on_conversazione=callback, now=NOW)
        callback.assert_called_once_with(SESSION)
        self.assertEqual(result.conversazione, SESSION)
        self.assertEqual(result.errori, [])
        self.assertEqual(result.osservazione_utilizzo["finestra_5h"], 80)
        self.assertEqual(result.osservazione_utilizzo["settimana"], 90)
        args = self.receipt_data()["argv"]
        self.assertEqual(args[args.index("-m") + 1], CODEX["modello"])
        self.assertIn('model_reasoning_effort="ultra"', args)
        self.assertEqual(config.modello_codex, "gpt-6.1-sol")
        resumed = invia("codex", config, CODEX, self.project, "Secondo", conversazione=SESSION)
        self.assertEqual(resumed.conversazione, SESSION)
        self.assertIn("resume", self.receipt_data()["argv"])
        self.assertNotIn("-C", self.receipt_data()["argv"])

    def test_claude_writes_resumes_and_logs_exact_utf8_stdin(self):
        prompt = 'caffè & | %PATH% "test"\n' + "x" * 65000
        config = self.configured("claude")
        callback = Mock()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "finto", "ANTHROPIC_API_KEY": "finto"}):
            result = invia("claude", config, CLAUDE, self.project, prompt, on_conversazione=callback)
        callback.assert_called_once_with(SESSION)
        self.assertEqual(result.errori, [])
        self.assertEqual(result.conversazione, SESSION)
        self.assertEqual(result.ultimo_messaggio, "STATO: FINITO\ncaffè")
        receipt = self.receipt_data()
        self.assertEqual(receipt["prompt"], prompt)
        self.assertEqual(receipt["cwd"], str(self.project))
        self.assertEqual(receipt["chiavi_presenti"], [])
        self.assertNotIn(prompt, receipt["argv"])
        self.assertIn("acceptEdits", receipt["argv"])
        self.assertNotIn("--no-session-persistence", receipt["argv"])
        self.assertEqual((self.project / "eseguito.txt").read_text(encoding="utf-8"), "caffè")
        self.assertEqual(result.osservazione_utilizzo["finestra_5h"], 47)
        self.assertTrue(any(path.read_text(encoding="utf-8") == prompt for path in (self.base / "log").glob("*.prompt.txt")))
        resumed = invia("claude", config, CLAUDE, self.project, "Correggi", conversazione=SESSION)
        self.assertEqual(resumed.errori, [])
        args = self.receipt_data()["argv"]
        self.assertEqual(args[args.index("--resume") + 1], SESSION)

    def test_exact_claude_executor_permissions_and_settings(self):
        command = build_claude_command(self.config, CLAUDE, esecutore=True, conversazione=SESSION)
        self.assertEqual(command, [*self.config.comando_claude, "-p", "--output-format", "stream-json", "--verbose",
            "--model", CLAUDE["modello"], "--effort", "high", "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "Bash", "--disallowedTools",
            *DISALLOWED_TOOLS, "--resume", SESSION])
        self.assertIn("Bash(git commit:*)", command)
        self.assertIn("Write(**/.env*)", command)
        with self.assertRaises(ValueError):
            build_claude_command(self.config, CLAUDE, esecutore=True, conversazione="../bad")

    def test_claude_limits_warnings_unknown_and_authentication(self):
        for case in ("warning", "limit", "weekly", "unknown", "auth", "text-limit", "error"):
            with self.subTest(case=case):
                result = invia("claude", self.configured("claude", case), CLAUDE, self.project, "Test", now=NOW)
                self.assertEqual(result.errore_autenticazione, case == "auth")
                if case in ("limit", "weekly", "text-limit"):
                    self.assertIsNotNone(result.limite)
                    self.assertEqual(result.limite.retry_at.timestamp(), RESET)
                    self.assertEqual(result.limite.weekly, case == "weekly")
                else:
                    self.assertIsNone(result.limite)
                if case == "unknown":
                    self.assertIsNone(result.osservazione_utilizzo["finestra_5h"])
                if case == "error":
                    self.assertTrue(result.errori)

    def test_claude_protocol_errors_are_not_successes(self):
        for case in ("malformed", "no-session", "no-result"):
            with self.subTest(case=case):
                result = invia("claude", self.configured("claude", case), CLAUDE, self.project, "Test")
                self.assertTrue(result.errori)

    def test_codex_limits_and_authentication(self):
        self.rollout({"primary": {"used_percent": 100, "resets_at": RESET}})
        result = invia("codex", self.configured("codex", "limit"), CODEX, self.project, "Test", now=NOW)
        self.assertEqual(result.limite.retry_at.timestamp(), RESET)
        self.assertEqual(result.limite.source, "rollout")
        result = invia("codex", self.configured("codex", "auth"), CODEX, self.project, "Test")
        self.assertTrue(result.errore_autenticazione)

    def test_antigravity_executor_and_controller_use_agy_safely(self):
        from orch.antigravity import RisultatoAgy, comando
        chiamate, salvate = [], []

        def agy(config, progetto, prompt, impostazioni, **kwargs):
            chiamate.append(kwargs)
            return RisultatoAgy(conversazione="conv-1", risposta="ESITO: OK\nNOTE: bene", stato="SUCCESS")

        result = invia("antigravity", self.config, {"modello": "g", "ragionamento": "low"}, self.project,
                       "Test", on_conversazione=salvate.append, run_agy=agy)
        self.assertEqual((result.conversazione, salvate), ("conv-1", ["conv-1"]))
        self.assertEqual(result.osservazione_utilizzo, osservazione_vuota())  # nessuna percentuale
        verdetto = revisiona("antigravity", self.config, {"modello": "g", "ragionamento": "low"},
                             self.project, "Test", run_agy=agy).review
        self.assertEqual(verdetto.kind, "ok")
        self.assertEqual(chiamate[0].get("modalita", "accept-edits"), "accept-edits")
        self.assertEqual(chiamate[1]["modalita"], "plan")
        cmd = comando({"modello": "g", "ragionamento": "low"}, prompt_in_riga="ciao", conversazione="c")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(cmd[-1], "-p=ciao")
        self.assertIn("--conversation", cmd)

        def limite(*args, **kwargs):
            return RisultatoAgy(errori=["429 RESOURCE_EXHAUSTED: quota exceeded"], limite=True)

        self.assertIsNotNone(invia("antigravity", self.config, {}, self.project, "Test", run_agy=limite).limite)
        with self.assertRaises(ValueError):
            invia("altro", self.config, {}, self.project, "Test")

    def test_launch_errors_both_executors_and_controllers(self):
        for tool, settings in (("codex", CODEX), ("claude", CLAUDE)):
            with self.subTest(tool=tool), patch("subprocess.Popen", side_effect=OSError("assente")):
                result = invia(tool, self.config, settings, self.project, "Test")
                self.assertEqual(result.errore_avvio, "assente")
                self.assertTrue(result.errori)
                controller = revisiona(tool, self.config, settings, self.project, "Test")
                self.assertEqual(controller.errore_avvio, "assente")
                self.assertEqual(controller.review.kind, "errore")

    def test_controllers_are_read_only_use_settings_and_never_resume(self):
        for tool, settings in (("codex", CODEX), ("claude", CLAUDE)):
            for case, kind in (("success", "ok"), ("correzioni", "correzioni")):
                with self.subTest(tool=tool, case=case):
                    config = self.configured(tool, case)
                    result = revisiona(tool, config, settings, self.project, "Revisione caffè")
                    self.assertEqual(result.review.kind, kind)
                    args = self.receipt_data()["argv"]
                    self.assertNotIn("resume", args)
                    self.assertNotIn("--resume", args)
                    if tool == "codex":
                        self.assertEqual(args[args.index("-s") + 1], "read-only")
                        self.assertEqual(args[args.index("-m") + 1], CODEX["modello"])
                    else:
                        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
                        self.assertIn("dontAsk", args)
                        self.assertIn("--no-session-persistence", args)
                        self.assertEqual(args[args.index("--model") + 1], CLAUDE["modello"])
                    self.assertFalse((self.project / "eseguito.txt").exists())

    def test_controllers_propagate_usage_limits_and_authentication(self):
        for tool, settings in (("codex", CODEX), ("claude", CLAUDE)):
            with self.subTest(tool=tool):
                result = revisiona(tool, self.configured(tool, "limit"), settings, self.project, "Test", now=NOW)
                self.assertEqual(result.review.kind, "limite")
                self.assertIsNotNone(result.limite)
                result = revisiona(tool, self.configured(tool, "auth"), settings, self.project, "Test")
                self.assertEqual(result.review.kind, "errore_autenticazione")

    def test_claude_timeout_terminates_tree_preserves_partial_usage_and_logs(self):
        raw = (json.dumps({"type": "system", "session_id": SESSION}) + "\n" +
               event(status="allowed", rateLimitType="five_hour", utilization=0.8, resetsAt=RESET) + "\n").encode()
        process = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 30, output=raw), (raw, b"timeout")]
        process.poll.return_value = -9
        with patch("orch.claude.subprocess.Popen", return_value=process), patch("orch.claude.terminate_process_tree", return_value=None) as stop:
            result = invia("claude", self.config, CLAUDE, self.project, "Test")
        stop.assert_called_once()
        self.assertTrue(result.timed_out)
        self.assertTrue(result.errori)
        self.assertEqual(result.conversazione, SESSION)
        self.assertEqual(result.osservazione_utilizzo["finestra_5h"], 80)
        self.assertTrue(any(path.read_bytes() == raw for path in (self.base / "log").glob("*.response.json")))

    def test_rollout_latest_snapshot_null_and_malformed_values(self):
        path = self.rollout({"primary": {"used_percent": 80, "resets_at": RESET},
                             "secondary": {"used_percent": 90}})
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"rate_limits": {"primary": None, "secondary": {
                "used_percent": 95, "resets_at": RESET + 1}}}) + "\n{bad\n")
        observed = osservazione_da_rollout(self.config.cartella_sessioni_codex, SESSION)
        self.assertIsNone(observed["finestra_5h"])
        self.assertIsNone(observed["resets_5h"])
        self.assertEqual(observed["settimana"], 95)
        self.assertEqual(osservazione_da_rollout(self.config.cartella_sessioni_codex, "../bad"), osservazione_vuota())
        self.assertEqual(osservazione_da_rollout(self.base / "assente", SESSION), osservazione_vuota())
        self.rollout({"primary": {"used_percent": True, "resets_at": "bad"},
                      "secondary": {"used_percent": 101, "resets_at": float("inf")}})
        self.assertEqual(osservazione_da_rollout(self.config.cartella_sessioni_codex, SESSION), osservazione_vuota())


if __name__ == "__main__":
    unittest.main()
