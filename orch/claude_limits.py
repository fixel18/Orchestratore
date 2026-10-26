"""Limiti Claude: epoch, ore locali e fusi IANA, senza rete né attese."""

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_AUTH = re.compile(r"oauth|\bauthenticat\w*|\blog(?:in|\s+in)\b|\bnot\s+logged\s+in\b|\bunauthorized\b", re.IGNORECASE)
_LIMIT = re.compile(
    r"\busage\s+limit\b|\bhit\s+your\s+limit\b|\blimit\s+reached\b|\bresets(?:\s+at)?\s+\d",
    re.IGNORECASE,
)
_EPOCH = re.compile(r"Claude\s+AI\s+usage\s+limit\s+reached\s*\|\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_CLOCK = re.compile(
    r"\bresets(?:\s+at)?\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
    r"(?:[ \t]*(?P<ampm>[ap]m))?"
    r"(?![\w:])"
    r"(?:[ \t]*\((?P<zone>[^)]+)\))?",
    re.IGNORECASE,
)


def is_authentication_error(text: str) -> bool:
    return bool(_AUTH.search(text))


def is_claude_limit(text: str, *, is_error: bool = False) -> bool:
    return is_error or bool(_LIMIT.search(text))


def is_weekly_limit(text: str, retry_at: datetime | None, *, now: datetime,
                    threshold_hours: float = 12) -> bool:
    return bool(re.search(r"\b(?:weekly|week|settimanale)\b", text, re.IGNORECASE)
                or (retry_at is not None and
                    retry_at.timestamp() - now.timestamp() > threshold_hours * 3600))


def claude_reset_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """Epoch autorevole (anche passato), altrimenti prossima ora futura.

    Senza fuso esplicito usa il fuso locale del sistema alla data del reset.
    Un fuso IANA non disponibile su Windows restituisce None: il ciclo userà
    l'attesa configurata, senza interpretare un orario in un fuso sbagliato.
    """
    epoch = _EPOCH.search(text)
    if epoch:
        try:
            return datetime.fromtimestamp(float(epoch[1]), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    match = _CLOCK.search(text)
    if not match:
        return None
    local_clock = now is None and not match["zone"]
    now = now or datetime.now().astimezone()
    try:
        hour = int(match["hour"])
        minute = int(match["minute"] or 0)
        if match["ampm"]:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if match["ampm"].casefold() == "pm" else 0)
        zone = ZoneInfo(match["zone"].strip()) if match["zone"] else now.tzinfo
        clock_now = now.astimezone(zone) if zone is not None else now
        for days in range(3):
            date = clock_now.date() + timedelta(days=days)
            naive = datetime(date.year, date.month, date.day, hour, minute)
            candidates = []
            for fold in (0, 1):
                candidate = (naive.replace(fold=fold).astimezone() if local_clock
                             else naive.replace(tzinfo=zone, fold=fold))
                # Evita ore inesistenti al cambio DST, distingue le due occorrenze.
                utc = candidate.astimezone(timezone.utc)
                roundtrip = utc.astimezone() if local_clock else utc.astimezone(zone)
                if (roundtrip.replace(tzinfo=None, fold=0) == naive
                        and candidate.timestamp() > now.timestamp()):
                    candidates.append(candidate)
            if candidates:
                return min(candidates, key=lambda value: value.timestamp())
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        return None
    return None
