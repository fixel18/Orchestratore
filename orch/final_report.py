"""Dati del periodo e report di riserva deterministico, senza azioni esterne."""

from datetime import datetime
import json

from .model import clean_report_text
from .queries import script_query_section


def period_data(period: dict, reason: str, end: datetime) -> dict:
    events = [{"at": event["at"], "tipo": event["tipo"],
               "messaggio": event["messaggio"],
               "task_id": (event.get("task") or {}).get("id")}
              for event in period["eventi"]]
    return json.loads(json.dumps({
        "inizio": period["inizio"], "fine": end.isoformat(), "motivo_chiusura": reason,
        "task": list(period["task"].values()), "eventi": events,
        "budget": period.get("budget", {}),
        "errori_operativi": [event for event in events if "errore" in event["tipo"]],
    }, ensure_ascii=False))


def _cell(value):
    if value is None:
        return "non disponibile"
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def fallback_report(data: dict) -> str:
    lines = ["# Report finale del periodo", "", "Nota: modello non disponibile.", "",
             "## Riepilogo", "", f"Periodo: {data['inizio']} → {data['fine']}.",
             f"Motivo della chiusura: {data['motivo_chiusura']}.", "", "## Task", ""]
    for number, task in enumerate(data["task"], 1):
        lines.append(f"{number}. {_cell(task['id'])} — {_cell(task['titolo'])}: {_cell(task['esito'])}. "
                     f"Pezzi {task.get('pezzi_completati', 0)}/{task.get('pezzi_totali', 0)}; "
                     f"rimandi {task.get('rimandi_totali', 0)}; correzioni {task.get('bocciature', 0)}. "
                     f"Motivo: {_cell(task.get('motivo'))}. "
                     f"Commit: {_cell(task.get('commit'))}; push: {_cell(task.get('push'))}.")
    lines.extend(["", "## Problemi riscontrati e cose da controllare", ""])
    issues = [event for event in data["eventi"]
              if "errore" in event["tipo"] or event["tipo"] in {"attesa_inizio", "task_scartata", "serve login"}]
    lines.extend(f"- {_cell(event['at'])}: {_cell(event['messaggio'])}" for event in issues)
    if not issues:
        lines.append("Nessun problema registrato nei dati disponibili.")
    return "\n".join(lines)


def render_report(data: dict, content: str, shutdown_at: datetime | None) -> str:
    body = clean_report_text(content) if isinstance(content, str) else ""
    lines = [body or fallback_report(data), "", "## Dati grezzi", "",
             f"Inizio: {data['inizio']}  ", f"Fine: {data['fine']}  ",
             f"Motivo della chiusura: {_cell(data['motivo_chiusura'])}", "",
             "| ID | Titolo | Esito | Pezzi | Rimandi | Correzioni | Commit | Push | Motivo |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for task in data["task"]:
        values = [task.get("id"), task.get("titolo"), task.get("esito"),
                  f"{task.get('pezzi_completati', 0)}/{task.get('pezzi_totali', 0)}",
                  task.get("rimandi_totali"), task.get("bocciature"), task.get("commit"),
                  task.get("push"), task.get("motivo")]
        lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines.extend(["", "### Esecutori e controllori", "",
                  "| Task | Esecutori usati | Controllori usati |", "| --- | --- | --- |"])
    for task in data["task"]:
        values = [task.get("id"), ", ".join(task.get("esecutori", [])) or "non disponibile",
                  ", ".join(task.get("controllori", [])) or task.get("controllore") or "non disponibile"]
        lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines.extend(["", "### Budget del periodo", "",
                  "| Esecutore | Consumato | Limite impostato |", "| --- | --- | --- |"])
    for name, entry in data.get("budget", {}).items():
        limit = entry.get("limite")
        values = [name, f"{entry.get('consumato', 0):g}%", f"{limit}%" if limit is not None else "senza limite"]
        lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines.extend(["", "### Attese per limiti", "",
                  "| Task | Strumento | Inizio | Sblocco previsto | Attesa fino a | Fine |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for task in data["task"]:
        for wait in task.get("attese", []):
            values = [task["id"], wait.get("strumento"), wait.get("inizio"),
                      wait.get("sblocco_previsto"), wait.get("fino_a"), wait.get("fine")]
            lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines.extend(["", "### Eventi ed errori operativi", "",
                  "| Ora | Task | Tipo | Dettaglio |", "| --- | --- | --- | --- |"])
    for event in data["eventi"]:
        lines.append("| " + " | ".join(_cell(event.get(key))
                     for key in ("at", "task_id", "tipo", "messaggio")) + " |")
    lines.extend(["", script_query_section(data["task"]), "",
                  (f"Spegnimento: programmato alle {shutdown_at:%H:%M}"
                        if shutdown_at else "Spegnimento: disattivato"), ""])
    return "\n".join(lines)
