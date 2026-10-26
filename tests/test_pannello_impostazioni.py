import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finti.catalogo_finto import CATALOGO  # noqa: E402
import impostazioni_gui as gui  # noqa: E402
import pannello  # noqa: E402
from orch import impostazioni as imp  # noqa: E402


class LogicaImpostazioniTests(unittest.TestCase):
    def test_tendine_dipendenti(self):
        modelli = gui.modelli_per("codex", CATALOGO)
        self.assertTrue(modelli)
        livelli = gui.livelli_per("codex", modelli[0], CATALOGO)
        self.assertTrue(livelli)
        self.assertEqual(gui.livelli_per("codex", "inesistente", CATALOGO), [])
        self.assertIn(gui.livello_predefinito("codex", modelli[0], CATALOGO, "zzz"), livelli)
        self.assertEqual(gui.livello_predefinito("codex", modelli[0], CATALOGO, livelli[-1]), livelli[-1])

    def test_antigravity_non_collegato(self):
        self.assertFalse(gui.collegato("antigravity", CATALOGO))
        self.assertTrue(gui.collegato("claude", CATALOGO))

    def test_fasce_e_limite(self):
        self.assertTrue(gui.fascia_valida("22:00", "07:00"))
        self.assertFalse(gui.fascia_valida("22:00", "22:00"))
        self.assertFalse(gui.fascia_valida("24:00", "07:00"))
        self.assertFalse(gui.fascia_valida("7:00", "08:00"))
        self.assertEqual(gui.limite_da_testo(" 30 "), 30)
        for sbagliato in ("0", "101", "trenta"):
            with self.assertRaises(ValueError):
                gui.limite_da_testo(sbagliato)

    def test_valori_costruiti_passano_la_validazione_vera(self):
        codex = gui.modelli_per("codex", CATALOGO)[0]
        claude = gui.modelli_per("claude", CATALOGO)[0]
        valori = gui.costruisci_valori(
            False, True, "fasce", [("22:00", "07:00")],
            {"strumento": "codex", "modello": codex, "ragionamento": gui.livello_predefinito("codex", codex, CATALOGO)},
            {"codex": {"attivo": True, "modello": codex, "ragionamento": gui.livello_predefinito("codex", codex, CATALOGO),
                       "priorita": 1, "limite_5h_percento": 30},
             "claude": {"attivo": True, "modello": claude, "ragionamento": gui.livello_predefinito("claude", claude, CATALOGO),
                        "priorita": 2, "limite_5h_percento": None},
             "antigravity": {"attivo": False, "modello": "", "ragionamento": "", "priorita": 3,
                             "limite_5h_percento": None}})
        controllati = imp.valida(valori, catalogo_modelli=CATALOGO)
        self.assertEqual(controllati["esecutori"]["codex"]["limite_5h_percento"], 30)
        self.assertEqual(controllati["avvio_automatico"]["fasce"], [{"da": "22:00", "a": "07:00"}])


class RigheRisorseTests(unittest.TestCase):
    def test_budget_e_finestra(self):
        with tempfile.TemporaryDirectory() as cartella:
            percorso = Path(cartella) / "impostazioni.json"
            percorso.write_text(json.dumps({
                "controllore": {"strumento": "codex", "modello": "m", "ragionamento": "medium"},
                "esecutori": {"claude": {"attivo": True, "modello": "c", "ragionamento": "high", "priorita": 2,
                                         "limite_5h_percento": None},
                              "codex": {"attivo": True, "modello": "m", "ragionamento": "medium", "priorita": 1,
                                        "limite_5h_percento": 30},
                              "antigravity": {"attivo": False, "priorita": 3}}}), encoding="utf-8")
            stato = {"periodo": {"budget": {"codex": {"consumato": 12.4}}},
                     "utilizzo_esecutori": {"codex": {"finestra_5h": 47.0}}}
            testo = pannello.righe_risorse(stato, percorso)
        righe = testo.splitlines()
        self.assertEqual(righe[0], "Controllore: codex m (medium)")
        self.assertIn("1. codex m (medium): usato 12% di 30% in questa sessione · finestra 5 ore al 47%", righe[1])
        self.assertIn("2. claude c (high): senza limite", righe[2])
        self.assertEqual(len(righe), 3)


if __name__ == "__main__":
    unittest.main()
