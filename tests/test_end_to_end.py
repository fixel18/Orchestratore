"""Processi Codex/Claude finti, shell e git reali solo nei repository temporanei.

Esecuzione: python -m unittest discover -s tests -v
Ollama usa il trasporto iniettato: nessun servizio esterno o rete reale.
"""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

import orchestratore
from orch.ciclo import Orchestrator, Services
from orch.codex import run_codex
from orch.config import load_config
from orch.git_ops import GitOperations
from orch.model import decide_next_action
from orch.persistence import StateStore
from orch.reporting import ReportStore
from orch.review import collect_review_diff
from orch.verification import list_changed_files
from finti.ollama_finto import transport
from finti.scenario import SCENARIO_ENV
from finti.catalogo_finto import CATALOGO


TEST_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_ROOT.parent
FAKES = TEST_ROOT / "finti"
DAY = "2026-10-22"


class TestClock:
    def __init__(self):
        self.value = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


@unittest.skipUnless(shutil.which("git"), "git necessario per i test end-to-end")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        catalog = patch("orch.impostazioni.catalogo", return_value=CATALOGO)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="orch e2e ", dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(TEST_ROOT))
        self.base = self.root / "orchestratore finto"
        self.project = self.root / "repo di prova"
        self.base.mkdir()
        self.project.mkdir()
        for folder in ("coda", "in-corso", "fatte", "scartate", "regole", "report", "log"):
            (self.base / folder).mkdir()
        for name in ("istruzioni-codex.md", "prompt-modello.md", "prompt-verifica-claude.md"):
            shutil.copyfile(PROJECT_ROOT / "regole" / name, self.base / "regole" / name)
        self.scenario_path = self.root / "scenario.json"
        self.scenario_path.write_text(json.dumps({
            "solo_test": True, "progetto": str(self.project), "cartella_base": str(self.base),
            "tasks": {}, "storia": [],
        }), encoding="utf-8")
        self.environment = patch.dict(os.environ, {
            SCENARIO_ENV: str(self.scenario_path), "OPENAI_API_KEY": "chiave-finta-da-rimuovere",
            "ANTHROPIC_API_KEY": "chiave-finta-da-rimuovere", "PYTHONIOENCODING": "utf-8",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # Impedisce davvero l'accesso HTTP anche se un refactoring perdesse l'iniezione.
        self.network_patch = patch("orch.model.urlopen", side_effect=AssertionError("Rete reale vietata nei test"))
        self.network = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.git("init", "-b", "produzione3")
        self.git("config", "user.name", "Orchestratore Test")
        self.git("config", "user.email", "orchestratore@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        hooks = self.root / "hooks vuoti"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        self.git("config", "core.autocrlf", "false")
        excludes = self.root / "esclusioni git vuote"
        excludes.write_text("", encoding="utf-8")
        self.git("config", "core.excludesFile", str(excludes))
        (self.project / "app.txt").write_text("iniziale\n", encoding="utf-8")
        self.git("add", "app.txt")
        self.git("commit", "-m", "iniziale")
        self.initial_commit = self.git("rev-parse", "HEAD").strip()
        self.config_path = self.base / "config.json"
        (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":false}', encoding="utf-8")
        self.config_path.write_text(json.dumps({
            "cartella_base": str(self.base), "python": sys.executable,
            "comando_codex": [sys.executable, str(FAKES / "codex_finto.py")],
            "comando_claude": [sys.executable, str(FAKES / "claude_finto.py")],
            "url_modello": "http://127.0.0.1:1/api/chat", "timeout_modello_s": 1,
            "num_ctx_modello": 16384, "push_abilitato": True, "spegnimento_abilitato": False,
            "cartella_sessioni_codex": str(self.root / "sessioni finte"),
            "attesa_limite_minuti": 0.001, "timeout_codex_minuti": 0.3,
            "timeout_claude_minuti": 0.3, "timeout_verifica_minuti": 0.3,
        }), encoding="utf-8")
        self.clock = TestClock()
        self.services = Services(decide_next_action=self.model, clock=self.clock.now, sleep=self.clock.sleep,
            generate_final_report=lambda *args: "# Report finto",
            shutdown=lambda *args: self.fail("Spegnimento disabilitato nei test end-to-end"))

    def git(self, *args):
        # Anche i comandi mutanti del test restano nel repository temporaneo.
        self.assertTrue(self.project.resolve().is_relative_to(self.root))
        result = subprocess.run(["git", *args], cwd=self.project, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout.decode("utf-8", errors="replace")

    def data(self):
        return json.loads(self.scenario_path.read_text(encoding="utf-8"))

    def task(self, identity, title, kind):
        data = self.data()
        data["tasks"][identity] = {"scenario": kind,
                                   "thread_id": str(uuid5(NAMESPACE_URL, str(self.root) + identity))}
        self.scenario_path.write_text(json.dumps(data), encoding="utf-8")
        args = [sys.executable, str(FAKES / "verifica_finta.py"), "--task", identity]
        command = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
        filename = f"{identity}-finta.md"
        (self.base / "coda" / filename).write_text(
            f"---\nid: {identity}\ntitolo: {title}\nprogetto: {self.project}\n"
            "branch: produzione3\npriorita: alta\npush: si\n---\n"
            "## Contesto\nScenario automatico locale: caffè.\n"
            "## Obiettivo\nCompleta app.txt.\n## Passi\n1. Completa il file app.txt.\n"
            f"## Verifica\n```\n{command}\n```\n## Fuori perimetro\nAltri file.\n",
            encoding="utf-8")
        return filename

    def model(self, *args, **kwargs):
        return decide_next_action(*args, **kwargs, transport=transport)

    def cli(self, *extra):
        # La CLI e il lock sono reali; l'unica sostituzione della factory inietta
        # trasporto modello, clock e (nel test C) interruzione del callback.
        with patch.object(orchestratore, "Orchestrator",
                          side_effect=lambda config, **kwargs: Orchestrator(
                              config, services=self.services, **kwargs)):
            result = orchestratore.main(["--config", str(self.config_path), "--una-volta", *extra])
        self.network.assert_not_called()
        self.assertFalse((self.base / "orchestratore.lock").exists())
        return result

    def events(self):
        return ReportStore(self.base).events(DAY)

    def history(self, actor, identity):
        return [entry for entry in self.data()["storia"]
                if entry["attore"] == actor and entry["id"] == identity]

    def test_success_with_referral_limit_and_three_rejections_share_report(self):
        done = self.task("001", "Task riuscita", "rimando_limite")
        rejected = self.task("002", "Task bocciata", "bocciata")
        self.assertEqual(self.cli(), 0)
        self.assertTrue((self.base / "fatte" / done).exists())
        self.assertTrue((self.base / "scartate" / rejected).exists())
        self.assertFalse(any((self.base / "coda").iterdir()))
        self.assertFalse(any((self.base / "in-corso").iterdir()))
        self.assertEqual(self.git("log", "--format=%s").splitlines(), ["001: Task riuscita", "iniziale"])
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), self.initial_commit)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("branch", "--show-current").strip(), "produzione3")
        self.assertEqual((self.project / "app.txt").read_text(encoding="utf-8"), "pronto\n")
        self.assertFalse((self.project / "non-tracciato.txt").exists())
        stash = self.git("stash", "list", "--format=%gs").splitlines()
        self.assertEqual(len(stash), 1)
        self.assertIn("orchestratore: task 002 scartata", stash[0])
        self.assertEqual(self.git("show", "stash@{0}:app.txt").strip(), "correzione-3")
        self.assertIn("Lavoro da conservare", self.git("show", "stash@{0}^3:non-tracciato.txt"))
        discarded = (self.base / "scartate" / rejected).read_text(encoding="utf-8")
        self.assertIn("## Esito orchestratore", discarded)
        self.assertIn("Raggiunte 3 bocciature Claude", discarded)
        self.assertIn("stash@{0}", discarded)
        success = self.history("codex", "001")
        self.assertEqual([call["resume"] for call in success], [False, True, True])
        self.assertEqual(len({call["thread_id"] for call in success}), 1)
        self.assertEqual(success[1]["prompt"], success[2]["prompt"])
        self.assertIn("Correggi app.txt", success[1]["prompt"])
        self.assertEqual(len(self.history("verifica", "001")), 2)
        self.assertEqual(len(self.history("modello", "001")), 2)
        self.assertEqual(len(self.history("claude", "001")), 1)
        self.assertEqual(len(self.history("claude", "002")), 3)
        failed_request = json.loads(self.history("modello", "001")[0]["payload"]["messages"][1]["content"])
        self.assertFalse(failed_request["verifiche"][0]["ok"])
        self.assertIn("AssertionError", "\n".join(failed_request["verifiche"][0]["righe_errore"]))
        self.assertTrue(failed_request["file_cambiati"])
        self.assertEqual(self.clock.sleeps, [60])  # Margine anche dopo un reset già passato.
        report = (self.base / "report" / f"{DAY}.md").read_text(encoding="utf-8")
        for text in ("1. 001: Task riuscita", "2. 002: Task bocciata", "Esito: fatta",
                     "Esito: scartata", "Rimandi: 1", "correzioni del controllore: 3",
                     "push saltato: nessun remoto", "Attesa limite codex"):
            self.assertIn(text, report)
        events = self.events()
        self.assertEqual(len([e for e in events if e["tipo"] == "attesa_inizio"]), 1)
        self.assertEqual(len([e for e in events if e["tipo"] == "attesa_fine"]), 1)
        self.assertEqual(StateStore(self.base).load()["fase"], "idle")
        self.assertEqual(len(list((self.base / "log").glob("001-*.jsonl"))), 3)
        raw = "\n".join(path.read_text(encoding="utf-8")
                        for path in (self.base / "log").glob("001-*.jsonl"))
        for text in ("thread.started", "agent_message", "turn.completed", "usage_limit_reached"):
            self.assertIn(text, raw)
        task_log = (self.base / "log" / f"001-{DAY}.log").read_text(encoding="utf-8")
        for text in ("PROMPT CODEX", "caffè", "AssertionError", "DECISIONE MODELLO", "ESITO: OK"):
            self.assertIn(text, task_log)

    def test_restart_after_persisted_thread_uses_resume_and_same_conversation(self):
        filename = self.task("003", "Task ripresa", "ripresa")
        stop = self.base / "STOP"

        def interrupted_codex(*args, **kwargs):
            callback = kwargs["on_thread"]

            def saved(thread):
                callback(thread)
                self.assertEqual(StateStore(self.base).load()["thread_id"], thread)
                deadline = time.monotonic() + 6
                while not stop.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(stop.exists(), "Il Codex finto non ha creato STOP dopo il salvataggio")
                raise KeyboardInterrupt()

            kwargs["on_thread"] = saved
            return run_codex(*args, **kwargs)

        self.services.run_codex = interrupted_codex
        self.assertEqual(self.cli(), 0)
        state = StateStore(self.base).load()
        thread = self.data()["tasks"]["003"]["thread_id"]
        self.assertEqual(state["fase"], "invio_codex")
        self.assertEqual(state["thread_id"], thread)
        self.assertTrue(stop.exists())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial_commit)
        self.assertEqual(len(self.history("codex", "003")), 1)
        self.assertEqual(len(self.history("modello", "003")), 0)
        # STOP blocca anche una nuova CLI senza --ignora-stop.
        self.assertEqual(self.cli(), 0)
        self.assertEqual(len(self.history("codex", "003")), 1)
        self.services.run_codex = run_codex
        self.assertEqual(self.cli("--ignora-stop"), 0)
        self.assertFalse(stop.exists())
        calls = self.history("codex", "003")
        self.assertEqual([call["resume"] for call in calls], [False, True])
        self.assertEqual([call["thread_id"] for call in calls], [thread, thread])
        self.assertIn("L'orchestratore si è riavviato durante il pezzo 1", calls[1]["prompt"])
        self.assertIn(thread, calls[1]["argv"])
        self.assertTrue((self.base / "fatte" / filename).exists())
        self.assertEqual(self.git("log", "--format=%s").splitlines(), ["003: Task ripresa", "iniziale"])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(StateStore(self.base).load()["fase"], "idle")
        self.assertEqual(len([e for e in self.events() if e["tipo"] == "ripresa_codex"]), 1)

    def test_verification_cache_is_not_committed_and_next_task_starts(self):
        (self.project / "calcoli.py").write_text("RISULTATO = 2 + 2\n", encoding="utf-8")
        self.git("add", "calcoli.py")
        self.git("commit", "-m", "Fixture per la verifica")
        first = self.task("004", "Prima con cache", "successo")
        second = self.task("005", "Seconda con cache", "successo")
        scenario = self.data()
        scenario["tasks"]["004"]["genera_cache"] = True
        scenario["tasks"]["005"].update(genera_cache=True, file_extra="secondo.txt")
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        self.assertEqual(self.cli(), 0)
        for filename in (first, second):
            self.assertTrue((self.base / "fatte" / filename).exists())
        cache = list((self.project / "__pycache__").glob("calcoli.*.pyc"))
        self.assertTrue(cache, "La verifica deve produrre davvero il bytecode")
        tracked = self.git("ls-files").splitlines()
        self.assertIn("calcoli.py", tracked)
        self.assertIn("secondo.txt", tracked)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in tracked))
        self.assertEqual(self.git("log", "-2", "--format=%s").splitlines(),
                         ["005: Seconda con cache", "004: Prima con cache"])
        self.assertIn("__pycache__", self.git("status", "--porcelain"))
        config = load_config(self.config_path)
        self.assertFalse(GitOperations(config).dirty(self.project))
        self.assertEqual(list_changed_files(config, self.project), ())
        for identity in ("004", "005"):
            request = json.loads(self.history("modello", identity)[0]["payload"]["messages"][1]["content"])
            self.assertFalse(any("__pycache__" in path for path in request["file_cambiati"]))
            review = self.history("claude", identity)[0]["prompt"].split("# DIFF PER LA REVISIONE", 1)[1]
            self.assertNotIn("__pycache__", review)

    def user_changes(self):
        (self.project / ".gitignore").write_text("cache/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "Fixture file utente")
        files = {".gitignore": b"cache/\n# modifica personale\n",
                 "documento con spazi.docx": b"PK\x00documento personale"}
        for name, content in files.items():
            (self.project / name).write_bytes(content)
        return files

    def test_preexisting_changes_are_committed_but_not_reviewed(self):
        import hashlib
        files = self.user_changes()
        filename = self.task("010", "Con modifiche utente", "successo")
        self.assertEqual(self.cli(), 0)
        self.assertTrue((self.base / "fatte" / filename).exists())
        message = self.git("show", "-s", "--format=%B", "HEAD")
        self.assertIn("Include modifiche preesistenti: .gitignore, documento con spazi.docx", message)
        self.assertEqual(set(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()),
                         {"app.txt", *files})
        for name, content in files.items():
            self.assertEqual((self.project / name).read_bytes(), content)
        prompt = self.history("codex", "010")[0]["prompt"]
        self.assertIn("NON leggerli per modificarli, NON toccarli", prompt)
        review = self.history("claude", "010")[0]["prompt"]
        self.assertIn("Modifiche dell'utente, non da revisionare: .gitignore", review)
        self.assertNotIn("modifica personale", review)
        self.assertNotIn("diff --git a/.gitignore", review)
        self.assertNotIn('File non tracciato: "documento con spazi.docx"', review)
        task_diff = review.split("# DIFF PER LA REVISIONE", 1)[1]
        for name in files:
            self.assertNotIn(name, task_diff)
        request = json.loads(self.history("modello", "010")[0]["payload"]["messages"][1]["content"])
        self.assertEqual(request["file_cambiati"], [" M app.txt"])
        initial = next(e for e in self.events() if e["tipo"] == "modifiche_preesistenti")
        self.assertEqual(initial["task"]["modifiche_preesistenti"],
                         {name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
        report = (self.base / "report" / f"{DAY}.md").read_text(encoding="utf-8")
        self.assertIn("Modifiche preesistenti lasciate intatte: .gitignore", report)
        self.assertIn("Include modifiche preesistenti: .gitignore", report)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def check_query_reports(self, *, model_down=False):
        folder = self.project / "supabase" / "queries"
        folder.mkdir(parents=True)
        (folder / "157_existing.sql").write_text("-- vecchia query\n", encoding="utf-8")
        self.git("add", "supabase/queries")
        self.git("commit", "-m", "Fixture query esistente")
        self.task("020", "Task con query", "successo")
        original = self.services.run_codex

        def codex_with_sql(*args, **kwargs):
            result = original(*args, **kwargs)
            for name in ("159_b.sql", "158_a.sql"):
                (folder / name).write_text("-- SQL finto da eseguire a mano\n", encoding="utf-8")
            (folder / "157_existing.sql").write_text("-- modifica di SQL esistente\n", encoding="utf-8")
            (folder / "README.txt").write_text("Non una query", encoding="utf-8")
            response = ("```text\nSTATO: FINITO\nNOTE:\nQUERY DA ESEGUIRE SU SUPABASE: "
                        "Controllare prima i prerequisiti.\nEseguire i due file in ordine.\n```\nFuori NOTE")
            return replace(result, output=replace(result.output, last_message=response))

        self.services.run_codex = codex_with_sql
        if model_down:
            self.services.generate_final_report = lambda *args: (_ for _ in ()).throw(TimeoutError("modello giù"))
        self.assertEqual(self.cli(), 0)
        expected = ["supabase/queries/158_a.sql", "supabase/queries/159_b.sql"]
        event = next(e for e in self.events() if e["tipo"] == "task_fatta")
        self.assertEqual(event["task"]["query_da_eseguire"], expected)
        self.assertEqual(event["task"]["note_query_codex"],
                         "Controllare prima i prerequisiti.\nEseguire i due file in ordine.")
        state = StateStore(self.base).load()
        self.assertEqual(state["ultimo_periodo"]["task"]["020"]["query_da_eseguire"], expected)
        daily = (self.base / "report" / f"{DAY}.md").read_text(encoding="utf-8")
        final_files = list((self.base / "report").glob("finale-*.md"))
        self.assertEqual(len(final_files), 1)
        final = final_files[0].read_text(encoding="utf-8")
        self.assertIn("### Query da eseguire su Supabase", daily)
        section = final.split("## Query da eseguire su Supabase (elenco dello script)", 1)[1]
        for report in (daily, section):
            self.assertLess(report.index(expected[0]), report.index(expected[1]))
            self.assertIn("Controllare prima i prerequisiti.", report)
            self.assertNotIn("157_existing.sql", report)
            self.assertNotIn("README.txt", report)
            self.assertNotIn("Fuori NOTE", report)
        for name in expected:
            self.assertIn(str(self.project / name), section)
        self.assertIn("task 020", section)
        if model_down:
            self.assertIn("modello non disponibile", final)

    def test_added_queries_are_in_daily_and_deterministic_final_report(self):
        self.check_query_reports()

    def test_added_queries_are_in_final_report_with_model_down(self):
        self.check_query_reports(model_down=True)

    def test_reports_with_no_queries_and_discarded_sql(self):
        self.task("021", "Senza SQL", "successo")
        self.task("022", "SQL scartato", "bocciata")
        original = self.services.run_codex

        def codex_with_discarded_sql(*args, **kwargs):
            result = original(*args, **kwargs)
            if "correzione-" in (self.project / "app.txt").read_text(encoding="utf-8"):
                folder = self.project / "supabase" / "queries"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "999_discarded.sql").write_text("-- SQL scartato\n", encoding="utf-8")
            return result

        self.services.run_codex = codex_with_discarded_sql
        self.assertEqual(self.cli(), 0)
        daily = (self.base / "report" / f"{DAY}.md").read_text(encoding="utf-8")
        final = next((self.base / "report").glob("finale-*.md")).read_text(encoding="utf-8")
        for report in (daily, final):
            self.assertIn("nessuna", report)
            self.assertIn("nessuna (task scartata: eventuali SQL sono nello stash, NON eseguirli)", report)
        section = final.split("## Query da eseguire su Supabase (elenco dello script)", 1)[1]
        self.assertNotIn("999_discarded.sql", section)
        self.assertIn("supabase/queries/999_discarded.sql",
                      self.git("ls-tree", "-r", "--name-only", "stash@{0}^3"))

    def test_rejection_stashes_only_task_work_preserving_user_bytes(self):
        files = self.user_changes()
        before = self.git("rev-parse", "HEAD").strip()
        filename = self.task("011", "Scarto con modifiche utente", "bocciata")
        self.assertEqual(self.cli(), 0)
        self.assertTrue((self.base / "scartate" / filename).exists())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        for name, content in files.items():
            self.assertEqual((self.project / name).read_bytes(), content)
        self.assertEqual((self.project / "app.txt").read_text(encoding="utf-8"), "iniziale\n")
        self.assertFalse((self.project / "non-tracciato.txt").exists())
        self.assertEqual(self.git("diff", "--name-only", "stash@{0}^1", "stash@{0}").splitlines(), ["app.txt"])
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", "stash@{0}^3").splitlines(), ["non-tracciato.txt"])
        git = GitOperations(load_config(self.config_path))
        self.assertFalse(git.dirty(self.project, exclude_paths=files))
        self.assertTrue(git.dirty(self.project))

    def test_touched_preexisting_file_is_reviewed_and_reported(self):
        self.user_changes()
        self.task("012", "Verifica tocca file utente", "successo")
        verify = self.services.run_verifications

        def touching_verification(*args, **kwargs):
            results = verify(*args, **kwargs)
            (self.project / ".gitignore").write_text("# toccato dalla verifica\n", encoding="utf-8")
            return results

        self.services.run_verifications = touching_verification
        self.assertEqual(self.cli(), 0)
        review = self.history("claude", "012")[0]["prompt"]
        self.assertIn("diff --git a/.gitignore", review)
        self.assertIn("toccato dalla verifica", review)
        request = json.loads(self.history("modello", "012")[0]["payload"]["messages"][1]["content"])
        self.assertIn(" M .gitignore", request["file_cambiati"])
        report = (self.base / "report" / f"{DAY}.md").read_text(encoding="utf-8")
        self.assertIn("Modifiche preesistenti toccate durante la task, incluse nella revisione: .gitignore", report)
        self.assertEqual(self.git("show", "HEAD:.gitignore").strip(), "# toccato dalla verifica")

    def test_preexisting_changes_disabled_rejects_without_running_actors(self):
        files = self.user_changes()
        settings = json.loads(self.config_path.read_text(encoding="utf-8"))
        settings["consenti_modifiche_preesistenti"] = False
        self.config_path.write_text(json.dumps(settings), encoding="utf-8")
        filename = self.task("013", "Rifiuto working tree sporco", "successo")
        self.assertEqual(self.cli(), 0)
        self.assertTrue((self.base / "scartate" / filename).exists())
        self.assertEqual(self.data()["storia"], [])
        self.assertEqual(self.git("stash", "list"), "")
        for name, content in files.items():
            self.assertEqual((self.project / name).read_bytes(), content)

    def test_preexisting_fingerprints_survive_restart_before_verification(self):
        files = self.user_changes()
        filename = self.task("014", "Ripresa con file utente", "successo")
        original = self.services.run_codex

        def stop_after_codex(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.base / "STOP").touch()
            return result

        self.services.run_codex = stop_after_codex
        self.assertEqual(self.cli(), 0)
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], "verifica")
        self.assertEqual(set(saved["modifiche_preesistenti"]), set(files))
        self.services.run_codex = original
        self.assertEqual(self.cli("--ignora-stop"), 0)
        self.assertTrue((self.base / "fatte" / filename).exists())
        self.assertEqual(len(self.history("codex", "014")), 1)
        completed = next(e for e in self.events() if e["tipo"] == "task_fatta")
        self.assertEqual(completed["task"]["modifiche_preesistenti"], saved["modifiche_preesistenti"])

    def test_excluded_tracked_and_untracked_files_are_filtered_by_git(self):
        # Nomi non riservati: nessun file .env* viene creato o aperto.
        tracked = ["vecchio.pyc", "sub/dati.tsbuildinfo", "__pycache__/vecchio.txt",
                   ".next/cache.txt", "sub/node_modules/modulo.txt"]
        untracked = ["sub/nuovo.pyc", "nuovo.tsbuildinfo", "sub/__pycache__/nuovo.txt",
                     "sub/.next/nuovo.txt", "node_modules/nuovo.txt"]
        for name in tracked:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("prima\n", encoding="utf-8")
        self.git("add", "-A")  # Solo preparazione della fixture: devono essere gia tracciati.
        self.git("commit", "-m", "Fixture artefatti tracciati")
        for name in tracked + untracked:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("contenuto escluso\n", encoding="utf-8")
        config = load_config(self.config_path)
        git = GitOperations(config)
        self.assertTrue(self.git("status", "--porcelain"))
        self.assertFalse(git.dirty(self.project))
        self.assertEqual(list_changed_files(config, self.project), ())
        diff = collect_review_diff(config, self.project)
        self.assertNotIn("contenuto escluso", diff)
        for name in tracked + untracked:
            self.assertNotIn(name, diff)
        (self.project / "app.txt").write_text("lavoro utile\n", encoding="utf-8")
        self.assertTrue(git.dirty(self.project))
        self.assertEqual(list_changed_files(config, self.project), (" M app.txt",))
        git.add(self.project)
        self.assertEqual(self.git("diff", "--cached", "--name-only").splitlines(), ["app.txt"])
        git.commit(self.project, "Solo lavoro utile")
        self.assertFalse(git.dirty(self.project))
        self.assertIn("lavoro utile", self.git("show", "HEAD:app.txt"))
        for name in tracked:
            self.assertEqual(self.git("show", f"HEAD:{name}").strip(), "prima")


if __name__ == "__main__":
    unittest.main()
