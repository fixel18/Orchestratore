from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from finti.catalogo_finto import CATALOGO

from orchestratore import main
from orch.persistence import LockError
from orch.ciclo import Services


TEST_ROOT = Path(__file__).resolve().parent


class CliTests(unittest.TestCase):
    def setUp(self):
        catalog = patch("orch.impostazioni.catalogo", return_value=CATALOGO)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "config.json"
        self.path.write_text("{}", encoding="utf-8")
        (self.base / "coda").mkdir()
        self.fake_shutdown = Mock()
        factory = patch("orch.ciclo.Services", return_value=Services(
            shutdown=self.fake_shutdown, generate_final_report=lambda *args: "# Report finto"))
        factory.start()
        self.addCleanup(factory.stop)

    def args(self, *args):
        return ["--config", str(self.path), *args]

    def test_una_volta_empty_queue_exits_and_releases_lock(self):
        self.assertEqual(main(self.args("--una-volta")), 0)
        self.assertFalse((self.base / "orchestratore.lock").exists())

    def test_stato_is_read_only_and_does_not_acquire_running_lock(self):
        lock = self.base / "orchestratore.lock"
        lock.write_text("lock esistente", encoding="utf-8")
        (self.base / "stato.json").write_text('{"fase":"verifica"}', encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output), patch("orchestratore.Orchestrator") as engine:
            self.assertEqual(main(self.args("--stato")), 0)
        engine.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["fase"], "verifica")
        self.assertEqual(lock.read_text(encoding="utf-8"), "lock esistente")

    def test_stop_blocks_start_and_is_not_removed(self):
        stop = self.base / "STOP"
        stop.touch()
        with redirect_stdout(io.StringIO()), patch("orchestratore.Orchestrator") as engine:
            self.assertEqual(main(self.args("--una-volta")), 0)
        engine.assert_not_called()
        self.assertTrue(stop.exists())

    def test_ignora_stop_removes_stop_after_lock_and_runs_once(self):
        stop = self.base / "STOP"
        stop.touch()
        engine = Mock(state={}, run=Mock(return_value=0))
        with patch("orchestratore.Orchestrator", return_value=engine):
            self.assertEqual(main(self.args("--ignora-stop", "--una-volta")), 0)
        self.assertFalse(stop.exists())
        engine.run.assert_called_once_with(una_volta=True)

    def test_report_does_not_start_actors(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("orchestratore.Orchestrator") as engine:
            self.assertEqual(main(self.args("--report")), 0)
        engine.assert_not_called()
        self.assertTrue(Path(output.getvalue().strip()).is_file())

    def test_non_spegnere_is_forwarded_to_engine(self):
        engine = Mock(state={}, run=Mock(return_value=0))
        with patch("orchestratore.Orchestrator", return_value=engine) as constructor:
            self.assertEqual(main(self.args("--non-spegnere", "--una-volta")), 0)
        self.assertTrue(constructor.call_args.kwargs["non_spegnere"])
        self.fake_shutdown.assert_not_called()

    def test_live_lock_does_not_start_engine(self):
        with redirect_stderr(io.StringIO()), patch("orchestratore.ProcessLock", side_effect=LockError("PID attivo")):
            with patch("orchestratore.Orchestrator") as engine:
                self.assertEqual(main(self.args("--una-volta")), 1)
        engine.assert_not_called()

    def test_login_exit_is_clear_and_lock_is_released(self):
        engine = Mock(state={"motivo": "serve login: codex"}, run=Mock(return_value=2))
        error = io.StringIO()
        with redirect_stderr(error), patch("orchestratore.Orchestrator", return_value=engine):
            self.assertEqual(main(self.args("--una-volta")), 2)
        self.assertIn("serve login", error.getvalue())
        self.assertFalse((self.base / "orchestratore.lock").exists())

    def test_stato_redirected_to_pipe_is_utf8_even_with_ascii_environment(self):
        (self.base / "stato.json").write_text('{"motivo":"città, caffè, perché"}', encoding="utf-8")
        environment = {**os.environ, "PYTHONIOENCODING": "ascii:replace"}
        result = subprocess.run(
            [sys.executable, str(TEST_ROOT.parent / "orchestratore.py"), *self.args("--stato")],
            cwd=self.base, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["motivo"], "città, caffè, perché")

    def test_stderr_is_reconfigured_before_printing_error(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="ascii", errors="replace")
        self.addCleanup(stream.close)
        engine = Mock(state={"motivo": "serve login: città"}, run=Mock(return_value=2))
        with redirect_stderr(stream), patch("orchestratore.Orchestrator", return_value=engine):
            self.assertEqual(main(self.args("--una-volta")), 2)
        stream.flush()
        self.assertEqual(buffer.getvalue().decode("utf-8").strip(), "serve login: città")
