"""Parser del formato Markdown delle task e selezione non distruttiva dalla coda.

Il front matter supporta coppie chiave: valore semplici, non YAML completo.
I numeri dei pezzi devono essere consecutivi a partire da 1. Liste annidate e
blocchi di codice appartengono al pezzo che li contiene.
"""

from dataclasses import dataclass
from pathlib import Path
import re


PRIORITIES = {"alta": 0, "media": 1, "bassa": 2}
REQUIRED_SECTIONS = ("contesto", "obiettivo", "passi", "verifica", "fuori perimetro")
_HEADING = re.compile(r"^\s{0,3}##(?!#)\s+(.+?)\s*$")
_STEP = re.compile(r"^([ \t]*)(\d+)[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


class TaskError(ValueError):
    """Fascicolo illeggibile o non valido; include il percorso sorgente."""


@dataclass(frozen=True)
class Piece:
    numero: int
    testo: str


@dataclass(frozen=True)
class Task:
    file: Path
    id: str
    titolo: str
    progetto: Path
    branch: str
    priorita: str
    push: bool
    fascicolo: str
    metadata: dict[str, str]
    sezioni: dict[str, str]
    pezzi: tuple[Piece, ...]
    comandi_verifica: tuple[str, ...]


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _fence_state(line: str, current: str | None) -> str | None:
    match = _FENCE.match(line)
    if not match:
        return current
    marker, suffix = match.groups()
    if current is None:
        return marker
    if marker[0] == current[0] and len(marker) >= len(current) and not suffix.strip():
        return None
    return current


def _sections(lines: list[str]) -> dict[str, str]:
    collected: dict[str, list[str]] = {}
    name = None
    fence = None
    for line in lines:
        match = _HEADING.match(line) if fence is None else None
        if match:
            name = _normalize(re.sub(r"\s+#+\s*$", "", match[1]))
            if name in collected:
                raise ValueError(f"Sezione duplicata: {name}")
            collected[name] = []
        elif name is not None:
            collected[name].append(line)
        fence = _fence_state(line, fence)
    missing = [name for name in REQUIRED_SECTIONS if name not in collected]
    if missing:
        raise ValueError(f"Sezioni mancanti: {', '.join(missing)}")
    return {name: "\n".join(body).strip("\n") for name, body in collected.items()}


def _pieces(body: str) -> tuple[Piece, ...]:
    pieces = []
    content: list[str] = []
    number = None
    base_indent = None
    fence = None
    for line in body.splitlines():
        match = _STEP.match(line) if fence is None else None
        indent = len(match[1].expandtabs(4)) if match else None
        if match and (base_indent is None or indent <= base_indent):
            if base_indent is not None and indent != base_indent:
                raise ValueError("Indentazione incoerente nell'elenco dei passi")
            if number is not None:
                pieces.append(Piece(number, "\n".join(content).strip()))
            number = int(match[2])
            if number != len(pieces) + 1:
                raise ValueError("I passi devono essere numerati consecutivamente da 1")
            base_indent = indent
            content = [match[3]]
        elif number is not None:
            content.append(line)
        elif line.strip():
            raise ValueError("Testo fuori dall'elenco numerato nella sezione Passi")
        # Il contenuto della prima riga può già aprire un blocco di codice.
        fence = _fence_state(match[3] if match and indent == base_indent else line, fence)
    if number is not None:
        pieces.append(Piece(number, "\n".join(content).strip()))
    if not pieces:
        raise ValueError("La sezione Passi deve contenere almeno un pezzo")
    return tuple(pieces)


def _commands(body: str) -> tuple[str, ...]:
    commands = []
    fence = None
    for line in body.splitlines():
        next_fence = _fence_state(line, fence)
        if next_fence != fence:
            fence = next_fence
            continue
        if line.strip():
            commands.append(line.strip())
    return tuple(commands)


def parse_task(text: str, source: str | Path = "<testo>") -> Task:
    """Valida il fascicolo senza accedere al progetto né eseguire comandi.

    Conserva il testo originale per i futuri prompt e il percorso progetto come
    Path, senza risolverlo né effettuare controlli git o di esistenza.
    """
    source = Path(source)
    try:
        lines = text.lstrip("\ufeff").splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip()), None)
        if start is None or lines[start].strip() != "---":
            raise ValueError("Front matter iniziale --- mancante")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip() == "---"),
            None,
        )
        if end is None:
            raise ValueError("Chiusura --- del front matter mancante")
        metadata = {}
        for line in lines[start + 1:end]:
            if not line.strip():
                continue
            key, separator, value = line.partition(":")
            key = _normalize(key)
            if not separator or not key:
                raise ValueError(f"Riga front matter non valida: {line}")
            if key in metadata:
                raise ValueError(f"Chiave front matter duplicata: {key}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
            metadata[key] = value
        for key in ("id", "titolo", "progetto", "branch", "priorita"):
            if not metadata.get(key):
                raise ValueError(f"Campo obbligatorio mancante o vuoto: {key}")
        priority = _normalize(metadata["priorita"])
        if priority not in PRIORITIES:
            raise ValueError("priorita deve essere alta, media o bassa")
        push = _normalize(metadata.get("push", "si"))
        if push not in ("si", "no"):
            raise ValueError("push deve essere si o no")
        sections = _sections(lines[end + 1:])
        return Task(
            file=source, id=metadata["id"], titolo=metadata["titolo"],
            progetto=Path(metadata["progetto"]), branch=metadata["branch"],
            priorita=priority, push=push == "si", fascicolo=text,
            metadata=metadata, sezioni=sections, pezzi=_pieces(sections["passi"]),
            comandi_verifica=_commands(sections["verifica"]),
        )
    except ValueError as exc:
        raise TaskError(f"{source}: {exc}") from exc


def load_task(path: str | Path) -> Task:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TaskError(f"Impossibile leggere {path}: {exc}") from exc
    return parse_task(text, path)


def scan_queue(queue: str | Path) -> tuple[list[Task], list[tuple[Path, str]]]:
    """Restituisce task valide ordinate e fascicoli non validi con il motivo.

    Non sposta né modifica file. Solo gli errori di accesso alla directory
    sollevano TaskError; un singolo fascicolo non interrompe la scansione.
    """
    queue = Path(queue)
    try:
        paths = sorted(
            (path for path in queue.iterdir() if path.is_file() and path.suffix.lower() == ".md"),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise TaskError(f"Impossibile leggere la coda {queue}: {exc}") from exc
    tasks = []
    errors = []
    for path in paths:
        try:
            tasks.append(load_task(path))
        except TaskError as exc:
            errors.append((path, str(exc)))
    tasks.sort(
        key=lambda task: (PRIORITIES[task.priorita], task.file.name.casefold(), task.file.name)
    )
    return tasks, errors


def select_next_task(queue: str | Path) -> Task | None:
    """Scorciatoia per la prima task valida; usare scan_queue per gli errori."""
    tasks, _ = scan_queue(queue)
    return tasks[0] if tasks else None
