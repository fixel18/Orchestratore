"""Trasporto Ollama iniettabile: stesso envelope JSON, nessuna socket."""

import json

from . import scenario


def transport(url, payload, timeout):
    if url != "http://127.0.0.1:1/api/chat" or timeout <= 0:
        raise ValueError("Configurazione del trasporto finto errata")
    expected = {"stream": False, "format": "json", "think": False,
                "options": {"temperature": 0, "num_ctx": 16384}}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("Payload Ollama non conforme")
    if payload["messages"][0]["role"] != "system" or not payload["messages"][0]["content"]:
        raise ValueError("Prompt di sistema mancante")
    request = json.loads(payload["messages"][1]["content"])
    identity = scenario.task_id(request["fascicolo"])
    scenario.record("modello", identity, payload=payload)
    failed = any(not result["ok"] for result in request["verifiche"])
    decision = {"esito": "rimanda_a_codex" if failed else "chiama_claude",
                "messaggio": "Correggi app.txt: la verifica richiede pronto." if failed else "",
                "motivo": "Verifica fallita" if failed else "Richiesta revisione"}
    return {"message": {"content": "<think>simulazione locale</think>\n" + json.dumps(decision)}}
