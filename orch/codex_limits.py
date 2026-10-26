"""Riconoscimento dei limiti Codex e calcolo dello sblocco, senza rete o attese."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re


LIMIT_CODES = frozenset({"usage_limit_reached", "rate_limit_reached", "quota_exceeded"})
_LIMIT_TEXT = re.compile(
    r"you['’]ve hit your usage limit\b|\b(?:usage_limit_reached|rate_limit_reached|quota_exceeded)\b"
    r"|\bweekly\s+(?:usage\s+)?limit\b|\blimite\s+settimanale\b",
    re.IGNORECASE,
)
_RESET = re.compile(
    r"try\s+again\s+at\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?:[ \t]*(?P<ampm>[ap]m))?"
    r"(?:\s+on\s+(?P<day>\d{1,2})\s+(?P<month>[a-z]{3})"
    r"(?:\s+(?P<year>\d{4}))?)?",
    re.IGNORECASE,
)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_DATE_FIRST = re.compile(
    r"try\s+again\s+at\s+(?P<month>[a-z]{3})\s+(?P<day>\d{1,2})"
    r"(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:[ \t]*(?P<ampm>[ap]m))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexError:
    message: str
    code: str | None = None


@dataclass(frozen=True)
class UsageLimit:
    message: str
    retry_at: datetime | None
    source: str  # rollout, testo oppure sconosciuto
    weekly: bool = False


def is_usage_limit(message: str, code: str | None = None) -> bool:
    return bool((code and code.casefold() in LIMIT_CODES) or _LIMIT_TEXT.search(message))


def reset_from_text(text: str, *, now: datetime | None = None,
                    recent_hours: float = 12) -> datetime | None:
    """Supporta ore 12/24h e date inglesi opzionali 'on 24 Oct [2026]'.

    Senza data un'ora appena trascorsa riprova tra due minuti; una data esplicita viene
    mantenuta anche se passata, permettendo un tentativo immediato. Senza `now`
    usa il fuso locale di Windows alla data dello sblocco (anche attraverso DST).
    """
    local_clock = now is None
    now = now or datetime.now().astimezone()
    match = _DATE_FIRST.search(text) or _RESET.search(text)
    if not match:
        return None
    try:
        hour, minute = int(match["hour"]), int(match["minute"])
        ampm = match["ampm"]
        if ampm:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if ampm.casefold() == "pm" else 0)
        if match["day"]:
            day = int(match["day"])
            month = _MONTHS[match["month"].casefold()]
            year = int(match["year"]) if match["year"] else now.year
            date = datetime(year, month, day).date()
        else:
            date = now.date()

        def at_date(date):
            naive = datetime(date.year, date.month, date.day, hour, minute)
            return naive.astimezone() if local_clock else naive.replace(tzinfo=now.tzinfo)

        result = at_date(date)
        if not match["day"] and result <= now:
            if now.timestamp() - result.timestamp() <= max(6, recent_hours) * 3600:
                return now + timedelta(minutes=2)
            result = at_date(date + timedelta(days=1))
        return result
    except (ValueError, KeyError, OverflowError, OSError):
        return None


def _rollout_snapshot(sessions: str | Path, thread_id: str | None) -> dict | None:
    """Ultima fotografia valida, anche quando manca resets_at."""
    if not thread_id or not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        return None
    try:
        paths = sorted(
            Path(sessions).glob(f"*/*/*/rollout-*-{thread_id}.jsonl"),
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
        )
    except OSError:
        return None
    snapshot = None
    for path in paths:
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload")
                    rates = event.get("rate_limits")
                    if not isinstance(rates, dict) and isinstance(payload, dict):
                        rates = payload.get("rate_limits")
                    if isinstance(rates, dict):
                        snapshot = rates
        except OSError:
            continue
    return snapshot


def reset_from_rollout(
    sessions: str | Path, thread_id: str | None, *, now: datetime | None = None,
) -> datetime | None:
    """Scadenza più lontana esaurita, altrimenti la futura più vicina."""
    now = now or datetime.now().astimezone()
    snapshot = _rollout_snapshot(sessions, thread_id)
    if snapshot is None:
        return None
    exhausted = []
    future = []
    for bucket in snapshot.values():
        if not isinstance(bucket, dict):
            continue
        reset, used = bucket.get("resets_at"), bucket.get("used_percent")
        if type(reset) not in (int, float):
            continue
        try:
            when = datetime.fromtimestamp(reset, timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        valid_used = type(used) is int or (type(used) is float and math.isfinite(used))
        if valid_used and used >= 100:
            exhausted.append(when)
        if when.timestamp() > now.timestamp():
            future.append(when)
    if exhausted:
        return max(exhausted)
    return min(future) if future else None


def osservazione_vuota() -> dict:
    return {"finestra_5h": None, "resets_5h": None,
            "settimana": None, "resets_settimana": None}


def percentuale(value, *, frazione=False):
    maximum = 1 if frazione else 100
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        return None
    return float(value) * (100 if frazione else 1)


def epoch_datetime(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def osservazione_da_rollout(sessions: str | Path, thread_id: str | None) -> dict:
    """Ultima fotografia del thread; primary null resta utilizzo sconosciuto."""
    rates = _rollout_snapshot(sessions, thread_id) or {}
    result = osservazione_vuota()
    for name, percent_key, reset_key in (
        ("primary", "finestra_5h", "resets_5h"),
        ("secondary", "settimana", "resets_settimana"),
    ):
        bucket = rates.get(name)
        if not isinstance(bucket, dict):
            continue
        result[percent_key] = percentuale(bucket.get("used_percent"))
        result[reset_key] = epoch_datetime(bucket.get("resets_at"))
    return result


def is_weekly_limit(text: str, retry_at: datetime | None, *, now: datetime,
                    threshold_hours: float = 12, snapshot: dict | None = None) -> bool:
    if re.search(r"\b(?:weekly|settimanale)\b", text, re.IGNORECASE):
        return True
    for name, bucket in (snapshot or {}).items():
        if not isinstance(bucket, dict):
            continue
        used, window = bucket.get("used_percent"), bucket.get("window_minutes")
        if (type(used) in (int, float) and math.isfinite(used) and used >= 100
                and (name == "secondary" or
                     (type(window) in (int, float) and math.isfinite(window) and window >= 1440))):
            return True
    return bool(retry_at is not None and
                retry_at.timestamp() - now.timestamp() > threshold_hours * 3600)


def detect_usage_limit(
    errors: list[CodexError], text: str, *, sessions: str | Path,
    thread_id: str | None, now: datetime | None = None,
    threshold_hours: float = 12,
) -> UsageLimit | None:
    """Il rollout, quando disponibile, prevale sull'ora ambigua del testo."""
    recognized = [error.message for error in errors if is_usage_limit(error.message, error.code)]
    if is_usage_limit(text):
        recognized.append(text)
    if not recognized:
        return None
    message = "\n".join(recognized)
    now = now or datetime.now().astimezone()
    reset = reset_from_rollout(sessions, thread_id, now=now)
    source = "rollout" if reset is not None else "sconosciuto"
    if reset is None:
        reset = reset_from_text(message, now=now, recent_hours=threshold_hours)
        if reset is not None:
            source = "testo"
    weekly = is_weekly_limit(message, reset, now=now, threshold_hours=threshold_hours,
                             snapshot=_rollout_snapshot(sessions, thread_id))
    return UsageLimit(message, reset, source, weekly)
