"""Invii Claude in scrittura o revisioni isolate in sola lettura."""

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from .claude_limits import claude_reset_time, is_authentication_error, is_claude_limit, is_weekly_limit
from .claude_stream import parse_claude_stream
from .codex import clean_child_environment, terminate_process_tree
from .codex_limits import UsageLimit, osservazione_vuota
from .config import Config
from .review import truncate_review_text
from .live_activity import activity_metadata


@dataclass(frozen=True)
class ClaudeReview:
    kind: str  # ok, correzioni, non_riconosciuto, limite, errore_autenticazione, errore
    text: str
    details: str = ""
    retry_at: datetime | None = None
    error: str | None = None

    @property
    def approved(self) -> bool:
        return self.kind == "ok"


DISALLOWED_TOOLS = [
    *(f"Bash(git {command}:*)" for command in ("commit", "push", "checkout", "switch", "reset", "stash", "rebase")),
    "Bash(supabase:*)", "Bash(psql:*)", "Read(**/.env*)", "Edit(**/.env*)", "Write(**/.env*)",
]


def build_claude_command(config: Config, impostazioni=None, *, esecutore=False, conversazione=None) -> list[str]:
    settings = impostazioni or {"modello": "claude-opus-5-5", "ragionamento": "medium"}
    command = [*config.comando_claude, "-p", "--output-format", "stream-json", "--verbose",
               "--model", settings["modello"], "--effort", settings["ragionamento"]]
    if esecutore:
        command += ["--permission-mode", "acceptEdits", "--allowedTools",
                    "Read", "Edit", "Write", "Glob", "Grep", "Bash",
                    "--disallowedTools", *DISALLOWED_TOOLS]
        if conversazione is not None:
            if not isinstance(conversazione, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", conversazione):
                raise ValueError("session_id Claude non valido")
            command += ["--resume", conversazione]
    else:
        if conversazione is not None:
            raise ValueError("Il controllore Claude deve usare una conversazione nuova")
        command += ["--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk", "--no-session-persistence"]
    return command


def _is_stream(raw):
    for line in raw.splitlines():
        try:
            event = json.loads(line.lstrip("\ufeff"))
        except ValueError:
            continue
        if isinstance(event, dict) and "type" in event:
            return True
    return False


def _stream_review(output, *, stderr, returncode, now, esecutore=False):
    diagnostic = output.text + "\n" + stderr
    if (output.is_error or returncode != 0 or not output.result_received) and is_authentication_error(diagnostic):
        return ClaudeReview("errore_autenticazione", output.text, error=diagnostic.strip())
    if output.usage_limit is not None:
        return ClaudeReview("limite", output.text or output.usage_limit.message, retry_at=output.usage_limit.retry_at)
    # I dettagli di una revisione riuscita possono citare login e limiti.
    if output.is_error or returncode != 0 or not output.result_received:
        if is_claude_limit(diagnostic):
            return ClaudeReview("limite", output.text, retry_at=claude_reset_time(diagnostic, now=now))
    if output.errors or output.is_error:
        return ClaudeReview("errore", output.text, error="; ".join(output.errors) or diagnostic.strip())
    if not output.result_received:
        return ClaudeReview("errore", output.text, error="Claude terminato senza evento result")
    if esecutore and returncode == 0:
        return ClaudeReview("risposta", output.text)
    return parse_claude_response(json.dumps({"result": output.text, "is_error": False}),
                                 stderr=stderr, returncode=returncode, now=now)


def parse_claude_response(
    raw: str, *, stderr: str = "", returncode: int = 0, now: datetime | None = None,
) -> ClaudeReview:
    if _is_stream(raw):
        return _stream_review(parse_claude_stream(raw), stderr=stderr, returncode=returncode, now=now)
    text = raw
    is_error = False
    malformed = None
    try:
        envelope = json.loads(raw.lstrip("\ufeff"))
        if (not isinstance(envelope, dict) or not isinstance(envelope.get("result"), str)
                or type(envelope.get("is_error")) is not bool):
            raise ValueError("Attesi result stringa e is_error booleano")
        text, is_error = envelope["result"], envelope["is_error"]
    except ValueError as exc:
        malformed = f"Risposta JSON Claude non valida: {exc}"
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    details = "\n".join(lines[1:]).strip()
    # Una revisione valida può citare login o limiti come argomento del codice.
    if not malformed and not is_error and returncode == 0:
        if first == "ESITO: OK":
            return ClaudeReview("ok", text, details)
        if first == "ESITO: CORREZIONI":
            return ClaudeReview("correzioni", text, details)
    diagnostic = f"{text}\n{stderr}"
    if is_authentication_error(diagnostic):
        return ClaudeReview("errore_autenticazione", text, error=diagnostic.strip())
    if is_claude_limit(diagnostic, is_error=is_error):
        return ClaudeReview("limite", text, retry_at=claude_reset_time(diagnostic, now=now))
    if returncode != 0:
        return ClaudeReview("errore", text, error=f"Claude terminato con codice {returncode}: {diagnostic}")
    return ClaudeReview("non_riconosciuto", text, details, error=malformed)


@dataclass(frozen=True)
class ClaudeResult:
    review: ClaudeReview
    returncode: int | None
    timed_out: bool
    prompt_path: Path
    response_path: Path
    stderr_path: Path
    raw_response: str
    stderr: str
    errors: tuple[str, ...]
    launch_error: str | None = None
    session_id: str | None = None
    last_message: str = ""
    usage_limit: UsageLimit | None = None
    osservazione_utilizzo: dict = field(default_factory=osservazione_vuota)


def run_claude(
    config: Config, project: str | Path, prompt: str, *,
    log_label: str = "claude", now: datetime | None = None,
    impostazioni=None, esecutore=False, conversazione=None, on_session=None,
) -> ClaudeResult:
    """Prompt su stdin UTF-8 e risposta grezza su disco, anche sul timeout.

    Le revisioni sono isolate; gli esecutori possono riprendere una sessione.
    Il ciclo decide rimandi e attese. Gli eventi grezzi restano nei log.
    """
    if not esecutore:
        prompt = truncate_review_text(prompt)
    command = build_claude_command(config, impostazioni, esecutore=esecutore, conversazione=conversazione)
    logs = config.cartella_base / "log"
    logs.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_-]", "_", log_label)[:80] or "claude"
    stem = f"{label}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex}"
    prompt_path = logs / f"{stem}.prompt.txt"
    response_path = logs / f"{stem}.response.json"
    stderr_path = logs / f"{stem}.stderr.log"
    prompt_path.write_bytes(prompt.encode("utf-8"))
    activity_metadata(logs / f"{stem}.activity.json", "claude", "esecutore" if esecutore else "controllore")
    stdout, stderr = b"", b""
    errors = []
    timed_out = False
    returncode = None
    launch_error = None
    drained = False
    environment = clean_child_environment(config.variabili_da_rimuovere)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {
        "start_new_session": True,
    }

    def stop(process):
        error = terminate_process_tree(process, environment=environment)
        if error:
            errors.append(error)

    # stdout/stderr go straight to files: the panel can tail them while Claude works.
    output_file = response_path.open("wb")
    error_file = stderr_path.open("wb")
    try:
        try:
            process = subprocess.Popen(
                command, cwd=str(project), env=environment, shell=False,
                stdin=subprocess.PIPE, stdout=output_file, stderr=error_file, **options,
            )
        except OSError as exc:
            launch_error = str(exc)
            errors.append(f"Avvio Claude non riuscito: {exc}")
        else:
            try:
                try:
                    stdout, stderr = process.communicate(
                        input=prompt.encode("utf-8"), timeout=config.timeout_claude_minuti * 60,
                    )
                    drained = True
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    errors.append("Timeout Claude")
                    stdout, stderr = exc.output or b"", exc.stderr or b""
                    stop(process)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                        drained = True
                    except subprocess.TimeoutExpired as tail:
                        stdout = tail.output if tail.output is not None else stdout
                        stderr = tail.stderr if tail.stderr is not None else stderr
                        stop(process)
                        errors.append("Pipe Claude non chiuse dopo la terminazione")
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            errors.append("Claude non termina dopo taskkill")
                returncode = process.poll()
            except BaseException:
                stop(process)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise
            finally:
                # Non chiudere una pipe con un reader ancora bloccato su Windows.
                if drained:
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
    finally:
        output_file.close()
        error_file.close()
        # Mocked/legacy transports may still return captured bytes from communicate.
        if stdout:
            response_path.write_bytes(stdout)
        if stderr:
            stderr_path.write_bytes(stderr)
    stdout = response_path.read_bytes()
    stderr = stderr_path.read_bytes()
    raw = stdout.decode("utf-8", errors="replace")
    diagnostic = stderr.decode("utf-8", errors="replace")
    output = parse_claude_stream(raw, session_id=conversazione, on_session=on_session) if _is_stream(raw) else None
    if errors:
        review = ClaudeReview("errore", raw, error="; ".join(errors))
    else:
        review = (_stream_review(output, stderr=diagnostic, returncode=returncode, now=now, esecutore=esecutore)
                  if output is not None else parse_claude_response(raw, stderr=diagnostic, returncode=returncode, now=now))
    limit = output.usage_limit if output is not None else None
    if limit is None and review.kind == "limite":
        limit = UsageLimit(review.text, review.retry_at, "testo" if review.retry_at else "sconosciuto",
                           is_weekly_limit(review.text + "\n" + diagnostic, review.retry_at,
                               now=now or datetime.now().astimezone(), threshold_hours=config.soglia_limite_settimanale_ore))
    if output is not None:
        errors.extend(output.errors)
        if esecutore and returncode == 0 and not output.session_id:
            errors.append("Claude terminato senza session_id")
    elif esecutore and launch_error is None and not timed_out:
        errors.append("Claude non ha restituito eventi stream-json con session_id e result")
    return ClaudeResult(
        review=review, returncode=returncode, timed_out=timed_out,
        prompt_path=prompt_path, response_path=response_path, stderr_path=stderr_path,
        raw_response=raw, stderr=diagnostic, errors=tuple(errors),
        launch_error=launch_error,
        session_id=output.session_id if output is not None else conversazione,
        last_message=output.text if output is not None else review.text,
        usage_limit=limit,
        osservazione_utilizzo=output.usage if output is not None else osservazione_vuota(),
    )
