"""Comandi di verifica e stato git: cattura output e timeout dell'albero processi."""

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import time

from .codex import clean_child_environment, terminate_process_tree
from .config import Config
from .git_paths import project_pathspecs


_ERROR_LINE = re.compile(r"error|errore|fail|exception|traceback|assert", re.IGNORECASE)


class VerificationError(RuntimeError):
    """Impossibile raccogliere informazioni affidabili sul progetto."""


def extract_error_lines(output: str, *, context: int = 2, max_lines: int = 60) -> tuple[str, ...]:
    """Finestre di contesto unite senza duplicati, con numeri di riga originali."""
    if context < 0 or max_lines < 0:
        raise ValueError("context e max_lines devono essere non negativi")
    lines = output.splitlines()
    selected = set()
    for index, line in enumerate(lines):
        if _ERROR_LINE.search(line):
            selected.update(range(max(0, index - context), min(len(lines), index + context + 1)))
    return tuple(f"{index + 1}: {lines[index]}" for index in sorted(selected)[:max_lines])


@dataclass(frozen=True)
class VerificationResult:
    command: str
    returncode: int | None
    timed_out: bool
    output: str
    error_lines: tuple[str, ...]
    error: str | None
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None


def _decode(value: bytes | str | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def run_verification_command(
    config: Config, project: str | Path, command: str, *, max_error_lines: int = 60,
) -> VerificationResult:
    """La riga del fascicolo è codice shell: su Windows Popen usa cmd /c.

    stdout e stderr condividono la stessa pipe per mantenere il contesto degli
    errori. Il timeout termina anche i figli; ogni attesa di pulizia è limitata.
    L'output integrale è restituito al chiamante per il log, non al modello.
    """
    environment = clean_child_environment(config.variabili_da_rimuovere)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {
        "start_new_session": True,
    }
    started = time.monotonic()
    output = b""
    errors = []
    returncode = None
    timed_out = False
    pipes_drained = False
    try:
        process = subprocess.Popen(
            command, shell=True, cwd=str(project), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **options,
        )
    except OSError as exc:
        errors.append(f"Avvio verifica non riuscito: {exc}")
    else:
        try:
            try:
                output, _ = process.communicate(timeout=config.timeout_verifica_minuti * 60)
                pipes_drained = True
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                errors.append("Timeout verifica")
                output = exc.output or b""
                kill_error = terminate_process_tree(process, environment=environment)
                if kill_error:
                    errors.append(kill_error)
                try:
                    # communicate ripetuto restituisce l'intero output, senza concatenarlo.
                    output, _ = process.communicate(timeout=5)
                    pipes_drained = True
                except subprocess.TimeoutExpired as tail:
                    output = tail.output if tail.output is not None else output
                    kill_error = terminate_process_tree(process, environment=environment)
                    if kill_error:
                        errors.append(kill_error)
                    errors.append("Pipe della verifica non chiuse dopo la terminazione")
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        errors.append("La verifica non termina dopo taskkill")
            returncode = process.poll()
        except BaseException:
            terminate_process_tree(process, environment=environment)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            # Su Windows communicate può avere un thread ancora bloccato sulla
            # pipe di un figlio: close in quel caso potrebbe bloccare a sua volta.
            if pipes_drained:
                if process.stdout is not None:
                    process.stdout.close()
    text = _decode(output)
    error_lines = extract_error_lines(text, max_lines=max_error_lines)
    if not error_lines and (timed_out or returncode not in (None, 0)):
        # Un tool può fallire senza usare le parole chiave: conserva almeno la coda.
        lines = text.splitlines()
        count = min(8, max_error_lines)
        start = max(0, len(lines) - count)
        error_lines = tuple(f"{index + 1}: {lines[index]}" for index in range(start, len(lines)))
    return VerificationResult(
        command=command, returncode=returncode, timed_out=timed_out, output=text,
        error_lines=error_lines, error="; ".join(errors) or None,
        duration_s=time.monotonic() - started,
    )


def run_verifications(
    config: Config, project: str | Path, commands: Sequence[str],
) -> tuple[VerificationResult, ...]:
    """Esegue tutte le righe, limitando a 60 le righe diagnostiche complessive."""
    results = []
    remaining = 60
    for command in commands:
        if (config.cartella_base / "STOP").exists():
            break
        result = run_verification_command(config, project, command, max_error_lines=remaining)
        results.append(result)
        remaining -= len(result.error_lines)
    return tuple(results)


def verification_summary(results: Sequence[VerificationResult]) -> list[dict]:
    """Dati compatti per il modello; l'output completo rimane nei risultati."""
    return [{
        "comando": result.command,
        "exit_code": result.returncode,
        "ok": result.ok,
        "timeout": result.timed_out,
        "errore": result.error,
        "righe_errore": list(result.error_lines),
    } for result in results]


def list_changed_files(config: Config, project: str | Path, *, exclude_paths=()) -> tuple[str, ...]:
    """Conserva i due caratteri XY e i nomi quoted di git status --porcelain."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", *project_pathspecs(config, exclude_paths)],
            cwd=str(project), env=clean_child_environment(config.variabili_da_rimuovere),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=config.timeout_verifica_minuti * 60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError(f"git status non disponibile: {exc}") from exc
    if result.returncode:
        raise VerificationError(f"git status fallito ({result.returncode}): {_decode(result.stderr)}")
    return tuple(line for line in _decode(result.stdout).splitlines() if line.strip())
