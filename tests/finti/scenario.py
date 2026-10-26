"""Scenario condiviso dai processi finti; nessun accesso a servizi esterni."""

import json
import os
from pathlib import Path
import re
import tempfile


SCENARIO_ENV = "ORCH_TEST_SCENARIO"


def load():
    path = Path(os.environ[SCENARIO_ENV]).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("solo_test") is not True:
        raise ValueError("Scenario non abilitato ai test")
    project = Path(data["progetto"]).resolve()
    base = Path(data["cartella_base"]).resolve()
    if not project.is_relative_to(path.parent) or not base.is_relative_to(path.parent):
        raise ValueError("Lo scenario deve restare nella cartella temporanea")
    return path, data


def save(path, data):
    handle, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def task_id(prompt):
    match = re.search(r"^\s*id\s*:\s*['\"]?(\d+)", prompt, re.MULTILINE)
    if not match:
        raise ValueError("Fascicolo senza id nel prompt")
    return match[1]


def record(actor, identity, **details):
    # Gli attori sono chiamati in sequenza dal ciclo, non in parallelo.
    path, data = load()
    task = data["tasks"][identity]
    counts = task.setdefault("chiamate", {})
    counts[actor] = counts.get(actor, 0) + 1
    data.setdefault("storia", []).append({"attore": actor, "id": identity,
                                          "numero": counts[actor], **details})
    save(path, data)
    return data, task, counts[actor]


def check_child(data):
    if Path.cwd().resolve() != Path(data["progetto"]).resolve():
        raise ValueError("cwd del figlio errata")
    if any(key.upper() in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"} for key in os.environ):
        raise ValueError("Ambiente del figlio non ripulito")
