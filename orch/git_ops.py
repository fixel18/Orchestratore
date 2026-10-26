"""Operazioni git dell'orchestratore: mai checkout, force o riscritture."""

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import subprocess

from .codex import clean_child_environment
from .config import Config
from .git_paths import project_pathspecs
from .queries import query_sort_key


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitOperations:
    def __init__(self, config: Config, *, logger=None, runner=None):
        self.config = config
        self.logger = logger or (lambda message: None)
        self.runner = subprocess.run if runner is None else runner

    def command(self, project: str | Path, arguments: list[str], *, check=True) -> GitResult:
        environment = clean_child_environment(self.config.variabili_da_rimuovere)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = self.runner(
                ["git", *arguments], cwd=str(project), env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.config.timeout_verifica_minuti * 60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {arguments[0]} non riuscito: {exc}") from exc
        output = GitResult(result.returncode, result.stdout.decode("utf-8", errors="replace"),
                           result.stderr.decode("utf-8", errors="replace"))
        self.logger(f"GIT {arguments!r}\n{output.stdout}\n{output.stderr}")
        if check and output.returncode:
            raise GitError(f"git {arguments[0]} fallito ({output.returncode}): {output.stderr or output.stdout}")
        return output

    def is_repo(self, project):
        result = self.command(project, ["rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def branch(self, project):
        return self.command(project, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def dirty(self, project, *, exclude_paths=()):
        return bool(self.command(project, ["status", "--porcelain", "--untracked-files=all",
                                           *project_pathspecs(self.config, exclude_paths)]).stdout.strip())

    def preexisting_changes(self, project):
        output = self.command(project, ["status", "--porcelain", "--untracked-files=all", "-z",
                                       *project_pathspecs(self.config)]).stdout
        records = iter(output.split("\0"))
        paths = set()
        for record in records:
            if not record:
                continue
            paths.add(record[3:])
            if "R" in record[:2] or "C" in record[:2]:
                paths.add(next(records))  # -z: destinazione, poi origine.
        return {name: self.fingerprint(project, name) for name in sorted(paths)}

    def fingerprint(self, project, name):
        path = Path(project) / name
        if any(part.casefold().startswith(".env") and part != ".env.example"
               for part in Path(name).parts):
            raise GitError("File riservato tra le modifiche preesistenti; configurare escludi_dal_commit")
        try:
            if path.is_symlink():
                data = os.fsencode(os.readlink(path))
            elif not path.resolve().is_relative_to(Path(project).resolve()):
                raise GitError("Percorso preesistente fuori dal progetto")
            else:
                data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GitError(f"Impronta del file preesistente non disponibile: {name}: {exc}") from exc
        return hashlib.sha256(data).hexdigest()

    def head(self, project):
        result = self.command(project, ["rev-parse", "--verify", "HEAD"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def commit_info(self, project):
        result = self.command(project, ["show", "-s", "--format=%H%n%P%n%B", "HEAD"])
        lines = result.stdout.splitlines()
        return {"hash": lines[0], "parents": lines[1].split(), "subject": "\n".join(lines[2:]).rstrip("\n")}

    def add(self, project):
        self.command(project, ["add", "-A", *project_pathspecs(self.config)])

    def added_queries(self, project, before, commit):
        arguments = (["diff", "--name-only", "--diff-filter=A", "-z", before, commit]
                     if before else ["diff-tree", "--root", "--no-commit-id", "--name-only",
                                     "--diff-filter=A", "-r", "-z", commit])
        result = self.command(project, [*arguments, "--", f":(literal){self.config.cartella_query}"])
        return sorted((name for name in result.stdout.split("\0")
                       if name and Path(name).suffix.lower() == ".sql"), key=query_sort_key)

    def commit(self, project, message):
        try:
            self.command(project, ["commit", "-m", message])
        except GitError as exc:
            if any(word in str(exc).lower() for word in ("identity", "user.name", "user.email", "auto-detect")):
                raise GitError(f"Identità git mancante: configurare user.name e user.email nel progetto. {exc}") from exc
            raise

    def has_origin(self, project):
        return self.command(project, ["remote", "get-url", "origin"], check=False).returncode == 0

    def push(self, project, branch):
        self.command(project, ["push", "origin", branch])

    def latest_stash(self, project):
        result = self.command(project, ["stash", "list", "-1", "--format=%H%n%gd%n%s"])
        lines = result.stdout.splitlines()
        return {"hash": lines[0], "name": lines[1], "subject": "\n".join(lines[2:])} if len(lines) >= 3 else None

    def stash(self, project, message, *, exclude_paths=()):
        self.command(project, ["stash", "push", "-u", "-m", message,
                               *project_pathspecs(self.config, exclude_paths)])
