"""Smistamento Ollama con trasporto iniettabile e fallback al revisore."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import re
from urllib.request import Request, urlopen

from .config import Config
from .verification import VerificationResult, verification_summary


ALLOWED_OUTCOMES = frozenset({"rimanda_a_codex", "chiama_claude", "limite_raggiunto"})
Transport = Callable[[str, dict, float], object]
_THINK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class ModelDecision:
    esito: str
    messaggio: str
    motivo: str
    raw_response: str = ""
    forced: bool = False


def _reject_constant(value: str):
    raise ValueError(f"Costante non JSON: {value}")


def parse_model_decision(content: str) -> ModelDecision:
    """Rimuove blocchi think; il resto deve essere un unico oggetto JSON."""
    data = json.loads(_THINK.sub("", content).strip(), parse_constant=_reject_constant)
    if not isinstance(data, dict):
        raise ValueError("La risposta del modello deve essere un oggetto JSON")
    outcome = data.get("esito")
    if not isinstance(outcome, str) or outcome not in ALLOWED_OUTCOMES:
        raise ValueError("Esito del modello sconosciuto")
    if not isinstance(data.get("messaggio"), str) or not isinstance(data.get("motivo"), str):
        raise ValueError("messaggio e motivo devono essere stringhe")
    return ModelDecision(outcome, data["messaggio"], data["motivo"], raw_response=content)


def ollama_transport(url: str, payload: dict, timeout: float) -> object:
    """POST standard library; nessuna dipendenza da Ollama nei test."""
    request = Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"), parse_constant=_reject_constant)


def clean_report_text(content: str) -> str:
    """Esclude anche un blocco think lasciato aperto dal modello."""
    content = _THINK.sub("", content)
    return re.sub(r"<think\b[^>]*>.*$", "", content,
                  flags=re.IGNORECASE | re.DOTALL).strip()


def generate_final_report(config: Config, data: dict, *,
                          transport: Transport | None = None) -> str:
    prompt = (config.cartella_base / "regole" / "prompt-report-finale.md").read_text(
        encoding="utf-8-sig")
    payload = {
        "model": config.nome_modello,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
        "stream": False, "think": False,
        "options": {"temperature": 0, "num_ctx": config.num_ctx_modello},
    }
    envelope = (transport or ollama_transport)(config.url_modello, payload, config.timeout_report_s)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("message"), dict):
        raise ValueError("Risposta Ollama senza message")
    content = envelope["message"].get("content")
    if not isinstance(content, str):
        raise ValueError("Risposta Ollama senza contenuto testuale")
    return clean_report_text(content)


def build_model_payload(
    config: Config, system_prompt: str, dossier: str, piece_number: int,
    referrals: int, last_codex_response: str,
    verifications: Sequence[VerificationResult], changed_files: Sequence[str],
) -> dict:
    data = {
        "fascicolo": dossier,
        "numero_pezzo": piece_number,
        "rimandi_pezzo": referrals,
        "ultima_risposta_codex": last_codex_response[:6000],
        "verifiche": verification_summary(verifications),
        "file_cambiati": list(changed_files),
    }
    return {
        "model": config.nome_modello,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        "stream": False, "format": "json", "think": False,
        "options": {"temperature": 0, "num_ctx": config.num_ctx_modello},
    }


def decide_next_action(
    config: Config, dossier: str, piece_number: int, referrals: int,
    last_codex_response: str, verifications: Sequence[VerificationResult],
    changed_files: Sequence[str], *, transport: Transport | None = None,
    system_prompt: str | None = None,
) -> ModelDecision:
    """Non invia mai rimandi oltre soglia; limiti e revisioni restano ammessi.

    Il limite Codex riconosciuto dallo script va gestito dal ciclo prima di
    chiamare questa funzione. Errori di trasporto/formato affidano il pezzo a
    Claude, senza ulteriori richieste al modello.
    """
    raw_response = ""
    try:
        if system_prompt is None:
            system_prompt = (config.cartella_base / "regole" / "prompt-modello.md").read_text(
                encoding="utf-8-sig"
            )
        payload = build_model_payload(
            config, system_prompt, dossier, piece_number, referrals,
            last_codex_response, verifications, changed_files,
        )
        envelope = (transport or ollama_transport)(config.url_modello, payload, config.timeout_modello_s)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("message"), dict):
            raise ValueError("Risposta Ollama senza message")
        content = envelope["message"].get("content")
        if not isinstance(content, str):
            raise ValueError("Risposta Ollama senza message.content testuale")
        raw_response = content
        decision = parse_model_decision(content)
    except Exception as exc:
        # Comprende HTTPError, URLError, timeout, JSON malformato e schema errato.
        return ModelDecision(
            "chiama_claude", "", f"Modello non disponibile o risposta non valida: {exc}",
            raw_response=raw_response, forced=True,
        )
    if decision.esito == "rimanda_a_codex" and referrals >= config.rimandi_max_per_pezzo:
        return ModelDecision(
            "chiama_claude", "",
            f"Limite rimandi raggiunto ({referrals}/{config.rimandi_max_per_pezzo}). {decision.motivo}",
            raw_response=raw_response, forced=True,
        )
    return decision
