from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest

from orch.config import ConfigError, load_config
from orchestratore import main


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "config.json"

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def test_project_config(self):
        config = load_config(ROOT / "config.json")
        self.assertEqual(config.cartella_base, ROOT)
        self.assertEqual(config.python, Path(sys.executable).resolve())
        self.assertEqual(config.comando_codex, ["codex.cmd"])
        self.assertEqual(config.modello_codex, "gpt-6.1-sol")
        self.assertEqual(config.ragionamento_codex, "medium")
        self.assertEqual(config.comando_claude, ["claude.cmd"])
        self.assertEqual(config.nome_modello, "qwen3:14b")
        self.assertEqual(config.sandbox_codex, "workspace-write")
        self.assertFalse(config.push_abilitato)
        self.assertFalse(config.consenti_modifiche_preesistenti)
        self.assertEqual(config.timeout_modello_s, 300)
        self.assertEqual(config.num_ctx_modello, 16384)
        self.assertEqual(config.rimandi_max_per_pezzo, 2)
        self.assertEqual(config.bocciature_claude_max, 3)
        self.assertEqual(config.attesa_limite_minuti, 30)
        self.assertEqual(config.variabili_da_rimuovere, ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
        self.assertEqual(config.escludi_dal_commit, [
            "**/__pycache__/**", "**/*.pyc", "**/*.tsbuildinfo",
            "**/.next/**", "**/node_modules/**", "**/.env*",
        ])

    def test_commit_exclusions_have_defaults_and_can_be_overridden_or_disabled(self):
        config = load_config(self.write({}))
        self.assertEqual(config.escludi_dal_commit, load_config(ROOT / "config.json").escludi_dal_commit)
        config = load_config(self.write({"escludi_dal_commit": ["**/cache con spazi/**"]}))
        self.assertEqual(config.escludi_dal_commit, ["**/cache con spazi/**"])
        self.assertEqual(load_config(self.write({"escludi_dal_commit": []})).escludi_dal_commit, [])

    def test_preexisting_changes_default_override_and_validation(self):
        self.assertTrue(load_config(self.write({})).consenti_modifiche_preesistenti)
        self.assertFalse(load_config(self.write({"consenti_modifiche_preesistenti": False}))
                         .consenti_modifiche_preesistenti)
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "consenti_modifiche_preesistenti"):
                load_config(self.write({"consenti_modifiche_preesistenti": value}))

    def test_query_folder_default_override_and_validation(self):
        self.assertEqual(load_config(self.write({})).cartella_query, "supabase/queries")
        self.assertEqual(load_config(self.write({"cartella_query": "sql/manuali"})).cartella_query, "sql/manuali")
        for value in (None, 1, "", "../fuori", str(self.base.resolve())):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "cartella_query"):
                load_config(self.write({"cartella_query": value}))

    def test_period_configuration_defaults_and_overrides(self):
        for config in (load_config(self.write({})), load_config(ROOT / "config.json")):
            self.assertEqual(config.spegnimento_abilitato, config.cartella_base != ROOT)
            self.assertEqual(config.comando_spegnimento,
                             ["shutdown", "/s", "/t", "0", "/c", "Orchestratore: lavoro terminato"])
            self.assertEqual(config.attesa_prima_spegnimento_s, 120)
            self.assertEqual(config.soglia_limite_settimanale_ore, 12)
            self.assertEqual(config.timeout_report_s, 600)
        config = load_config(self.write({"spegnimento_abilitato": False,
            "comando_spegnimento": ["finto", "argomento"], "attesa_prima_spegnimento_s": 0,
            "soglia_limite_settimanale_ore": 6.5, "timeout_report_s": 10}))
        self.assertFalse(config.spegnimento_abilitato)
        self.assertEqual(config.comando_spegnimento, ["finto", "argomento"])
        self.assertEqual(config.attesa_prima_spegnimento_s, 0)
        self.assertEqual(config.soglia_limite_settimanale_ore, 6.5)
        self.assertEqual(config.timeout_report_s, 10)

    def test_invalid_period_configuration(self):
        for key, values in (
            ("spegnimento_abilitato", (1, "true", None)),
            ("comando_spegnimento", ([], "shutdown", [""])),
            ("attesa_prima_spegnimento_s", (-1, True, float("inf"), float("nan"))),
            ("soglia_limite_settimanale_ore", (0, -1, True, float("inf"))),
            ("timeout_report_s", (0, "600", False, float("nan"))),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ConfigError, key):
                    load_config(self.write({key: value}))

    def test_defaults_and_relative_paths_are_based_on_config_location(self):
        config = load_config(self.write({"cartella_base": "progetto", "python": "bin/python.exe"}))
        self.assertEqual(config.cartella_base, self.base / "progetto")
        self.assertEqual(config.python, self.base / "bin" / "python.exe")
        self.assertEqual(config.timeout_codex_minuti, 60)
        self.assertEqual(config.timeout_verifica_minuti, 20)
        self.assertEqual(config.timeout_claude_minuti, 20)
        self.assertEqual(config.intervallo_coda_vuota_s, 300)
        self.assertTrue(config.push_abilitato)
        self.assertTrue(config.consenti_modifiche_preesistenti)
        self.assertEqual(config.timeout_modello_s, 300)
        self.assertEqual(config.num_ctx_modello, 16384)
        self.assertFalse(config.cartella_base.exists())
        self.assertEqual(config.modello_codex, "gpt-6.1-sol")
        self.assertEqual(config.ragionamento_codex, "medium")

    def test_alternative_command_lists_and_fractional_timeout(self):
        command = ["python.exe", "tests/finti/codex.py", "argomento con spazi"]
        config = load_config(self.write({
            "comando_codex": command, "timeout_modello_s": 0.5,
            "variabili_da_rimuovere": [], "rimandi_max_per_pezzo": 0,
        }))
        self.assertEqual(config.comando_codex, command)
        self.assertEqual(config.timeout_modello_s, 0.5)
        self.assertEqual(config.variabili_da_rimuovere, [])

    def test_bom_supported(self):
        self.path.write_text("{}", encoding="utf-8-sig")
        self.assertEqual(load_config(self.path).cartella_base, self.base)

    def test_invalid_json_missing_file_and_non_object(self):
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)
        for value in ([], None, "config"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    load_config(self.write(value))

    def test_invalid_field_types_and_values(self):
        cases = (
            ("comando_codex", "codex.cmd"), ("comando_codex", []),
            ("comando_claude", ["claude.cmd", 3]), ("comando_claude", [""]),
            ("variabili_da_rimuovere", "OPENAI_API_KEY"),
            ("escludi_dal_commit", "**/*.pyc"), ("escludi_dal_commit", [""]),
            ("escludi_dal_commit", [1]), ("escludi_dal_commit", None),
            ("cartella_base", ""), ("python", None), ("nome_modello", " "),
            ("modello_codex", ""), ("ragionamento_codex", None),
            ("url_modello", "file:///test"), ("url_modello", "http://"),
            ("push_abilitato", "no"), ("push_abilitato", 1),
            ("rimandi_max_per_pezzo", -1), ("rimandi_max_per_pezzo", True),
            ("bocciature_claude_max", 0), ("bocciature_claude_max", 2.5),
            ("num_ctx_modello", 0), ("num_ctx_modello", True),
            ("num_ctx_modello", 1.5),
            ("timeout_codex_minuti", 0), ("timeout_verifica_minuti", -2),
            ("timeout_claude_minuti", "20"), ("timeout_modello_s", True),
            ("intervallo_coda_vuota_s", float("inf")),
            ("attesa_limite_minuti", float("nan")), ("sconosciuto", 1),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ConfigError, key):
                    load_config(self.write({key: value}))

    def test_cli_stato_reads_alternative_config_without_mutations(self):
        (self.base / "coda").mkdir()
        self.write({})
        before = sorted(p.relative_to(self.base) for p in self.base.rglob("*"))
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--config", str(self.path), "--stato"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertEqual(before, sorted(p.relative_to(self.base) for p in self.base.rglob("*")))

    def test_cli_config_error(self):
        error = io.StringIO()
        with redirect_stderr(error):
            result = main(["--config", str(self.path)])
        self.assertEqual(result, 1)
        self.assertIn("Errore:", error.getvalue())


if __name__ == "__main__":
    unittest.main()
