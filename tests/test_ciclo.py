from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from finti.catalogo_finto import CATALOGO

from orch.claude import ClaudeResult, ClaudeReview
from orch.codex import CodexOutput, CodexResult
from orch.codex_limits import CodexError, UsageLimit
from orch.controllori import RisultatoControllore
from orch.ciclo import Orchestrator, Services
from orch.config import load_config
from orch.git_ops import GitError
from orch.model import ModelDecision
from orch.persistence import StateStore
from orch.reporting import ReportStore
from orch.verification import VerificationResult


TEST_ROOT = Path(__file__).resolve().parent
THREAD = "12345678-1234-1234-1234-123456789abc"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        self.sleeps = []
        self.hook = None

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)
        if self.hook:
            self.hook()


class FakeGit:
    def __init__(self):
        self.calls = []
        self.current_branch = "produzione3"
        self.is_repository = True
        self.is_dirty = False
        self.origin = False
        self.hash = "old-hash"
        self.parents = []
        self.subject = "initial"
        self.stash_entry = None
        self.commit_error = None
        self.push_error = None
        self.stash_error = None
        self.crash_commit = False
        self.crash_stash = False
        self.crash_push = False
        self.observer = None

    def mark(self, name, *args):
        self.calls.append((name, *args))
        if self.observer:
            self.observer(name)

    def is_repo(self, project):
        self.mark("repo")
        return self.is_repository

    def branch(self, project):
        self.mark("branch")
        return self.current_branch

    def dirty(self, project):
        self.mark("dirty")
        return self.is_dirty

    def preexisting_changes(self, project):
        self.mark("preexisting_changes")
        return {}

    def head(self, project):
        self.mark("head")
        return self.hash

    def commit_info(self, project):
        self.mark("commit_info")
        return {"hash": self.hash, "parents": self.parents, "subject": self.subject}

    def add(self, project):
        self.mark("add")

    def added_queries(self, project, before, commit):
        self.mark("added_queries", before, commit)
        return []

    def commit(self, project, message):
        self.mark("commit", message)
        if self.commit_error:
            raise GitError(self.commit_error)
        self.parents = [self.hash] if self.hash else []
        self.hash, self.subject, self.is_dirty = "new-hash", message, False
        if self.crash_commit:
            self.crash_commit = False
            raise KeyboardInterrupt()

    def has_origin(self, project):
        self.mark("origin")
        return self.origin

    def push(self, project, branch):
        self.mark("push", branch)
        if self.push_error:
            raise GitError(self.push_error)
        if self.crash_push:
            self.crash_push = False
            raise KeyboardInterrupt()

    def latest_stash(self, project):
        self.mark("latest_stash")
        return self.stash_entry

    def stash(self, project, message):
        self.mark("stash", message)
        if self.stash_error:
            raise GitError(self.stash_error)
        self.stash_entry = {"hash": "saved-hash", "name": "stash@{0}", "subject": "On produzione3: " + message}
        self.is_dirty = False
        if self.crash_stash:
            self.crash_stash = False
            raise KeyboardInterrupt()


class CycleTests(unittest.TestCase):
    def setUp(self):
        catalog = patch("orch.impostazioni.catalogo", return_value=CATALOGO)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.assertTrue(self.base.is_relative_to(TEST_ROOT))
        for folder in ("coda", "in-corso", "fatte", "scartate", "regole", "progetto"):
            (self.base / folder).mkdir()
        self.project = self.base / "progetto"
        (self.base / "regole" / "istruzioni-codex.md").write_text("ISTRUZIONI FISSE", encoding="utf-8")
        (self.base / "regole" / "prompt-verifica-claude.md").write_text("ISTRUZIONI REVISORE", encoding="utf-8")
        config_path = self.base / "config.json"
        config_path.write_text('{"cartella_sessioni_codex":"sessioni-finte","spegnimento_abilitato":false}', encoding="utf-8")
        self.config = load_config(config_path)
        (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":false}', encoding="utf-8")
        self.clock = Clock()
        self.git = FakeGit()
        self.codex_calls, self.claude_calls, self.model_calls, self.verify_calls = [], [], [], []
        self.codex_replies, self.claude_replies, self.decisions = [], [], []
        self.codex_hook = self.verify_hook = self.model_hook = self.changed_hook = None
        self.save_thread = True
        self.diff_calls = 0
        self.report_calls, self.shutdown_calls = [], []
        self.services = Services(
            run_codex=self.codex, run_claude=self.claude, decide_next_action=self.model,
            run_verifications=self.verify, list_changed_files=self.changed,
            collect_review_diff=self.diff, codex_reset=lambda *args: None,
            clock=self.clock.now, sleep=self.clock.sleep,
            generate_final_report=self.final_report, shutdown=self.shutdown)
        self.engine = Orchestrator(self.config, services=self.services, git=self.git)
        self.git.observer = self.observe_git
        self.stop = self.base / "STOP"

    def observe(self, phase, action):
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], phase)
        self.assertEqual(saved["azione_esterna"], action)

    def final_report(self, config, data):
        self.report_calls.append(data)
        return "# Report del modello finto\n\nRiepilogo verificato."

    def shutdown(self, config):
        self.shutdown_calls.append(config.comando_spegnimento)
        saved = StateStore(self.base).load()
        self.assertNotIn("finalizzazione", saved)
        self.assertEqual(saved["periodo"]["task_ids"], [])

    def observe_git(self, name):
        state = StateStore(self.base).load()
        self.assertNotEqual(state["fase"], "idle")
        self.assertIsNotNone(state["azione_esterna"])

    def once(self, attr):
        hook = getattr(self, attr)
        setattr(self, attr, None)
        if hook:
            hook()

    def codex_reply(self, *, errors=(), limit=None):
        return CodexResult(
            output=CodexOutput(THREAD, "STATO: FINITO — risposta completa", list(errors), True),
            returncode=1 if errors or limit else 0, timed_out=False, usage_limit=limit,
            jsonl_path=self.base / "fake.jsonl", stderr_path=self.base / "fake.stderr",
            last_message_path=self.base / "fake.last", prompt_path=self.base / "fake.prompt", stderr="")

    def codex(self, config, project, prompt, **kwargs):
        self.observe("invio_codex", "codex")
        self.codex_calls.append((prompt, kwargs["thread_id"]))
        if self.save_thread and not kwargs["thread_id"]:
            kwargs["on_thread"](THREAD)
            self.assertEqual(StateStore(self.base).load()["thread_id"], THREAD)
        self.git.is_dirty = True
        self.once("codex_hook")
        return self.codex_replies.pop(0) if self.codex_replies else self.codex_reply()

    def verify(self, *args):
        self.observe("verifica", "verifiche")
        self.verify_calls.append(args)
        self.once("verify_hook")
        return (VerificationResult("test finto", 0, False, "OUTPUT COMPLETO VERIFICHE", (), None, 1),)

    def changed(self, *args):
        self.observe("verifica", "git_status")
        self.once("changed_hook")
        return (" M app.py",)

    def model(self, *args):
        self.observe("modello", "modello")
        self.model_calls.append(args)
        self.once("model_hook")
        return self.decisions.pop(0) if self.decisions else ModelDecision("chiama_claude", "", "Verificare il pezzo")

    def diff(self, *args):
        self.observe("claude", "diff")
        self.diff_calls += 1
        return "DIFF FINTO"

    def claude(self, config, project, prompt, **kwargs):
        self.observe("claude", "claude")
        self.claude_calls.append(prompt)
        verdict = self.claude_replies.pop(0) if self.claude_replies else ClaudeReview("ok", "ESITO: OK")
        return ClaudeResult(verdict, 0, False, self.base / "cl.prompt", self.base / "cl.response",
                            self.base / "cl.stderr", verdict.text, "", ())

    def task(self, *, pieces=1, push="si"):
        path = self.base / "coda" / "001-finta.md"
        steps = "1. Primo pezzo.\n" + ("2. Secondo pezzo.\n" if pieces == 2 else "")
        path.write_text(
            f"---\nid: 001\ntitolo: Task finta\nprogetto: {self.project}\nbranch: produzione3\n"
            f"priorita: alta\npush: {push}\n---\n## Contesto\nFinto\n## Obiettivo\nFinto\n"
            f"## Passi\n{steps}\n## Verifica\ntest finto\n## Fuori perimetro\nAltro\n", encoding="utf-8")
        return path

    def restart(self):
        self.engine = Orchestrator(self.config, services=self.services, git=self.git)
        return self.engine

    def run_cycle(self):
        return self.engine.run(una_volta=True)

    def report(self):
        return (self.base / "report" / "2026-10-22.md").read_text(encoding="utf-8")

    def final_snapshot(self):
        return next(event["task"] for event in reversed(ReportStore(self.base).events("2026-10-22"))
                    if event.get("task"))

    def test_success_two_pieces_one_conversation_commit_and_report(self):
        self.task(pieces=2)
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([call[1] for call in self.codex_calls], [None, THREAD])
        self.assertIn("ISTRUZIONI FISSE", self.codex_calls[0][0])
        self.assertIn("QUERY DA ESEGUIRE SU SUPABASE:", self.codex_calls[0][0])
        self.assertEqual(self.codex_calls[1][0], "Secondo pezzo.")
        self.assertIn(("commit", "001: Task finta"), self.git.calls)
        self.assertTrue((self.base / "fatte" / "001-finta.md").exists())
        self.assertEqual(self.final_snapshot()["pezzi_completati"], 2)
        self.assertIn("push saltato: nessun remoto", self.report())
        self.assertEqual(StateStore(self.base).load()["fase"], "idle")
        self.assertIn("### Query da eseguire su Supabase\n\nnessuna", self.report())
        log = (self.base / "log" / "001-2026-10-22.log").read_text(encoding="utf-8")
        for text in ("PROMPT CODEX", "RISPOSTA CODEX", "OUTPUT COMPLETO VERIFICHE", "DECISIONE MODELLO", "PROMPT CLAUDE"):
            self.assertIn(text, log)

    def test_referrals_stop_at_threshold_and_reset_on_next_piece(self):
        self.task(pieces=2)
        self.decisions = [ModelDecision("rimanda_a_codex", "Completa", "incompleto")] * 3
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([args[3] for args in self.model_calls], [0, 1, 2, 0])
        self.assertEqual(len(self.codex_calls), 4)
        self.assertEqual(self.final_snapshot()["rimandi_totali"], 2)

    def test_three_rejections_discard_and_stash_once(self):
        self.task()
        self.claude_replies = [ClaudeReview("correzioni", "ESITO: CORREZIONI", "1. Correggi app.py")] * 3
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.codex_calls), 3)
        self.assertEqual(len([c for c in self.git.calls if c[0] == "stash"]), 1)
        self.assertEqual(self.final_snapshot()["bocciature"], 3)
        text = (self.base / "scartate" / "001-finta.md").read_text(encoding="utf-8")
        self.assertIn("stash@{0}", text)
        self.assertIn("bocciature", text)

    def test_unknown_review_retried_once_then_counts_as_correction(self):
        self.task()
        self.claude_replies = [ClaudeReview("non_riconosciuto", "Testo sconosciuto")] * 2
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.claude_calls), 3)
        self.assertEqual(self.claude_calls[0], self.claude_calls[1])
        self.assertIn("Testo sconosciuto", self.codex_calls[1][0])
        self.assertEqual(self.final_snapshot()["bocciature"], 1)

    def test_invalid_utf8_task_is_archived_before_valid_task(self):
        self.task()
        (self.base / "coda" / "000-rotta.md").write_bytes(b"\xff\xfe")
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue((self.base / "scartate" / "000-rotta.md").read_bytes().startswith(b"\xff\xfe"))
        self.assertTrue((self.base / "fatte" / "001-finta.md").exists())
        self.assertIn("## 1. 000", self.report())
        self.assertIn("## 2. 001", self.report())

    def test_initial_dirty_tree_is_rejected_without_stash(self):
        self.config = replace(self.config, consenti_modifiche_preesistenti=False)
        self.restart()
        self.task()
        self.git.is_dirty = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.codex_calls, [])
        self.assertFalse(any(c[0] == "stash" for c in self.git.calls))
        self.assertTrue(self.git.is_dirty)

    def test_wrong_initial_branch_is_rejected_without_checkout(self):
        self.task()
        self.git.current_branch = "altro"
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.codex_calls, [])
        self.assertIn("Branch corrente", self.report())

    def test_missing_project_or_repository_is_rejected(self):
        self.task()
        self.git.is_repository = False
        self.assertEqual(self.run_cycle(), 0)
        self.assertIn("non è un repository", self.report())
        self.assertEqual(self.codex_calls, [])

    def test_branch_checked_again_before_referral(self):
        self.task()
        self.decisions = [ModelDecision("rimanda_a_codex", "Continua", "incompleto")]
        self.model_hook = lambda: setattr(self.git, "current_branch", "altro")
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.codex_calls), 1)
        self.assertIn("Branch corrente", self.report())

    def test_commit_identity_failure_discards_and_preserves_work(self):
        self.task()
        self.git.commit_error = "Identità git mancante: user.name e user.email"
        self.assertEqual(self.run_cycle(), 0)
        self.assertIn("Identità git mancante", self.report())
        self.assertTrue(any(c[0] == "stash" for c in self.git.calls))

    def test_rejected_push_still_finishes_task(self):
        self.task()
        self.git.origin = True
        self.git.push_error = "non-fast-forward"
        self.assertEqual(self.run_cycle(), 0)
        self.assertIn("push fallito", self.report())
        self.assertTrue((self.base / "fatte" / "001-finta.md").exists())

    def test_push_condition_and_success(self):
        self.task(push="no")
        self.git.origin = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertFalse(any(c[0] in ("origin", "push") for c in self.git.calls))

    def test_push_success_uses_requested_branch(self):
        self.task()
        self.git.origin = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertIn(("push", "produzione3"), self.git.calls)
        self.assertEqual(self.final_snapshot()["push"], "push riuscito")

    def test_push_disabled_in_config_does_not_query_remote(self):
        self.task()
        self.config = replace(self.config, push_abilitato=False)
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertFalse(any(c[0] in ("origin", "push") for c in self.git.calls))

    def test_missing_project_is_rejected(self):
        self.task()
        self.project.rmdir()  # Solo la directory vuota creata dal fixture.
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.git.calls, [])
        self.assertIn("non esistente", self.report())

    def test_model_exception_falls_back_to_claude(self):
        self.task()
        self.model_hook = lambda: (_ for _ in ()).throw(TimeoutError("modello offline"))
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.claude_calls), 1)
        self.assertIn("modello offline", self.report())

    def test_claude_operational_error_waits_and_repeats_exact_prompt(self):
        self.task()
        self.claude_replies = [ClaudeReview("errore", "", error="Timeout Claude")]
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.claude_calls), 2)
        self.assertEqual(self.claude_calls[0], self.claude_calls[1])
        self.assertEqual(sum(self.clock.sleeps), self.config.attesa_limite_minuti * 60)
        self.assertEqual(self.final_snapshot()["errori_claude_consecutivi"], 0)
        self.assertEqual(self.final_snapshot()["bocciature"], 0)
        self.assertIn("Errore temporaneo Claude (1/3)", self.report())

    def test_three_claude_errors_stop_without_discard_and_resume(self):
        self.task()
        self.claude_replies = [ClaudeReview("errore", "", error="Rete indisponibile")] * 3
        self.assertEqual(self.run_cycle(), 1)
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], "claude")
        self.assertEqual(saved["errori_claude_consecutivi"], 3)
        self.assertTrue(saved["ripeti_esatto"])
        self.assertEqual(len(saved["attese"]), 2)
        self.assertEqual(self.engine.state["bocciature"], 0)
        self.assertTrue((self.base / "in-corso" / "001-finta.md").exists())
        self.assertNotIn("stash", [call[0] for call in self.git.calls])
        self.assertIn("orchestratore fermato, stato conservato per la ripresa", self.report())
        prompt = self.claude_calls[-1]
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.claude_calls[-1], prompt)
        self.assertEqual(self.final_snapshot()["errori_claude_consecutivi"], 0)

    def test_valid_correction_resets_consecutive_claude_errors(self):
        self.task()
        error = ClaudeReview("errore", "", error="Timeout")
        self.claude_replies = [error, error, ClaudeReview("correzioni", "", "Completa"), error, error]
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.claude_calls), 6)
        self.assertEqual(self.final_snapshot()["bocciature"], 1)
        self.assertEqual(self.final_snapshot()["errori_claude_consecutivi"], 0)

    def test_claude_error_wait_survives_stop_and_restart(self):
        self.task()
        self.claude_replies = [ClaudeReview("errore", "", error="Timeout")]
        self.clock.hook = lambda: self.stop.touch()
        self.assertEqual(self.run_cycle(), 0)
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], "attesa_limite")
        self.assertEqual(saved["errori_claude_consecutivi"], 1)
        self.assertEqual(len(self.claude_calls), 1)
        self.clock.hook = None
        self.stop.unlink()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.claude_calls[0], self.claude_calls[1])

    def test_valid_approval_resets_errors_for_next_piece(self):
        self.task(pieces=2)
        error = ClaudeReview("errore", "", error="Timeout")
        self.claude_replies = [error, error, ClaudeReview("ok", "ESITO: OK"), error, error]
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.final_snapshot()["pezzi_completati"], 2)
        self.assertEqual(len(self.claude_calls), 6)

    def test_unrecognized_review_does_not_reset_consecutive_errors(self):
        self.task()
        error = ClaudeReview("errore", "", error="Timeout")
        self.claude_replies = [error, error, ClaudeReview("non_riconosciuto", "Risposta incompleta"), error]
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(StateStore(self.base).load()["errori_claude_consecutivi"], 3)
        self.assertEqual(len(self.claude_calls), 4)
        self.assertEqual(self.engine.state["bocciature"], 0)

    def test_codex_limit_retries_exact_prompt_in_short_sleeps(self):
        self.task()
        self.codex_replies = [self.codex_reply(limit=UsageLimit("limit", self.clock.now() + timedelta(seconds=130), "testo"))]
        snapshots = []
        self.clock.hook = lambda: snapshots.append((len(self.git.calls), len(self.codex_calls), len(self.verify_calls)))
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.sleeps, [60, 60, 60, 10])
        self.assertEqual(len(set(snapshots)), 1)
        self.assertEqual(self.codex_calls[0][0], self.codex_calls[1][0])
        self.assertEqual(self.codex_calls[1][1], THREAD)
        self.assertEqual(len(self.model_calls), 1)
        self.assertIsNotNone(self.final_snapshot()["attese"][0]["fine"])

    def test_unknown_limit_reset_uses_configured_wait(self):
        self.task()
        self.codex_replies = [self.codex_reply(limit=UsageLimit("limit", None, "sconosciuto"))]
        self.config = replace(self.config, attesa_limite_minuti=2)
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.sleeps, [60, 60])

    def test_claude_limit_reuses_prompt_without_recollecting_diff(self):
        self.task()
        self.claude_replies = [ClaudeReview("limite", "usage limit", retry_at=self.clock.now() + timedelta(seconds=61))]
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.claude_calls[0], self.claude_calls[1])
        self.assertEqual(self.diff_calls, 1)
        self.assertEqual(self.clock.sleeps, [60, 60, 1])

    def select_review_controller(self, name, *, effort="medium"):
        path = self.base / "impostazioni.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["controllore"] = {"strumento": name, "modello": CATALOGO[name][0]["id"],
                                   "ragionamento": effort}
        path.write_text(json.dumps(settings), encoding="utf-8")

    def test_changed_controller_cancels_saved_wait_before_its_deadline(self):
        self.task()
        self.claude_replies = [ClaudeReview("limite", "usage limit", retry_at=self.clock.now() + timedelta(hours=2))]
        self.clock.hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], "attesa_limite")
        original_prompt = saved["prompt_claude"]
        original_task = saved["task_corrente"]
        original_thread = saved["thread_id"]
        slept = sum(self.clock.sleeps)
        self.clock.hook = None
        self.select_review_controller("codex")
        self.stop.unlink()
        self.restart()
        self.engine._resume_wait()
        self.assertEqual(sum(self.clock.sleeps), slept)
        self.assertEqual(self.engine.state["fase"], "claude")
        self.assertIsNone(self.engine.state["attesa_fino_a"])
        self.assertIsNone(self.engine.state["strumento_attesa"])
        self.assertIsNone(self.engine.state["azione_esterna"])
        self.assertIsNone(self.engine.state["prompt_claude"])
        self.assertFalse(self.engine.state["ripeti_esatto"])
        self.assertEqual(self.engine.state["task_corrente"], original_task)
        self.assertEqual(self.engine.state["thread_id"], original_thread)
        self.assertEqual(self.engine.state["pezzo_corrente"], 1)
        self.assertIsNotNone(self.engine.state["attese"][-1]["fine"])
        calls = []

        def review(name, config, settings, project, prompt, **kwargs):
            self.observe("claude", "codex")
            calls.append(name)
            self.assertEqual(name, "codex")
            self.assertEqual(self.diff_calls, 2)  # New controller receives freshly collected context.
            return RisultatoControllore(ClaudeReview("ok", "ESITO: OK"))

        self.services.review_controller = review
        self.restart()  # Cancellation is persisted, including after a second restart.
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(calls, ["codex"])
        self.assertEqual(len(self.codex_calls), 1)  # The completed executor step is not repeated.
        self.assertEqual(self.claude_calls, [original_prompt])
        self.assertIn("attesa_annullata", self.report())

    def test_changed_controller_is_detected_during_a_live_wait(self):
        self.task()
        calls = []

        def review(name, *args, **kwargs):
            calls.append(name)
            if name == "claude":
                return RisultatoControllore(ClaudeReview("limite", "usage limit",
                    retry_at=self.clock.now() + timedelta(hours=2)))
            return RisultatoControllore(ClaudeReview("ok", "ESITO: OK"))

        def change():
            self.select_review_controller("codex")
            self.clock.hook = None

        self.services.review_controller = review
        self.clock.hook = change
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(calls, ["claude", "codex"])
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual(self.diff_calls, 2)

    def test_same_controller_effort_change_does_not_bypass_its_limit(self):
        self.task()
        self.claude_replies = [ClaudeReview("limite", "usage limit",
            retry_at=self.clock.now() + timedelta(seconds=61))]

        def change():
            self.select_review_controller("claude", effort="high")
            self.clock.hook = None

        self.clock.hook = change
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.sleeps, [60, 60, 1])
        self.assertEqual(self.claude_calls[0], self.claude_calls[1])
        self.assertEqual(self.diff_calls, 1)
        self.assertNotIn("attesa_annullata", self.report())

    def test_changed_controller_cancels_weekly_wait_before_final_report_or_shutdown(self):
        self.task()
        self.claude_replies = [ClaudeReview("limite", "weekly limit",
            retry_at=self.clock.now() + timedelta(days=3))]
        original = self.services.run_claude

        def pause(*args, **kwargs):
            result = original(*args, **kwargs)
            self.stop.touch()
            return result

        self.services.run_claude = pause
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue(self.engine.state["limite_settimanale"])
        self.select_review_controller("codex")
        self.stop.unlink()
        self.restart()
        self.engine._resume_wait()
        self.assertEqual(self.engine.state["fase"], "claude")
        self.assertFalse(self.engine.state["limite_settimanale"])
        self.assertEqual(self.report_calls, [])
        self.assertEqual(self.shutdown_calls, [])
        self.assertEqual(self.clock.sleeps, [])

    def test_wait_survives_stop_and_restart_without_tools(self):
        self.task()
        self.codex_replies = [self.codex_reply(limit=UsageLimit("limit", self.clock.now() + timedelta(seconds=130), "testo"))]
        self.clock.hook = lambda: self.stop.touch()
        self.assertEqual(self.run_cycle(), 0)
        state = StateStore(self.base).load()
        self.assertEqual(state["fase"], "attesa_limite")
        self.assertEqual(self.verify_calls, [])
        self.stop.unlink()
        self.clock.hook = None
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.sleeps, [60, 60, 60, 10])
        self.assertEqual(self.codex_calls[0][0], self.codex_calls[1][0])

    def test_codex_interrupted_after_thread_id_resumes_same_conversation(self):
        self.task()
        self.codex_hook = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.run_cycle(), 0)
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.codex_calls[1][1], THREAD)
        self.assertIn("si è riavviato durante il pezzo 1", self.codex_calls[1][0])

    def test_period_survives_completion_restart_and_discard(self):
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.engine.state["periodo"]["task_ids"], [])
        self.task()
        self.assertEqual(self.run_cycle(), 0)
        period = StateStore(self.base).load()["ultimo_periodo"]
        self.assertEqual(period["task_ids"], ["001"])
        self.assertEqual(period["task"]["001"]["esito"], "fatta")
        self.assertEqual(period["task"]["001"]["commit"], "new-hash")
        event_ids = [event["event_id"] for event in period["eventi"]]
        self.clock.sleep(10)
        self.restart()
        self.assertEqual(self.engine.state["ultimo_periodo"], period)
        path = self.task()
        content = path.read_text(encoding="utf-8").replace("id: 001", "id: 002")
        path.write_text(content, encoding="utf-8")
        self.git.current_branch = "altro"
        self.assertEqual(self.run_cycle(), 0)
        period = StateStore(self.base).load()["ultimo_periodo"]
        self.assertEqual(period["task_ids"], ["002"])
        self.assertEqual(period["task"]["002"]["esito"], "scartata")
        self.assertFalse(set(event_ids) & {event["event_id"] for event in period["eventi"]})

    def test_period_keeps_waits_across_restart(self):
        self.task()
        self.codex_replies = [self.codex_reply(limit=UsageLimit("usage limit", None, "testo"))]
        self.clock.hook = lambda: self.stop.touch()
        self.assertEqual(self.run_cycle(), 0)
        state = StateStore(self.base).load()
        self.assertFalse(state["limite_settimanale"])
        self.assertFalse(state["periodo"]["task"]["001"]["attese"][0]["settimanale"])
        self.restart()
        self.assertEqual(self.engine.state["periodo"], state["periodo"])

    def test_legacy_state_migrates_period_without_duplicate_events(self):
        self.task()
        self.codex_hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        state = StateStore(self.base).load()
        state.pop("periodo")
        StateStore(self.base).save(state)
        self.restart()
        period = self.engine.state["periodo"]
        self.assertEqual(period["task_ids"], ["001"])
        self.assertEqual(period["eventi"], state["eventi"])
        self.restart()
        self.assertEqual(self.engine.state["periodo"], period)

    def test_weekly_wait_classification_and_margin(self):
        for tool, text, hours, expected in (
            ("codex", "usage limit", 5, False), ("codex", "weekly", 1, True),
            ("codex", "usage limit", 13, True), ("claude", "week limit", 1, True),
            ("claude", "usage limit", 5, False), ("claude", "usage limit", 13, True),
        ):
            with self.subTest(tool=tool, text=text, hours=hours):
                retry = self.clock.now() + timedelta(hours=hours)
                self.engine._wait_limit(tool, retry, "invio_codex", text=text)
                self.assertEqual(self.engine.state["limite_settimanale"], expected)
                self.assertEqual(datetime.fromisoformat(self.engine.state["attesa_fino_a"]),
                                 retry + timedelta(seconds=60))

    def test_recent_reset_wait_is_short_with_margin(self):
        from orch.codex_limits import reset_from_text
        self.clock.value = self.clock.now().replace(day=23, hour=0, minute=49, second=6)
        retry = reset_from_text("try again at 12:49 AM.", now=self.clock.now())
        self.engine._wait_limit("codex", retry, "invio_codex")
        self.assertFalse(self.engine.state["limite_settimanale"])
        until = datetime.fromisoformat(self.engine.state["attesa_fino_a"])
        self.assertEqual((until - self.clock.now()).total_seconds(), 180)

    def test_codex_launch_failure_stops_without_verification_or_referrals(self):
        from orch.codex import run_codex
        self.task()
        self.services.run_codex = run_codex
        with patch("orch.codex.subprocess.Popen", side_effect=FileNotFoundError("eseguibile assente")):
            self.assertEqual(self.run_cycle(), 1)
        state = StateStore(self.base).load()
        self.assertEqual(state["fase"], "invio_codex")
        self.assertFalse(state["azione_iniziata"])
        self.assertFalse(state["task_ha_lavorato"])
        self.assertEqual((state["rimandi_totali"], state["bocciature"]), (0, 0))
        self.assertEqual((self.verify_calls, self.model_calls, self.claude_calls), ([], [], []))
        self.assertIn("Impossibile avviare Codex", self.report())
        self.assertIn(self.config.comando_codex[0], self.report())
        self.assertEqual(state["periodo"]["eventi"][-1]["tipo"], "errore_operativo")
        self.services.run_codex = self.codex
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.codex_calls[0][0], state["ultimo_messaggio"])

    def enable_shutdown(self):
        self.config = replace(self.config, spegnimento_abilitato=True, attesa_prima_spegnimento_s=2)
        (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":true}', encoding="utf-8")
        self.restart()

    def test_settings_override_legacy_shutdown_config(self):
        self.assertFalse(self.engine.shutdown_enabled)
        (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":true}', encoding="utf-8")
        self.restart()
        self.assertFalse(self.config.spegnimento_abilitato)
        self.assertTrue(self.engine.shutdown_enabled)

    def test_settings_are_reloaded_before_each_send_and_review(self):
        self.task(pieces=2)
        original_codex = self.services.run_codex
        original_claude = self.services.run_claude
        observations = []

        def execute(*args, **kwargs):
            observations.append(("codex", self.engine.impostazioni["spegnimento_automatico"]))
            (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":true}', encoding="utf-8")
            return original_codex(*args, **kwargs)

        def review(*args, **kwargs):
            observations.append(("claude", self.engine.impostazioni["spegnimento_automatico"]))
            (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":false}', encoding="utf-8")
            return original_claude(*args, **kwargs)

        self.services.run_codex = execute
        self.services.run_claude = review
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(observations, [("codex", False), ("claude", True)] * 2)
        self.assertEqual(self.shutdown_calls, [])

    def test_switch_to_reserve_executor_on_codex_limit_mid_task(self):
        (self.base / "impostazioni.json").write_text(
            '{"spegnimento_automatico":false,"esecutori":{"claude":{"attivo":true,"priorita":2}}}', encoding="utf-8")
        self.restart()
        self.task(pieces=2)
        from orch.esecutori import RisultatoEsecutore
        calls = []

        def send(nome, config, impostazioni, progetto, prompt, conversazione=None, **kwargs):
            calls.append((nome, prompt, conversazione))
            if nome == "codex" and len(calls) == 2:  # pezzo 2: Codex finisce le 5 ore
                return RisultatoEsecutore(conversazione=THREAD, limite=UsageLimit(
                    "You've hit your usage limit.", self.clock.now() + timedelta(hours=3), "testo"))
            self.git.is_dirty = True
            ident = THREAD if nome == "codex" else "sessione-claude"
            if conversazione is None and kwargs.get("on_conversazione"):
                kwargs["on_conversazione"](ident)
            return RisultatoEsecutore(conversazione=ident, ultimo_messaggio="STATO: FINITO")

        self.services.send_executor = send
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([c[0] for c in calls], ["codex", "codex", "claude"])
        passaggio = calls[2][1]
        self.assertIsNone(calls[2][2])  # conversazione nuova per la riserva
        self.assertEqual(passaggio.count("# FASCICOLO DELLA TASK"), 1)
        self.assertIn("I pezzi 1..1 sono già stati completati e approvati", passaggio)
        types = [e["tipo"] for e in ReportStore(self.base).events("2026-10-22")]
        self.assertIn("cambio_esecutore", types)
        self.assertEqual(len(list((self.base / "fatte").glob("*.md"))), 1)
        self.assertIn("Esecutori: codex, claude", self.report())

    def prepare_interrupted_reserve(self, first="codex", reserve="claude"):
        from orch.esecutori import RisultatoEsecutore
        self.task(pieces=2)
        settings = {"spegnimento_automatico": False, "esecutori": {
            first: {"attivo": True, "priorita": 1},
            reserve: {"attivo": True, "priorita": 2}}}
        (self.base / "impostazioni.json").write_text(json.dumps(settings), encoding="utf-8")
        self.restart()
        calls = []

        def send(name, config, selected, project, prompt, conversazione=None, **kwargs):
            calls.append((name, prompt, conversazione))
            ident = THREAD if name == "codex" else "sessione-claude"
            if conversazione is None:
                kwargs["on_conversazione"](ident)
            if len(calls) == 2:
                return RisultatoEsecutore(conversazione=ident, limite=UsageLimit(
                    "five hour limit", self.clock.now() + timedelta(hours=2), "testo"))
            if len(calls) == 3:
                raise KeyboardInterrupt()
            self.git.is_dirty = True
            return RisultatoEsecutore(conversazione=ident, ultimo_messaggio="STATO: FINITO")

        self.services.send_executor = send
        self.services.review_controller = lambda *a, **kw: RisultatoControllore(ClaudeReview("ok", "ESITO: OK"))
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([call[0] for call in calls], [first, first, reserve])
        self.assertEqual(self.engine.state["esecutore_corrente"], reserve)
        self.assertEqual(self.engine.state["pezzi_completati"], 1)
        self.assertEqual(self.engine.state["pezzo_corrente"], 2)
        return calls

    def test_restart_reselects_recovered_priority_one_executor_and_preserves_work(self):
        calls = self.prepare_interrupted_reserve()
        self.clock.value += timedelta(hours=3)
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([call[0] for call in calls], ["codex", "codex", "claude", "codex"])
        handoff = calls[-1]
        self.assertIsNone(handoff[2])
        self.assertEqual(handoff[1].count("# FASCICOLO DELLA TASK"), 1)
        self.assertIn("I pezzi 1..1 sono già stati completati e approvati", handoff[1])
        self.assertIn("lavoro parziale nel working tree", handoff[1])
        self.assertEqual(self.final_snapshot()["pezzi_completati"], 2)
        self.assertIn("priorità rivalutate all'avvio", self.report())

    def test_restart_also_reselects_claude_when_it_has_priority_one(self):
        calls = self.prepare_interrupted_reserve(first="claude", reserve="codex")
        self.clock.value += timedelta(hours=3)
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([call[0] for call in calls], ["claude", "claude", "codex", "claude"])
        self.assertIsNone(calls[-1][2])
        self.assertIn("I pezzi 1..1 sono già stati completati e approvati", calls[-1][1])

    def test_restart_keeps_reserve_while_priority_one_executor_is_still_limited(self):
        calls = self.prepare_interrupted_reserve()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([call[0] for call in calls], ["codex", "codex", "claude", "claude"])
        self.assertEqual(calls[-1][2], "sessione-claude")
        self.assertEqual(self.clock.sleeps, [])

    def test_restart_respects_configured_budget_when_reconsidering_priority(self):
        calls = self.prepare_interrupted_reserve()
        self.clock.value += timedelta(hours=3)
        path = self.base / "impostazioni.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["esecutori"]["codex"]["limite_5h_percento"] = 30
        path.write_text(json.dumps(settings), encoding="utf-8")
        self.engine.state["periodo"]["budget"]["codex"]["consumato"] = 30
        self.engine._save()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([call[0] for call in calls], ["codex", "codex", "claude", "claude"])

    def test_restart_can_reconsider_previously_retired_only_active_executor(self):
        calls = self.prepare_interrupted_reserve()
        self.clock.value += timedelta(hours=3)
        path = self.base / "impostazioni.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["esecutori"]["claude"]["attivo"] = False
        path.write_text(json.dumps(settings), encoding="utf-8")
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(calls[-1][0], "codex")
        self.assertEqual(self.final_snapshot()["pezzi_completati"], 2)

    def test_uninterrupted_execution_does_not_switch_away_from_available_reserve(self):
        self.prepare_interrupted_reserve()
        self.clock.value += timedelta(hours=3)
        # Same instance: no new Start, so keep the active conversation.
        self.assertEqual(self.engine._select_executor(), "claude")

    def test_automatico_fuori_fascia_attende_senza_strumenti_poi_lavora(self):
        (self.base / "impostazioni.json").write_text(
            '{"spegnimento_automatico":false,"avvio_automatico":{"attivo":true,"modalita":"fasce",'
            '"fasce":[{"da":"13:00","a":"14:00"}]}}', encoding="utf-8")
        self.task()
        self.engine = Orchestrator(self.config, services=self.services, git=self.git, automatico=True)
        istanti = []
        originale = self.services.run_codex

        def codex(*args, **kwargs):
            istanti.append(self.clock.now())
            return originale(*args, **kwargs)

        self.services.run_codex = codex
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue(istanti and istanti[0].hour == 13)
        self.assertAlmostEqual(sum(self.clock.sleeps), 3600, delta=5)
        types = [e["tipo"] for e in ReportStore(self.base).events("2026-10-22")]
        self.assertLess(types.index("fuori_fascia"), types.index("inizio_fascia"))
        self.assertLess(types.index("inizio_fascia"), types.index("task_fatta"))

    def test_avvio_manuale_ignora_le_fasce(self):
        (self.base / "impostazioni.json").write_text(
            '{"spegnimento_automatico":false,"avvio_automatico":{"attivo":true,"modalita":"fasce",'
            '"fasce":[{"da":"13:00","a":"14:00"}]}}', encoding="utf-8")
        self.task()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(self.codex_calls), 1)

    def test_invalid_live_settings_stop_before_executor(self):
        self.task()
        (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":1}', encoding="utf-8")
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.codex_calls, [])
        self.assertIn("spegnimento_automatico", self.engine.state["ultimo_errore"])

    def test_live_shutdown_disabled_during_countdown(self):
        self.enable_shutdown()
        self.task()
        original_sleep = self.services.sleep

        def sleep(seconds):
            (self.base / "impostazioni.json").write_text('{"spegnimento_automatico":false}', encoding="utf-8")
            original_sleep(seconds)

        self.services.sleep = sleep
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.shutdown_calls, [])
        self.assertTrue(self.final_report_file().endswith("Spegnimento: disattivato\n"))

    def final_report_file(self):
        files = list((self.base / "report").glob("finale-*.md"))
        self.assertEqual(len(files), 1)
        return files[0].read_text(encoding="utf-8")

    def test_queue_completion_reports_and_shuts_down_once_in_continuous_mode(self):
        self.enable_shutdown()
        self.task()
        self.assertEqual(self.engine.run(), 0)
        self.assertEqual(len(self.shutdown_calls), 1)
        self.assertEqual(len(self.report_calls), 1)
        self.assertEqual(self.report_calls[0]["motivo_chiusura"], "coda terminata")
        report = self.final_report_file()
        self.assertIn("Report del modello finto", report)
        self.assertIn("## Dati grezzi", report)
        self.assertIn("new-hash", report)
        self.assertIn("Spegnimento: programmato alle 12:00", report)
        types = [event["tipo"] for event in ReportStore(self.base).events("2026-10-22")]
        self.assertEqual(types.count("report_finale"), 1)
        self.assertEqual(types.count("spegnimento_programmato"), 1)
        self.assertLess(types.index("report_finale"), types.index("spegnimento_programmato"))
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.shutdown_calls), 1)

    def test_queue_completion_also_shuts_down_once_in_una_volta(self):
        self.enable_shutdown()
        self.task()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.shutdown_calls), 1)

    def test_discarded_task_also_finishes_period(self):
        self.enable_shutdown()
        self.task()
        self.git.current_branch = "altro"
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.shutdown_calls), 1)
        self.assertEqual(self.report_calls[0]["task"][0]["esito"], "scartata")
        self.assertIn("Branch corrente", self.final_report_file())

    def test_empty_start_never_reports_or_shuts_down(self):
        self.enable_shutdown()
        self.assertEqual(self.run_cycle(), 0)
        self.clock.hook = self.stop.touch
        self.assertEqual(self.engine.run(), 0)
        self.assertTrue(self.clock.sleeps)
        self.assertEqual(self.shutdown_calls, [])
        self.assertEqual(self.report_calls, [])

    def check_weekly_finalization(self, tool):
        self.enable_shutdown()
        first = self.task()
        (first.parent / "002-finta.md").write_text(
            first.read_text(encoding="utf-8").replace("id: 001", "id: 002"), encoding="utf-8")
        retry = self.clock.now() + timedelta(days=3)
        if tool == "codex":
            self.codex_replies = [self.codex_reply(limit=UsageLimit("weekly limit", retry, "rollout", True))]
        else:
            self.claude_replies = [ClaudeReview("limite", "weekly limit", retry_at=retry)]
        self.assertEqual(self.run_cycle(), 0)
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], "attesa_limite")
        self.assertEqual(saved["periodo"]["task_ids"], [])
        self.assertTrue((first.parent / "002-finta.md").exists())
        self.assertEqual(len(self.shutdown_calls), 1)
        self.assertIn(f"limite settimanale {tool}, sblocco previsto {retry.isoformat()}", self.final_report_file())
        self.clock.hook = self.stop.touch
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.engine.state["fase"], "attesa_limite")
        self.assertEqual(len(self.shutdown_calls), 1)
        self.assertEqual(len(self.report_calls), 1)

    def test_weekly_codex_reports_with_queue_and_restart_only_waits(self):
        self.check_weekly_finalization("codex")

    def test_weekly_claude_reports_with_queue_and_restart_only_waits(self):
        self.check_weekly_finalization("claude")

    def test_five_hour_limit_waits_without_report_or_shutdown(self):
        self.enable_shutdown()
        self.task()
        self.codex_replies = [self.codex_reply(limit=UsageLimit(
            "usage limit", self.clock.now() + timedelta(hours=5), "rollout"))]
        self.clock.hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.engine.state["fase"], "attesa_limite")
        self.assertEqual(self.report_calls, [])
        self.assertEqual(self.shutdown_calls, [])

    def test_stop_during_shutdown_countdown_cancels(self):
        self.enable_shutdown()
        self.task()
        self.clock.hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.sleeps, [1])
        self.assertEqual(self.shutdown_calls, [])
        self.assertEqual(len(self.report_calls), 1)
        self.assertIn("spegnimento_annullato", self.report())
        self.assertIn("Spegnimento: disattivato", self.final_report_file())
        self.assertEqual(self.engine.state["periodo"]["task_ids"], [])

    def test_model_down_or_empty_still_writes_fallback_with_raw_data(self):
        self.enable_shutdown()
        self.task()
        self.services.generate_final_report = Mock(side_effect=TimeoutError("modello giù"))
        self.assertEqual(self.run_cycle(), 0)
        report = self.final_report_file()
        self.assertIn("modello non disponibile", report)
        self.assertIn("Task finta", report)
        self.assertIn("new-hash", report)
        self.assertIn("Spegnimento: programmato alle", report)
        self.assertEqual(len(self.shutdown_calls), 1)

    def test_interrupted_countdown_reuses_report_and_non_spegnere_cancels_on_restart(self):
        self.enable_shutdown()
        self.task()
        self.clock.hook = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue(StateStore(self.base).load()["finalizzazione"]["scritto"])
        self.assertEqual(self.shutdown_calls, [])
        self.clock.hook = None
        self.engine = Orchestrator(self.config, services=self.services, git=self.git, non_spegnere=True)
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.report_calls), 1)
        self.assertEqual(self.shutdown_calls, [])
        self.assertIn("Spegnimento: disattivato", self.final_report_file())

    def test_non_spegnere_overrides_enabled_config(self):
        self.enable_shutdown()
        self.task()
        self.engine = Orchestrator(self.config, services=self.services, git=self.git, non_spegnere=True)
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.shutdown_calls, [])
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("Spegnimento: disattivato", self.final_report_file())

    def test_claude_launch_failure_stops_in_same_phase_and_reuses_prompt(self):
        from orch.claude import run_claude
        self.task()
        self.services.run_claude = run_claude
        with patch("orch.claude.subprocess.Popen", side_effect=OSError("eseguibile assente")):
            self.assertEqual(self.run_cycle(), 1)
        state = StateStore(self.base).load()
        self.assertEqual(state["fase"], "claude")
        self.assertEqual((state["rimandi_totali"], state["bocciature"],
                          state["errori_claude_consecutivi"]), (0, 0, 0))
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("Impossibile avviare Claude", self.report())
        self.assertIn("controllare l'installazione", self.report())
        self.services.run_claude = self.claude
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.claude_calls[0], state["prompt_claude"])
        self.assertEqual(len(self.verify_calls), 1)

    def test_codex_interrupted_without_id_restarts_first_prompt(self):
        self.task()
        self.save_thread = False
        self.codex_hook = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.run_cycle(), 0)
        self.save_thread = True
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertIsNone(self.codex_calls[1][1])
        self.assertIn("ISTRUZIONI FISSE", self.codex_calls[1][0])

    def test_verification_phase_restarts_without_reinvoking_codex(self):
        self.task()
        self.codex_hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "verifica")
        self.stop.unlink()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.codex_calls), 1)

    def test_model_phase_restarts_from_saved_verification(self):
        self.task()
        self.changed_hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "modello")
        self.stop.unlink()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.verify_calls), 1)

    def test_stop_after_verifications_prevents_following_git_action(self):
        self.task()
        self.verify_hook = self.stop.touch
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "verifica")
        self.assertEqual(self.model_calls, [])
        self.stop.unlink()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(len(self.verify_calls), 2)

    def test_codex_not_logged_in_error_is_not_discarded(self):
        self.task()
        self.codex_replies = [self.codex_reply(errors=[CodexError("401 Unauthorized: not logged in")])]
        self.assertEqual(self.run_cycle(), 2)
        self.assertEqual(self.engine.state["serve_login"], "codex")
        self.assertEqual(self.model_calls, [])

    def test_commit_after_crash_is_recognized_without_second_commit(self):
        self.task()
        self.git.crash_commit = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "commit")
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len([c for c in self.git.calls if c[0] == "commit"]), 1)
        self.assertEqual(self.final_snapshot()["commit"], "new-hash")

    def test_query_inventory_failure_after_commit_retries_without_discard_or_second_commit(self):
        self.task()
        self.git.added_queries = Mock(side_effect=[GitError("diff non disponibile"),
                                                   ["supabase/queries/158_a.sql"]])
        self.assertEqual(self.run_cycle(), 1)
        saved = StateStore(self.base).load()
        self.assertEqual(saved["fase"], "commit")
        self.assertEqual(saved["commit_hash"], "new-hash")
        self.assertFalse(any(call[0] == "stash" for call in self.git.calls))
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len([c for c in self.git.calls if c[0] == "commit"]), 1)
        self.assertEqual(self.final_snapshot()["query_da_eseguire"], ["supabase/queries/158_a.sql"])

    def test_stash_after_crash_is_recognized_without_second_stash(self):
        self.task()
        self.claude_replies = [ClaudeReview("correzioni", "no", "correggi")] * 3
        self.git.crash_stash = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "scarto")
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len([c for c in self.git.calls if c[0] == "stash"]), 1)

    def test_push_phase_can_be_repeated_without_new_commit(self):
        self.task()
        self.git.origin = True
        self.git.crash_push = True
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "push")
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len([c for c in self.git.calls if c[0] == "commit"]), 1)
        self.assertEqual(len([c for c in self.git.calls if c[0] == "push"]), 2)

    def test_preparation_recovers_move_done_before_state_update(self):
        self.task()
        rename = Path.rename

        def crash_after_move(path, target):
            result = rename(path, target)
            if Path(target).parent.name == "in-corso":
                raise KeyboardInterrupt()
            return result

        with patch.object(Path, "rename", crash_after_move):
            self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(StateStore(self.base).load()["fase"], "preparazione")
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue((self.base / "fatte" / "001-finta.md").exists())

    def test_report_event_is_recovered_from_write_ahead_state(self):
        self.task()
        with patch.object(self.engine.reports, "record", side_effect=OSError("report interrotto")):
            with self.assertRaises(OSError):
                self.engine._choose()
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        events = ReportStore(self.base).events("2026-10-22")
        self.assertEqual(len([e for e in events if e["tipo"] == "task_iniziata"]), 1)
        self.assertIn("Esito: fatta", self.report())

    def test_clean_tree_at_rejection_does_not_create_stash(self):
        self.task()
        self.claude_replies = [ClaudeReview("correzioni", "no", "correggi")] * 3
        original = self.services.run_claude

        def review_clean(*args, **kwargs):
            result = original(*args, **kwargs)
            if len(self.claude_calls) == 3:
                self.git.is_dirty = False
            return result

        self.services.run_claude = review_clean
        self.assertEqual(self.run_cycle(), 0)
        self.assertFalse(any(c[0] == "stash" for c in self.git.calls))

    def test_closure_after_move_is_idempotent(self):
        self.task()
        rename = Path.rename

        def crash_after_move(path, target):
            result = rename(path, target)
            if Path(target).parent.name == "fatte":
                raise KeyboardInterrupt()
            return result

        with patch.object(Path, "rename", crash_after_move):
            self.assertEqual(self.run_cycle(), 0)
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        text = (self.base / "fatte" / "001-finta.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Esito orchestratore"), 1)

    def test_codex_authentication_stops_without_discard_then_resumes(self):
        self.task()
        self.codex_replies = [self.codex_reply(errors=[CodexError("OAuth expired: login")])]
        self.assertEqual(self.run_cycle(), 2)
        self.assertEqual(StateStore(self.base).load()["fase"], "invio_codex")
        self.assertTrue((self.base / "in-corso" / "001-finta.md").exists())
        self.assertIn("serve login", self.report())
        self.restart()
        self.assertEqual(self.run_cycle(), 0)

    def test_claude_authentication_stops_without_discard_then_resumes(self):
        self.task()
        self.claude_replies = [ClaudeReview("errore_autenticazione", "OAuth: login")]
        self.assertEqual(self.run_cycle(), 2)
        self.assertEqual(StateStore(self.base).load()["fase"], "claude")
        self.restart()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(self.claude_calls[0], self.claude_calls[1])

    def test_stash_failure_keeps_task_and_dirty_work_for_retry(self):
        self.task()
        self.claude_replies = [ClaudeReview("correzioni", "no", "correggi")] * 3
        self.git.stash_error = "stash fallito"
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(StateStore(self.base).load()["fase"], "scarto")
        self.assertTrue(self.git.is_dirty)
        self.assertTrue((self.base / "in-corso" / "001-finta.md").exists())

    def test_dirty_tree_remaining_after_stash_prevents_archival(self):
        self.task()
        self.claude_replies = [ClaudeReview("correzioni", "no", "correggi")] * 3
        stash = self.git.stash

        def incomplete_stash(*args):
            stash(*args)
            self.git.is_dirty = True

        self.git.stash = incomplete_stash
        self.assertEqual(self.run_cycle(), 1)
        self.assertTrue((self.base / "in-corso" / "001-finta.md").exists())
        self.assertEqual(StateStore(self.base).load()["fase"], "scarto")

    def test_continuous_empty_queue_wait_checks_stop_in_short_steps(self):
        self.clock.hook = self.stop.touch
        self.assertEqual(self.engine.run(), 0)
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual(self.git.calls, [])

    def test_stop_at_start_does_not_move_task_or_call_tools(self):
        path = self.task()
        self.stop.touch()
        self.assertEqual(self.run_cycle(), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.git.calls, [])
