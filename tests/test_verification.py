from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from orch.config import load_config
from orch.git_paths import project_pathspecs
from orch.verification import (
    VerificationError, extract_error_lines, list_changed_files,
    run_verification_command, run_verifications, verification_summary,
)


TEST_ROOT = Path(__file__).resolve().parent


def fake_process(output=b"", returncode=0):
    process = Mock(pid=12345)
    process.communicate.return_value = (output, None)
    process.poll.return_value = returncode
    process.wait.return_value = returncode
    process.stdout = io.BytesIO(output)
    return process


class ErrorLinesTests(unittest.TestCase):
    def test_keywords_are_case_insensitive_and_context_is_merged(self):
        output = "\n".join(("prima", "ERROR uno", "contesto", "AssertionError due", "dopo", "ultimo"))
        self.assertEqual(extract_error_lines(output, context=1), (
            "1: prima", "2: ERROR uno", "3: contesto", "4: AssertionError due", "5: dopo",
        ))

    def test_all_required_keywords(self):
        output = "\n".join(("error", "errore", "FAIL", "Exception", "Traceback", "assert x"))
        self.assertEqual(len(extract_error_lines(output, context=0)), 6)

    def test_limit_sixty_and_original_line_numbers(self):
        output = "\n".join(f"FAIL {i}" for i in range(100))
        lines = extract_error_lines(output)
        self.assertEqual(len(lines), 60)
        self.assertEqual(lines[-1], "60: FAIL 59")
        self.assertEqual(extract_error_lines("ok\nok\nerror\nok", context=0), ("3: error",))

    def test_empty_no_errors_and_zero_budget(self):
        for output in ("", "Tutto completato\nOK"):
            self.assertEqual(extract_error_lines(output), ())
        self.assertEqual(extract_error_lines("error", max_lines=0), ())
        with self.assertRaises(ValueError):
            extract_error_lines("error", context=-1)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        path = self.base / "config.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        self.config = replace(load_config(path), timeout_verifica_minuti=0.5)

    def test_command_runs_as_shell_line_in_project_with_timeout_and_combined_output(self):
        output = "città\ncontesto\nERROR test fallito\ndopo\n".encode("utf-8")
        process = fake_process(output, 1)
        command = '"C:\\Python con spazi\\python.exe" -m unittest && echo fine'
        with patch("orch.verification.subprocess.Popen", return_value=process) as popen:
            result = run_verification_command(self.config, self.base, command)
        self.assertEqual(popen.call_args.args, (command,))
        self.assertTrue(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.base))
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        process.communicate.assert_called_once_with(timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.output, output.decode("utf-8"))
        self.assertIn("3: ERROR test fallito", result.error_lines)
        self.assertIsNone(result.error)
        self.assertTrue(process.stdout.closed)

    def test_success_depends_on_exit_code_even_if_output_mentions_error(self):
        with patch("orch.verification.subprocess.Popen", return_value=fake_process(b"0 errors, OK")):
            result = run_verification_command(self.config, self.base, "test")
        self.assertTrue(result.ok)
        self.assertFalse(result.timed_out)

    def test_failure_without_keywords_preserves_output_tail(self):
        output = "\n".join(f"Riga {i}" for i in range(20)).encode("utf-8")
        with patch("orch.verification.subprocess.Popen", return_value=fake_process(output, 2)):
            result = run_verification_command(self.config, self.base, "test")
        self.assertEqual(len(result.error_lines), 8)
        self.assertEqual(result.error_lines[-1], "20: Riga 19")

    def test_spawn_failure_is_a_failed_verification(self):
        with patch("orch.verification.subprocess.Popen", side_effect=OSError("Shell assente")):
            result = run_verification_command(self.config, self.base, "test")
        self.assertIsNone(result.returncode)
        self.assertFalse(result.ok)
        self.assertIn("Shell assente", result.error)

    def test_timeout_kills_tree_and_keeps_full_output_without_duplicates(self):
        process = fake_process(returncode=-9)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("test", 30, output=b"prima\n"),
            (b"prima\nERROR dopo\n", None),
        ]
        with patch("orch.verification.subprocess.Popen", return_value=process):
            with patch("orch.verification.terminate_process_tree", return_value=None) as kill:
                result = run_verification_command(self.config, self.base, "test")
        kill.assert_called_once()
        self.assertIs(kill.call_args.args[0], process)
        self.assertIn("environment", kill.call_args.kwargs)
        self.assertEqual(process.communicate.call_args_list[1].kwargs, {"timeout": 5})
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "prima\nERROR dopo\n")
        self.assertIn("Timeout verifica", result.error)

    def test_timeout_cleanup_is_bounded_and_records_kill_error(self):
        process = fake_process(returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("test", 30, output=b"prima\n"),
            subprocess.TimeoutExpired("test", 5, output=b"prima\nERROR finale\n"),
        ]
        process.wait.side_effect = subprocess.TimeoutExpired("test", 5)
        with patch("orch.verification.subprocess.Popen", return_value=process):
            with patch("orch.verification.terminate_process_tree", return_value="taskkill fallito") as kill:
                result = run_verification_command(self.config, self.base, "test")
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(result.output, "prima\nERROR finale\n")
        self.assertIn("taskkill fallito", result.error)
        self.assertIn("non termina", result.error)
        self.assertFalse(process.stdout.closed)
        process.stdout.close()

    def test_interrupt_still_terminates_the_tree(self):
        process = fake_process()
        process.communicate.side_effect = KeyboardInterrupt()
        with patch("orch.verification.subprocess.Popen", return_value=process):
            with patch("orch.verification.terminate_process_tree", return_value=None) as kill:
                with self.assertRaises(KeyboardInterrupt):
                    run_verification_command(self.config, self.base, "test")
        kill.assert_called_once()
        process.stdout.close()

    def test_batch_runs_all_commands_with_global_diagnostic_budget(self):
        output = "\n".join(f"FAIL {i}" for i in range(80)).encode("utf-8")
        with patch("orch.verification.subprocess.Popen", side_effect=[
            fake_process(output, 1), fake_process(output, 1),
        ]) as popen:
            results = run_verifications(self.config, self.base, ["primo", "secondo"])
        self.assertEqual(popen.call_count, 2)
        self.assertEqual([r.command for r in results], ["primo", "secondo"])
        self.assertEqual(sum(len(r.error_lines) for r in results), 60)
        self.assertEqual(results[1].error_lines, ())
        self.assertEqual(len(results[1].output.splitlines()), 80)
        summary = verification_summary(results)
        self.assertEqual(summary[1]["exit_code"], 1)
        self.assertFalse(summary[1]["ok"])
        self.assertNotIn("output", summary[0])
        self.assertEqual(run_verifications(self.config, self.base, []), ())

    def test_git_status_preserves_status_columns_untracked_and_renames(self):
        raw = ' M app.py\nA  nuovo.py\n?? cartella/file.txt\nR  "prima a" -> "dopo a"\n'
        completed = Mock(returncode=0, stdout=raw.encode("utf-8"), stderr=b"")
        with patch("orch.verification.subprocess.run", return_value=completed) as run:
            changed = list_changed_files(self.config, self.base)
        self.assertEqual(changed, tuple(raw.splitlines()))
        self.assertEqual(run.call_args.args[0], ["git", "status", "--porcelain", "--untracked-files=all",
                                               *project_pathspecs(self.config)])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.base))
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_stop_between_commands_does_not_launch_remaining_checks(self):
        def launch(*args, **kwargs):
            (self.base / "STOP").touch()
            return fake_process(b"OK", 0)

        with patch("orch.verification.subprocess.Popen", side_effect=launch) as popen:
            results = run_verifications(self.config, self.base, ["primo", "secondo"])
        self.assertEqual(len(results), 1)
        popen.assert_called_once()

    def test_git_clean_tree_and_failures(self):
        with patch("orch.verification.subprocess.run", return_value=Mock(returncode=0, stdout=b"")):
            self.assertEqual(list_changed_files(self.config, self.base), ())
        with patch("orch.verification.subprocess.run", return_value=Mock(returncode=128, stderr=b"not a git repository")):
            with self.assertRaisesRegex(VerificationError, "128"):
                list_changed_files(self.config, self.base)
        for error in (OSError("Git assente"), subprocess.TimeoutExpired("git", 30)):
            with self.subTest(error=error):
                with patch("orch.verification.subprocess.run", side_effect=error):
                    with self.assertRaises(VerificationError):
                        list_changed_files(self.config, self.base)


if __name__ == "__main__":
    unittest.main()
