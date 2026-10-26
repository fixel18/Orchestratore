from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from orch.claude import build_claude_command, parse_claude_response, run_claude
from orch.config import load_config


TEST_ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def response(text="ESITO: OK", is_error=False):
    return json.dumps({"result": text, "is_error": is_error}, ensure_ascii=False)


def fake_process(stdout=b"", stderr=b"", returncode=0):
    process = Mock(pid=12345)
    process.stdin = io.BytesIO()
    process.stdout = io.BytesIO(stdout)
    process.stderr = io.BytesIO(stderr)
    process.communicate.return_value = (stdout, stderr)
    process.poll.return_value = returncode
    process.wait.return_value = returncode
    return process


class ClaudeParserTests(unittest.TestCase):
    def test_ok_first_nonempty_line_and_optional_notes(self):
        review = parse_claude_response(response("\n\n  ESITO: OK\nNOTE: città verificata\n"))
        self.assertEqual(review.kind, "ok")
        self.assertTrue(review.approved)
        self.assertEqual(review.details, "NOTE: città verificata")

    def test_corrections_keep_complete_list(self):
        review = parse_claude_response(response("ESITO: CORREZIONI\n1. app.py:4 - errore\n2. test.py:8 - test mancante"))
        self.assertEqual(review.kind, "correzioni")
        self.assertFalse(review.approved)
        self.assertEqual(review.details, "1. app.py:4 - errore\n2. test.py:8 - test mancante")

    def test_unrecognized_first_line_is_not_searched_further_down(self):
        for text in ("", "Approvato", "ESITO: OKAY", "Introduzione\nESITO: OK", "esito: ok"):
            with self.subTest(text=text):
                self.assertEqual(parse_claude_response(response(text)).kind, "non_riconosciuto")

    def test_invalid_json_and_envelope_schema_are_unrecognized(self):
        for raw in ("not json", "[]", "{}", '{"result":3,"is_error":false}',
                    '{"result":"ESITO: OK"}', '{"result":"ESITO: OK","is_error":1}'):
            with self.subTest(raw=raw):
                review = parse_claude_response(raw)
                self.assertEqual(review.kind, "non_riconosciuto")
                self.assertIsNotNone(review.error)

    def test_bom_json_is_supported(self):
        self.assertTrue(parse_claude_response("\ufeff" + response()).approved)

    def test_error_flag_indicates_limit_without_known_reset(self):
        review = parse_claude_response(response("Riprova più tardi", is_error=True))
        self.assertEqual(review.kind, "limite")
        self.assertIsNone(review.retry_at)
        self.assertFalse(review.approved)

    def test_text_limit_patterns_even_without_error_flag_or_json(self):
        for text in ("usage limit", "You've hit your limit", "limit reached", "resets 5pm"):
            with self.subTest(text=text):
                self.assertEqual(parse_claude_response(response(text)).kind, "limite")
                self.assertEqual(parse_claude_response(text, returncode=1).kind, "limite")

    def test_epoch_and_local_clock_reset(self):
        reset = NOW.replace(hour=17)
        review = parse_claude_response(response(f"Claude AI usage limit reached|{reset.timestamp()}", True), now=NOW)
        self.assertEqual(review.kind, "limite")
        self.assertEqual(review.retry_at, reset)
        review = parse_claude_response(response("usage limit reached; resets at 17:00", True), now=NOW)
        self.assertEqual(review.retry_at, reset)

    def test_authentication_error_has_priority_over_limit_flag_and_text(self):
        for text in ("OAuth expired", "Please authenticate", "Please login", "OAuth usage limit reached"):
            with self.subTest(text=text):
                review = parse_claude_response(response(text, True))
                self.assertEqual(review.kind, "errore_autenticazione")
                self.assertIsNone(review.retry_at)

    def test_auth_and_limit_can_be_reported_only_on_stderr(self):
        auth = parse_claude_response("", stderr="OAuth expired: login", returncode=1)
        self.assertEqual(auth.kind, "errore_autenticazione")
        limit = parse_claude_response("", stderr="usage limit reached; resets 5pm", returncode=1, now=NOW)
        self.assertEqual(limit.kind, "limite")
        self.assertEqual(limit.retry_at, NOW.replace(hour=17))

    def test_successful_review_can_discuss_authentication_and_limits(self):
        for first in ("ESITO: OK", "ESITO: CORREZIONI"):
            with self.subTest(first=first):
                review = parse_claude_response(response(first + "\nNOTE: test OAuth, login e usage limit reached."))
                self.assertEqual(review.kind, "ok" if first == "ESITO: OK" else "correzioni")

    def test_nonzero_exit_cannot_approve(self):
        review = parse_claude_response(response(), returncode=2)
        self.assertEqual(review.kind, "errore")
        self.assertFalse(review.approved)


class ClaudeRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        path = self.base / "config.json"
        path.write_text(json.dumps({"comando_claude": ["python.exe", "claude_finto.py"]}), encoding="utf-8")
        self.config = replace(load_config(path), timeout_claude_minuti=0.5)

    def test_exact_read_only_command(self):
        self.assertEqual(build_claude_command(self.config), [
            "python.exe", "claude_finto.py", "-p", "--output-format", "stream-json", "--verbose",
            "--model", "claude-opus-5-5", "--effort", "medium", "--tools", "Read,Grep,Glob",
            "--permission-mode", "dontAsk", "--no-session-persistence",
        ])

    def test_prompt_only_on_utf8_stdin_clean_environment_and_raw_logs(self):
        raw = (response("ESITO: OK\nNOTE: città") + "\r\n").encode("utf-8")
        stderr = "Avviso città\n".encode("utf-8")
        process = fake_process(raw, stderr)
        prompt = 'Revisione città: & | < > %PATH% "test"\nSeconda riga.'
        with patch.dict(os.environ, {"OPENAI_API_KEY": "finto", "ANTHROPIC_API_KEY": "finto"}):
            with patch("orch.claude.subprocess.Popen", return_value=process) as popen:
                result = run_claude(self.config, self.base, prompt, log_label="004")
        self.assertEqual(popen.call_args.args[0], build_claude_command(self.config))
        self.assertNotIn(prompt, popen.call_args.args[0])
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(self.base))
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        process.communicate.assert_called_once_with(input=prompt.encode("utf-8"), timeout=30)
        self.assertEqual(result.response_path.read_bytes(), raw)
        self.assertEqual(result.stderr_path.read_bytes(), stderr)
        self.assertEqual(result.prompt_path.read_bytes(), prompt.encode("utf-8"))
        self.assertTrue(result.review.approved)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.errors, ())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_public_progress_is_readable_before_claude_finishes(self):
        from orch.live_activity import ActivityReader
        process = fake_process()
        progress = {'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Controllo i componenti'}]}}
        observed = []

        def launch(*args, **kwargs):
            # Real Popen receives open files, not PIPEs buffered until communicate ends.
            output = kwargs['stdout']
            self.assertTrue(hasattr(output, 'fileno'))
            process.stdout = process.stderr = None

            def communicate(**communication):
                output.write((json.dumps(progress) + '\n').encode('utf-8'))
                output.flush()
                observed.extend(ActivityReader()._read(Path(output.name), 'claude').messages)
                output.write((json.dumps({'type': 'result', 'result': 'ESITO: OK', 'is_error': False}) + '\n').encode('utf-8'))
                output.flush()
                return None, None

            process.communicate.side_effect = communicate
            return process

        with patch('orch.claude.subprocess.Popen', side_effect=launch):
            result = run_claude(self.config, self.base, 'Prompt', log_label='015')
        self.assertEqual(observed, ['Controllo i componenti'])
        self.assertTrue(result.review.approved)
        metadata = next((self.base / 'log').glob('*.activity.json'))
        self.assertEqual(json.loads(metadata.read_text(encoding='utf-8'))['role'], 'controllore')

    def test_timeout_terminates_tree_and_preserves_partial_then_complete_response(self):
        raw = response().encode("utf-8")
        process = fake_process(returncode=-9)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("claude", 30, output=b'{"result":', stderr=b"prima\n"),
            (raw, b"prima\ndopo\n"),
        ]
        with patch("orch.claude.subprocess.Popen", return_value=process):
            with patch("orch.claude.terminate_process_tree", return_value=None) as kill:
                result = run_claude(self.config, self.base, "Prompt")
        kill.assert_called_once()
        self.assertIs(kill.call_args.args[0], process)
        self.assertIn("environment", kill.call_args.kwargs)
        self.assertEqual(process.communicate.call_args_list[1].kwargs, {"timeout": 5})
        self.assertTrue(result.timed_out)
        self.assertEqual(result.review.kind, "errore")
        self.assertFalse(result.review.approved)
        self.assertEqual(result.response_path.read_bytes(), raw)
        self.assertEqual(result.stderr_path.read_bytes(), b"prima\ndopo\n")

    def test_timeout_cleanup_is_bounded_and_keeps_partial_json(self):
        process = fake_process(returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("claude", 30, output=b'{"result":', stderr=b"prima"),
            subprocess.TimeoutExpired("claude", 5, output=b'{"result":"parziale', stderr=b"dopo"),
        ]
        process.wait.side_effect = subprocess.TimeoutExpired("claude", 5)
        with patch("orch.claude.subprocess.Popen", return_value=process):
            with patch("orch.claude.terminate_process_tree", return_value="taskkill fallito") as kill:
                result = run_claude(self.config, self.base, "Prompt")
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(result.review.kind, "errore")
        self.assertEqual(result.response_path.read_bytes(), b'{"result":"parziale')
        self.assertEqual(result.stderr_path.read_bytes(), b"dopo")
        self.assertTrue(any("non termina" in error for error in result.errors))
        for pipe in (process.stdin, process.stdout, process.stderr):
            self.assertFalse(pipe.closed)
            pipe.close()

    def test_start_failure_is_error_and_logs_still_exist(self):
        with patch("orch.claude.subprocess.Popen", side_effect=FileNotFoundError("Claude assente")):
            result = run_claude(self.config, self.base, "Prompt")
        self.assertEqual(result.review.kind, "errore")
        self.assertIsNone(result.returncode)
        self.assertEqual(result.launch_error, "Claude assente")
        self.assertEqual(result.response_path.read_bytes(), b"")
        self.assertEqual(result.stderr_path.read_bytes(), b"")
        self.assertEqual(result.prompt_path.read_bytes(), b"Prompt")

    def test_no_automatic_retry_for_unrecognized_review(self):
        process = fake_process(response("Formato non riconosciuto").encode("utf-8"))
        with patch("orch.claude.subprocess.Popen", return_value=process) as popen:
            result = run_claude(self.config, self.base, "Prompt")
        popen.assert_called_once()
        self.assertEqual(result.review.kind, "non_riconosciuto")

    def test_prompt_truncation_and_unique_safe_log_paths(self):
        first_process = fake_process(response().encode("utf-8"))
        with patch("orch.claude.subprocess.Popen", side_effect=[
            first_process, fake_process(response().encode("utf-8")),
        ]) as popen:
            first = run_claude(self.config, self.base, "x" * 70000, log_label="../004")
            second = run_claude(self.config, self.base, "Prompt", log_label="../004")
        self.assertNotIn("x" * 100, " ".join(popen.call_args_list[0].args[0]))
        logged = first.prompt_path.read_text(encoding="utf-8")
        self.assertEqual(first_process.communicate.call_args.kwargs["input"], logged.encode("utf-8"))
        self.assertEqual(len(logged), 60000)
        self.assertIn("TRONCATO", logged)
        self.assertEqual(first.prompt_path.parent, self.base / "log")
        self.assertNotEqual(first.prompt_path, second.prompt_path)

    def test_limit_and_authentication_results_from_fake_claude(self):
        for text, expected in (("resets 5pm", "limite"), ("OAuth expired: login", "errore_autenticazione")):
            with self.subTest(text=text):
                process = fake_process(response(text, True).encode("utf-8"), returncode=1)
                with patch("orch.claude.subprocess.Popen", return_value=process):
                    result = run_claude(self.config, self.base, "Prompt", now=NOW)
                self.assertEqual(result.review.kind, expected)

    def test_interrupt_terminates_tree_and_leaves_log_artifacts(self):
        process = fake_process()
        process.communicate.side_effect = KeyboardInterrupt()
        with patch("orch.claude.subprocess.Popen", return_value=process):
            with patch("orch.claude.terminate_process_tree", return_value=None) as kill:
                with self.assertRaises(KeyboardInterrupt):
                    run_claude(self.config, self.base, "Prompt")
        kill.assert_called_once()
        self.assertEqual(len(list((self.base / "log").glob("*.response.json"))), 1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


if __name__ == "__main__":
    unittest.main()
