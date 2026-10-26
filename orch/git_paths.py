"""Pathspec condivisi: lascia a Git l'interpretazione dei glob configurati."""

from .config import Config


def project_pathspecs(config: Config, exclude_paths=()) -> list[str]:
    return ["--", ".", *(f":(exclude,glob){pattern}" for pattern in config.escludi_dal_commit),
            *(f":(exclude,literal){path}" for path in exclude_paths)]
