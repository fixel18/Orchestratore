from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

from orch.config import load_config
from orch.model import decide_next_action, ollama_transport, parse_model_decision
from orch.verification import VerificationResult


TEST_ROOT = Path(__file__).resolve().parent


def content(outcome="chiama_claude", message="", reason="Verifiche completate"):
    return json.dumps({"esito": outcome, "messaggio": message, "motivo": reason}, ensure_ascii=False)


def envelope(text=None):
    return {"message": {"content": content() if text is None else text}}


class ModelParserTests(unittest.TestCase):
    def test_three_allowed_outcomes(self):
        for outcome in ("rimanda_a_codex", "chiama_claude", "limite_raggiunto"):
            with self.subTest(outcome=outcome):
                parsed = parse_model_decision(content(outcome, "Correggi città", "Motivo"))
                self.assertEqual(parsed.esito, outcome)
                self.assertEqual(parsed.messaggio, "Correggi città")
                self.assertEqual(parsed.motivo, "Motivo")

    def test_multiline_think_blocks_are_removed_and_original_response_retained(self):
        raw = '<think>prima\n{"finto": true}\nseconda</think>\n<THINK>altro</THINK>\n' + content()
        parsed = parse_model_decision(raw)
        self.assertEqual(parsed.esito, "chiama_claude")
        self.assertEqual(parsed.raw_response, raw)

    def test_invalid_json_schema_fields_and_unknown_outcome(self):
        invalid = [
            "", "Non JSON", "[]", "null", "{}", '```json\n' + content() + '\n```',
            content("finito"), '<think>non chiuso ' + content(),
            '{"esito":"chiama_claude","messaggio":"","motivo":"ok","extra":NaN}',
        ]
        for key in ("esito", "messaggio", "motivo"):
            for value in (None, 3, [], {}):
                data = json.loads(content())
                data[key] = value
                invalid.append(json.dumps(data))
            data = json.loads(content())
            del data[key]
            invalid.append(json.dumps(data))
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_model_decision(raw)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        path = self.base / "config.json"
        path.write_text("{}", encoding="utf-8")
        self.config = load_config(path)
        (self.base / "regole").mkdir()
        self.system_prompt = "/no_think\nIstruzioni finte per lo smistatore."
        (self.base / "regole" / "prompt-modello.md").write_text(self.system_prompt, encoding="utf-8")

    def decide(self, *, transport, referrals=0, config=None, **kwargs):
        return decide_next_action(
            config or self.config, "Fascicolo intero", 3, referrals,
            "STATO: FINITO", [], [], transport=transport, **kwargs,
        )

    def test_payload_system_prompt_parameters_and_trimmed_codex_message(self):
        transport = Mock(return_value=envelope())
        verification = VerificationResult(
            "python -m unittest", 1, False, "OUTPUT INTEGRALE NON INVIARE",
            ("2: AssertionError: città",), None, 1.0,
        )
        response = "x" * 7000
        decision = decide_next_action(
            self.config, "Fascicolo completo", 3, 1, response, [verification],
            [" M app.py", "?? nuovo.py"], transport=transport,
        )
        self.assertEqual(decision.esito, "chiama_claude")
        url, payload, timeout = transport.call_args.args
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(timeout, 300)
        self.assertEqual(payload["model"], "qwen3:14b")
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["options"], {"temperature": 0, "num_ctx": 16384})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": self.system_prompt})
        user = json.loads(payload["messages"][1]["content"])
        self.assertEqual(user["fascicolo"], "Fascicolo completo")
        self.assertEqual(user["numero_pezzo"], 3)
        self.assertEqual(user["rimandi_pezzo"], 1)
        self.assertEqual(user["ultima_risposta_codex"], "x" * 6000)
        self.assertEqual(user["file_cambiati"], [" M app.py", "?? nuovo.py"])
        self.assertEqual(user["verifiche"][0]["righe_errore"], ["2: AssertionError: città"])
        self.assertEqual(user["verifiche"][0]["exit_code"], 1)
        self.assertNotIn("OUTPUT INTEGRALE", payload["messages"][1]["content"])

    def test_configured_context_timeout_and_model_are_used(self):
        transport = Mock(return_value=envelope())
        config = replace(self.config, num_ctx_modello=32768, timeout_modello_s=15, nome_modello="finto")
        self.decide(transport=transport, config=config)
        _, payload, timeout = transport.call_args.args
        self.assertEqual(payload["options"]["num_ctx"], 32768)
        self.assertEqual(payload["model"], "finto")
        self.assertEqual(timeout, 15)

    def test_referral_below_threshold_keeps_exact_message(self):
        decision = self.decide(
            transport=Mock(return_value=envelope(content("rimanda_a_codex", "Correggi app.py: errore preciso"))),
            referrals=1,
        )
        self.assertEqual(decision.esito, "rimanda_a_codex")
        self.assertEqual(decision.messaggio, "Correggi app.py: errore preciso")
        self.assertFalse(decision.forced)

    def test_referral_at_or_above_configured_threshold_is_forced_to_claude(self):
        for threshold, referrals in ((2, 2), (2, 3), (4, 4), (0, 0)):
            with self.subTest(threshold=threshold, referrals=referrals):
                config = replace(self.config, rimandi_max_per_pezzo=threshold)
                decision = self.decide(
                    transport=Mock(return_value=envelope(content("rimanda_a_codex", "Riprova"))),
                    config=config, referrals=referrals,
                )
                self.assertEqual(decision.esito, "chiama_claude")
                self.assertEqual(decision.messaggio, "")
                self.assertTrue(decision.forced)
                self.assertIn("Limite rimandi", decision.motivo)

    def test_model_limit_and_claude_decision_are_not_overridden_at_threshold(self):
        for outcome in ("limite_raggiunto", "chiama_claude"):
            with self.subTest(outcome=outcome):
                decision = self.decide(transport=Mock(return_value=envelope(content(outcome))), referrals=10)
                self.assertEqual(decision.esito, outcome)
                self.assertFalse(decision.forced)

    def test_invalid_json_or_unknown_decision_falls_back_without_retry(self):
        for response in ("not json", content("esito sconosciuto"), "{}"):
            with self.subTest(response=response):
                transport = Mock(return_value=envelope(response))
                decision = self.decide(transport=transport)
                self.assertEqual(decision.esito, "chiama_claude")
                self.assertTrue(decision.forced)
                self.assertEqual(decision.raw_response, response)
                transport.assert_called_once()

    def test_http_error_connection_failure_and_timeout_fall_back(self):
        errors = (
            HTTPError("http://finto", 503, "Non disponibile", {}, None),
            URLError("Connessione rifiutata"), TimeoutError("Timeout modello"),
        )
        for error in errors:
            with self.subTest(error=error):
                transport = Mock(side_effect=error)
                decision = self.decide(transport=transport)
                self.assertEqual(decision.esito, "chiama_claude")
                self.assertEqual(decision.messaggio, "")
                self.assertTrue(decision.forced)
                transport.assert_called_once()

    def test_invalid_ollama_envelope_falls_back(self):
        for response in (None, [], {}, {"error": "modello assente"},
                         {"message": None}, {"message": {"content": 123}}):
            with self.subTest(response=response):
                decision = self.decide(transport=Mock(return_value=response))
                self.assertEqual(decision.esito, "chiama_claude")
                self.assertTrue(decision.forced)

    def test_missing_system_prompt_falls_back_without_network(self):
        (self.base / "regole" / "prompt-modello.md").unlink()
        transport = Mock()
        decision = self.decide(transport=transport)
        self.assertEqual(decision.esito, "chiama_claude")
        transport.assert_not_called()

    def test_injected_system_prompt_and_think_response(self):
        transport = Mock(return_value=envelope("<think>Testo interno\ncon dettagli</think>" + content()))
        decision = self.decide(transport=transport, system_prompt="Sistema iniettato")
        self.assertEqual(decision.esito, "chiama_claude")
        self.assertFalse(decision.forced)
        self.assertEqual(transport.call_args.args[1]["messages"][0]["content"], "Sistema iniettato")


class TransportTests(unittest.TestCase):
    def test_post_json_utf8_and_timeout_using_mocked_urlopen(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(envelope()).encode("utf-8")
        payload = {"model": "finto", "messages": [{"role": "user", "content": "città"}]}
        with patch("orch.model.urlopen", return_value=response) as urlopen:
            result = ollama_transport("http://127.0.0.1:11434/api/chat", payload, 300)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)
        self.assertIn("application/json", request.get_header("Content-type"))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 300)
        self.assertEqual(result, envelope())

    def test_invalid_http_body_raises_for_caller_to_fallback(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        with patch("orch.model.urlopen", return_value=response):
            with self.assertRaises(ValueError):
                ollama_transport("http://finto/api/chat", {}, 1)


if __name__ == "__main__":
    unittest.main()
