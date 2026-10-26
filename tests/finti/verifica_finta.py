"""Verifica eseguita dalla shell reale sul file modificato dal Codex finto."""

import argparse
from pathlib import Path
import py_compile

import scenario


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    _, task, _ = scenario.record("verifica", args.task)
    if task.get("genera_cache"):
        py_compile.compile("calcoli.py", doraise=True)
    actual = Path("app.txt").read_text(encoding="utf-8").strip()
    ok = actual.startswith("correzione-") if task["scenario"] == "bocciata" else actual == "pronto"
    if not ok:
        print(f"AssertionError: app.txt incompleto: {actual}")
        return 1
    print("OK: app.txt verificato")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
