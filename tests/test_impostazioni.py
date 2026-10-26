from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finti.catalogo_finto import CATALOGO
from orch.config import load_config
from orch.impostazioni import carica, salva, valida, predefinite, ImpostazioniError


class ImpostazioniTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "impostazioni.json"

    def validate(self, data):
        return valida(data, catalogo_modelli=CATALOGO)

    def test_missing_file_is_created_with_expected_defaults(self):
        result = carica(self.path, catalogo_modelli=CATALOGO)
        self.assertEqual(result, predefinite())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), result)
        self.assertTrue(result["spegnimento_automatico"])
        self.assertEqual(list(self.base.iterdir()), [self.path])

    def test_partial_file_gets_nested_defaults_without_being_rewritten(self):
        text = '{"esecutori":{"codex":{"limite_5h_percento":30}},"spegnimento_automatico":false}'
        self.path.write_text(text, encoding="utf-8-sig")
        result = carica(self.path, catalogo_modelli=CATALOGO)
        self.assertFalse(result["spegnimento_automatico"])
        self.assertEqual(result["esecutori"]["codex"]["limite_5h_percento"], 30)
        self.assertEqual(result["esecutori"]["claude"], predefinite()["esecutori"]["claude"])
        self.assertEqual(self.path.read_text(encoding="utf-8-sig"), text)

    def test_codex_defaults_follow_config_but_shutdown_has_new_default(self):
        config_path = self.base / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        config = replace(load_config(config_path), modello_codex="custom", ragionamento_codex="high",
                         spegnimento_abilitato=False)
        models = deepcopy(CATALOGO)
        models["codex"].append({"id": "custom", "livelli": ["high"]})
        result = carica(self.path, config=config, catalogo_modelli=models)
        self.assertEqual(result["esecutori"]["codex"]["modello"], "custom")
        self.assertEqual(result["esecutori"]["codex"]["ragionamento"], "high")
        self.assertTrue(result["spegnimento_automatico"])

    def test_save_and_reload_round_trip_and_input_not_mutated(self):
        data = {"esecutori": {"claude": {"attivo": True, "limite_5h_percento": 1}}}
        original = deepcopy(data)
        result = salva(self.path, data, catalogo_modelli=CATALOGO)
        self.assertEqual(carica(self.path, catalogo_modelli=CATALOGO), result)
        self.assertEqual(data, original)
        result["avvio_automatico"]["fasce"].clear()
        self.assertTrue(predefinite()["avvio_automatico"]["fasce"])

    def test_invalid_json_is_not_overwritten(self):
        for text in ("{", "null", "[]", '{"sconosciuto":1}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ImpostazioniError):
                    carica(self.path, catalogo_modelli=CATALOGO)
                self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_invalid_save_keeps_previous_file(self):
        salva(self.path, {}, catalogo_modelli=CATALOGO)
        original = self.path.read_bytes()
        with self.assertRaises(ImpostazioniError):
            salva(self.path, {"spegnimento_automatico": 1}, catalogo_modelli=CATALOGO)
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_atomic_replace_keeps_old_file_and_removes_temporary(self):
        salva(self.path, {}, catalogo_modelli=CATALOGO)
        original = self.path.read_bytes()
        with patch("orch.persistence.os.replace", side_effect=OSError("errore finto")):
            with self.assertRaises(ImpostazioniError):
                salva(self.path, {"spegnimento_automatico": False}, catalogo_modelli=CATALOGO)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.base.iterdir()), [self.path])

    def test_priorities_distinct_even_for_inactive_executors(self):
        for value in (0, 4, True, 1.0, "1", None, 2, 3):
            with self.subTest(value=value), self.assertRaises(ImpostazioniError):
                self.validate({"esecutori": {"codex": {"priorita": value}}})

    def test_budget_null_or_integer_1_to_100(self):
        for value in (None, 1, 30, 100):
            self.validate({"esecutori": {"codex": {"limite_5h_percento": value}}})
        for value in (0, 101, -1, True, 1.0, "30"):
            with self.subTest(value=value), self.assertRaises(ImpostazioniError):
                self.validate({"esecutori": {"codex": {"limite_5h_percento": value}}})

    def test_at_least_one_active_and_strict_booleans(self):
        for data in ({"esecutori": {"codex": {"attivo": False}}},
                     {"esecutori": {"claude": {"attivo": "si"}}},
                     {"avvio_automatico": {"attivo": 1}}, {"spegnimento_automatico": None}):
            with self.subTest(data=data), self.assertRaises(ImpostazioniError):
                self.validate(data)

    def test_antigravity_accetta_il_limite_percentuale(self):
        for value in (None, 1, 30, 100):
            with self.subTest(value=value):
                self.validate({"esecutori": {"antigravity": {"limite_5h_percento": value}}})
        for value in (True, "30", 0, 101):
            with self.subTest(value=value), self.assertRaisesRegex(ImpostazioniError, "antigravity"):
                self.validate({"esecutori": {"antigravity": {"limite_5h_percento": value}}})

    def test_bands_accept_midnight_and_reject_bad_times_or_equal_ends(self):
        self.validate({"avvio_automatico": {"modalita": "fasce", "fasce": [
            {"da": "22:00", "a": "07:00"}, {"da": "08:30", "a": "12:00"}]}})
        for bands in ([], None, [{"da": "07:00", "a": "07:00"}], [{"da": "8:00", "a": "09:00"}],
                      [{"da": "24:00", "a": "01:00"}], [{"da": "22:60", "a": "01:00"}],
                      [{"da": 123, "a": "09:00"}], [{"da": "08:00"}], [None]):
            with self.subTest(bands=bands), self.assertRaises(ImpostazioniError):
                self.validate({"avvio_automatico": {"modalita": "fasce", "fasce": bands}})
        self.validate({"avvio_automatico": {"modalita": "sempre", "fasce": []}})

    def test_invalid_selections_and_unknown_fields(self):
        for data in ({"controllore": {"strumento": "altro"}}, {"controllore": {"strumento": []}},
                     {"controllore": {"modello": "assente"}}, {"controllore": {"ragionamento": "ultra"}},
                     {"esecutori": {"claude": {"modello": "assente"}}},
                     {"esecutori": {"codex": {"ragionamento": "assente"}}},
                     {"esecutori": {"altro": {}}}, {"esecutori": []},
                     {"avvio_automatico": {"modalita": "altro"}}, {"controllore": {"extra": 1}}):
            with self.subTest(data=data), self.assertRaises(ImpostazioniError):
                self.validate(data)

    def test_codex_controller_and_antigravity_are_selectable(self):
        self.validate({"controllore": {"strumento": "codex", "modello": "gpt-6.1-sol",
                                       "ragionamento": "ultra"}})
        # Antigravity non collegato (catalogo vuoto): ammesso solo se non usato.
        with self.assertRaisesRegex(ImpostazioniError, "Antigravity non è collegato"):
            self.validate({"controllore": {"strumento": "antigravity", "modello": "", "ragionamento": ""}})
        with self.assertRaisesRegex(ImpostazioniError, "Antigravity non è collegato"):
            self.validate({"esecutori": {"codex": {"attivo": False}, "antigravity": {"attivo": True}}})
        collegato = dict(CATALOGO, antigravity=[{"id": "gemini-x", "nome": "Gemini X",
                                                 "livelli": ["low", "medium"], "predefinito": "medium"}])
        valida({"controllore": {"strumento": "antigravity", "modello": "gemini-x", "ragionamento": "low"},
                "esecutori": {"antigravity": {"attivo": True, "modello": "gemini-x", "ragionamento": "medium"}}},
               catalogo_modelli=collegato)

    def test_antigravity_collegato_ma_spento_senza_modello_resta_valido(self):
        collegato = deepcopy(CATALOGO)
        collegato["antigravity"] = [{"id": "gemini-x", "nome": "Gemini X", "livelli": ["low", "medium"],
                                     "predefinito": "medium"}]
        self.assertEqual(valida({}, catalogo_modelli=collegato)["esecutori"]["antigravity"]["modello"], "")
        data = predefinite()
        data["esecutori"]["antigravity"]["attivo"] = True
        with self.assertRaisesRegex(ImpostazioniError, "antigravity"):
            valida(data, catalogo_modelli=collegato)

    def test_public_project_settings_disable_automatic_shutdown_and_start(self):
        path = Path(__file__).resolve().parent.parent / "impostazioni.json"
        settings = carica(path, catalogo_modelli=CATALOGO)
        expected = predefinite()
        expected["spegnimento_automatico"] = False
        self.assertEqual(settings, expected)
