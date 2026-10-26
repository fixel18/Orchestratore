"""Stato, accesso e uscita degli account usati dall'orchestratore (Codex, Claude, Antigravity).

- Lo stato si legge con i comandi ufficiali (`codex login status`, `claude auth status`).
- L'accesso apre una finestra del prompt con il login del programma: si completa nel browser
  e resta salvato, quindi l'orchestratore non lo richiede più.
- L'uscita esegue il logout del programma.
Le variabili con chiavi API vengono tolte, così si usa sempre l'abbonamento.
Il runner/launcher è iniettabile: i test non toccano mai gli account veri.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess

NPM = Path.home() / "AppData" / "Roaming" / "npm"
CODEX = NPM / "codex.cmd"
CLAUDE = NPM / "claude.cmd"
ANTIGRAVITY = Path.home() / "AppData" / "Local" / "Programs" / "antigravity" / "Antigravity.exe"
AGY = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
VARIABILI_DA_TOGLIERE = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@dataclass
class StatoAccount:
    strumento: str
    installato: bool
    collegato: bool | None  # None = non verificabile (es. Antigravity)
    dettaglio: str
    puo_accedere: bool = True
    puo_uscire: bool = True


def _ambiente():
    return {k: v for k, v in os.environ.items() if k.upper() not in VARIABILI_DA_TOGLIERE}


def _esegui(comando, runner=None, timeout=60):
    runner = runner or subprocess.run
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return runner(comando, capture_output=True, text=True, timeout=timeout, env=_ambiente(),
                  creationflags=flags)


def stato(strumento, *, runner=None):
    if strumento == "codex":
        if not CODEX.exists():
            return StatoAccount("codex", False, False, "Codex CLI non installato", False, False)
        r = _esegui([str(CODEX), "login", "status"], runner)
        testo = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())
        collegato = "logged in" in testo.lower() and "not logged in" not in testo.lower()
        return StatoAccount("codex", True, collegato, testo or "stato non disponibile")
    if strumento == "claude":
        if not CLAUDE.exists():
            return StatoAccount("claude", False, False, "Claude CLI non installato", False, False)
        r = _esegui([str(CLAUDE), "auth", "status"], runner)
        try:
            dati = json.loads(r.stdout or "{}")
        except ValueError:
            dati = {}
        collegato = bool(dati.get("loggedIn"))
        if collegato:
            dettaglio = f"collegato con {dati.get('authMethod', '?')}"
            if dati.get("subscriptionType"):
                dettaglio += f", piano {dati['subscriptionType']}"
        else:
            dettaglio = "non collegato"
        return StatoAccount("claude", True, collegato, dettaglio)
    if strumento == "antigravity":
        if not AGY.exists():
            return StatoAccount("antigravity", False, False, "CLI Antigravity (agy) non installata", False, False)
        r = _esegui([str(AGY), "models"], runner)
        modelli = [riga for riga in (r.stdout or "").splitlines() if "\t" in riga]
        collegato = r.returncode == 0 and bool(modelli)
        dettaglio = (f"collegato (CLI agy, {len(modelli)} modelli disponibili)" if collegato
                     else "non collegato: premi Accedi e completa l'accesso con Google")
        # agy non ha un comando di uscita: l'account si cambia dall'app Antigravity.
        return StatoAccount("antigravity", True, collegato, dettaglio, puo_accedere=True, puo_uscire=False)
    raise ValueError(f"Strumento sconosciuto: {strumento}")


def comando_accesso(strumento):
    if strumento == "codex":
        return [str(CODEX), "login"]
    if strumento == "claude":
        return [str(CLAUDE), "auth", "login", "--claudeai"]  # abbonamento, mai la Console a pagamento
    if strumento == "antigravity":
        return [str(AGY)]  # la sessione interattiva chiede l'accesso Google se manca
    raise ValueError(f"Strumento sconosciuto: {strumento}")


def accedi(strumento, *, launcher=None):
    """Apre il login in una finestra propria (il browser si apre da solo)."""
    comando = comando_accesso(strumento)
    if launcher:
        return launcher(comando)
    # cmd /k lascia la finestra aperta per leggere l'esito; si chiude a mano a login finito.
    riga = subprocess.list2cmdline(comando)
    return subprocess.Popen(["cmd.exe", "/k", f"title Accesso {strumento} & {riga}"], env=_ambiente(),
                            creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)


def esci(strumento, *, runner=None):
    if strumento == "codex":
        r = _esegui([str(CODEX), "logout"], runner)
    elif strumento == "claude":
        r = _esegui([str(CLAUDE), "auth", "logout"], runner)
    else:
        raise ValueError("L'uscita si fa solo per Codex e Claude")
    if r.returncode:
        raise RuntimeError(((r.stderr or r.stdout or "") or "logout non riuscito").strip())
    return True
