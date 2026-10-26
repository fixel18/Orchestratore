from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orch.config import load_config
from orch.final_report import period_data, render_report
from orch.model import generate_final_report
from orch.ciclo import shutdown_pc


TEST_ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 23, 10, 5, tzinfo=timezone.utc)


class FinalReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "config.json").write_text("{}", encoding="utf-8")
        self.config = load_config(self.base / "config.json")
        (self.base / "regole").mkdir()
        (self.base / "regole" / "prompt-report-finale.md").write_text(
            "Scrivi un report in italiano senza inventare dati.", encoding="utf-8")
        self.task = {"id": "004", "titolo": "Titolo | speciale", "esito": "scartata",
            "pezzi_completati": 1, "pezzi_totali": 2, "rimandi_totali": 2, "bocciature": 3,
            "commit": None, "push": "non eseguito", "motivo": "Verifica fallita",
            "attese": [{"strumento": "codex", "inizio": "09:00", "fino_a": "10:00", "fine": None}]}
        self.event = {"event_id": "event-1", "at": NOW.isoformat(), "tipo": "errore_operativo",
                      "messaggio": "Eseguibile assente", "task": self.task}
        self.period = {"inizio": NOW.isoformat(), "task_ids": ["004"],
                       "task": {"004": self.task}, "eventi": [self.event]}
        self.data = period_data(self.period, "coda terminata", NOW)
        no_network = patch("orch.model.urlopen", side_effect=AssertionError("Rete reale vietata"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def test_free_text_payload_timeout_and_think_removal(self):
        transport = Mock(return_value={"message": {"content": "<think>segreto</think>\n# Report italiano"}})
        result = generate_final_report(self.config, self.data, transport=transport)
        self.assertEqual(result, "# Report italiano")
        url, payload, timeout = transport.call_args.args
        self.assertEqual(url, self.config.url_modello)
        self.assertEqual(timeout, 600)
        self.assertNotIn("format", payload)
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], self.config.num_ctx_modello)
        self.assertEqual(json.loads(payload["messages"][1]["content"]), self.data)

    def test_bad_response_is_reported_to_caller_for_fallback(self):
        for response in (None, {}, {"message": {}}, {"message": {"content": 123}}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                generate_final_report(self.config, self.data, transport=lambda *args: response)

    def test_empty_and_think_only_responses_have_data_fallback(self):
        for content in ("", "  ", "<think>ragionamento</think>", "<think>incompleto"):
            with self.subTest(content=content):
                report = render_report(self.data, content, None)
                self.assertIn("modello non disponibile", report)
                self.assertIn("Verifica fallita", report)
                self.assertIn("Eseguibile assente", report)
                self.assertNotIn("<think>", report)
                self.assertTrue(report.endswith("Spegnimento: disattivato\n"))

    def test_script_appends_raw_data_and_shutdown_line_to_model_text(self):
        report = render_report(self.data, "# Riepilogo del modello", NOW)
        self.assertIn("Titolo \\| speciale", report)
        self.assertIn("| 004 |", report)
        self.assertIn("| 1/2 | 2 | 3 |", report)
        self.assertIn("09:00", report)
        self.assertIn("errore_operativo", report)
        self.assertIn("coda terminata", report)
        self.assertTrue(report.endswith("Spegnimento: programmato alle 10:05\n"))
        self.task["titolo"] = "Cambiato"
        self.assertEqual(self.data["task"][0]["titolo"], "Titolo | speciale")

    def test_query_section_is_deterministic_even_when_model_omits_it_or_is_unavailable(self):
        project = self.base / "progetto"
        files = ["supabase/queries/159_b.sql", "supabase/queries/158_a.sql"]
        data = {**self.data, "task": [{**self.task, "esito": "fatta", "progetto": str(project),
                "query_da_eseguire": files, "note_query_codex": "Verificare prerequisiti"}]}
        for content in ("# Testo del modello senza query", ""):
            with self.subTest(content=content):
                report = render_report(data, content, None)
                section = report.split("## Query da eseguire su Supabase (elenco dello script)", 1)[1]
                first = str(project / files[1])
                second = str(project / files[0])
                self.assertLess(section.index(first), section.index(second))
                self.assertIn("task 004", section)
                self.assertIn("Verificare prerequisiti", section)

    def test_query_section_always_reports_no_queries_or_discard_warning(self):
        for outcome, expected in (("fatta", "nessuna"), ("scartata", "NON eseguirli")):
            data = {**self.data, "task": [{**self.task, "esito": outcome}]}
            for content in ("# Modello", ""):
                with self.subTest(outcome=outcome, content=content):
                    section = render_report(data, content, None).split(
                        "## Query da eseguire su Supabase (elenco dello script)", 1)[1]
                    self.assertIn(expected, section)

    def test_repository_prompt_requires_query_section_and_discard_warning(self):
        prompt = (TEST_ROOT.parent / "regole" / "prompt-report-finale.md").read_text(encoding="utf-8")
        self.assertIn('sezione obbligatoria "Query da eseguire su Supabase"', prompt)
        self.assertIn("note_query_codex", prompt)
        self.assertIn("NON eseguirli", prompt)

    def test_shutdown_command_is_passed_without_shell_and_never_executed(self):
        with patch("orch.ciclo.subprocess.run") as fake:
            shutdown_pc(self.config)
        fake.assert_called_once_with(self.config.comando_spegnimento, shell=False, check=True)
        self.assertNotIn("/f", [arg.casefold() for arg in self.config.comando_spegnimento])


if __name__ == "__main__":
    unittest.main()
