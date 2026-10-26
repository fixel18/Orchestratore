"""Catalogo locale dei modelli. Nessuna interrogazione ai servizi remoti."""

import json
from pathlib import Path
import re
import subprocess

from .config import ConfigError

STRUMENTI = ("codex", "claude", "antigravity")
NOTA_ANTIGRAVITY = "da collegare"
LIMITE_PERCENTUALE_SUPPORTATO = {"codex": True, "claude": True, "antigravity": True}
LIVELLI_CLAUDE = ("low", "medium", "high", "xhigh", "max")
# Elenco modificabile: Claude CLI non espone un elenco dei modelli.
MODELLI_CLAUDE = (
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
    "claude-haiku-5-5", "claude-fable-5-1",
)


class CatalogoError(ConfigError):
    """Catalogo aggiuntivo non valido o illeggibile."""


def _modello(identity, name, levels, default):
    if (not isinstance(identity, str) or not identity.strip()
            or not isinstance(name, str) or not name.strip()
            or not isinstance(levels, list) or not levels
            or any(not isinstance(level, str) or not level.strip() for level in levels)
            or not isinstance(default, str) or default not in levels
            or len(set(levels)) != len(levels)):
        raise CatalogoError("Modello: richiesti id, nome, livelli e predefinito valido")
    return {"id": identity, "nome": name, "livelli": list(levels), "predefinito": default}


def _codex(cache):
    # Riserva minima per installazioni senza cache o cache illeggibile.
    fallback = [_modello("gpt-6.1-sol", "gpt-6.1-sol",
                         ["low", "medium", "high", "xhigh", "max", "ultra"], "medium")]
    try:
        data = json.loads(cache.read_text(encoding="utf-8-sig"))
        result = []
        for item in data["models"]:
            if item.get("visibility") != "list":
                continue
            result.append(_modello(item["slug"], item["display_name"],
                [level["effort"] for level in item["supported_reasoning_levels"]],
                item["default_reasoning_level"]))
        if len({item["id"] for item in result}) != len(result):
            return fallback
        return result
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return fallback


def _livelli_claude(command, runner):
    try:
        response = runner([*command, "--help"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=10,
                          shell=False, check=False)
        if response.returncode == 0:
            match = re.search(r"--effort\b[^\n]*(?:\n[^\n]*)?", response.stdout)
            if match:
                values = re.search(r"\((low\s*,[^)]+)\)", match.group())
                if values:
                    levels = [value.strip() for value in values[1].split(",")]
                    if all(re.fullmatch(r"[a-z]+", level) for level in levels):
                        return list(dict.fromkeys(levels))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return list(LIVELLI_CLAUDE)


_CACHE_AGY = {"quando": 0.0, "voci": []}


def _modelli_antigravity(durata_s=600):
    """`agy models` richiede rete: si rilegge al massimo ogni 10 minuti."""
    import time
    from . import antigravity
    adesso = time.monotonic()
    if not _CACHE_AGY["voci"] or adesso - _CACHE_AGY["quando"] > durata_s:
        voci = antigravity.modelli()
        if voci or adesso - _CACHE_AGY["quando"] > durata_s:
            _CACHE_AGY.update(quando=adesso, voci=voci)
    return [dict(v, livelli=list(v["livelli"])) for v in _CACHE_AGY["voci"]]


def catalogo(cartella_base=None, *, cache_codex=None, comando_claude=None, runner=None, modelli_antigravity=None):
    """Restituisce liste per strumento; Antigravity è vuoto (da collegare).

    La chiave limite_percentuale_supportato contiene una mappa strumento ->
    bool, separata dalle liste per conservarne l'interfaccia. Questa capacità
    dipende dall'adattatore e non è modificabile tramite catalogo-extra.json.

    Cache e lettore dell'help sono iniettabili. catalogo-extra.json arricchisce
    le liste e sostituisce, per id, eventuali voci esistenti. Un extra errato
    è segnalato esplicitamente, senza ignorare gli inserimenti dell'utente.
    """
    base = Path(cartella_base) if cartella_base is not None else Path(__file__).resolve().parent.parent
    cache = Path(cache_codex) if cache_codex is not None else Path.home() / ".codex" / "models_cache.json"
    levels = _livelli_claude(comando_claude or ["claude.cmd"], runner or subprocess.run)
    result = {"codex": _codex(cache), "claude": [
        _modello(identity, identity, levels, "medium" if "medium" in levels else levels[0])
        for identity in MODELLI_CLAUDE],
        "antigravity": modelli_antigravity() if modelli_antigravity else _modelli_antigravity(),
        "limite_percentuale_supportato": dict(LIMITE_PERCENTUALE_SUPPORTATO)}
    extra = base / "catalogo-extra.json"
    if not extra.exists():
        return result
    try:
        data = json.loads(extra.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.keys() - set(STRUMENTI):
            raise CatalogoError("Attesi solo gli strumenti codex, claude, antigravity")
        for tool, entries in data.items():
            if not isinstance(entries, list):
                raise CatalogoError(f"{tool}: attesa una lista di modelli")
            seen = set()
            for entry in entries:
                item = _modello(entry["id"], entry["nome"], entry["livelli"], entry["predefinito"])
                if item["id"] in seen:
                    raise CatalogoError(f"{tool}: modello duplicato {item['id']}")
                seen.add(item["id"])
                result[tool] = [old for old in result[tool] if old["id"] != item["id"]] + [item]
        return result
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise CatalogoError(f"Impossibile leggere {extra}: {exc}") from exc
