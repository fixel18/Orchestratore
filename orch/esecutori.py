"""Un solo invio a un esecutore. Scelta, budget e attese spettano al ciclo."""

from dataclasses import dataclass, field, replace

from . import antigravity, claude, codex
from .claude_limits import is_authentication_error
from .codex_limits import UsageLimit, osservazione_da_rollout, osservazione_vuota


@dataclass
class RisultatoEsecutore:
    conversazione: str | None = None
    ultimo_messaggio: str = ""
    errori: list[str] = field(default_factory=list)
    limite: UsageLimit | None = None
    osservazione_utilizzo: dict = field(default_factory=osservazione_vuota)
    errore_avvio: str | None = None
    non_configurato: bool = False
    errore_autenticazione: bool = False
    timed_out: bool = False
    returncode: int | None = None
    stderr: str = ""


def utilizzo_antigravity(impostazioni, run_agy=None, leggi_utilizzo=None):
    """Percentuali da `agy /usage` (non consuma quota). Con un agy finto e nessun lettore: vuota."""
    if leggi_utilizzo is None and run_agy is not None:
        return osservazione_vuota()
    return (leggi_utilizzo or antigravity.utilizzo)((impostazioni or {}).get("modello", ""))


def limite_antigravity(messaggio, osservazione):
    """Sblocco dalla finestra esaurita: la settimanale prevale sulla finestra da 5 ore."""
    if (osservazione.get("settimana") or 0) >= 99.5 and osservazione.get("resets_settimana"):
        return UsageLimit(messaggio, osservazione["resets_settimana"], "usage", weekly=True)
    if (osservazione.get("finestra_5h") or 0) >= 99.5 and osservazione.get("resets_5h"):
        return UsageLimit(messaggio, osservazione["resets_5h"], "usage")
    return UsageLimit(messaggio, None, "sconosciuto")


def _antigravity(config, impostazioni_esecutore, progetto, prompt, conversazione=None, *,
                 on_conversazione=None, log_label=None, now=None, run_agy=None, leggi_utilizzo=None):
    """CLI `agy` in modalità accept-edits: modifica i file, non esegue comandi."""
    r = (run_agy or antigravity.esegui)(config, progetto, prompt, impostazioni_esecutore,
                                        conversazione=conversazione, log_label=log_label or "antigravity")
    if r.conversazione and r.conversazione != conversazione and on_conversazione:
        on_conversazione(r.conversazione)
    osservazione = (utilizzo_antigravity(impostazioni_esecutore, run_agy, leggi_utilizzo)
                    if not r.errore_avvio else osservazione_vuota())
    limite = limite_antigravity("\n".join(r.errori) or "Limite Antigravity", osservazione) if r.limite else None
    return RisultatoEsecutore(
        conversazione=r.conversazione, ultimo_messaggio=r.risposta, errori=r.errori, limite=limite,
        osservazione_utilizzo=osservazione,
        errore_avvio=r.errore_avvio, errore_autenticazione=r.autenticazione, timed_out=r.timed_out,
        returncode=r.returncode, stderr=r.stderr)


def invia(nome, config, impostazioni_esecutore, progetto, prompt, conversazione=None, *,
          on_conversazione=None, log_label=None, now=None, run_codex=None, run_claude=None, run_agy=None,
          leggi_utilizzo=None):
    if nome == "antigravity":
        return _antigravity(config, impostazioni_esecutore, progetto, prompt, conversazione,
                            on_conversazione=on_conversazione, log_label=log_label, now=now, run_agy=run_agy,
                            leggi_utilizzo=leggi_utilizzo)
    if nome == "codex":
        selected = replace(config, modello_codex=impostazioni_esecutore["modello"],
                           ragionamento_codex=impostazioni_esecutore["ragionamento"])
        result = (run_codex or codex.run_codex)(selected, progetto, prompt, thread_id=conversazione,
                                 on_thread=on_conversazione, log_label=log_label or nome, now=now)
        errors = [error.message for error in result.output.errors]
        return RisultatoEsecutore(
            conversazione=result.output.thread_id, ultimo_messaggio=result.output.last_message,
            errori=errors, limite=result.usage_limit,
            osservazione_utilizzo=osservazione_da_rollout(config.cartella_sessioni_codex, result.output.thread_id),
            errore_avvio=result.launch_error,
            errore_autenticazione=is_authentication_error("\n".join(errors) + "\n" + result.stderr),
            timed_out=result.timed_out, returncode=result.returncode, stderr=result.stderr)
    if nome == "claude":
        result = (run_claude or claude.run_claude)(config, progetto, prompt, impostazioni=impostazioni_esecutore,
                                   esecutore=True, conversazione=conversazione, on_session=on_conversazione,
                                   log_label=log_label or nome, now=now)
        errors = list(result.errors)
        if result.review.kind in ("errore", "errore_autenticazione") and result.review.error:
            if result.review.error not in errors:
                errors.append(result.review.error)
        return RisultatoEsecutore(
            conversazione=result.session_id, ultimo_messaggio=result.last_message, errori=errors,
            limite=result.usage_limit, osservazione_utilizzo=result.osservazione_utilizzo,
            errore_avvio=result.launch_error,
            errore_autenticazione=result.review.kind == "errore_autenticazione",
            timed_out=result.timed_out, returncode=result.returncode, stderr=result.stderr)
    raise ValueError(f"Esecutore sconosciuto: {nome}")
