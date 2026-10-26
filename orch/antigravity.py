"""Antigravity tramite la sua CLI `agy` (%LOCALAPPDATA%\\agy\\bin\\agy.exe).

Verificato il 23/10/2026:
- `agy models` elenca i modelli ("id<TAB>nome") e funziona solo con l'account collegato;
- `agy --output-format json -p=<prompt>` restituisce {"conversation_id","status","response","usage",
  "denied_actions"}; `--conversation <id>` riprende la stessa conversazione;
- `--model`, `--effort (low|medium|high|xhigh|max)`, `--mode accept-edits|plan|auto`;
- `agy --output-format json -p=/usage` (verificato il 23/10/2026 sulla 1.4.6) restituisce, senza
  consumare quota, i limiti per gruppo di modelli ("Gemini Models", "Claude and GPT models"):
  per ciascuno una finestra "5h" e una "weekly" con remaining_fraction e reset_time. Il gruppo
  si sceglie dal modello in uso; la percentuale usata è 100 × (1 − remaining_fraction).
Come esecutore usa `--mode accept-edits`: modifica i file ma non può eseguire comandi
(niente commit, push, Supabase o rete; i test li esegue l'orchestratore). Come controllore
usa `--mode plan`, che non modifica nulla. Mai `--dangerously-skip-permissions`.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess

AGY = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
LIVELLI = ["low", "medium", "high", "xhigh", "max"]
MAX_PROMPT_IN_RIGA = 24000  # oltre, il prompt va in un file (limite della riga di comando di Windows)
_LIMITE = re.compile(r"quota|rate.?limit|resource.?exhausted|\b429\b|usage limit|too many requests", re.I)
_AUTH = re.compile(r"login|log in|sign.?in|unauthenticated|not authenticated|authenticat", re.I)


@dataclass
class RisultatoAgy:
    conversazione: str | None = None
    risposta: str = ""
    stato: str = ""
    errori: list[str] = field(default_factory=list)
    limite: bool = False
    autenticazione: bool = False
    errore_avvio: str | None = None
    timed_out: bool = False
    returncode: int | None = None
    stderr: str = ""
    azioni_negate: list = field(default_factory=list)


def _ambiente(config=None):
    togliere = {v.upper() for v in getattr(config, "variabili_da_rimuovere", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])}
    return {k: v for k, v in os.environ.items() if k.upper() not in togliere}


def modelli(*, runner=None, eseguibile=AGY):
    """[{id, nome, livelli, predefinito}] da `agy models`; [] se agy manca o non è collegato."""
    if not Path(eseguibile).exists():
        return []
    try:
        r = (runner or subprocess.run)([str(eseguibile), "models"], capture_output=True, text=True, timeout=60,
                                       env=_ambiente(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode:
        return []
    voci = []
    for riga in (r.stdout or "").splitlines():
        if "\t" not in riga:
            continue
        ident, nome = (p.strip() for p in riga.split("\t", 1))
        if ident:
            # Il livello di ragionamento è già nel modello (es. gemini-3.8-flash-high): nessuna scelta a parte.
            voci.append({"id": ident, "nome": nome or ident, "livelli": [], "predefinito": ""})
    return voci


def _osservazione_vuota():
    return {"finestra_5h": None, "resets_5h": None, "settimana": None, "resets_settimana": None}


def _data(valore):
    try:
        return datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _gruppo(gruppi, modello):
    """Gruppo di quota del modello: Gemini con Gemini, Claude/GPT-OSS con l'altro."""
    gruppi = [g for g in gruppi if isinstance(g, dict) and isinstance(g.get("buckets"), list)]
    if not gruppi:
        return None
    famiglia = "gemini" if str(modello or "").lower().startswith("gemini") else "claude"
    for g in gruppi:
        testo = f"{g.get('name', '')} {g.get('description', '')}".lower()
        if famiglia in testo:
            return g
    return gruppi[0] if len(gruppi) == 1 else None


def osservazione_da_usage(dati, modello):
    """Dal JSON di `/usage` alla stessa osservazione di Codex e Claude (percentuali usate)."""
    risultato = _osservazione_vuota()
    comando_usage = (dati or {}).get("command") or {}
    gruppo = _gruppo(((comando_usage.get("data") or {}).get("groups")) or [], modello)
    if gruppo is None:
        return risultato
    for bucket in gruppo["buckets"]:
        if not isinstance(bucket, dict):
            continue
        finestra = str(bucket.get("window", "")).lower()
        chiavi = {"5h": ("finestra_5h", "resets_5h"), "weekly": ("settimana", "resets_settimana")}.get(finestra)
        resto = bucket.get("remaining_fraction")
        if chiavi is None or type(resto) not in (int, float) or not 0 <= resto <= 1:
            continue
        risultato[chiavi[0]] = round((1 - float(resto)) * 100, 2)
        risultato[chiavi[1]] = _data(bucket.get("reset_time"))
    return risultato


def utilizzo(modello, *, runner=None, eseguibile=AGY):
    """Percentuali usate per il modello; tutto None se agy manca o la lettura non riesce."""
    if not Path(eseguibile).exists() and runner is None:
        return _osservazione_vuota()
    try:
        r = (runner or subprocess.run)([str(eseguibile), "--output-format", "json", "-p=/usage"],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=60, env=_ambiente(), stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return _osservazione_vuota()
    if r.returncode:
        return _osservazione_vuota()
    for riga in reversed((r.stdout or "").strip().splitlines()):
        try:
            return osservazione_da_usage(json.loads(riga), modello)
        except (ValueError, AttributeError):
            continue
    return _osservazione_vuota()


def comando(impostazioni, *, prompt_in_riga, conversazione=None, modalita="accept-edits",
            timeout_s=3600, cartelle_extra=(), eseguibile=AGY):
    cmd = [str(eseguibile), "--output-format", "json", "--mode", modalita,
           "--print-timeout", f"{int(timeout_s)}s"]
    if impostazioni.get("modello"):
        cmd += ["--model", impostazioni["modello"]]
    if impostazioni.get("ragionamento"):
        cmd += ["--effort", impostazioni["ragionamento"]]
    if conversazione:
        cmd += ["--conversation", conversazione]
    for cartella in cartelle_extra:
        cmd += ["--add-dir", str(cartella)]
    # Il prompt va attaccato a -p: altrimenti agy prende il flag successivo come prompt.
    cmd.append(f"-p={prompt_in_riga}")
    return cmd


def esegui(config, progetto, prompt, impostazioni, *, conversazione=None, modalita="accept-edits",
           log_label="antigravity", timeout_s=None, runner=None, eseguibile=AGY):
    if not Path(eseguibile).exists():
        return RisultatoAgy(errore_avvio=f"CLI Antigravity non trovata: {eseguibile}")
    timeout_s = timeout_s or int(getattr(config, "timeout_codex_minuti", 60) * 60)
    extra = []
    if len(prompt) > MAX_PROMPT_IN_RIGA:
        cartella = Path(config.cartella_base) / "log" / "prompt-antigravity"
        cartella.mkdir(parents=True, exist_ok=True)
        file_prompt = cartella / f"{re.sub(r'[^A-Za-z0-9_-]', '_', log_label)}.md"
        file_prompt.write_text(prompt, encoding="utf-8")
        extra.append(cartella)
        in_riga = (f"Le istruzioni complete sono nel file {file_prompt}: leggilo per intero ed eseguile. "
                   "Lavora solo nella cartella corrente del progetto.")
    else:
        in_riga = prompt
    cmd = comando(impostazioni, prompt_in_riga=in_riga, conversazione=conversazione, modalita=modalita,
                  timeout_s=timeout_s, cartelle_extra=extra, eseguibile=eseguibile)
    try:
        r = (runner or subprocess.run)(cmd, cwd=str(progetto), capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=timeout_s + 120, env=_ambiente(config),
                                       stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return RisultatoAgy(conversazione=conversazione, errori=["Timeout Antigravity"], timed_out=True)
    except OSError as exc:
        return RisultatoAgy(errore_avvio=f"Avvio Antigravity non riuscito: {exc}")
    stdout, stderr = r.stdout or "", r.stderr or ""
    dati = {}
    for riga in reversed(stdout.strip().splitlines()):
        try:
            dati = json.loads(riga)
            break
        except ValueError:
            continue
    risposta = dati.get("response", "") if isinstance(dati, dict) else ""
    stato = dati.get("status", "") if isinstance(dati, dict) else ""
    errori = []
    if r.returncode or (stato and stato != "SUCCESS"):
        errori.append((stderr or stdout or f"Antigravity terminato con codice {r.returncode}").strip()[:2000])
    diagnostica = "\n".join(errori)
    return RisultatoAgy(
        conversazione=dati.get("conversation_id") or conversazione, risposta=risposta, stato=stato,
        errori=errori, limite=bool(errori) and bool(_LIMITE.search(diagnostica)),
        autenticazione=bool(errori) and bool(_AUTH.search(diagnostica)) and not _LIMITE.search(diagnostica),
        returncode=r.returncode, stderr=stderr, azioni_negate=dati.get("denied_actions", []) or [])
