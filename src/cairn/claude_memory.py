"""``cairn claude memory import``: Claude Code auto memory as ``## Decisions`` rows.

Claude Code keeps one memory directory per project, ``<projects>/<name>/memory/``: a
``MEMORY.md`` index and one markdown file per memory, whose frontmatter holds a
one-line ``description`` and a ``type`` (``user``, ``feedback``, ``project`` or
``reference``), either at the top level or under ``metadata:``. The import reads
every file except the index and adds each memory as one row of a ``## Decisions``
table: in the session that wrote it (``originSessionId``) when cairn exported that
session, else in the project decisions document ``<data_root>/decisions/<label>.md``.

The source cell of an imported row is ``memory:<label>/<stem>``. It is the key: a
re-run adds only memories that have no row, whatever that row's state. A changed
description is reported and never rewritten (rows are append-only); a deleted memory
leaves its row; a withdrawn claim is never added again. The import never writes to
the memory directory.

Writes go through :func:`cairn.decisions.apply`, one call per target document, under
the document lock. The withdrawal ledger is read inside each edit, so the lock order
is the document first and the ledger second, as :mod:`cairn.withdrawals` requires.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from . import decisions, errors, withdrawals
from .config import get_config
from .decisions import DecisionRow
from .frontmatter import _escape_scalar
from .harnesses.claude import PROJECT_DIR_NAME_LIMIT, claude_project_dir_name, projects_root
from .mutations import FindFile
from .projects import project_name_from_dir, repo_name_from_dir
from .storage import atomic_write, document_lock, lock_path, locked

if TYPE_CHECKING:
    from .config import CairnConfig
    from .sources.base import ConversationSource

SCHEMA_VERSION: Final = 1
INDEX_NAME: Final = "MEMORY.md"
MAX_READ_BYTES: Final = 64 * 1024
TYPES: Final[tuple[str, ...]] = ("user", "feedback", "project", "reference")
KIND_BY_TYPE: Final[dict[str, str]] = {
    "feedback": "decision",
    "project": "decision",
    "user": "fact",
    "reference": "fact",
}
_STEM_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KEY_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_BLOCK_SCALARS: Final = frozenset({"|", ">", "|-", ">-", "|+", ">+"})
_ESCAPES: Final[dict[str, str]] = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}
_PLAIN_SCALAR_RE: Final = re.compile(r"^[A-Za-z0-9~/._-][^\"#\n]*$")


# --------------------------------------------------------------------------
# The memory reader
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Memory:
    stem: str
    name: str | None
    description: str | None
    type: str | None
    origin: str | None
    modified: str | None
    mtime: float


@dataclass(frozen=True)
class MemoryProblem:
    stem: str
    reason: str


class _Bad(ValueError):
    def __init__(self, line: int, problem: str):
        super().__init__(f"frontmatter line {line}: {problem}")


def _scalar(value: str, line: int) -> str:
    """One frontmatter value: double-quoted, single-quoted or plain."""
    if value[:1] in ("[", "{"):
        raise _Bad(line, "flow values are not supported")
    if value in _BLOCK_SCALARS:
        raise _Bad(line, "block scalars are not supported")
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise _Bad(line, "unterminated double-quoted value")
        out, inner, index = [], value[1:-1], 0
        while index < len(inner):
            char = inner[index]
            if char == '"':
                raise _Bad(line, "unescaped '\"' in a double-quoted value")
            if char == "\\":
                escape = inner[index + 1 : index + 2]
                if not escape:  # the closing quote was escaped
                    raise _Bad(line, "unterminated double-quoted value")
                if escape not in _ESCAPES:
                    raise _Bad(line, f"unsupported escape '\\{escape}'")
                out.append(_ESCAPES[escape])
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise _Bad(line, "unterminated single-quoted value")
        inner = value[1:-1]
        if "'" in inner.replace("''", ""):
            raise _Bad(line, 'unescaped "\'" in a single-quoted value')
        return inner.replace("''", "'")
    return value


def _split(text: str, line: int) -> tuple[str, str]:
    if text.startswith("- ") or text == "-":
        raise _Bad(line, "list items are not supported")
    key, sep, value = text.partition(":")
    key = key.strip()
    if not sep or not _KEY_RE.match(key):
        raise _Bad(line, f"expected 'key: value', got {text!r}")
    if value and not value[0].isspace():
        raise _Bad(line, f"expected 'key: value', got {text!r}")
    return key, value.strip()


def parse_head(text: str) -> dict[str, str | dict[str, str]]:
    """The frontmatter of ``text``: top-level scalars and one level of mappings.

    Raises :class:`ValueError` with ``no frontmatter`` or ``frontmatter line <n>:
    <problem>`` (``n`` is the file line).
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if not lines or lines[0] != "---":
        raise ValueError("no frontmatter")
    head: dict[str, str | dict[str, str]] = {}
    mapping: dict[str, str] | None = None
    for index, line in enumerate(lines[1:], 2):
        if line == "---":
            return head
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if line[indent : indent + 1] == "\t" or "\t" in line[:indent]:
            raise _Bad(index, "tab indentation is not supported")
        if indent == 0:
            key, value = _split(line, index)
            if value:
                head[key] = _scalar(value, index)
                mapping = None
            else:
                mapping = {}
                head[key] = mapping
        elif indent == 2 and mapping is not None:
            key, value = _split(line[2:], index)
            mapping[key] = _scalar(value, index) if value else ""
        else:
            raise _Bad(index, "unexpected indentation")
    raise _Bad(len(lines), "the frontmatter is not closed by '---'")


def read_memory(path: Path) -> Memory | MemoryProblem:
    """The memory at ``path``, or the reason it cannot be read. At most 64 KiB is read."""
    stem = path.stem
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_READ_BYTES)
        mtime = path.stat().st_mtime
    except OSError as exc:
        return MemoryProblem(stem, exc.strerror or str(exc))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if len(data) < MAX_READ_BYTES or exc.start < len(data) - 3:
            return MemoryProblem(stem, "not UTF-8")
        text = data[: exc.start].decode("utf-8")  # the read cut a character in two
    try:
        head = parse_head(text)
    except ValueError as exc:
        return MemoryProblem(stem, str(exc))
    meta = head.get("metadata")
    meta = meta if isinstance(meta, dict) else {}

    def pick(key: str) -> str | None:
        value = meta.get(key)
        if value is None:
            top = head.get(key)
            value = top if isinstance(top, str) else None
        return value or None

    name = head.get("name")
    description = head.get("description")
    return Memory(
        stem=stem,
        name=name if isinstance(name, str) else None,
        description=description if isinstance(description, str) else None,
        type=pick("type"),
        origin=pick("originSessionId"),
        modified=pick("modified"),
        mtime=mtime,
    )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


class MemoryImportError(Exception):
    """A refusal before any memory is read; ``code`` is a registry code or ``None``."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MemoryDir:
    path: Path
    label: str
    project: str
    cwd: str | None


def project_root(cwd: Path) -> Path:
    """The project root Claude Code names the memory directory after.

    The main checkout of the git repository that holds ``cwd`` (a linked worktree
    resolves to it through ``--git-common-dir``), else the repository top level, else
    ``cwd`` when git is absent or ``cwd`` is not in a repository.
    """

    def git(*argv: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *argv],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        out = done.stdout.strip()
        return out if done.returncode == 0 and out else None

    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is not None and Path(common).name == ".git":
        return Path(common).parent
    top = git("rev-parse", "--show-toplevel")
    return Path(top) if top is not None else cwd


def _label(encoded: str) -> str:
    label = repo_name_from_dir(encoded).lstrip("-")
    if not decisions.MEMORY_LABEL_RE.fullmatch(label):
        raise MemoryImportError(
            f"cannot derive a project label from the directory name {encoded!r}"
        )
    return label


def memory_dir_of(encoded: str, path: Path, cwd: str | None = None) -> MemoryDir:
    return MemoryDir(path, _label(encoded), project_name_from_dir(encoded), cwd)


def current_directory(projects: Path, cwd: Path) -> MemoryDir:
    root = project_root(cwd)
    name = claude_project_dir_name(root)
    if len(name) > PROJECT_DIR_NAME_LIMIT:
        prefix = name[:PROJECT_DIR_NAME_LIMIT]
        matches = sorted(
            p for p in projects.glob(f"{_glob_escape(prefix)}*") if _is_dir(p / "memory")
        )
        if len(matches) > 1:
            raise MemoryImportError(
                f"more than one project directory starts with {prefix}; pass --memory-dir DIR"
            )
        # Never stat the full name when nothing matches: it can exceed NAME_MAX.
        name = matches[0].name if matches else f"{prefix}*"
    path = projects / name / "memory"
    if not _is_dir(path):
        raise MemoryImportError(
            f"no Claude memory directory at {path} for {root}; pass --memory-dir DIR",
            "MEMORY_DIR_MISSING",
        )
    return memory_dir_of(name, path, str(root))


def _is_dir(path: Path) -> bool:
    """``path.is_dir()``; ``False`` for a name the file system refuses (Python 3.11
    raises ``ENAMETOOLONG`` where later versions return ``False``)."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def directories(args: argparse.Namespace, config: CairnConfig) -> list[MemoryDir]:
    store = config.collection("claude-code-sessions").require_store()
    projects = projects_root(store)
    if args.memory_dir:
        path = Path(args.memory_dir).expanduser()
        if not _is_dir(path):
            raise MemoryImportError(f"no Claude memory directory at {path}", "MEMORY_DIR_MISSING")
        resolved = path.resolve()  # `.` and `../memory` name the directory too
        encoded = resolved.parent.name if resolved.name == "memory" else resolved.name
        return [memory_dir_of(encoded, path)]
    if args.all_projects:
        if not projects.is_dir():
            raise MemoryImportError(
                f"no Claude projects directory at {projects}; pass --memory-dir DIR",
                "MEMORY_DIR_MISSING",
            )
        return [
            memory_dir_of(p.parent.name, p) for p in sorted(projects.glob("*/memory")) if p.is_dir()
        ]
    return [current_directory(projects, Path.cwd())]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class Item:
    """One memory file and what the import does with it."""

    directory: MemoryDir
    stem: str
    memory: Memory | None = None
    claim: str = ""
    kind: str = ""
    since: str = ""
    action: str = ""  # added, existing, withdrawn, reference, invalid, failed
    reason: str | None = None
    placement: str | None = None  # session or project
    target: Path | None = None
    target_ref: str = ""
    number: int | None = None
    changed: bool = False
    fingerprint: str | None = None
    heading: str | None = None  # the conversation heading of the target document

    @property
    def marker(self) -> str:
        return f"memory:{self.directory.label}/{self.stem}"

    @property
    def key(self) -> str:
        return f"{self.directory.label}/{self.stem}"


def _since(memory: Memory) -> str:
    if memory.modified:
        try:
            stamp = datetime.fromisoformat(memory.modified)
        except ValueError:
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return stamp.astimezone(UTC).date().isoformat()
    return datetime.fromtimestamp(memory.mtime, UTC).date().isoformat()


def _map(item: Item, include_reference: bool) -> None:
    """Fill the row cells of ``item`` from its memory, or set ``invalid``/``reference``."""
    memory = item.memory
    assert memory is not None
    if not _STEM_RE.fullmatch(item.stem):
        item.action, item.reason = "invalid", f"file name {item.stem!r} cannot form a marker"
        return
    if not memory.description or not memory.description.strip():
        item.action, item.reason = "invalid", "no description"
        return
    if memory.type is None:
        item.action, item.reason = "invalid", "no type"
        return
    if memory.type not in TYPES:
        item.action, item.reason = "invalid", f"unknown type '{memory.type}'"
        return
    claim = decisions.normalise_claim([memory.description])
    error = decisions.claim_error(claim)
    if error:
        item.action, item.reason = "invalid", error
        return
    try:
        item.fingerprint = withdrawals.fingerprint(claim)
    except withdrawals.EmptyClaim:
        item.action, item.reason = "invalid", "claim is empty after normalisation"
        return
    item.claim, item.kind, item.since = claim, KIND_BY_TYPE[memory.type], _since(memory)
    if memory.type == "reference" and not include_reference:
        item.action = "reference"


def read_items(directory: MemoryDir, include_reference: bool) -> list[Item]:
    items = []
    for path in sorted(directory.path.glob("*.md")):
        if path.name == INDEX_NAME or not path.is_file():
            continue
        item = Item(directory, path.stem)
        read = read_memory(path)
        if isinstance(read, MemoryProblem):
            item.action, item.reason = "invalid", read.reason
        else:
            item.memory = read
            _map(item, include_reference)
        if item.action == "invalid":
            print(f"Warning: {path}: {item.reason}; skipped", file=sys.stderr)
        items.append(item)
    return items


@dataclass(frozen=True)
class Unchecked:
    """A document whose table could not be read, so its markers are unknown.

    ``labels`` holds the labels whose ``memory:<label>/`` text appears in the file;
    ``None`` means the file could not be read at all, so any label may be in it."""

    path: Path
    problem: str
    labels: frozenset[str] | None

    def blocks(self, label: str) -> bool:
        return self.labels is None or label in self.labels


def _labels_in(path: Path, labels: set[str], heading: str | None) -> frozenset[str] | None:
    """The labels whose ``memory:<label>/`` text is in the part of ``path`` before
    its frame anchor (the whole file when it is not UTF-8); ``None`` when unreadable."""
    try:
        try:
            text = decisions._read_prefix(path, heading)
        except UnicodeDecodeError:
            text = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    prefix = decisions.MEMORY_SOURCE
    return frozenset(label for label in labels if f"{prefix}:{label}/" in text)


def _existing_markers(
    paths: dict[Path, tuple[str, str | None]],
    config: CairnConfig,
    labels: set[str],
    *,
    persist: bool,
) -> tuple[dict[str, tuple[str, DecisionRow]], list[Unchecked]]:
    """Every ``memory`` marker in the tables of ``paths`` and of every Claude record:
    ``label/stem -> (ref, row)``, and the documents whose table could not be checked.
    ``paths`` maps a path to its reference and conversation heading.

    Every record is read, whatever its project label: a session exported from a
    worktree or a subdirectory carries a different label than the memory directory,
    and a memory whose ``originSessionId`` changed must still find its old row. The
    read stops at the frame anchor, so a session costs its head, not its transcript.
    """
    from .store import SessionStore

    scan = dict(paths)
    for record in SessionStore(config).records(["claude"], persist=persist):
        scan.setdefault(record.path, (record.reference, "## Conversation"))
    # A document the catalog skipped (broken frontmatter, a missing id) still holds
    # its table, so every markdown file of the collection is read.
    output = config.output_dir("claude-code-sessions")
    if output.is_dir():
        for path in sorted(output.rglob("*.md")):
            scan.setdefault(path, (path.name, "## Conversation"))
    found: dict[str, tuple[str, DecisionRow]] = {}
    unchecked: list[Unchecked] = []
    for path, (ref, heading) in scan.items():
        if not path.exists():
            continue
        try:
            rows, problems = decisions.read_table(path, heading)
        except UnicodeDecodeError:
            hits = _labels_in(path, labels, heading)
            if hits is None or hits:
                unchecked.append(Unchecked(path, "not UTF-8", hits))
            continue
        except OSError as exc:
            unchecked.append(Unchecked(path, exc.strerror or str(exc), None))
            continue
        if problems:
            hits = _labels_in(path, labels, heading)
            if hits is None or hits:
                first = problems[0]
                unchecked.append(
                    Unchecked(path, f"## Decisions line {first.line}: {first.reason}", hits)
                )
        for row in rows:
            if row.memory is not None:
                found.setdefault(row.memory, (f"{ref}#{row.number}", row))
    return found, unchecked


def _place(item: Item, source: ConversationSource, config: CairnConfig) -> None:
    """Set the target document of a memory that is to be added."""
    memory = item.memory
    assert memory is not None
    path = None
    # Only a full session id places a row in a session: a prefix, a ``source:`` form
    # or a ``memory:`` id would route the row through another lookup.
    if memory.origin and ":" not in memory.origin:
        try:
            record = source.find_record(memory.origin)
        except FileExistsError as exc:
            item.action, item.reason = "failed", str(exc)
            print(f"Error: {exc}", file=sys.stderr)
            return
        if record is not None and record.session_id == memory.origin:
            path = record.path
    if path is not None and memory.origin:
        item.placement, item.target = "session", path
        item.target_ref = f"{source.source_name}:{memory.origin}"
        item.heading = source.conversation_heading
        return
    item.placement = "project"
    item.target = decisions.memory_document(config, item.directory.label)
    item.target_ref = f"{decisions.MEMORY_SOURCE}:{item.directory.label}"
    item.heading = None


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _frontmatter_value(value: str) -> str:
    if (
        _PLAIN_SCALAR_RE.fullmatch(value)
        and value == value.strip()
        and ": " not in value
        and not value.endswith(":")
    ):
        return value
    return f'"{_escape_scalar(value)}"'


def _home_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return "~/" + resolved.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def project_document_text(directory: MemoryDir) -> str:
    label = directory.label
    head = [
        "---",
        f"type: {decisions.MEMORY_DOC_TYPE}",
        f"native_id: {decisions.MEMORY_SOURCE}:{label}",
        f'title: "Claude memory: {label}"',
        f"project: {_frontmatter_value(directory.project)}",
    ]
    if directory.cwd and "\n" not in directory.cwd and "\r" not in directory.cwd:
        head.append(f"cwd: {_frontmatter_value(directory.cwd)}")
    memory_dir = _home_relative(directory.path)
    if "\n" not in memory_dir and "\r" not in memory_dir:
        head.append(f"memory_dir: {_frontmatter_value(memory_dir)}")
    head += ["provenance: agent", "---"]
    body = [
        "",
        f"# Claude memory: {label}",
        "",
        "## My Notes",
        "",
        "Rows imported from Claude Code auto memory by cairn claude memory import.",
        "",
        decisions.HEADING,
        "",
        decisions.HEADER,
        decisions.SEPARATOR,
    ]
    return "\n".join(head + body) + "\n"


def _create_project_document(path: Path, directory: MemoryDir) -> bool:
    """Write the template at ``path`` unless it exists; True when it was written."""
    with locked(document_lock(path)):
        if path.exists():
            return False
        atomic_write(path, project_document_text(directory))
        return True


def _remove_if_template(path: Path, directory: MemoryDir) -> None:
    """Delete a project document this run created when no row went into it (every
    memory was withdrawn or failed under the lock), so none is left empty."""
    with locked(document_lock(path)):
        try:
            if path.read_text(encoding="utf-8") == project_document_text(directory):
                path.unlink()
        except (OSError, UnicodeDecodeError):
            pass  # a document that changed or cannot be read stays


def _write_group(
    target: Path,
    group: list[Item],
    source: ConversationSource,
    ledger: Path,
) -> None:
    """Append the rows of ``group`` to ``target`` under its document lock."""
    first = group[0]
    heading = first.heading

    def edit(rows: list[DecisionRow]) -> list[DecisionRow]:
        state = withdrawals.load(ledger)  # document lock first, ledger lock second
        present = {row.memory for row in rows if row.memory is not None}
        number = max((row.number for row in rows), default=0) + 1
        new = []
        for item in group:
            item.action, item.number = "", None
            if item.key in present:
                item.action = "existing"
                continue
            assert item.fingerprint is not None
            if withdrawals.is_withdrawn(state, item.fingerprint) is not None:
                item.action = "withdrawn"
                continue
            if number > decisions.MAX_ROW_NUMBER:
                raise decisions.DecisionError(
                    f"no row number is left after #{decisions.MAX_ROW_NUMBER}"
                )
            new.append(
                DecisionRow(
                    number,
                    item.claim,
                    item.kind,
                    decisions.DEFAULT_CONFIDENCE,
                    item.since,
                    "",
                    item.marker,
                )
            )
            item.action, item.number = "added", number
            present.add(item.key)
            number += 1
        return [*rows, *new]

    find_file: FindFile
    if first.placement == "session":
        find_file, lookup = source.find_file, first.target_ref.split(":", 1)[1]
    else:
        find_file, lookup = (lambda _sid: target), first.target_ref
    said = io.StringIO()
    reason: str | None = None
    try:
        with contextlib.redirect_stderr(said):
            written = decisions.apply(
                find_file,
                lookup,
                edit,
                source=source.source_name,
                conversation_heading=heading,
                id_field=source.id_field,
            )
    except OSError as exc:
        sys.stderr.write(said.getvalue())
        _write_failed(target, exc, group)
        return
    except UnicodeDecodeError:
        reason = f"{target} is not UTF-8"
        written = None
    except withdrawals.LedgerError as exc:
        reason = str(exc)
        written = None
    sys.stderr.write(said.getvalue())
    if written is None:
        if reason is not None:
            print(f"Error: {reason}", file=sys.stderr)
        else:  # apply printed its reason as one "Error: ..." line
            lines = [ln for ln in said.getvalue().splitlines() if ln.startswith("Error: ")]
            reason = lines[-1].removeprefix("Error: ") if lines else "the table was not written"
        for item in group:
            if item.action in ("", "added"):
                item.action, item.number, item.reason = "failed", None, reason


def _write_failed(path: Path, exc: OSError, group: list[Item]) -> None:
    reason = exc.strerror or str(exc)
    print(f"Error: could not write {path}: {reason}", file=sys.stderr)
    errors.emit("MEMORY_IMPORT_WRITE_FAILED")
    for item in group:
        if item.action in ("", "added"):
            item.action, item.number, item.reason = "failed", None, reason


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def import_lock(config: CairnConfig) -> Path:
    """The lock one import holds from the marker scan to its last write, so two imports
    cannot both find a memory absent and add it to two different documents."""
    return lock_path(decisions.memory_directory(config), "memory-import")


def _fail(item: Item, reason: str) -> None:
    item.action, item.reason, item.number = "failed", reason, None


def _check_identity(found: list[MemoryDir], items: list[Item], config: CairnConfig) -> None:
    """Fail the memories of a directory whose label is not its own.

    Two directories can strip to one label (``~/Code/app`` and ``~/app``). Their
    markers would be equal, so the second directory's notes would be taken for the
    first's. Refuse both in one run, and refuse a directory whose label already has a
    project document imported from another memory directory.
    """
    from .frontmatter import read_frontmatter_head

    by_label: dict[str, list[MemoryDir]] = {}
    for directory in found:
        by_label.setdefault(directory.label, []).append(directory)
    refused: dict[Path, str] = {}
    for label, group in by_label.items():
        if len(group) > 1:
            for directory in group:
                refused[directory.path] = (
                    f"label {label} is shared by {len(group)} memory directories; "
                    "import one of them with --memory-dir DIR"
                )
            continue
        directory = group[0]
        doc = decisions.memory_document(config, label)
        if not doc.exists():
            continue
        try:
            fm = read_frontmatter_head(doc)
        except (OSError, ValueError) as exc:  # ValueError: parse and decode errors
            fm, problem = {}, str(exc) or type(exc).__name__
        else:
            problem = ""
        if (
            problem
            or fm.get("type") != decisions.MEMORY_DOC_TYPE
            or fm.get("native_id") != f"{decisions.MEMORY_SOURCE}:{label}"
        ):
            why = problem or f"type {fm.get('type')!r}, native_id {fm.get('native_id')!r}"
            refused[directory.path] = (
                f"{doc} is not a project decisions document ({why}); move it away and re-run"
            )
            continue
        recorded = fm.get("memory_dir")
        here = _home_relative(directory.path)
        if isinstance(recorded, str) and recorded != here:
            refused[directory.path] = (
                f"{doc} was imported from {recorded}, not {here}; "
                f"label {label} belongs to that directory"
            )
    for path, reason in refused.items():
        print(f"Error: {reason}", file=sys.stderr)
        for item in items:
            if item.directory.path == path and item.action == "":
                _fail(item, reason)


def _uncoded_failure(message: str, as_json: bool) -> int:
    """A refusal before any memory is read that has no registry code: ``Error:`` on
    stderr and, with ``--json``, the failure object with ``code: null`` on stdout."""
    print(f"Error: {message}", file=sys.stderr)
    if as_json:
        failure = {"ok": False, "code": None, "error": message, "exit": 1, "retryable": False}
        print(json.dumps(failure, ensure_ascii=False))
    return 1


def _ledger_state(ledger: Path, *, lock: bool) -> dict[str, withdrawals.Withdrawal]:
    if lock:
        return withdrawals.load(ledger)
    return withdrawals._read(ledger) if ledger.exists() else {}  # a dry run takes no lock


def _short(ref: str) -> str:
    """``claude:<uuid>[#n]`` as ``claude:<first 8>[#n]``; ``memory:<label>[#n]`` as is."""
    document, hash_, number = ref.partition("#")
    source, colon, native = document.partition(":")
    if colon and source != decisions.MEMORY_SOURCE:
        document = f"{source}:{native[:8]}"
    return document + hash_ + number


def _row_json(item: Item, dry_run: bool) -> dict:
    written = item.action == "added" and not dry_run and item.number is not None
    return {
        "memory": item.key,
        "action": item.action,
        "placement": item.placement,
        "ref": f"{item.target_ref}#{item.number}" if written else None,
        "kind": item.kind or None,
        "since": item.since or None,
        "path": str(item.target) if item.target is not None else None,
        "changed": item.changed,
        "reason": item.reason,
    }


def cmd_memory_import(source: ConversationSource, args: argparse.Namespace) -> int:
    """``cairn claude memory import``; see the module docstring."""
    as_json = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False)) or as_json
    dry_run = bool(args.dry_run)
    config = get_config()
    try:
        found = directories(args, config)
    except MemoryImportError as exc:
        if exc.code is None:
            return _uncoded_failure(str(exc), as_json)
        return errors.fail(exc.code, str(exc), exit=1, prefix="Error: ", as_json=as_json)
    try:
        with contextlib.nullcontext() if dry_run else locked(import_lock(config)):
            return _import(source, args, config, found, as_json=as_json, quiet=quiet)
    finally:
        if dry_run:
            source._catalog = None  # a dry run leaves the catalog cache as it was


def _import(
    source: ConversationSource,
    args: argparse.Namespace,
    config: CairnConfig,
    found: list[MemoryDir],
    *,
    as_json: bool,
    quiet: bool,
) -> int:
    dry_run = bool(args.dry_run)
    ledger = withdrawals.ledger_path(config)
    try:
        state = _ledger_state(ledger, lock=not dry_run)
    except withdrawals.LedgerError as exc:
        return _uncoded_failure(str(exc), as_json)

    items: list[Item] = []
    for directory in found:
        items.extend(read_items(directory, bool(args.include_reference)))
    _check_identity(found, items, config)
    for item in items:
        if item.action == "":
            _place(item, source, config)
    valid = [i for i in items if i.action in ("", "reference")]

    targets: dict[Path, tuple[str, str | None]] = {}
    for item in valid:
        if item.target is not None:
            targets[item.target] = (item.target_ref, item.heading)
    for directory in found:
        doc = decisions.memory_document(config, directory.label)
        targets.setdefault(doc, (f"{decisions.MEMORY_SOURCE}:{directory.label}", None))
    markers, unchecked = _existing_markers(
        targets, config, {d.label for d in found}, persist=not dry_run
    )
    for blocked in unchecked:
        print(
            f"Error: cannot check {blocked.path} for imported memories: {blocked.problem}",
            file=sys.stderr,
        )

    for item in valid:
        existing = markers.get(item.key)
        if existing is not None:
            ref, row = existing
            item.action = "existing"
            if row.state == "active" and withdrawals.normalise_claim(
                row.claim
            ) != withdrawals.normalise_claim(item.claim):
                item.changed = True
                print(
                    f"Warning: memory {item.key} changed since it was imported as {_short(ref)}; "
                    "supersede that row to update it",
                    file=sys.stderr,
                )
            continue
        if item.action == "reference":
            continue
        blocker = next((d for d in unchecked if d.blocks(item.directory.label)), None)
        if blocker is not None:
            _fail(item, f"cannot check {blocker.path} for imported memories: {blocker.problem}")
            continue
        assert item.fingerprint is not None
        if withdrawals.is_withdrawn(state, item.fingerprint) is not None:
            item.action = "withdrawn"
            continue
        item.action = "added"

    pending = [i for i in items if i.action == "added"]
    if not dry_run:
        groups: dict[Path, list[Item]] = {}
        for item in pending:
            assert item.target is not None
            groups.setdefault(item.target, []).append(item)
        for target, group in groups.items():
            created = False
            if group[0].placement == "project":
                try:
                    created = _create_project_document(target, group[0].directory)
                except OSError as exc:
                    _write_failed(target, exc, group)
                    continue
            _write_group(target, group, source, ledger)
            if created and not any(item.action == "added" for item in group):
                _remove_if_template(target, group[0].directory)

    for item in items:
        memory = item.memory
        if item.action != "added" or item.placement != "project" or memory is None:
            continue
        if memory.origin:
            verb = "would add to" if dry_run else "added to"
            print(
                f"Warning: session {memory.origin[:8]} of memory {item.key} is not exported; "
                f"{verb} decisions/{item.directory.label}.md",
                file=sys.stderr,
            )
    if not dry_run and "decisions" not in config.collections:
        written_docs = sorted(
            {str(i.target) for i in items if i.action == "added" and i.placement == "project"}
        )
        for path in written_docs:
            print(
                f"Warning: collections.decisions is not configured; QMD will not index {path}",
                file=sys.stderr,
            )

    counts = {
        "added": sum(i.action == "added" for i in items),
        "skipped_existing": sum(i.action == "existing" for i in items),
        "skipped_withdrawn": sum(i.action == "withdrawn" for i in items),
        "skipped_reference": sum(i.action == "reference" for i in items),
        "unknown_session": sum(
            i.action in ("added", "failed") and i.placement == "project" for i in items
        ),
        "changed": sum(i.changed for i in items),
        "invalid": sum(i.action == "invalid" for i in items),
        "failed": sum(i.action == "failed" for i in items),
    }
    exit_code = 1 if counts["invalid"] or counts["failed"] else 0

    if as_json:
        data = {
            "schema_version": SCHEMA_VERSION,
            "dry_run": dry_run,
            "directories": [
                {"memory_dir": _home_relative(d.path), "label": d.label, "project": d.project}
                for d in found
            ],
            **counts,
            "rows": [_row_json(i, dry_run) for i in items],
        }
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return exit_code
    if not quiet:
        for item in items:
            if item.action != "added":
                continue
            if dry_run:
                where = _short(item.target_ref)
                prefix = "Would add"
            else:
                where = f"{_short(item.target_ref)}#{item.number}"
                prefix = "Added"
            print(f"{prefix} {where}  {item.kind}  {item.since}  {item.marker}  {item.claim}")
    if dry_run:
        print(f"Dry run: would import {counts['added']} memories; nothing written")
        return exit_code
    sessions = sum(i.action == "added" and i.placement == "session" for i in items)
    summary = (
        f"Imported {counts['added']} memories ({sessions} into sessions, "
        f"{counts['added'] - sessions} into project documents); skipped "
        f"{counts['skipped_existing']} existing, {counts['skipped_withdrawn']} withdrawn, "
        f"{counts['skipped_reference']} reference, {counts['invalid']} invalid"
    )
    if counts["failed"]:
        summary += f"; {counts['failed']} failed"
    print(summary)
    return exit_code


def add_parser(
    subparsers: argparse._SubParsersAction,
    common: argparse.ArgumentParser,
    source: ConversationSource,
) -> None:
    """``cairn claude memory import`` on the Claude source's subparsers."""
    p_memory = subparsers.add_parser("memory", help="Claude Code auto memory")
    memory_sub = p_memory.add_subparsers(dest="memory_command", metavar="COMMAND", required=True)
    p_import = memory_sub.add_parser(
        "import",
        parents=[common],
        help="add auto-memory notes as ## Decisions rows",
        description=(
            "Add each Claude Code auto-memory note of the current project as one "
            "## Decisions row: in the session that wrote it when cairn exported that "
            "session, else in <data_root>/decisions/<project>.md. A re-run adds only new "
            "memories. The memory directory is never written."
        ),
    )
    where = p_import.add_mutually_exclusive_group()
    where.add_argument(
        "--all-projects", action="store_true", help="every <projects>/*/memory directory"
    )
    where.add_argument(
        "--memory-dir", metavar="DIR", help="exactly this memory directory (autoMemoryDirectory)"
    )
    p_import.add_argument(
        "--include-reference",
        action="store_true",
        help="also import 'reference' memories, as kind fact",
    )
    p_import.add_argument(
        "--dry-run", action="store_true", help="print the plan; write nothing, take no lock"
    )
    p_import.set_defaults(func=lambda args: cmd_memory_import(source, args))


__all__ = [
    "Memory",
    "MemoryProblem",
    "add_parser",
    "claude_project_dir_name",
    "cmd_memory_import",
    "parse_head",
    "project_document_text",
    "project_root",
    "read_memory",
]
