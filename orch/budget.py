"""Contabilità del periodo: differenze nella finestra, somma sulle nuove finestre."""

from .codex_limits import percentuale


def aggiorna_budget(entry, osservazione, *, controllore=False):
    """Muta una voce persistibile; restituisce True per un nuovo utilizzo ignoto.

    Le osservazioni del controllore spostano soltanto la base: il suo consumo
    non deve essere addebitato al successivo invio allo stesso strumento.
    """
    entry.setdefault("consumato", 0.0)
    entry.setdefault("ultima", None)
    value = percentuale(osservazione.get("finestra_5h"))
    reset = osservazione.get("resets_5h")
    reset = reset.isoformat() if hasattr(reset, "isoformat") else reset
    if value is None:
        if controllore:
            # Senza una base dopo la revisione non si può attribuire il delta.
            entry["ultima"] = None
            return False
        key = reset or "sconosciuta"
        reported = entry.setdefault("finestre_non_leggibili", [])
        if key not in reported:
            reported.append(key)
            return True
        return False
    previous = entry["ultima"]
    if previous is not None and not controllore:
        entry["consumato"] += (max(0.0, value - previous["percento"])
                               if previous["resets"] == reset else value)
    entry["ultima"] = {"percento": value, "resets": reset}
    return False
