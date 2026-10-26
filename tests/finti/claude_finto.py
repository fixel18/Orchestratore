"""CLI Claude finta: verifica gli argomenti e produce il JSON documentato."""

import argparse
import json
import sys

import scenario


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", action="store_true", required=True)
    parser.add_argument("--tools", choices=["Read,Grep,Glob"], required=True)
    parser.add_argument("--permission-mode", choices=["dontAsk"], required=True)
    parser.add_argument("--output-format", choices=["stream-json"], required=True)
    parser.add_argument("--verbose", action="store_true", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--no-session-persistence", action="store_true", required=True)
    parser.parse_args()
    prompt = sys.stdin.buffer.read().decode("utf-8")
    _, data = scenario.load()
    scenario.check_child(data)
    identity = scenario.task_id(prompt)
    _, task, _ = scenario.record("claude", identity, argv=sys.argv[1:], prompt=prompt)
    if task["scenario"] == "bocciata":
        result = "ESITO: CORREZIONI\n1. app.txt:1: completa il requisito richiesto."
    else:
        result = "ESITO: OK\nVerifiche e diff approvati: caffè."
    print(json.dumps({"type": "result", "result": result, "is_error": False}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
