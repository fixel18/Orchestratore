import json
import os
import unittest
from unittest.mock import patch

from orch import account


def fake(stdout="", returncode=0, stderr=""):
    calls = []

    def run(comando, **kwargs):
        calls.append((comando, kwargs))
        return type("R", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()

    run.calls = calls
    return run


class AccountTests(unittest.TestCase):
    def setUp(self):
        esiste = patch.object(account.Path, "exists", return_value=True)
        esiste.start()
        self.addCleanup(esiste.stop)

    def test_codex_collegato_e_non(self):
        self.assertTrue(account.stato("codex", runner=fake("Logged in using ChatGPT")).collegato)
        self.assertFalse(account.stato("codex", runner=fake("Not logged in", 1)).collegato)

    def test_claude_collegato_con_piano(self):
        run = fake(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro",
                               "email": "x@y"}))
        s = account.stato("claude", runner=run)
        self.assertTrue(s.collegato)
        self.assertEqual(s.dettaglio, "collegato con claude.ai, piano pro")
        self.assertNotIn("x@y", s.dettaglio)  # l'email non si mostra
        self.assertFalse(account.stato("claude", runner=fake('{"loggedIn": false}')).collegato)
        self.assertFalse(account.stato("claude", runner=fake("non json")).collegato)

    def test_chiavi_api_tolte_dall_ambiente(self):
        run = fake("Logged in using ChatGPT")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "ALTRO": "z"}):
            account.stato("codex", runner=run)
        env = run.calls[0][1]["env"]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["ALTRO"], "z")

    def test_comandi_di_accesso_usano_l_abbonamento(self):
        self.assertEqual(account.comando_accesso("codex")[1:], ["login"])
        self.assertEqual(account.comando_accesso("claude")[1:], ["auth", "login", "--claudeai"])
        lanciati = []
        account.accedi("claude", launcher=lanciati.append)
        self.assertEqual(lanciati[0][1:], ["auth", "login", "--claudeai"])

    def test_uscita(self):
        run = fake()
        self.assertTrue(account.esci("codex", runner=run))
        self.assertEqual(run.calls[0][0][1:], ["logout"])
        run = fake()
        account.esci("claude", runner=run)
        self.assertEqual(run.calls[0][0][1:], ["auth", "logout"])
        with self.assertRaisesRegex(RuntimeError, "errore"):
            account.esci("codex", runner=fake(returncode=1, stderr="errore"))
        with self.assertRaises(ValueError):
            account.esci("antigravity")

    def test_antigravity_via_cli_agy(self):
        s = account.stato("antigravity", runner=fake("Fetching...\ngemini-x\tGemini X\nclaude-y\tClaude Y\n"))
        self.assertTrue(s.collegato)
        self.assertIn("2 modelli", s.dettaglio)
        self.assertFalse(s.puo_uscire)  # agy non ha logout: si cambia account dall'app
        self.assertFalse(account.stato("antigravity", runner=fake("", 1, "please sign in")).collegato)
        self.assertEqual(account.comando_accesso("antigravity")[-1].lower()[-7:], "agy.exe")


if __name__ == "__main__":
    unittest.main()
