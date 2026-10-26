"""Attività di Utilità di pianificazione che apre il pannello all'accesso di Windows.

Nessun privilegio amministrativo: l'attività è dell'utente e gira con livello limitato.
Il comando PowerShell è iniettabile perché i test non devono mai toccare il sistema.
"""

from pathlib import Path
import subprocess

NOME_ATTIVITA = "Orchestratore Codex"
BASE = Path(__file__).resolve().parent.parent
PYTHONW = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "pythonw.exe"


def _powershell(script, runner=None):
    runner = runner or subprocess.run
    risultato = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60)
    if risultato.returncode:
        raise RuntimeError((risultato.stderr or risultato.stdout or "PowerShell non riuscito").strip())
    return (risultato.stdout or "").strip()


def _quota(testo):
    return "'" + str(testo).replace("'", "''") + "'"


def script_registrazione(attivo, *, base=BASE, pythonw=PYTHONW):
    argomenti = f'"{Path(base) / "pannello.py"}" --automatico'
    righe = [
        "$utente = \"$env:USERDOMAIN\\$env:USERNAME\"",
        f"$azione = New-ScheduledTaskAction -Execute {_quota(pythonw)} -Argument {_quota(argomenti)} "
        f"-WorkingDirectory {_quota(base)}",
        "$avvio = New-ScheduledTaskTrigger -AtLogOn -User $utente",
        "$opzioni = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew",
        "$chi = New-ScheduledTaskPrincipal -UserId $utente -LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName {_quota(NOME_ATTIVITA)} -Description "
        "'Apre il pannello dell orchestratore all accesso e avvia le task se previsto.' "
        "-Action $azione -Trigger $avvio -Settings $opzioni -Principal $chi -Force | Out-Null",
        ("Enable" if attivo else "Disable") + f"-ScheduledTask -TaskName {_quota(NOME_ATTIVITA)} | Out-Null",
    ]
    return "; ".join(righe)


def imposta_avvio_automatico(attivo: bool, *, runner=None):
    """Crea/aggiorna l'attività perché apra `pannello.py --automatico`, e la abilita o disabilita."""
    _powershell(script_registrazione(attivo), runner)
    return stato_avvio_automatico(runner=runner)


def stato_avvio_automatico(*, runner=None):
    """True se l'attività esiste ed è abilitata, False se disabilitata o assente."""
    script = (f"$t = Get-ScheduledTask -TaskName {_quota(NOME_ATTIVITA)} -ErrorAction SilentlyContinue; "
              "if ($t) { $t.State } else { 'Assente' }")
    stato = _powershell(script, runner)
    return stato.strip().lower() not in ("disabled", "assente", "")
