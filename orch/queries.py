"""Inventario e presentazione delle query: nessuna esecuzione SQL."""

from pathlib import Path, PurePosixPath
import re


DISCARDED_QUERIES = "nessuna (task scartata: eventuali SQL sono nello stash, NON eseguirli)"


def query_sort_key(name):
    basename = PurePosixPath(name).name
    prefix = re.match(r"^(\d+)(?:_|\b)", basename)
    return (0, int(prefix.group(1)), name) if prefix else (1, 0, name)


def extract_query_notes(response: str) -> str:
    marker = re.search(r"QUERY DA ESEGUIRE SU SUPABASE:\s*", response, re.IGNORECASE)
    if marker is None:
        return ""
    tail = response[marker.end():]
    # NOTE è l'ultimo campo del blocco di risposta; la chiusura del fence
    # e gli eventuali campi successivi non appartengono alle note.
    return re.split(r"(?m)^\s*(?:`{3,}|~{3,}|STATO:|FILE MODIFICATI:|VERIFICHE ESEGUITE:)",
                    tail, maxsplit=1)[0].strip()


def query_task_lines(task: dict) -> list[str]:
    if task.get("esito") == "scartata":
        lines = [DISCARDED_QUERIES]
    else:
        files = sorted(task.get("query_da_eseguire", []), key=query_sort_key)
        lines = [f"- {name}" for name in files] or ["nessuna"]
    notes = task.get("note_query_codex", "")
    lines.extend(["", "Note di Codex (da controllare):", "", notes or "nessuna"])
    return lines


def script_query_section(tasks: list[dict]) -> str:
    lines = ["## Query da eseguire su Supabase (elenco dello script)", ""]
    entries = []
    for task in tasks:
        if task.get("esito") != "scartata":
            for name in task.get("query_da_eseguire", []):
                full_path = str(Path(task["progetto"]) / name)
                entries.append((name, full_path, task["id"]))
    for number, (name, full_path, identity) in enumerate(
            sorted(entries, key=lambda item: query_sort_key(item[0])), 1):
        lines.append(f"{number}. {full_path} — task {identity}")
    if not entries:
        lines.append("nessuna")
    for task in tasks:
        lines.extend(["", f"### Task {task['id']}: {task['titolo']}", "", *query_task_lines(task)])
    return "\n".join(lines)
