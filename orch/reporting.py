"""Eventi persistenti, report giornaliero rigenerabile e log completi."""

from datetime import datetime
import json
import os
from pathlib import Path
import re

from .persistence import atomic_write
from .queries import query_task_lines


def safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:80] or "task"


class TaskLogger:
    def __init__(self, base: Path):
        self.folder = base / "log"

    def write(self, at: str, message: str, *, task_id: str | None = None, day: str | None = None):
        self.folder.mkdir(parents=True, exist_ok=True)
        paths = [self.folder / "orchestratore.log"]
        if task_id is not None:
            paths.append(self.folder / f"{safe_label(task_id)}-{day or at[:10]}.log")
        for path in paths:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n[{at}] {message}\n")


class ReportStore:
    def __init__(self, base: Path):
        self.folder = base / "report"

    def events(self, day: str) -> list[dict]:
        path = self.folder / f"eventi-{day}.jsonl"
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue  # Una coda troncata da un arresto non perde gli eventi precedenti.
            if isinstance(item, dict) and "event_id" in item:
                result.append(item)
        return result

    def record(self, event: dict):
        day = event["at"][:10]
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"eventi-{day}.jsonl"
        if not any(item["event_id"] == event["event_id"] for item in self.events(day)):
            with path.open("ab+") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell():
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        stream.seek(0)
                        data = stream.read()
                        tail_start = data.rfind(b"\n") + 1
                        try:
                            json.loads(data[tail_start:].decode("utf-8"))
                        except (ValueError, UnicodeError):
                            # Rimuove solo il frammento incompleto; l'evento
                            # completo è già nel write-ahead di stato.json.
                            stream.truncate(tail_start)
                        else:
                            stream.write(b"\n")
                stream.seek(0, os.SEEK_END)
                stream.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        self.regenerate(day)

    def regenerate(self, day: str | None = None) -> Path:
        day = day or datetime.now().astimezone().date().isoformat()
        datetime.strptime(day, "%Y-%m-%d")
        groups = {}
        general = []
        for event in self.events(day):
            if event.get("run_id") and event.get("task"):
                groups.setdefault(event["run_id"], []).append(event)
            else:
                general.append(event)
        lines = [f"# Report orchestratore — {day}", ""]
        for number, events in enumerate(groups.values(), 1):
            task = events[-1]["task"]
            lines.extend([
                f"## {number}. {task['id']}: {task['titolo']}", "",
                f"- Esito: {task['esito']}",
                f"- Pezzi completati: {task['pezzi_completati']}/{task['pezzi_totali']}",
                f"- Rimandi: {task['rimandi_totali']}; correzioni del controllore: {task['bocciature']}",
                f"- Esecutori: {', '.join(task.get('esecutori', [])) or 'non disponibile'}",
                f"- Controllori: {', '.join(task.get('controllori', [])) or task.get('controllore') or 'non disponibile'}",
                f"- Inizio: {task['inizio']}; fine: {task.get('fine') or '—'}",
                f"- Commit: {task.get('commit') or '—'}",
                f"- Push: {task.get('push') or 'non eseguito'}",
            ])
            if task.get("motivo"):
                lines.append(f"- Motivo: {task['motivo']}")
            if task.get("stash"):
                lines.append(f"- Stash: {task['stash']}")
            for wait in task.get("attese", []):
                lines.append(f"- Attesa limite {wait['strumento']}: {wait['inizio']} → {wait.get('fine') or wait['fino_a']}")
            lines.extend(["", "### Query da eseguire su Supabase", "", *query_task_lines(task)])
            lines.extend(["", "Eventi:", ""])
            lines.extend(f"- {e['at']}: {e['tipo']} — {e['messaggio']}" for e in events)
            lines.append("")
        if general:
            lines.extend(["## Eventi generali", ""])
            lines.extend(f"- {e['at']}: {e['tipo']} — {e['messaggio']}" for e in general)
        if not groups and not general:
            lines.append("Nessuna task registrata.")
        path = self.folder / f"{day}.md"
        atomic_write(path, ("\n".join(lines) + "\n").encode("utf-8"))
        return path
