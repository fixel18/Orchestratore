"""Parsing degli eventi Claude, condiviso da esecutore e controllore."""

from dataclasses import dataclass, field
import json
import re

from .codex_limits import UsageLimit, epoch_datetime, osservazione_vuota, percentuale


@dataclass
class ClaudeOutput:
    session_id: str | None = None
    text: str = ""
    is_error: bool = False
    result_received: bool = False
    errors: list[str] = field(default_factory=list)
    usage_limit: UsageLimit | None = None
    usage: dict = field(default_factory=osservazione_vuota)


class ClaudeJSONLParser:
    def __init__(self, *, session_id=None, on_session=None):
        self.output = ClaudeOutput(session_id=session_id)
        self.on_session = on_session

    def feed(self, line):
        if not line.strip():
            return
        try:
            event = json.loads(line.lstrip("\ufeff"))
        except ValueError as exc:
            self.output.errors.append(f"JSONL Claude non valido: {exc}")
            return
        if not isinstance(event, dict):
            self.output.errors.append("Evento Claude non è un oggetto JSON")
            return
        session = event.get("session_id")
        if session is not None:
            if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
                self.output.errors.append("session_id Claude non valido")
            elif self.output.session_id and session != self.output.session_id:
                self.output.errors.append("Claude ha cambiato session_id durante la ripresa")
            elif self.output.session_id is None:
                self.output.session_id = session
                if self.on_session:
                    self.on_session(session)
        if event.get("type") == "rate_limit_event":
            self._rate(event.get("rate_limit_info"))
        elif event.get("type") == "result":
            if not isinstance(event.get("result"), str) or type(event.get("is_error")) is not bool:
                self.output.errors.append("Risultato Claude senza result stringa e is_error booleano")
                return
            self.output.result_received = True
            self.output.text = event["result"]
            self.output.is_error = event["is_error"]

    def _rate(self, info):
        if not isinstance(info, dict):
            return
        windows = info.get("unifiedWindows")
        windows = windows if isinstance(windows, dict) else {}
        for name, key, reset_key in (("five_hour", "finestra_5h", "resets_5h"),
                                      ("seven_day", "settimana", "resets_settimana")):
            bucket = windows.get(name)
            if name not in windows and info.get("rateLimitType") == name:
                bucket = info
            if name in windows or info.get("rateLimitType") == name:
                bucket = bucket if isinstance(bucket, dict) else {}
                self.output.usage[key] = percentuale(bucket.get("utilization"), frazione=True)
                self.output.usage[reset_key] = epoch_datetime(bucket.get("resetsAt"))
        if info.get("status") == "rejected":
            weekly = info.get("rateLimitType") == "seven_day"
            reset = epoch_datetime(info.get("resetsAt"))
            reset = reset or self.output.usage["resets_settimana" if weekly else "resets_5h"]
            limit = UsageLimit("Claude: limite " + ("settimanale" if weekly else "5 ore") + " raggiunto",
                               reset, "rate_limit_event", weekly)
            previous = self.output.usage_limit
            # Un rifiuto settimanale non viene nascosto da un successivo evento 5h.
            if previous is None or (weekly and not previous.weekly) or (
                    weekly == previous.weekly and reset is not None
                    and (previous.retry_at is None or reset > previous.retry_at)):
                self.output.usage_limit = limit


def parse_claude_stream(raw, *, session_id=None, on_session=None):
    parser = ClaudeJSONLParser(session_id=session_id, on_session=on_session)
    for line in raw.splitlines():
        parser.feed(line)
    return parser.output
