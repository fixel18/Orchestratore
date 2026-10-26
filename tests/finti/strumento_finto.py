"""CLI autonoma per test adattatori: nessun servizio, git o rete."""

import json
import os
from pathlib import Path
import sys


SESSION = "11111111-2222-3333-4444-555555555555"
RESET = 1792760400


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    tool, case, receipt = sys.argv[1:4]
    args = sys.argv[4:]
    prompt = sys.stdin.buffer.read().decode("utf-8")
    Path(receipt).write_text(json.dumps({"argv": args, "prompt": prompt, "cwd": str(Path.cwd()),
        "chiavi_presenti": [name for name in os.environ if name.upper() in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")]},
        ensure_ascii=False), encoding="utf-8")
    if tool == "claude":
        if case != "no-session":
            emit({"type": "system", "subtype": "init", "session_id": SESSION})
        if case in ("success", "warning", "limit", "weekly", "unknown", "correzioni"):
            emit({"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected" if case in ("limit", "weekly") else "allowed_warning" if case == "warning" else "allowed",
                "resetsAt": RESET, "rateLimitType": "seven_day" if case == "weekly" else "five_hour",
                "utilization": None if case == "unknown" else 0.47,
                "unifiedWindows": {"five_hour": {"utilization": None if case == "unknown" else 0.47, "resetsAt": RESET},
                                   "seven_day": {"utilization": 0.95, "resetsAt": RESET + 86400}}}})
        if case == "malformed":
            print("{non-json", flush=True)
        if case == "no-result":
            return 0
        if case == "auth":
            text, error = "OAuth expired: please login", True
        elif case == "text-limit":
            text, error = f"Claude AI usage limit reached|{RESET}", True
        elif case == "error":
            text, error = "Errore operativo temporaneo", True
        else:
            readonly = "--tools" in args
            text = ("ESITO: CORREZIONI\nCorreggi app.txt" if case == "correzioni" else "ESITO: OK\nVerificato") if readonly else "STATO: FINITO\ncaffè"
            error = False
            if not readonly and case == "success":
                Path("eseguito.txt").write_text("caffè", encoding="utf-8")
        emit({"type": "result", "result": text, "is_error": error,
              **({"session_id": SESSION} if case != "no-session" else {})})
        return 1 if error or case in ("limit", "weekly") else 0
    if tool != "codex":
        raise ValueError("strumento sconosciuto")
    thread = args[-2] if "resume" in args else SESSION
    emit({"type": "thread.started", "thread_id": thread})
    if case == "limit":
        emit({"type": "turn.failed", "error": {"code": "usage_limit_reached", "message": "You've hit your usage limit"}})
        return 1
    if case == "auth":
        emit({"type": "error", "message": "OAuth expired: please login"})
        return 1
    readonly = "-s" in args and args[args.index("-s") + 1] == "read-only"
    if readonly:
        text = "ESITO: CORREZIONI\nCorreggi app.txt" if case == "correzioni" else "ESITO: OK\nVerificato"
    else:
        text = "STATO: FINITO\ncaffè"
        Path("eseguito.txt").write_text("caffè", encoding="utf-8")
    Path(args[args.index("-o") + 1]).write_text(text, encoding="utf-8")
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    emit({"type": "turn.completed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
