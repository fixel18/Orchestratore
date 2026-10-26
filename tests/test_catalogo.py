import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from orch.catalogo import catalogo, CatalogoError, LIVELLI_CLAUDE, NOTA_ANTIGRAVITY


class CatalogoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.cache = self.base / "models_cache.json"
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0,
            "  --effort <level>\n      Reasoning (low, medium, high, xhigh, max)\n", ""))

    def read(self):
        return catalogo(self.base, cache_codex=self.cache, modelli_antigravity=lambda: [],
                        comando_claude=["claude-finto", "arg"], runner=self.runner)

    def test_cache_filters_hidden_and_preserves_reasoning_and_display_name(self):
        model = {"slug": "nuovo", "display_name": "Nome leggibile", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}],
                 "default_reasoning_level": "ultra"}
        self.cache.write_text(json.dumps({"models": [model, {"visibility": "hide"}]}), encoding="utf-8-sig")
        data = self.read()
        self.assertEqual(data["codex"], [{"id": "nuovo", "nome": "Nome leggibile",
                          "livelli": ["low", "ultra"], "predefinito": "ultra"}])
        self.assertEqual(len(data["claude"]), 5)
        self.assertEqual(data["claude"][0]["livelli"], list(LIVELLI_CLAUDE))
        self.assertEqual(data["antigravity"], [])
        self.assertEqual(data["limite_percentuale_supportato"], {
            "codex": True, "claude": True, "antigravity": True})
        self.assertEqual(NOTA_ANTIGRAVITY, "da collegare")
        self.assertEqual(self.runner.call_args.args[0], ["claude-finto", "arg", "--help"])
        self.assertFalse(self.runner.call_args.kwargs["shell"])

    def test_help_can_add_levels_on_same_line(self):
        self.runner.return_value.stdout = "--effort <level> (low, medium, ultra)\n"
        self.assertEqual(self.read()["claude"][0]["livelli"], ["low", "medium", "ultra"])

    def test_missing_or_corrupt_cache_uses_minimal_reserve(self):
        for content in (None, "{", "[]", '{"models":null}', '{"models":[null]}'):
            with self.subTest(content=content):
                if content is not None:
                    self.cache.write_text(content, encoding="utf-8")
                self.assertEqual(self.read()["codex"][0]["id"], "gpt-6.1-sol")

    def test_valid_cache_with_only_hidden_models_stays_empty(self):
        self.cache.write_text('{"models":[{"visibility":"hide"}]}', encoding="utf-8")
        self.assertEqual(self.read()["codex"], [])

    def test_help_failures_use_known_levels(self):
        for error in (FileNotFoundError("assente"), subprocess.TimeoutExpired("fake", 10)):
            with self.subTest(error=error):
                self.runner.side_effect = error
                self.assertEqual(self.read()["claude"][0]["livelli"], list(LIVELLI_CLAUDE))
        self.runner.side_effect = None
        for code, text in ((1, "errore"), (0, "help senza effort")):
            self.runner.return_value = subprocess.CompletedProcess([], code, text, "")
            self.assertEqual(self.read()["claude"][0]["livelli"], list(LIVELLI_CLAUDE))

    def test_extra_adds_and_overrides_by_id_without_duplicates(self):
        item = {"id": "gpt-6.1-sol", "nome": "Personalizzato", "livelli": ["max"], "predefinito": "max"}
        added = {**item, "id": "manuale"}
        (self.base / "catalogo-extra.json").write_text(json.dumps({"codex": [item, added]}), encoding="utf-8")
        self.assertEqual(self.read()["codex"], [item, added])

    def test_invalid_extra_is_explicit_error(self):
        item = {"id": "custom", "nome": "Custom", "livelli": ["medium"], "predefinito": "medium"}
        for value in ([], {"altro": []}, {"codex": {}}, {"claude": [{}]},
                      {"codex": [item, item]}, {"codex": [{**item, "predefinito": "max"}]},
                      {"codex": [{**item, "livelli": ["medium", "medium"]}]}):
            with self.subTest(value=value):
                (self.base / "catalogo-extra.json").write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(CatalogoError):
                    self.read()

    def test_results_are_independent(self):
        first = self.read()
        first["claude"][0]["livelli"].clear()
        first["limite_percentuale_supportato"]["codex"] = False
        self.assertTrue(self.read()["claude"][0]["livelli"])
        self.assertTrue(self.read()["limite_percentuale_supportato"]["codex"])
