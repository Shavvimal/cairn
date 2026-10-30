"""Register QMD context descriptions for a session collection.

Identical across sources except for the collection name; the project and repo
descriptions come from the shared catalog in :mod:`cairn.config`. Every QMD
mutation goes through the ``qmd`` CLI (never by editing its index file directly),
so QMD owns its own state.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import get_config, state_home
from .procs import run_capped
from .runtime import qmd_resources
from .storage import atomic_write, lock_path, locked

COLLECTION_TIMEOUT = 30  # discovery/registration must not hold sync locks indefinitely


@contextmanager
def locked_index(*, blocking: bool = True) -> Iterator[None]:
    """Serialize writers sharing either QMD's config or its database.

    Lock each canonical resource so aliases, differing cache homes, and
    explicit/default environment settings all coordinate. Acquire in path order
    to avoid deadlocks when only one resource is shared by two configurations.
    """
    index_yml, database = qmd_resources()
    paths = sorted({lock_path(path, "qmd") for path in (index_yml, database)})
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(locked(path, blocking=blocking))
        # QMD creates these directories for its default locations but not for an
        # explicit INDEX_PATH / QMD_CONFIG_DIR, which the pinned hook and cron
        # environments always pass. Without this, the first hook job on a machine
        # where QMD has never run fails with "directory does not exist".
        for directory in (database.parent, index_yml.parent):
            directory.mkdir(parents=True, exist_ok=True)
        yield


def add_context(path: str, description: str, qmd_binary: str | None = None) -> bool:
    """Run ``qmd context add`` for one ``qmd://`` path; report status; return success.

    On failure we surface stderr rather than swallowing it: a registration that
    silently failed would leave QMD search results without their context.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    result = subprocess.run(
        [qmd_binary, "context", "add", path, description],
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    print(f"  [{'ok' if ok else 'FAILED'}] {path}: {description}")
    if not ok and result.stderr.strip():
        print(f"      {result.stderr.strip()}", file=sys.stderr)
    return ok


def register_descriptions(descriptions: dict[str, str], qmd_binary: str | None = None) -> int:
    """Register a map of ``qmd://`` path → description; return 0 iff all succeeded.

    The shared body of every source's ``context`` command: register each entry,
    count failures, print the summary, and return an exit code that distinguishes a
    partial failure from a clean run. *Which* paths to register is each source's
    policy (project/repo catalog, a single collection, or per-service docs); this
    only owns the registration loop and reporting.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    failures = sum(
        0 if add_context(path, description, qmd_binary) else 1
        for path, description in descriptions.items()
    )
    if failures:
        print(
            f"\n{failures} context registration(s) FAILED. Run 'qmd context list' to inspect.",
            file=sys.stderr,
        )
        return 1
    print("\nDone. Run 'qmd context list' to verify.")
    return 0


def register_context(collection: str) -> int:
    """Register project- and repo-level QMD context descriptions for a collection.

    Convenience wrapper over :func:`register_descriptions` building the path map from
    the shared project/repo catalog in :mod:`cairn.config`.
    """
    cfg = get_config()
    descriptions = {
        f"qmd://{collection}/{project}": description
        for project, description in cfg.project_descriptions.items()
    }
    for project, repos in cfg.repo_catalog.items():
        for repo, description in repos.items():
            descriptions[f"qmd://{collection}/{project}/{repo}"] = description
    return register_descriptions(descriptions, cfg.qmd_binary)


# ---------------------------------------------------------------------------
# Collection registration (the bootstrap that makes the index non-empty)
# ---------------------------------------------------------------------------
#
# QMD only indexes directories that have been *registered* as collections; until
# then ``qmd update``/``qmd embed`` have nothing to work on and every search is
# empty. cairn never edits QMD's index file - it drives the ``qmd`` CLI, exactly
# as :func:`add_context` does - so these wrap ``qmd collection list/add`` and stay
# idempotent (safe to run on every sync).


# A registered collection shows up as a ``qmd://<name>/`` URI in ``collection
# list`` (and in ``show``/``status``); that token is the one stable anchor across
# qmd's human-readable, no-``--json`` output, so we parse names out of it.
_COLLECTION_URI = re.compile(r"qmd://([^/\s)]+)/")


_FILES_LINE = re.compile(r"^\s*Files:\s*([\d,]+)\s*$")
_UPDATED_LINE = re.compile(r"^\s*Updated:\s*(\d+)\s*([a-z]+)\s+ago\s*$")
_AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass(frozen=True)
class CollectionInfo:
    """One block of ``qmd collection list``: the indexed file count and update age.

    ``updated_age_s`` is ``Updated:  9m ago`` in seconds and ``updated_unit_s`` the
    unit it was printed in, so a caller can allow for QMD's rounding. Either is
    ``None`` when the block has no line cairn can read.
    """

    files: int | None = None
    updated_age_s: int | None = None
    updated_unit_s: int | None = None


def parse_collection_list(text: str) -> dict[str, CollectionInfo]:
    """Parse ``qmd collection list`` into ``{name: CollectionInfo}``.

    A block starts at a line with a ``qmd://NAME/`` token; the indented ``Files:``
    and ``Updated:`` lines below it belong to that collection. A missing or
    unreadable line leaves its field ``None``, never zero.
    """
    found: dict[str, dict[str, int | None]] = {}
    current: str | None = None
    for line in strip_ansi(text).splitlines():
        names = _COLLECTION_URI.findall(line)
        if names:
            for name in names:
                found.setdefault(name, {})
            current = names[-1]
            continue
        if current is None:
            continue
        if files := _FILES_LINE.match(line):
            found[current]["files"] = int(files.group(1).replace(",", ""))
        elif updated := _UPDATED_LINE.match(line):
            unit = _AGE_UNITS.get(updated.group(2))
            if unit is not None:
                found[current]["updated_age_s"] = int(updated.group(1)) * unit
                found[current]["updated_unit_s"] = unit
    return {name: CollectionInfo(**fields) for name, fields in found.items()}


def collection_details(qmd_binary: str, timeout: float) -> dict[str, CollectionInfo]:
    """Run ``qmd collection list`` once and parse every block.

    Raises ``RuntimeError`` on a timeout or a non-zero exit, and lets ``OSError``
    through when the binary cannot start, exactly as :func:`list_collections` does.
    """
    try:
        result = run_capped([qmd_binary, "collection", "list"], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"qmd collection list timed out after {timeout:g}s") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"`qmd collection list` failed (exit {result.returncode}): "
            f"{result.stderr.strip() or 'unknown error'}"
        )
    return parse_collection_list(result.stdout)


def list_collections(qmd_binary: str | None = None) -> set[str]:
    """Return the set of collection names qmd currently has registered.

    Raises on a *real* ``qmd`` failure (non-zero exit) rather than returning an
    empty set: mistaking "qmd is broken" for "nothing registered" would make the
    caller re-add collections forever.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    return set(collection_details(qmd_binary, COLLECTION_TIMEOUT))


# ---------------------------------------------------------------------------
# Index stamp: when cairn last ran a successful ``qmd update``
# ---------------------------------------------------------------------------
#
# QMD's CLI does not say when a collection was last scanned in a form precise enough
# to count unindexed files, so cairn records it itself. ``started_ns`` is read
# before ``qmd update`` starts: a file written during the update counts as
# unindexed, so the stamp errs towards reporting a gap, never towards hiding one.

STAMP_VERSION = 1


def stamp_path() -> Path:
    """``$XDG_STATE_HOME/cairn/qmd-index.json``."""
    return state_home() / "cairn" / "qmd-index.json"


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_update(started_ns: int, collections: Iterable[str]) -> None:
    """Write the index stamp after a ``qmd update`` that exited 0.

    Call it while the index lock is still held. ``collections`` are the names
    registered when the update ran. A failed write warns on stderr and never fails
    the caller: the stamp only sharpens ``cairn recall gaps``.
    """
    payload = {
        "version": STAMP_VERSION,
        "database": str(qmd_resources()[1]),
        "started_ns": started_ns,
        "started_at": _iso(started_ns),
        "collections": sorted(set(collections)),
    }
    try:
        atomic_write(stamp_path(), json.dumps(payload) + "\n")
    except OSError as exc:
        print(f"Warning: could not record the qmd update time: {exc}", file=sys.stderr)


def read_stamp() -> dict | None:
    """The index stamp, or ``None`` when it is missing, corrupt or of another version."""
    try:
        data = json.loads(stamp_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("version") != STAMP_VERSION
        or not isinstance(data.get("database"), str)
        or type(data.get("started_ns")) is not int
        or not isinstance(data.get("collections"), list)
        or not all(isinstance(name, str) for name in data["collections"])
    ):
        return None
    return data


def _has_markdown(path: Path) -> bool:
    """True iff ``path`` is a directory containing at least one ``.md`` file."""
    return path.is_dir() and next(path.rglob("*.md"), None) is not None


def ensure_collection(
    name: str,
    path: Path,
    qmd_binary: str | None = None,
    known: set[str] | None = None,
) -> str:
    """Idempotently register ``path`` as the qmd collection ``name``.

    Returns a status word so callers can report what happened:

    - ``"skipped"`` - the dir is missing or has no markdown yet (e.g. a source that
      has not produced data). Not an error: it registers on a later sync once it
      does. This is what lets cursor/codex/granola self-heal when they first appear.
    - ``"exists"``  - already a qmd collection (idempotent no-op).
    - ``"added"``   - newly registered via ``qmd collection add``.
    - ``"FAILED"``  - ``qmd collection add`` returned non-zero; stderr is surfaced.

    Pass ``known`` (a pre-fetched :func:`list_collections` set) to avoid one
    ``collection list`` call per collection when registering several at once.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    path = Path(path)
    if not _has_markdown(path):
        return "skipped"
    if known is None:
        known = list_collections(qmd_binary)
    if name in known:
        return "exists"
    try:
        result = run_capped(
            [qmd_binary, "collection", "add", str(path), "--name", name],
            timeout=COLLECTION_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"qmd collection add timed out after {COLLECTION_TIMEOUT}s") from exc
    if result.returncode != 0:
        if result.stderr.strip():
            print(f"      {result.stderr.strip()}", file=sys.stderr)
        return "FAILED"
    return "added"


# ---------------------------------------------------------------------------
# Index diagnostics (shared by ``cairn sync`` and ``cairn doctor``)
# ---------------------------------------------------------------------------
#
# QMD's CLI is the integration boundary: cairn parses ``qmd --version``, ``qmd
# status`` and ``qmd embed`` output and never reads QMD's SQLite tables. Units
# differ and are kept apart: ``Total`` counts indexed files, ``Vectors`` counts
# embedded chunks, ``Pending`` counts document hashes that still need vectors.

STATUS_TIMEOUT = 30
VERSION_TIMEOUT = 10
# The release cairn's embedding checks were verified against. Not a hard floor:
# doctor warns below it, sync judges every version by the embed output and status.
TESTED_QMD_VERSION = (2, 5, 3)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_VERSION = re.compile(r"\bqmd\s+v?(\d+)\.(\d+)\.(\d+)(?![-.\w])")
_TOTAL = re.compile(r"^\s*Total:\s*([\d,]+) files? indexed", re.MULTILINE)
_VECTORS = re.compile(r"^\s*Vectors:\s*([\d,]+) embedded", re.MULTILINE)
_PENDING = re.compile(r"^\s*Pending:\s*(\d[\d,]*) need embedding", re.MULTILINE)
_PENDING_LINE = re.compile(r"^\s*Pending:", re.MULTILINE)
# "⚠ 48219 chunks failed" (QMD 1.x) and "⚠ 48,219 chunks still failed after retries" (2.x).
_FAILED_CHUNKS = re.compile(r"(\d[\d,]*) chunks? (?:still )?failed")
_CHUNK_ERROR = re.compile(r"Error embedding\b")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``qmd 2.5.3 (179d450)`` -> ``(2, 5, 3)``; a prerelease or other text -> ``None``."""
    m = _VERSION.search(strip_ansi(text))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _count(match: re.Match[str]) -> int:
    return int(match.group(1).replace(",", ""))


@dataclass(frozen=True)
class IndexStatus:
    """The index-wide counts ``qmd status`` reports (all QMD collections, not only cairn's)."""

    documents: int  # indexed files
    vectors: int  # embedded chunks
    pending: int  # document hashes still needing vectors

    @property
    def state(self) -> str:
        """``empty`` | ``no-vectors`` | ``pending`` | ``complete``."""
        if self.documents == 0:
            return "empty"
        if self.vectors == 0:
            return "no-vectors"
        return "pending" if self.pending else "complete"

    def describe(self) -> str:
        return f"{self.documents} documents, {self.vectors} vectors, {self.pending} pending"


def parse_status(text: str) -> IndexStatus | None:
    """Parse the ``Documents`` block of ``qmd status``; ``None`` when it is unrecognised.

    QMD prints ``Pending:`` only when work remains, so a block with ``Total`` and
    ``Vectors`` but no ``Pending`` line means zero pending. A missing ``Documents``
    heading or count, or a ``Pending`` line in another shape, is unknown, never zero.
    """
    clean = strip_ansi(text)
    if not re.search(r"^Documents\s*$", clean, re.MULTILINE):
        return None
    total, vectors = _TOTAL.search(clean), _VECTORS.search(clean)
    if total is None or vectors is None:
        return None
    pending = _PENDING.search(clean)
    if pending is None and _PENDING_LINE.search(clean):
        return None  # a Pending line cairn cannot read is unknown, not zero
    return IndexStatus(_count(total), _count(vectors), _count(pending) if pending else 0)


class StatusFormatError(RuntimeError):
    """``qmd status`` ran but its output is not a format cairn recognises."""


def index_status(qmd_binary: str) -> IndexStatus:
    """Run ``qmd status`` (the runtime probe) and parse it.

    Raises ``RuntimeError`` when QMD cannot run (a binary that prints its version
    but crashes opening its index fails here) and :class:`StatusFormatError` when
    it runs but prints an unrecognised format.
    """
    try:
        result = run_capped([qmd_binary, "status"], timeout=STATUS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`qmd status` timed out after {STATUS_TIMEOUT}s") from exc
    except OSError as exc:
        raise RuntimeError(f"`qmd status` could not start: {exc}") from exc
    if result.returncode != 0:
        how = (
            f"killed by signal {-result.returncode}"
            if result.returncode < 0
            else f"exit {result.returncode}"
        )
        tail = strip_ansi(result.stderr or result.stdout).strip()[-500:]
        raise RuntimeError(f"`qmd status` failed ({how}): {tail or 'no output'}")
    status = parse_status(result.stdout)
    if status is None:
        raise StatusFormatError("`qmd status` output has no recognisable Documents Total/Vectors")
    return status


def qmd_version(qmd_binary: str) -> str:
    """The first line of ``qmd --version``; raise ``RuntimeError`` when it cannot run."""
    try:
        result = run_capped([qmd_binary, "--version"], timeout=VERSION_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`qmd --version` timed out after {VERSION_TIMEOUT}s") from exc
    except OSError as exc:
        raise RuntimeError(f"`qmd --version` could not start: {exc}") from exc
    text = strip_ansi(result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"`qmd --version` failed (exit {result.returncode}): {text[-300:]}")
    return text.splitlines()[0] if text else ""


def embed_failures(output: str) -> list[str]:
    """Explicit failures in the *complete* ``qmd embed`` output (stdout + stderr).

    QMD exits 0 when chunks fail (its embedding session has a fixed 30-minute
    limit; chunks left when it expires fail with ``SessionReleasedError``). Call
    this before any head/tail truncation. ``0 chunks failed`` is not a failure, and
    other stderr (such as the Metal ``error compiling source`` probe) is ignored.
    """
    clean = strip_ansi(output)
    problems = [
        f"{m.group(1)} chunks failed"
        for m in _FAILED_CHUNKS.finditer(clean)
        if int(m.group(1).replace(",", "")) > 0
    ]
    errors = [line.strip() for line in clean.splitlines() if _CHUNK_ERROR.search(line)]
    if errors:
        problems.append(f"{len(errors)} chunk error line(s), first: {errors[0][:200]}")
    if "SessionReleasedError" in clean or "Session expired" in clean:
        problems.append("QMD's embedding session expired before every chunk was embedded")
    if "Error rate too high" in clean:
        problems.append("QMD aborted embedding because the error rate was too high")
    return problems


__all__ = [
    "STAMP_VERSION",
    "CollectionInfo",
    "IndexStatus",
    "StatusFormatError",
    "add_context",
    "collection_details",
    "embed_failures",
    "ensure_collection",
    "index_status",
    "list_collections",
    "parse_collection_list",
    "parse_status",
    "parse_version",
    "qmd_version",
    "read_stamp",
    "record_update",
    "register_context",
    "register_descriptions",
    "stamp_path",
    "strip_ansi",
]
