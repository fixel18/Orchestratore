"""Impostazioni modificabili durante il ciclo, validate e salvate atomicamente."""

from copy import deepcopy
import json
from pathlib import Path
import re

from .catalogo import STRUMENTI, LIMITE_PERCENTUALE_SUPPORTATO, catalogo
from .config import ConfigError
from .persistence import atomic_write


class ImpostazioniError(ConfigError):
    """File impostazioni illeggibile o valori non validi."""


def predefinite(config=None):
    """Nuovo oggetto indipendente; migra modello/livello Codex dal config."""
    return {
        "spegnimento_automatico": True,
        "avvio_automatico": {"attivo": False, "modalita": "sempre",
                             "fasce": [{"da": "22:00", "a": "07:00"}]},
        "controllore": {"strumento": "claude", "modello": "claude-opus-5-5", "ragionamento": "medium"},
        "esecutori": {
            "codex": {"attivo": True, "modello": getattr(config, "modello_codex", "gpt-6.1-sol"),
                      "ragionamento": getattr(config, "ragionamento_codex", "medium"),
                      "priorita": 1, "limite_5h_percento": None},
            "claude": {"attivo": False, "modello": "claude-opus-5-5", "ragionamento": "medium",
                       "priorita": 2, "limite_5h_percento": None},
            "antigravity": {"attivo": False, "modello": "", "ragionamento": "",
                            "priorita": 3, "limite_5h_percento": None},
        },
    }


def _completa(values, defaults, path="impostazioni"):
    if not isinstance(values, dict):
        raise ImpostazioniError(f"{path}: atteso un oggetto JSON")
    unknown = values.keys() - defaults.keys()
    if unknown:
        raise ImpostazioniError(f"{path}: campi sconosciuti: {', '.join(sorted(unknown))}")
    result = deepcopy(defaults)
    for key, value in values.items():
        result[key] = (_completa(value, defaults[key], f"{path}.{key}")
                       if isinstance(defaults[key], dict) else deepcopy(value))
    return result


def valida(values, *, catalogo_modelli=None, config=None, cartella_base=None):
    """Completa i campi omessi e restituisce una copia validata."""
    result = _completa(values, predefinite(config))
    if type(result["spegnimento_automatico"]) is not bool:
        raise ImpostazioniError("spegnimento_automatico: atteso sì/no (booleano)")
    automatic = result["avvio_automatico"]
    if type(automatic["attivo"]) is not bool:
        raise ImpostazioniError("avvio_automatico.attivo: atteso un booleano")
    if automatic["modalita"] not in ("sempre", "fasce"):
        raise ImpostazioniError("avvio_automatico.modalita: scegliere sempre o fasce")
    if not isinstance(automatic["fasce"], list):
        raise ImpostazioniError("avvio_automatico.fasce: attesa una lista")
    if automatic["modalita"] == "fasce" and not automatic["fasce"]:
        raise ImpostazioniError("Specificare almeno una fascia oraria")
    for band in automatic["fasce"]:
        if not isinstance(band, dict) or set(band) != {"da", "a"}:
            raise ImpostazioniError("Ogni fascia deve contenere da e a")
        for value in band.values():
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                raise ImpostazioniError("Fasce: usare HH:MM tra 00:00 e 23:59")
        if band["da"] == band["a"]:
            raise ImpostazioniError("Fasce: da e a devono essere diversi")
    controller = result["controllore"]
    if not isinstance(controller["strumento"], str) or controller["strumento"] not in STRUMENTI:
        raise ImpostazioniError("controllore.strumento: scegliere codex, claude o antigravity")
    priorities = []
    for tool, executor in result["esecutori"].items():
        if type(executor["attivo"]) is not bool:
            raise ImpostazioniError(f"{tool}.attivo: atteso un booleano")
        priority = executor["priorita"]
        if type(priority) is not int or not 1 <= priority <= 3:
            raise ImpostazioniError(f"{tool}.priorita: atteso un intero da 1 a 3")
        priorities.append(priority)
        limit = executor["limite_5h_percento"]
        if not LIMITE_PERCENTUALE_SUPPORTATO[tool] and limit is not None:
            raise ImpostazioniError(f"{tool}.limite_5h_percento: utilizzo non leggibile, il limite deve essere null")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ImpostazioniError(f"{tool}.limite_5h_percento: usare null oppure un intero da 1 a 100")
    if len(set(priorities)) != 3:
        raise ImpostazioniError("Le priorità degli esecutori devono essere tutte diverse")
    if not any(item["attivo"] for item in result["esecutori"].values()):
        raise ImpostazioniError("Attivare almeno un esecutore")
    models = catalogo_modelli if catalogo_modelli is not None else catalogo(
        cartella_base, comando_claude=getattr(config, "comando_claude", None))
    selections = [(controller["strumento"], controller), *result["esecutori"].items()]
    for tool, selection in selections:
        if any(not isinstance(selection[key], str) for key in ("modello", "ragionamento")):
            raise ImpostazioniError(f"{tool}: modello e ragionamento devono essere stringhe")
        if selection is not controller and not selection["attivo"] and not selection["modello"]:
            continue  # esecutore spento e mai configurato (es. Antigravity appena collegato)
        if tool == "antigravity" and not models.get("antigravity"):
            # CLI agy assente o non collegata: ammesso solo se non viene usato.
            in_uso = (tool == controller["strumento"] and selection is controller) or (
                selection is not controller and selection["attivo"])
            if in_uso:
                raise ImpostazioniError("Antigravity non è collegato: accedi dalla sezione Account oppure disattivalo")
            continue
        model = next((item for item in models.get(tool, []) if item["id"] == selection["modello"]), None)
        if model is None:
            raise ImpostazioniError(f"{tool}: modello non presente nel catalogo: {selection['modello']}")
        if not model["livelli"]:
            selection["ragionamento"] = ""  # livello già compreso nel modello (Antigravity)
        elif selection["ragionamento"] not in model["livelli"]:
            raise ImpostazioniError(f"{tool}: livello di ragionamento non valido: {selection['ragionamento']}")
    return result


def salva(path, values, *, catalogo_modelli=None, config=None):
    path = Path(path)
    result = valida(values, catalogo_modelli=catalogo_modelli, config=config, cartella_base=path.parent)
    try:
        atomic_write(path, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except OSError as exc:
        raise ImpostazioniError(f"Impossibile salvare {path}: {exc}") from exc
    return result


def carica(path, *, catalogo_modelli=None, config=None):
    """Crea il file mancante; non sovrascrive mai un file non valido."""
    path = Path(path)
    if not path.exists():
        return salva(path, predefinite(config), catalogo_modelli=catalogo_modelli, config=config)
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ImpostazioniError(f"Impossibile leggere {path}: {exc}") from exc
    return valida(values, catalogo_modelli=catalogo_modelli, config=config, cartella_base=path.parent)
