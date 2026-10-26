"""Simula exec/resume e modifica soltanto il repository dello scenario."""

import argparse
import json
from pathlib import Path
import sys
import time

import scenario


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args or args.pop(0) != "exec":
        raise ValueError("Atteso exec")
    resume = bool(args and args[0] == "resume")
    if resume:
        args.pop(0)
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("-m", required=True)
    parser.add_argument("-c", action="append", default=[])
    parser.add_argument("-s")
    parser.add_argument("-C")
    parser.add_argument("-o", required=True)
    parser.add_argument("positionals", nargs="+")
    options = parser.parse_args(args)
    if options.positionals[-1] != "-" or len(options.positionals) != (2 if resume else 1):
        raise ValueError("Prompt consentito solo su stdin con argomento -")
    prompt = sys.stdin.buffer.read().decode("utf-8")
    _, data = scenario.load()
    scenario.check_child(data)
    if options.m != "gpt-6.1-sol" or 'model_reasoning_effort="medium"' not in options.c:
        raise ValueError("Modello o ragionamento Codex non conformi")
    if resume:
        thread = options.positionals[0]
        identity = next(key for key, task in data["tasks"].items() if task["thread_id"] == thread)
        if options.C is not None or options.s is not None or 'sandbox_mode="workspace-write"' not in options.c:
            raise ValueError("Argomenti resume errati")
        if not data["tasks"][identity].get("chiamate", {}).get("codex"):
            raise ValueError("Resume senza conversazione precedente")
    else:
        identity = scenario.task_id(prompt)
        thread = data["tasks"][identity]["thread_id"]
        if options.s != "workspace-write" or Path(options.C).resolve() != Path.cwd().resolve():
            raise ValueError("Argomenti exec errati")
        if data["tasks"][identity].get("chiamate", {}).get("codex"):
            raise ValueError("Conversazione riavviata con exec anziche resume")
    data, task, number = scenario.record(
        "codex", identity, resume=resume, thread_id=thread, argv=sys.argv[1:], prompt=prompt)
    emit({"type": "thread.started", "thread_id": thread})
    if task["scenario"] == "rimando_limite" and number == 2:
        message = "You've hit your usage limit. Please try again at 01:00 on 1 Jan 2020."
        emit({"type": "error", "code": "usage_limit_reached", "message": message})
        emit({"type": "turn.failed", "error": {"code": "usage_limit_reached", "message": message}})
        return 1
    if task["scenario"] == "ripresa" and number == 1:
        # Il padre salva l'id nel callback. Solo dopo quel salvataggio creiamo STOP.
        state = Path(data["cartella_base"]) / "stato.json"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                if json.loads(state.read_text(encoding="utf-8"))["thread_id"] == thread:
                    (state.parent / "STOP").touch()
                    # Il test interrompe il padre nel callback e verifica il kill del figlio.
                    time.sleep(8)
                    return 1
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.01)
        raise RuntimeError("thread_id non salvato dal padre")
    if task["scenario"] == "bocciata":
        content = f"correzione-{number}\n"
        Path("non-tracciato.txt").write_text("Lavoro da conservare nello stash\n", encoding="utf-8")
    elif task["scenario"] == "rimando_limite" and number == 1:
        content = "incompleto\n"
    else:
        content = "pronto\n"
    Path("app.txt").write_text(content, encoding="utf-8")
    if task.get("file_extra"):
        Path(task["file_extra"]).write_text("Lavoro della seconda task\n", encoding="utf-8")
    message = "Pezzo completato: caffè.\nSTATO: FINITO"
    Path(options.o).write_text(message, encoding="utf-8")
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": message}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
