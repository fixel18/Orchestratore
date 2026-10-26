from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from orch.codex import (
    CodexJSONLParser, build_codex_command, build_first_prompt, build_resume_prompt,
    clean_child_environment, parse_codex_jsonl, run_codex, terminate_process_tree,
)
from orch.config import load_config


TEST_ROOT = Path(__file__).resolve().parent
THREAD = "12345678-1234-1234-1234-123456789abc"


def jsonl(*events):
    return "\r\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\r\n"


def successful_events():
    return (
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Finito: città ✓"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    )


class ParserTests(unittest.TestCase):
    def test_thread_last_message_and_completion(self):
        start, message, complete = successful_events()
        output = parse_codex_jsonl(jsonl(
            start,
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Primo"}},
            {"type": "item.completed", "item": {"type": "command_execution", "text": "Ignorato"}},
            message, complete,
        ))
        self.assertEqual(output.thread_id, THREAD)
        self.assertEqual(output.last_message, "Finito: città ✓")
        self.assertTrue(output.turn_completed)
        self.assertEqual(output.errors, [])

    def test_callback_is_called_immediately_and_only_once(self):
        callback = Mock()
        parser = CodexJSONLParser(on_thread=callback)
        parser.feed(json.dumps(successful_events()[0]))
        callback.assert_called_once_with(THREAD)
        self.assertFalse(parser.output.turn_completed)
        parser.feed(json.dumps(successful_events()[0]))
        callback.assert_called_once()

    def test_callback_failure_propagates(self):
        parser = CodexJSONLParser(on_thread=Mock(side_effect=OSError("Stato non salvato")))
        with self.assertRaisesRegex(OSError, "Stato non salvato"):
            parser.feed(json.dumps(successful_events()[0]))

    def test_error_and_turn_failed_codes(self):
        output = parse_codex_jsonl(jsonl(
            {"type": "error", "message": "Errore", "code": "rate_limit_reached"},
            {"type": "turn.failed", "error": {"message": "Quota", "code": "quota_exceeded"}},
            {"type": "turn.failed", "error": {"message": "Altro"}},
        ))
        self.assertEqual([e.message for e in output.errors], ["Errore", "Quota", "Altro"])
        self.assertEqual([e.code for e in output.errors], ["rate_limit_reached", "quota_exceeded", None])

    def test_malformed_and_partial_jsonl_does_not_hide_later_events(self):
        output = parse_codex_jsonl('not json\n[]\n{"type":\n' + jsonl(*successful_events()))
        self.assertEqual(len(output.errors), 3)
        self.assertEqual(output.thread_id, THREAD)
        self.assertTrue(output.turn_completed)

    def test_bom_blank_and_unknown_events(self):
        output = parse_codex_jsonl("\ufeff" + jsonl(*successful_events()) + '\n{"type":"future.event"}\n')
        self.assertEqual(output.errors, [])

    def test_invalid_thread_and_changed_resume_thread(self):
        for thread_id in (None, "", "../x", 12):
            with self.subTest(thread_id=thread_id):
                output = parse_codex_jsonl(jsonl({"type": "thread.started", "thread_id": thread_id}))
                self.assertEqual(len(output.errors), 1)
        parser = CodexJSONLParser(thread_id=THREAD)
        parser.feed(json.dumps({"type": "thread.started", "thread_id": "another-thread"}))
        self.assertEqual(parser.output.thread_id, THREAD)
        self.assertEqual(len(parser.output.errors), 1)


class RecordingInput(io.BytesIO):
    saved = None

    def close(self):
        if not self.closed:
            self.saved = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, running=False):
        self.stdin = RecordingInput()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.running = running
        self.pid = 12345

    def poll(self):
        return None if self.running else self.returncode

    def wait(self, timeout=None):
        if self.running:
            raise subprocess.TimeoutExpired("codex finto", timeout)
        return self.returncode

    def kill(self):
        self.running = False
        self.returncode = -9


class CodexRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        config_path = self.base / "config.json"
        config_path.write_text(json.dumps({
            "comando_codex": ["python.exe", "codex_finto.py"],
            "cartella_sessioni_codex": "sessioni-finte",
        }), encoding="utf-8")
        self.config = load_config(config_path)

    def test_exec_and_resume_commands_keep_argument_lists(self):
        project = self.base / "progetto con spazi"
        last = self.base / "ultimo messaggio.txt"
        self.assertEqual(build_codex_command(self.config, project, last), [
            "python.exe", "codex_finto.py", "exec", "--json",
            "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"',
            "-s", "workspace-write",
            "-C", str(project), "-o", str(last), "-",
        ])
        resume = build_codex_command(self.config, project, last, thread_id=THREAD)
        self.assertEqual(resume, [
            "python.exe", "codex_finto.py", "exec", "resume", "--json",
            "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"',
            "-c", 'sandbox_mode="workspace-write"',
            "-o", str(last), THREAD, "-",
        ])
        self.assertNotIn("-C", resume)
        self.assertNotIn("-s", resume)
        with self.assertRaises(ValueError):
            build_codex_command(self.config, project, last, thread_id="../*")

    def test_model_and_reasoning_are_explicit_for_both_commands(self):
        config = replace(self.config, modello_codex="modello-finto", ragionamento_codex="low")
        for thread_id in (None, THREAD):
            with self.subTest(thread_id=thread_id):
                command = build_codex_command(config, self.base, self.base / "last.txt", thread_id=thread_id)
                self.assertEqual(command[command.index("-m") + 1], "modello-finto")
                self.assertIn('model_reasoning_effort="low"', command)

    def test_environment_is_copied_and_keys_removed_case_insensitively(self):
        environment = {"PATH": "bin", "OPENAI_API_KEY": "finto", "anthropic_api_key": "finto", "CUSTOM": "ok"}
        result = clean_child_environment(self.config.variabili_da_rimuovere, environment)
        self.assertEqual(result, {"PATH": "bin", "CUSTOM": "ok"})
        self.assertEqual(environment["OPENAI_API_KEY"], "finto")
        self.assertEqual(clean_child_environment(["CUSTOM"], environment)["PATH"], "bin")

    def test_first_prompt_and_followups(self):
        prompt = build_first_prompt("Regole fisse", "Fascicolo intero", 2, "Integra Codex.")
        self.assertIn("Regole fisse", prompt)
        self.assertIn("Fascicolo intero", prompt)
        self.assertIn("Esegui ora il pezzo 2: Integra Codex.", prompt)
        self.assertEqual(build_resume_prompt("Correzioni: completa i test."), "Correzioni: completa i test.")

    def test_prompt_is_only_stdin_utf8_eof_raw_jsonl_and_clean_environment(self):
        raw = jsonl(*successful_events()).encode("utf-8")
        process = FakeProcess(raw, "Avviso: città\n".encode("utf-8"))
        callback = Mock()
        prompt = 'Correggi città, & echo niente | < > %PATH% "test"\nSeconda riga.'
        with patch.dict(os.environ, {"OPENAI_API_KEY": "finto", "ANTHROPIC_API_KEY": "finto"}):
            with patch("orch.codex.subprocess.Popen", return_value=process) as popen:
                result = run_codex(self.config, self.base, prompt, on_thread=callback, log_label="001")
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn(prompt, args[0])
        self.assertEqual(args[0][-1], "-")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(self.base))
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        self.assertEqual(process.stdin.saved, prompt.encode("utf-8"))
        self.assertTrue(process.stdin.closed)
        self.assertEqual(result.jsonl_path.parent, self.base / "log")
        self.assertEqual(result.jsonl_path.read_bytes(), raw)
        self.assertEqual(result.prompt_path.read_bytes(), prompt.encode("utf-8"))
        self.assertEqual(result.stderr_path.read_bytes(), "Avviso: città\n".encode("utf-8"))
        self.assertEqual(result.output.last_message, "Finito: città ✓")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output.errors, [])
        self.assertIsNone(result.usage_limit)
        callback.assert_called_once_with(THREAD)

    def test_streaming_callback_before_output_completes(self):
        gate = Event()
        process = FakeProcess(jsonl(*successful_events()).encode("utf-8"))
        original_readline = process.stdout.readline
        reads = 0
        released_before_tail = []

        def readline():
            nonlocal reads
            reads += 1
            if reads == 2:
                released_before_tail.append(gate.wait(timeout=2))
            return original_readline()

        with patch.object(process.stdout, "readline", side_effect=readline):
            with patch("orch.codex.subprocess.Popen", return_value=process):
                result = run_codex(self.config, self.base, "Prompt", on_thread=lambda _: gate.set())
        self.assertTrue(gate.is_set())
        self.assertEqual(released_before_tail, [True])
        self.assertTrue(result.output.turn_completed)

    def test_resume_preserves_existing_thread_without_thread_started_event(self):
        process = FakeProcess(jsonl(*successful_events()[1:]).encode("utf-8"))
        with patch("orch.codex.subprocess.Popen", return_value=process) as popen:
            result = run_codex(self.config, self.base, "Altro pezzo", thread_id=THREAD)
        self.assertEqual(result.output.thread_id, THREAD)
        self.assertEqual(result.output.errors, [])
        self.assertIn("resume", popen.call_args.args[0])

    def test_timeout_terminates_tree_and_preserves_partial_jsonl(self):
        raw = jsonl(successful_events()[0]).encode("utf-8")
        process = FakeProcess(raw, running=True)
        config = replace(self.config, timeout_codex_minuti=0.001)
        with patch("orch.codex.subprocess.Popen", return_value=process):
            with patch("orch.codex.terminate_process_tree", side_effect=lambda p, **kw: p.kill()) as kill:
                result = run_codex(config, self.base, "Prompt")
        kill.assert_called_once()
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, -9)
        self.assertEqual(result.jsonl_path.read_bytes(), raw)
        self.assertEqual(result.output.thread_id, THREAD)
        self.assertIn("timeout", [e.code for e in result.output.errors])

    def test_callback_failure_terminates_tree_and_is_not_swallowed(self):
        process = FakeProcess(jsonl(*successful_events()).encode("utf-8"), running=True)
        with patch("orch.codex.subprocess.Popen", return_value=process):
            with patch("orch.codex.terminate_process_tree", side_effect=lambda p, **kw: p.kill()) as kill:
                with self.assertRaisesRegex(OSError, "Disco pieno"):
                    run_codex(self.config, self.base, "Prompt", on_thread=Mock(side_effect=OSError("Disco pieno")))
        kill.assert_called_once()
        self.assertEqual(len(list((self.base / "log").glob("*.jsonl"))), 1)

    def test_usage_limit_error_has_text_reset(self):
        raw = jsonl(
            successful_events()[0],
            {"type": "turn.failed", "error": {
                "message": "You've hit your usage limit. or try again at 3:05 PM on 24 Oct 2026.",
                "code": "usage_limit_reached",
            }},
        ).encode("utf-8")
        process = FakeProcess(raw, returncode=1)
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Prompt", now=datetime(2026, 10, 22, tzinfo=timezone.utc))
        self.assertEqual(result.usage_limit.source, "testo")
        self.assertEqual(result.usage_limit.retry_at, datetime(2026, 10, 24, 15, 5, tzinfo=timezone.utc))

    def test_agent_message_quoting_usage_limits_is_not_a_limit(self):
        message = "Ho testato usage_limit_reached, rate_limit_reached e You've hit your usage limit."
        process = FakeProcess(jsonl(
            successful_events()[0],
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
            successful_events()[2],
        ).encode("utf-8"))
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Prompt")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.output.turn_completed)
        self.assertEqual(result.output.last_message, message)
        self.assertEqual(result.output.errors, [])
        self.assertIsNone(result.usage_limit)

    def test_stderr_usage_limit_is_still_recognized(self):
        process = FakeProcess(
            jsonl(successful_events()[0]).encode("utf-8"),
            b"You've hit your usage limit. try again at 15:05", returncode=1,
        )
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Prompt")
        self.assertIsNotNone(result.usage_limit)

    def test_resume_limit_uses_fake_rollout_including_past_reset(self):
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        reset = datetime(2026, 10, 22, 11, tzinfo=timezone.utc)
        folder = self.config.cartella_sessioni_codex / "2026" / "10" / "22"
        folder.mkdir(parents=True)
        (folder / f"rollout-finto-{THREAD}.jsonl").write_text(json.dumps({
            "payload": {"rate_limits": {"primary": {
                "used_percent": 100, "window_minutes": 300, "resets_at": reset.timestamp(),
            }}},
        }), encoding="utf-8")
        process = FakeProcess(jsonl({
            "type": "error", "code": "quota_exceeded", "message": "Limite raggiunto",
        }).encode("utf-8"), returncode=1)
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Riprova", thread_id=THREAD, now=now)
        self.assertEqual(result.usage_limit.source, "rollout")
        self.assertEqual(result.usage_limit.retry_at, reset)
        self.assertFalse(result.timed_out)

    def test_success_without_thread_id_is_a_protocol_error(self):
        process = FakeProcess(jsonl(*successful_events()[1:]).encode("utf-8"))
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Prompt")
        self.assertIn("senza thread.started", result.output.errors[0].message)

    def test_nonzero_exit_spawn_failure_and_incomplete_success(self):
        process = FakeProcess(stderr=b"Errore di connessione", returncode=2)
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Prompt")
        self.assertIn("codice 2", result.output.errors[0].message)
        with patch("orch.codex.subprocess.Popen", side_effect=FileNotFoundError("Codex assente")):
            result = run_codex(self.config, self.base, "Prompt")
        self.assertIsNone(result.returncode)
        self.assertIn("Avvio Codex", result.output.errors[0].message)
        self.assertEqual(result.launch_error, "Codex assente")
        process = FakeProcess(jsonl(successful_events()[0]).encode("utf-8"))
        with patch("orch.codex.subprocess.Popen", return_value=process):
            result = run_codex(self.config, self.base, "Prompt")
        self.assertIn("senza turn.completed", result.output.errors[0].message)

    def test_last_message_file_is_fallback_and_log_names_are_unique(self):
        def fake_start(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text("Ultimo messaggio", encoding="utf-8")
            return FakeProcess(jsonl(successful_events()[0], successful_events()[2]).encode("utf-8"))

        with patch("orch.codex.subprocess.Popen", side_effect=fake_start):
            first = run_codex(self.config, self.base, "Uno", log_label="../001")
            second = run_codex(self.config, self.base, "Due", log_label="../001")
        self.assertEqual(first.output.last_message, "Ultimo messaggio")
        self.assertNotEqual(first.jsonl_path, second.jsonl_path)
        self.assertEqual(first.jsonl_path.parent, self.base / "log")


class TerminationTests(unittest.TestCase):
    def test_windows_taskkill_targets_the_whole_tree_with_argument_list(self):
        process = Mock(pid=12345)
        with patch("orch.codex.subprocess.run", return_value=Mock(returncode=0)) as run:
            self.assertIsNone(terminate_process_tree(process, windows=True, environment={"PATH": "bin"}))
        self.assertEqual(run.call_args.args[0], ["taskkill", "/T", "/F", "/PID", "12345"])
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "bin"})
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        process.kill.assert_not_called()

    def test_taskkill_failure_or_timeout_uses_fallback_and_reports_failure(self):
        for effect in (Mock(returncode=1), subprocess.TimeoutExpired("taskkill", 15)):
            with self.subTest(effect=effect):
                process = Mock(pid=12345)
                options = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
                with patch("orch.codex.subprocess.run", **options):
                    self.assertIsNotNone(terminate_process_tree(process, windows=True))
                process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
