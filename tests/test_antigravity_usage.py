from datetime import datetime, timezone
import json
import unittest

from orch import antigravity
from orch.controllori import revisiona
from orch.esecutori import invia, limite_antigravity

# Risposta reale di `agy --output-format json -p=/usage` (versione 1.4.6, 23/10/2026).
USAGE = {"conversation_id": "", "status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
    {"name": "Gemini Models", "description": "Models within this group: Gemini Flash, Gemini Pro", "buckets": [
        {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.9185220003128052,
         "reset_time": "2026-10-28T08:06:40Z"},
        {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.9987363219261169,
         "reset_time": "2026-10-23T20:20:53Z"}]},
    {"name": "Claude and GPT models", "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
     "buckets": [
         {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1, "reset_time": "2026-10-30T16:07:00Z"},
         {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.25, "reset_time": "2026-10-23T21:07:00Z"}]}]}}}


class Risposta:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""


class UsageAntigravityTests(unittest.TestCase):
    def test_gruppo_scelto_dal_modello(self):
        gemini = antigravity.osservazione_da_usage(USAGE, "gemini-3.8-flash-high")
        self.assertAlmostEqual(gemini["finestra_5h"], 0.13, places=2)
        self.assertAlmostEqual(gemini["settimana"], 8.15, places=2)
        self.assertEqual(gemini["resets_5h"], datetime(2026, 10, 23, 20, 20, 53, tzinfo=timezone.utc))
        claude = antigravity.osservazione_da_usage(USAGE, "claude-opus-4-6-thinking")
        self.assertEqual((claude["finestra_5h"], claude["settimana"]), (75.0, 0.0))
        gpt = antigravity.osservazione_da_usage(USAGE, "gpt-oss-120b-medium")
        self.assertEqual(gpt["finestra_5h"], 75.0)

    def test_risposte_strane_danno_utilizzo_ignoto(self):
        for dati in ({}, {"command": {"data": {"groups": "x"}}}, {"command": {"data": {"groups": [
                {"name": "A", "buckets": []}, {"name": "B", "buckets": []}]}}}):
            with self.subTest(dati=dati):
                self.assertIsNone(antigravity.osservazione_da_usage(dati, "gemini-x")["finestra_5h"])
        rotto = {"command": {"data": {"groups": [{"name": "Gemini", "buckets": [
            {"window": "5h", "remaining_fraction": 7}]}]}}}
        self.assertIsNone(antigravity.osservazione_da_usage(rotto, "gemini-x")["finestra_5h"])

    def test_lettura_usa_slash_usage_in_json_senza_consumare(self):
        chiamate = []

        def runner(cmd, **kwargs):
            chiamate.append(cmd)
            return Risposta("avviso\n" + json.dumps(USAGE))

        oss = antigravity.utilizzo("gemini-x", runner=runner)
        self.assertEqual(chiamate[0][1:], ["--output-format", "json", "-p=/usage"])
        self.assertIsNotNone(oss["finestra_5h"])
        self.assertIsNone(antigravity.utilizzo("gemini-x", runner=lambda c, **k: Risposta("", 1))["finestra_5h"])

    def test_esecutore_e_controllore_riportano_le_percentuali(self):
        def agy(config, progetto, prompt, impostazioni, **kwargs):
            return antigravity.RisultatoAgy(conversazione="c", risposta="ESITO: OK", stato="SUCCESS")

        lettore = lambda modello: antigravity.osservazione_da_usage(USAGE, modello)
        r = invia("antigravity", None, {"modello": "claude-sonnet-4-6"}, ".", "x", run_agy=agy, leggi_utilizzo=lettore)
        self.assertEqual(r.osservazione_utilizzo["finestra_5h"], 75.0)
        c = revisiona("antigravity", None, {"modello": "claude-sonnet-4-6"}, ".", "x", run_agy=agy,
                      leggi_utilizzo=lettore)
        self.assertEqual(c.osservazione_utilizzo["finestra_5h"], 75.0)

    def test_limite_usa_l_orario_di_sblocco(self):
        reset5 = datetime(2026, 10, 23, 21, tzinfo=timezone.utc)
        resetw = datetime(2026, 10, 30, tzinfo=timezone.utc)
        l = limite_antigravity("quota", {"finestra_5h": 100.0, "resets_5h": reset5,
                                         "settimana": 40.0, "resets_settimana": resetw})
        self.assertEqual((l.retry_at, l.weekly), (reset5, False))
        l = limite_antigravity("quota", {"finestra_5h": 100.0, "resets_5h": reset5,
                                         "settimana": 100.0, "resets_settimana": resetw})
        self.assertEqual((l.retry_at, l.weekly), (resetw, True))
        self.assertIsNone(limite_antigravity("quota", {"finestra_5h": None}).retry_at)


class RagionamentoAntigravityTests(unittest.TestCase):
    def test_livello_compreso_nel_modello(self):
        import tempfile
        from pathlib import Path
        from finti.catalogo_finto import CATALOGO
        from orch.impostazioni import predefinite, valida
        with tempfile.TemporaryDirectory() as cartella:
            finto = Path(cartella) / "agy.exe"
            finto.write_text("")
            voci = antigravity.modelli(runner=lambda c, **k: Risposta("Fetching...\ngemini-3.8-flash-high\tGemini (High)\n"),
                                       eseguibile=finto)
        self.assertEqual(voci, [{"id": "gemini-3.8-flash-high", "nome": "Gemini (High)", "livelli": [], "predefinito": ""}])
        cat = dict(CATALOGO, antigravity=voci)
        dati = predefinite()
        dati["esecutori"]["antigravity"].update(attivo=True, modello="gemini-3.8-flash-high", ragionamento="max")
        dati["controllore"] = {"strumento": "antigravity", "modello": "gemini-3.8-flash-high", "ragionamento": "low"}
        risultato = valida(dati, catalogo_modelli=cat)
        self.assertEqual(risultato["esecutori"]["antigravity"]["ragionamento"], "")
        self.assertEqual(risultato["controllore"]["ragionamento"], "")
        cmd = antigravity.comando({"modello": "gemini-3.8-flash-high", "ragionamento": ""}, prompt_in_riga="x")
        self.assertNotIn("--effort", cmd)


if __name__ == "__main__":
    unittest.main()
