"""Fasce orarie dell'avvio automatico: funzioni pure, anche a cavallo della mezzanotte."""

from datetime import datetime, time, timedelta


def _ora(testo):
    ore, minuti = testo.split(":")
    return time(int(ore), int(minuti))


def in_fascia(adesso: datetime, fasce) -> bool:
    """True se `adesso` cade in almeno una fascia [da, a) dell'elenco."""
    orario = adesso.time().replace(second=0, microsecond=0)
    for fascia in fasce:
        da, a = _ora(fascia["da"]), _ora(fascia["a"])
        if da < a and da <= orario < a:
            return True
        if da > a and (orario >= da or orario < a):  # es. 22:00 → 07:00
            return True
    return False


def prossimo_inizio(adesso: datetime, fasce) -> datetime | None:
    """Primo istante futuro in cui inizia una fascia (None se l'elenco è vuoto)."""
    candidati = []
    for fascia in fasce:
        da = _ora(fascia["da"])
        inizio = adesso.replace(hour=da.hour, minute=da.minute, second=0, microsecond=0)
        if inizio <= adesso:
            inizio += timedelta(days=1)
        candidati.append(inizio)
    return min(candidati) if candidati else None
