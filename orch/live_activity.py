"""Messaggi pubblici delle CLI e lettura incrementale dello storico della sessione."""

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re

_LOG = re.compile(r"^(?P<task>[A-Za-z0-9_-]+)-(?P<stamp>\d{8}-\d{6})-[a-f0-9]{32}\.(?P<kind>jsonl|response\.json)$")
_CHUNK = 4 * 1024 * 1024
_MAX_LINE = 4 * 1024 * 1024
_NAMES = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity"}


def activity_metadata(path, tool, role):
    """Solo metadati operativi: nessun prompt, argomento di comando o segreto."""
    try:
        Path(path).write_text(json.dumps({"tool": tool, "role": role}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # La visualizzazione non deve impedire il lavoro dell'esecutore.


def clean_text(text):
    if not isinstance(text, str):
        return ""
    text = re.sub(r"<(?:think|analysis)\b[^>]*>.*?(?:</(?:think|analysis)>|$)", "", text, flags=re.I | re.S)
    text = re.sub(r"(?i)\b(Bearer\s+)\S+", r"\1[omesso]", text)
    text = re.sub(r"(?i)\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[=:]\s*)[^\s,;]+", r"\1[omesso]", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32).strip()


def _command_label(command):
    command = str(command).lower()
    if re.search(r"--test\b|\bunittest\b|\bpytest\b|\bnpm(?:\.cmd)?\s+(?:run\s+)?(?:-s\s+)?test", command):
        return "Esecuzione dei test"
    if "typecheck" in command or re.search(r"\btsc\b", command):
        return "Controllo TypeScript"
    if "eslint" in command or re.search(r"\bnpm(?:\.cmd)?\s+(?:run\s+)?(?:-s\s+)?lint", command):
        return "Controllo lint"
    if re.search(r"\bgit\s+(?:diff|status|show|log)\b", command):
        return "Controllo delle modifiche Git"
    if re.search(r"\brg\b|\bget-content\b|\bcat\b|\breadfile", command):
        return "Lettura e ricerca nel codice"
    return "Esecuzione di un comando"


def public_messages(event, tool):
    """Whitelist di messaggi finali/pubblici e stati dei tool; mai ragionamenti o output grezzo."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    result = []
    if tool == "codex":
        item = event.get("item") or {}
        if not isinstance(item, dict):
            return []
        subtype = item.get("type")
        if kind == "item.completed" and subtype == "agent_message":
            result.append(item.get("text"))
        elif kind in {"item.started", "item.completed"} and subtype == "command_execution":
            label = _command_label(item.get("command", ""))
            if kind == "item.completed":
                label += f" — terminato (esito {item.get('exit_code', '?')})"
            result.append(label)
        elif kind == "item.completed" and subtype == "file_change":
            result.append("Modifiche ai file completate")
        elif kind == "turn.completed":
            result.append("Risposta completata")
        elif kind in {"error", "turn.failed"}:
            result.append("Errore durante l'esecuzione; dettagli nel log della chiamata")
    elif tool == "claude":
        if kind == "assistant":
            message = event.get("message") or {}
            content = message.get("content", []) if isinstance(message, dict) else []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        result.append(block.get("text"))
                    elif block.get("type") == "tool_use":
                        name = block.get("name")
                        if name in {"Read", "Grep", "Glob"}:
                            result.append("Lettura e ricerca nel codice")
                        elif name in {"Edit", "Write", "MultiEdit"}:
                            result.append("Modifica dei file")
                        elif name == "Bash":
                            args = block.get("input") or {}
                            result.append(_command_label(args.get("command", "") if isinstance(args, dict) else ""))
                        else:
                            result.append("Operazione in corso")
        elif kind == "result" or (kind is None and "result" in event):
            result.append(event.get("result"))
        # stream_event/thinking_delta, tool results and permission details are excluded.
    return [value for text in result if (value := clean_text(text))]


def session_history(state):
    current = state.get("periodo") or {}
    previous = state.get("ultimo_periodo") or {}
    period = current if current.get("eventi") else previous if previous.get("eventi") else current
    events = period.get("eventi") or state.get("eventi") or []
    unique = []
    seen = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        key = event.get("event_id") or f"legacy-{index}"
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return period, unique


def _date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None


@dataclass
class _Cursor:
    offset: int = 0
    buffer: bytes = b""
    messages: list[str] = field(default_factory=list)
    seen_text: set[str] = field(default_factory=set)
    role: str = ""
    latest_at: str = ""


class ActivityReader:
    def __init__(self):
        self.files = {}
        self.session = None

    def _read(self, path, tool):
        cursor = self.files.setdefault(str(path), _Cursor())
        try:
            with path.open("rb") as stream:
                # Directory metadata can remain stale while a CLI keeps its file open.
                stream.seek(0, 2)
                if stream.tell() < cursor.offset:
                    cursor = self.files[str(path)] = _Cursor()
                stream.seek(cursor.offset)
                data = stream.read(_CHUNK)
                cursor.offset = stream.tell()
        except OSError:
            return cursor
        if not data:
            return cursor
        buffer = cursor.buffer + data
        lines = buffer.split(b"\n")
        cursor.buffer = lines.pop()
        # Legacy final JSON need not end in a newline; partial JSON waits for more bytes.
        if cursor.buffer:
            try:
                json.loads(cursor.buffer.decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                if len(cursor.buffer) > _MAX_LINE:
                    cursor.buffer = b""
            else:
                lines.append(cursor.buffer)
                cursor.buffer = b""
        for line in lines:
            try:
                event = json.loads(line.decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            if tool == "claude" and event.get("type") == "system" and event.get("subtype") == "init":
                if event.get("permissionMode") in {"acceptEdits", "bypassPermissions"}:
                    cursor.role = "esecutore"
                elif event.get("permissionMode") in {"dontAsk", "plan"}:
                    cursor.role = "controllore"
            for text in public_messages(event, tool):
                # Final result often repeats the last assistant message verbatim.
                is_final = tool == "claude" and event.get("type") in {"result", None}
                if is_final and text in cursor.seen_text:
                    continue
                cursor.seen_text.add(text)
                cursor.messages.append(text)
                cursor.latest_at = datetime.now().astimezone().strftime("%H:%M:%S")
        return cursor

    def read(self, base, state):
        base = Path(base)
        period, events = session_history(state)
        key = (str(base), period.get("inizio"))
        if key != self.session:
            self.files.clear()
            self.session = key
        start = _date(period.get("inizio"))
        end = _date(period.get("fine"))
        labels = {str(task) for task in period.get("task_ids", [])}
        labels.update(str((e.get("task") or {}).get("id", "")) for e in events)
        if state.get("task_corrente"):
            labels.add(str(state["task_corrente"].get("id", "")))
        logs = base / "log"
        groups = []
        activity = ""
        active_task = str((state.get("task_corrente") or {}).get("id", ""))
        active_tool = state.get("azione_esterna")
        active_role = "controllore" if state.get("fase") == "claude" else "esecutore"
        for path in sorted(logs.glob("*")):
            match = _LOG.fullmatch(path.name)
            if not match or match["task"] not in labels or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(logs.resolve()):
                continue
            try:
                when = datetime.strptime(match["stamp"], "%Y%m%d-%H%M%S").astimezone()
            except ValueError:
                continue
            if start and when < start.replace(microsecond=0):
                continue
            if end and when > end:
                continue
            tool = "codex" if match["kind"] == "jsonl" else "claude"
            stem = path.name[:-len("." + match["kind"])]
            metadata = logs / (stem + ".activity.json")
            role = ""
            if metadata.is_file() and not metadata.is_symlink():
                try:
                    meta = json.loads(metadata.read_text(encoding="utf-8"))
                    role = meta.get("role", "") if isinstance(meta, dict) else ""
                except (OSError, ValueError):
                    pass
            cursor = self._read(path, tool)
            role = role if role in {"esecutore", "controllore"} else cursor.role
            title = _NAMES[tool] + (f" ({role})" if role else "")
            # The timestamp identifies the invocation; its messages stay grouped underneath.
            lines = [f"{when:%d/%m %H:%M:%S}  {match['task']:>3}  {title}"]
            lines.extend(f"    [{_NAMES[tool]}] {text}" for text in cursor.messages)
            if cursor.messages:
                groups.append((when.timestamp(), path.name, "\n".join(lines)))
            if (state.get("fase") in {"invio_codex", "claude"} and tool == active_tool
                    and match["task"] == active_task and (not role or role == active_role)):
                detail = cursor.messages[-1].splitlines()[0][:240] if cursor.messages else "Elaborazione in corso"
                activity = f"{_NAMES[tool]} ({active_role}): {detail}"
                if cursor.latest_at:
                    activity += f" · ultimo messaggio ricevuto alle {cursor.latest_at}"
        for index, event in enumerate(events):
            when = _date(event.get("at"))
            task = str((event.get("task") or {}).get("id", ""))
            message = clean_text(event.get("messaggio", ""))
            if message:
                label = when.strftime("%d/%m %H:%M:%S") if when else "?"
                groups.append((when.timestamp() if when else 0, f"event-{index:08}", f"{label}  {task:>3}  {message}"))
        groups.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in groups], activity, period
