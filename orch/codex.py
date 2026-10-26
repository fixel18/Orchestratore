"""Invocazioni Codex exec/resume: stdin UTF-8, JSONL e timeout dei processi."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
from threading import Thread
import time
from uuid import uuid4
import re

from .codex_limits import CodexError, UsageLimit, detect_usage_limit
from .config import Config
from .live_activity import activity_metadata


@dataclass
class CodexOutput:
    thread_id: str | None = None
    last_message: str = ""
    errors: list[CodexError] = field(default_factory=list)
    turn_completed: bool = False


class CodexJSONLParser:
    def __init__(
        self, *, thread_id: str | None = None,
        on_thread: Callable[[str], None] | None = None,
    ):
        self.output = CodexOutput(thread_id=thread_id)
        self.on_thread = on_thread

    def feed(self, line: str) -> None:
        """Una riga alla volta; il callback può salvare subito lo stato.

        Le eccezioni del callback si propagano: continuare senza aver salvato
        l'ID potrebbe aprire una conversazione duplicata alla ripresa.
        """
        if not line.strip():
            return
        try:
            event = json.loads(line.lstrip("\ufeff"))
        except ValueError as exc:
            self.output.errors.append(CodexError(f"JSONL Codex non valido: {exc}"))
            return
        if not isinstance(event, dict):
            self.output.errors.append(CodexError("Evento Codex non è un oggetto JSON"))
            return
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
                self.output.errors.append(CodexError("thread.started senza thread_id valido"))
            elif self.output.thread_id is not None and self.output.thread_id != thread_id:
                self.output.errors.append(CodexError("Codex ha cambiato thread_id durante la ripresa"))
            elif self.output.thread_id is None:
                self.output.thread_id = thread_id
                if self.on_thread:
                    self.on_thread(thread_id)
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                if isinstance(item.get("text"), str):
                    self.output.last_message = item["text"]
        elif kind in ("error", "turn.failed"):
            error = event.get("error")
            if isinstance(error, dict):
                message = error.get("message", event.get("message", "Errore Codex"))
                code = error.get("code", event.get("code"))
            else:
                message = event.get("message", error or "Errore Codex")
                code = event.get("code")
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            self.output.errors.append(CodexError(message, code if isinstance(code, str) else None))
        elif kind == "turn.completed":
            self.output.turn_completed = True


def parse_codex_jsonl(text: str) -> CodexOutput:
    parser = CodexJSONLParser()
    for line in text.splitlines():
        parser.feed(line)
    return parser.output


def build_codex_command(
    config: Config, project: str | Path, last_message_file: str | Path,
    *, thread_id: str | None = None,
) -> list[str]:
    """Il prompt non entra mai negli argomenti del comando."""
    command = [*config.comando_codex, "exec"]
    model_options = [
        "-m", config.modello_codex,
        "-c", f'model_reasoning_effort="{config.ragionamento_codex}"',
    ]
    if thread_id is None:
        return command + [
            "--json", *model_options, "-s", config.sandbox_codex, "-C", str(project),
            "-o", str(last_message_file), "-",
        ]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        raise ValueError("thread_id non valido")
    return command + [
        "resume", "--json", *model_options,
        "-c", f'sandbox_mode="{config.sandbox_codex}"',
        "-o", str(last_message_file), thread_id, "-",
    ]


def clean_child_environment(
    variables_to_remove: list[str], environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copia l'ambiente senza modificarlo; i nomi Windows non distinguono il caso."""
    excluded = {name.casefold() for name in variables_to_remove}
    environment = os.environ if environment is None else environment
    return {name: value for name, value in environment.items() if name.casefold() not in excluded}


def build_first_prompt(instructions: str, dossier: str, number: int, piece: str) -> str:
    return (
        f"{instructions.rstrip()}\n\n# FASCICOLO DELLA TASK\n{dossier.rstrip()}\n\n"
        f"# ESEGUI ORA IL PEZZO {number}\nEsegui ora il pezzo {number}: {piece}\n"
    )


def build_resume_prompt(message: str) -> str:
    """Per i turni seguenti passa solo il pezzo o la richiesta di correzione."""
    return message


def terminate_process_tree(
    process: subprocess.Popen, *, environment: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> str | None:
    """Termina anche i figli della .cmd; restituisce un eventuale errore.

    Su POSIX il chiamante avvia il processo in una sessione separata, per poter
    terminare il gruppo. Su Windows taskkill /T /F è necessario per le .cmd.
    """
    windows = os.name == "nt" if windows is None else windows
    try:
        if windows:
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                check=False, env=environment,
            )
            if result.returncode:
                process.kill()
                return f"taskkill terminato con codice {result.returncode}"
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()
        except OSError:
            pass
        return f"Terminazione albero Codex non riuscita: {exc}"
    return None


@dataclass
class CodexResult:
    output: CodexOutput
    returncode: int | None
    timed_out: bool
    usage_limit: UsageLimit | None
    jsonl_path: Path
    stderr_path: Path
    last_message_path: Path
    prompt_path: Path
    stderr: str
    launch_error: str | None = None


def run_codex(
    config: Config, project: str | Path, prompt: str, *,
    thread_id: str | None = None, on_thread: Callable[[str], None] | None = None,
    log_label: str = "codex", now: datetime | None = None,
) -> CodexResult:
    """Esegue un singolo invio; non decide rimandi, attese o avanzamento pezzi.

    Il callback on_thread è chiamato appena arriva thread.started, prima della
    fine del turno. Il ciclo persistente del pezzo 5 lo userà per salvare l'ID.
    L'output grezzo resta in log/ anche in caso di timeout o errore. Tre thread
    drenano le pipe ed inviano lo stdin, evitando blocchi con output abbondante.
    """
    logs = config.cartella_base / "log"
    logs.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_-]", "_", log_label)[:80] or "codex"
    stem = f"{label}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex}"
    jsonl_path = logs / f"{stem}.jsonl"
    stderr_path = logs / f"{stem}.stderr.log"
    last_message_path = logs / f"{stem}.last.txt"
    prompt_path = logs / f"{stem}.prompt.txt"
    prompt_path.write_bytes(prompt.encode("utf-8"))
    activity_metadata(logs / f"{stem}.activity.json", "codex",
                      "controllore" if config.sandbox_codex == "read-only" else "esecutore")
    command = build_codex_command(config, project, last_message_path, thread_id=thread_id)
    environment = clean_child_environment(config.variabili_da_rimuovere)
    parser = CodexJSONLParser(thread_id=thread_id, on_thread=on_thread)
    stderr_chunks = []
    timed_out = False
    returncode = None
    launch_error = None
    events = Queue()

    def read_pipe(pipe, kind):
        try:
            for chunk in iter(pipe.readline, b""):
                events.put((kind, chunk))
        except OSError as exc:
            events.put(("io_error", f"{kind}: {exc}"))
        finally:
            events.put(("done", kind))

    def write_stdin(pipe):
        try:
            pipe.write(prompt.encode("utf-8"))
            pipe.flush()
        except BrokenPipeError:
            pass  # L'errore di uscita/JSONL del figlio resta disponibile.
        except OSError as exc:
            events.put(("io_error", f"stdin: {exc}"))
        finally:
            try:
                pipe.close()  # EOF necessario: Codex con '-' altrimenti aspetta.
            except OSError:
                pass
            events.put(("done", "stdin"))

    with jsonl_path.open("xb") as raw, stderr_path.open("xb") as stderr_log:
        def handle(kind, value, *, callbacks=True):
            if kind == "stdout":
                raw.write(value)
                raw.flush()
                if callbacks:
                    parser.feed(value.decode("utf-8", errors="replace"))
            elif kind == "stderr":
                stderr_log.write(value)
                stderr_log.flush()
                stderr_chunks.append(value)
            elif kind == "io_error":
                parser.output.errors.append(CodexError(value))

        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {
            "start_new_session": True,
        }
        try:
            process = subprocess.Popen(
                command, cwd=str(project), env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, **options,
            )
        except OSError as exc:
            launch_error = str(exc)
            parser.output.errors.append(CodexError(f"Avvio Codex non riuscito: {exc}"))
        else:
            threads = [
                Thread(target=read_pipe, args=(process.stdout, "stdout"), daemon=True),
                Thread(target=read_pipe, args=(process.stderr, "stderr"), daemon=True),
                Thread(target=write_stdin, args=(process.stdin,), daemon=True),
            ]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + config.timeout_codex_minuti * 60
            done = set()
            callback_failed = False
            try:
                while len(done) < 3 or process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        parser.output.errors.append(CodexError("Timeout Codex", "timeout"))
                        break
                    try:
                        kind, value = events.get(timeout=min(remaining, 0.1))
                    except Empty:
                        continue
                    if kind == "done":
                        done.add(value)
                    else:
                        handle(kind, value)
            except BaseException:
                callback_failed = True
                raise
            finally:
                if timed_out or callback_failed:
                    error = terminate_process_tree(process, environment=environment)
                    if error:
                        parser.output.errors.append(CodexError(error))
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    error = terminate_process_tree(process, environment=environment)
                    if error:
                        parser.output.errors.append(CodexError(error))
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        parser.output.errors.append(CodexError("Codex non termina dopo taskkill"))
                returncode = process.poll()
                for thread in threads:
                    thread.join(timeout=5)
                while True:
                    try:
                        kind, value = events.get_nowait()
                    except Empty:
                        break
                    handle(kind, value, callbacks=not callback_failed)
                if any(thread.is_alive() for thread in threads):
                    parser.output.errors.append(CodexError("Pipe Codex non chiuse dopo la terminazione"))
                else:
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()

    output = parser.output
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if not output.last_message and last_message_path.is_file():
        try:
            output.last_message = last_message_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            output.errors.append(CodexError(f"Ultimo messaggio Codex illeggibile: {exc}"))
    if returncode not in (None, 0) and not output.errors:
        output.errors.append(CodexError(f"Codex terminato con codice {returncode}: {stderr}"))
    if returncode == 0 and not output.turn_completed and not output.errors:
        output.errors.append(CodexError("Codex terminato senza turn.completed"))
    if returncode == 0 and output.thread_id is None:
        output.errors.append(CodexError("Codex terminato senza thread.started/thread_id"))
    limit = detect_usage_limit(
        output.errors, stderr,
        sessions=config.cartella_sessioni_codex, thread_id=output.thread_id, now=now,
        threshold_hours=config.soglia_limite_settimanale_ore,
    )
    return CodexResult(
        output=output, returncode=returncode, timed_out=timed_out, usage_limit=limit,
        jsonl_path=jsonl_path, stderr_path=stderr_path,
        last_message_path=last_message_path, prompt_path=prompt_path, stderr=stderr,
        launch_error=launch_error,
    )
