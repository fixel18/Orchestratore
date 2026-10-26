"""Diff e prompt per la revisione Claude, senza leggere file .env*."""

from collections.abc import Sequence
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess

from .codex import clean_child_environment
from .config import Config
from .git_paths import project_pathspecs
from .verification import VerificationResult, verification_summary


MAX_REVIEW_CHARS = 60000
MAX_UNTRACKED_BYTES = 200 * 1024
_TRUNCATED = "\n[TRONCATO: raggiunto il limite di caratteri]\n"


class ReviewDiffError(RuntimeError):
    """Diff non raccoglibile: il chiamante non deve approvare al suo posto."""


def truncate_review_text(text: str, max_chars: int = MAX_REVIEW_CHARS) -> str:
    if max_chars < len(_TRUNCATED):
        raise ValueError("Limite troppo piccolo per indicare il taglio")
    if len(text) <= max_chars:
        return text
    return text[:max_chars - len(_TRUNCATED)] + _TRUNCATED


def _git_output(config: Config, project: Path, arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=str(project),
            env=clean_child_environment(config.variabili_da_rimuovere),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=config.timeout_verifica_minuti * 60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewDiffError(f"Impossibile raccogliere il diff: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace")
        raise ReviewDiffError(f"git {arguments[0]} fallito ({result.returncode}): {message}")
    return result.stdout


def collect_review_diff(
    config: Config, project: str | Path, *, max_chars: int = MAX_REVIEW_CHARS, exclude_paths=(),
) -> str:
    """Diff HEAD e file untracked; limita dimensioni e accessi al progetto.

    I pathspec escludono anche .env* tracciati prima che git ne legga il diff.
    I nomi non tracciati sono NUL-separated: spazi e newline restano sicuri.
    Link, percorsi esterni, binari e file grandi sono indicati senza contenuto.
    """
    project = Path(project).resolve()
    pathspecs = [*project_pathspecs(config, exclude_paths),
                 ":(glob,exclude,icase)**/.env*", ":(glob,exclude,icase).env*"]
    tracked = _git_output(config, project, [
        "diff", "--no-ext-diff", "--no-textconv", "HEAD", *pathspecs,
    ]).decode("utf-8", errors="replace")
    names = _git_output(config, project, ["ls-files", "--others", "--exclude-standard", "-z", *pathspecs])
    chunks = ["## Diff dei file tracciati\n", tracked, "\n## File non tracciati\n"]
    length = sum(map(len, chunks))
    for name in names.decode("utf-8", errors="replace").split("\0"):
        if not name:
            continue
        posix = PurePosixPath(name)
        windows = PureWindowsPath(name)
        if any(part.casefold().startswith(".env") for part in (*posix.parts, *windows.parts)):
            continue  # Nessuno stat/open del file; niente nome o contenuto nel prompt.
        label = f"\n### File non tracciato: {json.dumps(name, ensure_ascii=False)}\n"
        if length + len(label) >= max_chars:
            chunks.append(label + _TRUNCATED)
            break
        path = project / name
        try:
            if (posix.is_absolute() or windows.drive or windows.root
                    or ".." in posix.parts or ".." in windows.parts
                    or path.is_symlink() or not path.resolve().is_relative_to(project)):
                body = "[omesso: link o percorso non sicuro]\n"
            elif not path.is_file():
                body = "[omesso: file non disponibile]\n"
            elif path.stat().st_size > MAX_UNTRACKED_BYTES:
                body = "[omesso: file oltre 200 KB]\n"
            else:
                with path.open("rb") as stream:
                    data = stream.read(MAX_UNTRACKED_BYTES + 1)
                if len(data) > MAX_UNTRACKED_BYTES:
                    body = "[omesso: file oltre 200 KB]\n"
                elif b"\0" in data or any(byte < 32 and byte not in (9, 10, 12, 13) for byte in data):
                    body = "[omesso: file binario]\n"
                else:
                    try:
                        body = data.decode("utf-8-sig") + "\n"
                    except UnicodeError:
                        body = "[omesso: file binario o non UTF-8]\n"
        except OSError as exc:
            body = f"[omesso: file non leggibile ({exc.__class__.__name__})]\n"
        chunks.extend((label, body))
        length += len(label) + len(body)
        if length > max_chars:
            break
    return truncate_review_text("".join(chunks), max_chars)


def build_claude_prompt(
    config: Config, dossier: str, piece_number: int, piece: str,
    last_codex_response: str, verifications: Sequence[VerificationResult], diff: str,
    *, max_chars: int = MAX_REVIEW_CHARS, preexisting_paths=(), touched_preexisting_paths=(),
) -> str:
    """Il tetto riguarda l'intero prompt, con indicazione esplicita del taglio."""
    instructions = (config.cartella_base / "regole" / "prompt-verifica-claude.md").read_text(
        encoding="utf-8-sig"
    )
    if preexisting_paths:
        instructions += ("\n\nModifiche dell'utente, non da revisionare: " + ", ".join(preexisting_paths))
    if touched_preexisting_paths:
        instructions += ("\n\nFile preesistenti toccati durante la task, da revisionare: "
                         + ", ".join(touched_preexisting_paths))
    prompt = (
        f"{instructions.rstrip()}\n\n# FASCICOLO DELLA TASK\n{dossier}\n\n"
        f"# PEZZO {piece_number}\n{piece}\n\n"
        f"# ULTIMA RISPOSTA CODEX\n{last_codex_response}\n\n"
        f"# VERIFICHE\n{json.dumps(verification_summary(verifications), ensure_ascii=False)}\n\n"
        f"# DIFF PER LA REVISIONE\n{diff}\n"
    )
    return truncate_review_text(prompt, max_chars)
