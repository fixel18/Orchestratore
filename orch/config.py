"""Caricamento e validazione della configurazione, senza effetti collaterali."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlsplit


DEFAULT_COMMIT_EXCLUSIONS = (
    "**/__pycache__/**", "**/*.pyc", "**/*.tsbuildinfo",
    "**/.next/**", "**/node_modules/**", "**/.env*",
)


class ConfigError(ValueError):
    """Configurazione illeggibile o non valida."""


@dataclass(frozen=True)
class Config:
    cartella_base: Path
    python: Path
    comando_codex: list[str]
    modello_codex: str
    ragionamento_codex: str
    comando_claude: list[str]
    url_modello: str
    nome_modello: str
    timeout_modello_s: float
    num_ctx_modello: int
    sandbox_codex: str
    variabili_da_rimuovere: list[str]
    escludi_dal_commit: list[str]
    push_abilitato: bool
    rimandi_max_per_pezzo: int
    bocciature_claude_max: int
    attesa_limite_minuti: float
    timeout_codex_minuti: float
    timeout_verifica_minuti: float
    timeout_claude_minuti: float
    intervallo_coda_vuota_s: float
    cartella_sessioni_codex: Path
    spegnimento_abilitato: bool
    comando_spegnimento: list[str]
    attesa_prima_spegnimento_s: float
    soglia_limite_settimanale_ore: float
    timeout_report_s: float
    consenti_modifiche_preesistenti: bool = True
    cartella_query: str = "supabase/queries"


def _string(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key}: attesa una stringa non vuota")
    return value


def _strings(value: object, key: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ConfigError(f"{key}: attesa una lista di stringhe")
    return [_string(item, key) for item in value]


def load_config(path: str | Path) -> Config:
    """Legge JSON UTF-8 (anche con BOM); i percorsi relativi partono dal file.

    I campi omessi usano i valori predefiniti. Non controlla l'esistenza degli
    eseguibili: una configurazione alternativa può puntare a strumenti finti.
    Gli argomenti dei comandi restano liste, senza interpretazione da shell.
    """
    config_path = Path(path).resolve()
    try:
        supplied = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"Impossibile leggere {config_path}: {exc}") from exc
    if not isinstance(supplied, dict):
        raise ConfigError("La configurazione deve essere un oggetto JSON")

    values = {
        "cartella_base": ".",
        "python": sys.executable,
        "comando_codex": ["codex.cmd"],
        "modello_codex": "gpt-6.1-sol",
        "ragionamento_codex": "medium",
        "comando_claude": ["claude.cmd"],
        "url_modello": "http://127.0.0.1:11434/api/chat",
        "nome_modello": "qwen3:14b",
        "timeout_modello_s": 300,
        "num_ctx_modello": 16384,
        "sandbox_codex": "workspace-write",
        "variabili_da_rimuovere": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        "escludi_dal_commit": list(DEFAULT_COMMIT_EXCLUSIONS),
        "push_abilitato": True,
        "consenti_modifiche_preesistenti": True,
        "cartella_query": "supabase/queries",
        "rimandi_max_per_pezzo": 2,
        "bocciature_claude_max": 3,
        "attesa_limite_minuti": 30,
        "timeout_codex_minuti": 60,
        "timeout_verifica_minuti": 20,
        "timeout_claude_minuti": 20,
        "intervallo_coda_vuota_s": 300,
        "cartella_sessioni_codex": str(Path.home() / ".codex" / "sessions"),
        "spegnimento_abilitato": True,
        "comando_spegnimento": ["shutdown", "/s", "/t", "0", "/c", "Orchestratore: lavoro terminato"],
        "attesa_prima_spegnimento_s": 120,
        "soglia_limite_settimanale_ore": 12,
        "timeout_report_s": 600,
    }
    unknown = supplied.keys() - values.keys()
    if unknown:
        raise ConfigError(f"Campi sconosciuti: {', '.join(sorted(unknown))}")
    values.update(supplied)

    for key in ("cartella_base", "python", "cartella_sessioni_codex"):
        value = Path(_string(values[key], key))
        values[key] = (config_path.parent / value).resolve()
    for key in ("comando_codex", "comando_claude", "comando_spegnimento", "variabili_da_rimuovere"):
        values[key] = _strings(
            values[key], key, allow_empty=(key == "variabili_da_rimuovere")
        )
    values["escludi_dal_commit"] = _strings(
        values["escludi_dal_commit"], "escludi_dal_commit", allow_empty=True,
    )
    query_folder = Path(_string(values["cartella_query"], "cartella_query"))
    if query_folder.is_absolute() or query_folder.drive or query_folder.root or ".." in query_folder.parts:
        raise ConfigError("cartella_query: atteso un percorso relativo interno al progetto")
    values["cartella_query"] = query_folder.as_posix()
    for key in (
        "url_modello", "nome_modello", "sandbox_codex", "modello_codex", "ragionamento_codex",
    ):
        values[key] = _string(values[key], key)
    try:
        url = urlsplit(values["url_modello"])
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("atteso URL HTTP o HTTPS")
    except ValueError as exc:
        raise ConfigError(f"url_modello: {exc}") from exc
    if type(values["push_abilitato"]) is not bool:
        raise ConfigError("push_abilitato: atteso un booleano JSON")
    if type(values["consenti_modifiche_preesistenti"]) is not bool:
        raise ConfigError("consenti_modifiche_preesistenti: atteso un booleano JSON")
    if type(values["spegnimento_abilitato"]) is not bool:
        raise ConfigError("spegnimento_abilitato: atteso un booleano JSON")
    for key, minimum in (
        ("rimandi_max_per_pezzo", 0), ("bocciature_claude_max", 1),
        ("num_ctx_modello", 1),
    ):
        if type(values[key]) is not int or values[key] < minimum:
            raise ConfigError(f"{key}: atteso un intero >= {minimum}")
    for key in (
        "timeout_modello_s", "attesa_limite_minuti", "timeout_codex_minuti",
        "timeout_verifica_minuti", "timeout_claude_minuti", "intervallo_coda_vuota_s",
        "soglia_limite_settimanale_ore", "timeout_report_s",
    ):
        value = values[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value <= 0):
            raise ConfigError(f"{key}: atteso un numero finito > 0")
    value = values["attesa_prima_spegnimento_s"]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ConfigError("attesa_prima_spegnimento_s: atteso un numero finito >= 0")
    return Config(**values)
