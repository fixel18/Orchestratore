"""Revisioni isolate in sola lettura, con lo stesso prompt e formato ESITO."""

from dataclasses import dataclass, field, replace

from . import antigravity, claude, codex
from .codex_limits import UsageLimit, osservazione_da_rollout, osservazione_vuota
from .claude_limits import is_authentication_error
from .review import truncate_review_text


@dataclass
class RisultatoControllore:
    review: claude.ClaudeReview
    limite: UsageLimit | None = None
    osservazione_utilizzo: dict = field(default_factory=osservazione_vuota)
    errore_avvio: str | None = None
    errori: list[str] = field(default_factory=list)
    stderr: str = ""


def run_codex_review(config, progetto, prompt, impostazioni, *, log_label="revisione-codex", now=None, runner=None):
    selected = replace(config, sandbox_codex="read-only", modello_codex=impostazioni["modello"],
                       ragionamento_codex=impostazioni["ragionamento"])
    # Nessun thread_id: ogni revisione apre una conversazione nuova.
    result = (runner or codex.run_codex)(selected, progetto, truncate_review_text(prompt), log_label=log_label, now=now)
    text = result.output.last_message
    errors = [item.message for item in result.output.errors]
    diagnostic = "\n".join(errors) + "\n" + result.stderr
    if is_authentication_error(diagnostic):
        verdict = claude.ClaudeReview("errore_autenticazione", text, error=diagnostic.strip())
    elif result.usage_limit:
        verdict = claude.ClaudeReview("limite", text or result.usage_limit.message, retry_at=result.usage_limit.retry_at)
    elif errors or result.launch_error or result.timed_out:
        verdict = claude.ClaudeReview("errore", text, error=diagnostic.strip() or result.launch_error)
    else:
        # Riusa solo il formato del verdetto, senza interpretare testi di limite Claude.
        lines = text.strip().splitlines()
        first = lines[0].strip() if lines else ""
        kind = {"ESITO: OK": "ok", "ESITO: CORREZIONI": "correzioni"}.get(first, "non_riconosciuto")
        verdict = claude.ClaudeReview(kind, text, "\n".join(lines[1:]).strip())
    return RisultatoControllore(verdict, result.usage_limit,
        osservazione_da_rollout(config.cartella_sessioni_codex, result.output.thread_id), result.launch_error, errors, result.stderr)


def revisiona(nome, config, impostazioni_controllore, progetto, prompt, *, log_label=None, now=None,
              run_codex=None, run_claude=None, run_agy=None, leggi_utilizzo=None):
    if nome == "antigravity":
        # Modalità plan: legge ma non modifica nulla; ogni revisione in una conversazione nuova.
        r = (run_agy or antigravity.esegui)(config, progetto, truncate_review_text(prompt), impostazioni_controllore,
                                            modalita="plan", log_label=log_label or "revisione-antigravity")
        from .esecutori import limite_antigravity, utilizzo_antigravity
        osservazione = (utilizzo_antigravity(impostazioni_controllore, run_agy, leggi_utilizzo)
                        if not r.errore_avvio else osservazione_vuota())
        limite = limite_antigravity("\n".join(r.errori) or "Limite Antigravity", osservazione) if r.limite else None
        if r.errore_avvio or r.timed_out or (r.errori and not r.limite):
            kind = "errore_autenticazione" if r.autenticazione else "errore"
            verdict = claude.ClaudeReview(kind, r.risposta, error=r.errore_avvio or "\n".join(r.errori))
        elif r.limite:
            verdict = claude.ClaudeReview("limite", "\n".join(r.errori), retry_at=limite.retry_at)
        else:
            lines = r.risposta.strip().splitlines()
            first = lines[0].strip() if lines else ""
            kind = {"ESITO: OK": "ok", "ESITO: CORREZIONI": "correzioni"}.get(first, "non_riconosciuto")
            verdict = claude.ClaudeReview(kind, r.risposta, "\n".join(lines[1:]).strip())
        return RisultatoControllore(verdict, limite, osservazione, r.errore_avvio, list(r.errori), r.stderr)
    if nome == "codex":
        return run_codex_review(config, progetto, prompt, impostazioni_controllore,
                                log_label=log_label or "revisione-codex", now=now, runner=run_codex)
    if nome == "claude":
        result = (run_claude or claude.run_claude)(config, progetto, prompt, impostazioni=impostazioni_controllore,
                                   log_label=log_label or "revisione-claude", now=now)
        return RisultatoControllore(result.review, result.usage_limit,
            result.osservazione_utilizzo, result.launch_error, list(result.errors), result.stderr)
    raise ValueError(f"Controllore sconosciuto: {nome}")
