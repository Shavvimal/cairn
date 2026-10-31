"""``cairn recall gaps``: what recall and search cannot see right now.

A deterministic coverage report built only from facts cairn already has: config
flags, a ``stat`` of the native stores, the exported store's catalog, one ``qmd
collection list`` call and the index stamp cairn writes after each ``qmd update``.
No model writes any of it, so every line can be tested.

:func:`gather` collects the facts (stat and catalog reads; the only write is the
catalog cache save ``cairn recall list`` already does). :func:`classify` turns them
into gaps and is pure: the same facts and ``now`` give the same gaps. Native
stores are walked by ``os.scandir``/``stat`` only and no source module is imported,
because those read the config and raise at import time.

The report never repairs anything: a gap with a ``fix`` names the command that fixes it.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from .catalog import load_catalog, scan
from .config import CairnConfig, Collection
from .harnesses.antigravity import conversation_transcript, transcript_conversation_id
from .jsonl import iter_subagent_files, subagent_meta_path
from .qmd import CollectionInfo, collection_details, read_stamp
from .registry import SOURCES, Source
from .runtime import qmd_resources
from .store import record_from_frontmatter
from .timeutil import parse_iso

SCHEMA_VERSION = 1
EXPORT_GRACE_S = 3600  # one hour: the default cron sync is hourly
QMD_PROBE_TIMEOUT_S = 5
CLOCK_SKEW_S = 60
SESSION_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")

WARN, INFO = "warn", "info"
STORE, INDEX = "store", "index"
# The fixed vocabulary, in report order within a severity.
KINDS = (
    "export-lag",
    "never-exported",
    "unreadable-exports",
    "source-disabled",
    "store-missing",
    "collection-missing",
    "index-lag",
    "index-count",
    "index-stamp-missing",
    "index-unknown",
    "rg-missing",
)
FIX_SYNC = "cairn sync"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexFacts:
    """What QMD holds for one collection."""

    registered: bool
    files: int | None = None
    unindexed_files: int | None = None
    basis: str | None = None  # cairn (the stamp) | qmd-relative (QMD's Updated age)


@dataclass(frozen=True)
class CollectionFacts:
    collection: str
    source: str | None
    enabled: bool
    store: Path | None = None
    store_exists: bool = False
    store_error: str | None = None
    session: bool = False  # the registry source exports sessions (has a session type)
    markdown_files: int = 0
    exported_docs: int = 0
    unreadable: int = 0
    last_export_at: datetime | None = None
    newest_activity_at: datetime | None = None
    native_newest_at: datetime | None = None
    native_basis: str = "none"  # file | store | none
    index: IndexFacts | None = None
    # Per-file comparison (claude, codex, antigravity): one ``(native mtime, activity_at
    # of its export or None)`` per native file changed after its own export. ``None``:
    # not compared per file, so the collection's newest export is the reference.
    unexported: tuple[tuple[datetime, datetime | None], ...] | None = None


@dataclass(frozen=True)
class Facts:
    collections: tuple[CollectionFacts, ...]
    qmd: str = "skipped"  # available | missing | failed | skipped (coverage never asks QMD)
    qmd_error: str | None = None
    stamp_at: datetime | None = None
    stamp_basis: str | None = None  # cairn when a usable stamp was read
    rg: bool | None = None  # None: not checked


@dataclass(frozen=True)
class Gap:
    kind: str
    severity: str
    scope: str
    collection: str | None
    source: str | None
    message: str
    fix: str | None
    facts: dict = field(default_factory=dict)
    # The unexported interval for export-lag / never-exported (window filter only).
    interval: tuple[datetime | None, datetime | None] | None = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "scope": self.scope,
            "collection": self.collection,
            "source": self.source,
            "message": self.message,
            "fix": self.fix,
            "facts": self.facts,
        }

    def line(self) -> str:
        where = f"{self.collection}: " if self.collection else ""
        fix = f"; run {self.fix}" if self.fix else ""
        return f"{self.severity:<5} {self.kind:<19} {where}{self.message}{fix}"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utc(value: object) -> datetime | None:
    parsed = parse_iso(value) if isinstance(value, str) else None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _from_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def iso(value: datetime | None) -> str | None:
    """UTC ISO-8601 at seconds precision, ``2026-10-30T09:11:40Z``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _minute(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ") if value else "never"


def duration(seconds: float) -> str:
    """``3h22m``, ``2d4h``, ``45m``, ``30s``: two units at most."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Native probes: stat only, never a source import
# ---------------------------------------------------------------------------


def _current_session_ids() -> tuple[str, ...]:
    return tuple(value for name in SESSION_ENV_VARS if (value := os.environ.get(name)))


class _Scan:
    """One native-store walk: every stat-able file and every traversal error.

    A file or folder that vanishes during the walk is a race and is ignored. Any other
    ``OSError`` (a permission error on a project or date folder) is recorded, so an
    incomplete walk is reported instead of read as "no native activity".
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, exc: OSError) -> None:
        if not isinstance(exc, FileNotFoundError):
            self.errors.append(str(exc))

    def scandir(self, path: Path) -> list[os.DirEntry]:
        try:
            with os.scandir(path) as entries:
                return list(entries)
        except OSError as exc:
            self.error(exc)
            return []

    def walk(self, root: Path, accept: Callable[[str], bool]) -> Iterable[Path]:
        for directory, _dirs, files in os.walk(root, onerror=self.error, followlinks=False):
            for name in files:
                if accept(name):
                    yield Path(directory) / name

    def files(self, paths: Iterable[Path]) -> list[tuple[Path, int]]:
        """``(path, mtime_ns)`` of non-empty regular files."""
        found = []
        for path in paths:
            try:
                st = os.stat(path)
            except OSError as exc:
                self.error(exc)
                continue
            if stat_mod.S_ISREG(st.st_mode) and st.st_size > 0:
                found.append((path, st.st_mtime_ns))
        return found


def _claude_files(store: Path, scan: _Scan) -> Iterable[Path]:
    for project in scan.scandir(store):
        if project.is_dir(follow_symlinks=False):
            for entry in scan.scandir(Path(project.path)):
                # A top-level agent-*.jsonl is a legacy side thread the exporter skips.
                if entry.name.endswith(".jsonl") and not entry.name.startswith("agent-"):
                    yield Path(entry.path)


def _claude_mtime_ns(path: Path, mtime_ns: int, scan: _Scan) -> int:
    """A session's newest write: its file, or a subagent that finished after it.

    The export selects and revises a parent by the same value (``jsonl.newest_mtime``).
    """
    try:
        children = iter_subagent_files(path)
    except OSError as exc:
        scan.error(exc)
        return mtime_ns
    for child in children:
        for item in (child, subagent_meta_path(child)):
            try:
                mtime_ns = max(mtime_ns, os.stat(item).st_mtime_ns)
            except OSError as exc:
                if item == child:
                    scan.error(exc)
    return mtime_ns


def _antigravity_files(store: Path, scan: _Scan) -> Iterable[Path]:
    for entry in scan.scandir(store / "brain"):
        if entry.is_dir(follow_symlinks=False):
            transcript = conversation_transcript(store, entry.name)
            if transcript is not None:
                yield transcript


def _granola_files(store: Path, scan: _Scan) -> Iterable[Path]:
    for entry in scan.scandir(store):
        if entry.name.startswith("cache-v") and entry.name.endswith(".json"):
            yield Path(entry.path)


def _claude_ids(path: Path) -> tuple[str, ...]:
    return (path.stem,)  # <store>/<project>/<session id>.jsonl


def _codex_ids(path: Path) -> tuple[str, ...]:
    # rollout-<timestamp>-<thread uuid>.jsonl; the bare form is kept for odd names.
    return (path.stem[-36:], path.stem.removeprefix("rollout-"))


def _antigravity_ids(path: Path) -> tuple[str, ...]:
    conversation = transcript_conversation_id(path)
    return (conversation,) if conversation else ()


# source -> (probe over the store, basis, native ids of one probed file). ``store``
# basis: the mtime changes without a new session (a database or cache rewrite), so
# its lag is never a warn. A source with an id function is compared file by file
# with its own export; the others with the collection's newest export.
NativeProbe = Callable[[Path, _Scan], Iterable[Path]]
NativeIds = Callable[[Path], tuple[str, ...]]
PROBES: dict[str, tuple[NativeProbe, str, NativeIds | None]] = {
    "claude": (_claude_files, "file", _claude_ids),
    "codex": (
        lambda store, scan: scan.walk(
            store, lambda n: n.startswith("rollout-") and n.endswith(".jsonl")
        ),
        "file",
        _codex_ids,
    ),
    "antigravity": (_antigravity_files, "file", _antigravity_ids),
    "cursor": (
        lambda store, _scan: (
            store / name for name in ("globalStorage/state.vscdb", "globalStorage/state.vscdb-wal")
        ),
        "store",
        None,
    ),
    "granola": (_granola_files, "store", None),
}


@dataclass(frozen=True)
class _NativeFacts:
    newest_at: datetime | None = None
    basis: str = "none"
    store_exists: bool = False
    store_error: str | None = None
    # ``(native ids, mtime_ns)`` per probed file, for a source compared file by file.
    files: tuple[tuple[tuple[str, ...], int], ...] | None = None


def _scan_error(scan: _Scan) -> str | None:
    if not scan.errors:
        return None
    more = f" (+{len(scan.errors) - 1} more)" if len(scan.errors) > 1 else ""
    return f"native store read incompletely: {scan.errors[0]}{more}"


def _native(source: str | None, coll: Collection) -> _NativeFacts:
    """The newest native file of one collection, and each file when compared per file.

    A file whose native id is a current session id is skipped, so the live session
    never reports itself. The id is compared, not the path: a project folder named
    after a working directory can contain a session id too.
    """
    skip = set(_current_session_ids())
    scan = _Scan()
    if source == "service-docs":
        roots = [s.path for s in coll.service_sources]
        missing = [str(r) for r in roots if not r.is_dir()]
        found = scan.files(p for r in roots for p in scan.walk(r, lambda n: n.endswith(".md")))
        newest = max((m for _, m in found), default=None)
        error = f"service source {', '.join(missing)} not found" if missing else None
        return _NativeFacts(
            _from_ns(newest) if newest is not None else None,
            "file",
            bool(roots) and not missing,
            error or _scan_error(scan),
        )
    if coll.store is None or source not in PROBES:
        return _NativeFacts()
    store = coll.store
    try:
        os.stat(store)
    except FileNotFoundError:
        return _NativeFacts()
    except OSError as exc:
        return _NativeFacts(store_error=str(exc))
    if not os.access(store, os.R_OK | os.X_OK) and store.is_dir():
        return _NativeFacts(store_exists=True, store_error=f"store {store} is not readable")
    probe, basis, ids = PROBES[source]
    found = scan.files(probe(store, scan))
    if source == "claude":
        found = [(path, _claude_mtime_ns(path, m, scan)) for path, m in found]
    rows = [(ids(path) if ids is not None else (), path, m) for path, m in found]
    rows = [row for row in rows if not skip.intersection(row[0])]
    newest = max((m for _, _, m in rows), default=None)
    return _NativeFacts(
        _from_ns(newest) if newest is not None else None,
        basis,
        True,
        _scan_error(scan),
        tuple((row_ids, m) for row_ids, _, m in rows) if ids is not None else None,
    )


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def _source_of(collection: str) -> Source | None:
    return next((s for s in SOURCES if s.collection == collection), None)


# native id -> (newest export mtime_ns, its activity_at)
ExportIndex = dict[str, tuple[int, datetime | None]]


def _exports(output_dir: Path, source: Source | None) -> tuple[dict, list[int], ExportIndex]:
    """Export facts, every markdown file's ``mtime_ns`` and the session exports by id.

    A session document recall would skip (frontmatter that does not parse, a source
    that disagrees with the collection, no native id) counts as ``unreadable`` and not
    as an export: coverage applies the same record check as ``recall list``.
    """
    by_id: ExportIndex = {}
    if source is not None and source.session_type:
        catalog = load_catalog(output_dir)
        catalog.save()
        mtimes = [entry.mtime_ns for entry in catalog.entries.values()]
        newest: datetime | None = None
        exported = rejected = 0
        for rel, entry in catalog.entries.items():
            fm = entry.frontmatter
            if fm.get("type") not in source.session_types:
                continue
            try:
                record = record_from_frontmatter(
                    catalog.path_of(rel), fm, source.source, source.id_field, source.session_types
                )
            except ValueError:
                record = None
            if record is None:
                rejected += 1
                continue
            exported += 1
            stamp = _utc(fm.get("activity_at")) or _utc(fm.get("started_at"))
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
            known = by_id.get(record.session_id)
            if known is None or entry.mtime_ns > known[0]:
                by_id[record.session_id] = (entry.mtime_ns, stamp)
        facts = {
            "markdown_files": len(catalog.entries) + len(catalog.errors),
            "exported_docs": exported,
            "unreadable": len(catalog.errors) + rejected,
            "newest_activity_at": newest,
        }
    else:
        found = scan(output_dir) if output_dir.is_dir() else {}
        mtimes = [st.st_mtime_ns for st in found.values()]
        facts = {
            "markdown_files": len(found),
            "exported_docs": len(found),
            "unreadable": 0,
            "newest_activity_at": None,
        }
    facts["last_export_at"] = _from_ns(max(mtimes)) if mtimes else None
    return facts, mtimes, by_id


def _unexported(
    files: tuple[tuple[tuple[str, ...], int], ...],
    by_id: ExportIndex,
    newest_activity_at: datetime | None,
) -> tuple[tuple[datetime, datetime | None], ...]:
    """Native files written after their own export, newest first.

    An export rewrites its document whenever the native revision changes, so a
    document at least as new as its native file covers it. Another session's export,
    a peer exchange or a note on another session proves nothing about this file. A
    file with no export counts only when it is newer than every exported activity:
    an older one is outside the export window or one the exporter skips on purpose
    (no messages), and ``cairn sync`` would not clear it.
    """
    result = []
    for ids, mtime_ns in files:
        export = next((by_id[i] for i in ids if i in by_id), None)
        if export is not None and export[0] >= mtime_ns:
            continue
        at = _from_ns(mtime_ns)
        if export is None and newest_activity_at is not None and at <= newest_activity_at:
            continue
        result.append((at, export[1] if export is not None else None))
    return tuple(sorted(result, key=lambda row: row[0], reverse=True))


def selected_collections(
    config: CairnConfig, sources: Iterable[str] | None
) -> list[tuple[str, Collection, Source | None]]:
    """Configured collections in name order; ``sources`` keeps only those sources'."""
    wanted = set(sources) if sources else None
    result = []
    for name, coll in sorted(config.collections.items()):
        source = _source_of(name)
        if wanted is not None and (source is None or source.source not in wanted):
            continue
        result.append((name, coll, source))
    return result


def _unindexed(
    mtimes: list[int], info: CollectionInfo, stamp: dict | None, name: str, now: datetime
) -> tuple[int | None, str | None]:
    if stamp is not None and name in stamp["collections"]:
        started = stamp["started_ns"]
        return sum(m > started for m in mtimes), "cairn"
    if info.updated_age_s is None or info.updated_unit_s is None:
        return None, None
    # QMD prints a coarse age ("9m ago") that may be floored or rounded. Half a unit
    # less than the printed age is younger than the real update either way, so the
    # count is a lower bound.
    age_s = max(0.0, info.updated_age_s - info.updated_unit_s / 2)
    cutoff = int((now.timestamp() - age_s) * 1e9)
    return sum(m > cutoff for m in mtimes), "qmd-relative"


def gather(
    config: CairnConfig,
    sources: Iterable[str] | None = None,
    *,
    index: bool = True,
    now: datetime | None = None,
) -> Facts:
    """Collect the facts for the selected collections; ``index=False`` skips QMD and rg."""
    now = now or datetime.now(UTC)
    rows: list[tuple[CollectionFacts, list[int]]] = []
    for name, coll, source in selected_collections(config, sources):
        exports, mtimes, by_id = _exports(config.output_dir(name), source)
        native = (
            _native(source.source if source else None, coll)
            if coll.sync.enabled
            else _NativeFacts(store_exists=coll.store is not None and coll.store.exists())
        )
        rows.append(
            (
                CollectionFacts(
                    collection=name,
                    source=source.source if source else None,
                    enabled=coll.sync.enabled,
                    store=coll.store,
                    store_exists=native.store_exists,
                    store_error=native.store_error,
                    session=bool(source and source.session_type),
                    native_newest_at=native.newest_at,
                    native_basis=native.basis,
                    unexported=(
                        _unexported(native.files, by_id, exports["newest_activity_at"])
                        if native.files is not None
                        else None
                    ),
                    **exports,
                ),
                mtimes,
            )
        )
    if not index:
        return Facts(tuple(facts for facts, _ in rows))

    qmd_state, qmd_error = "available", None
    details: dict[str, CollectionInfo] = {}
    binary = config.qmd_binary
    if not (shutil.which(binary) or Path(binary).expanduser().is_file()):
        qmd_state = "missing"
    else:
        try:
            details = collection_details(binary, QMD_PROBE_TIMEOUT_S, strict=True)
        except (RuntimeError, OSError) as exc:
            qmd_state, qmd_error = "failed", str(exc)
    stamp = read_stamp()
    if stamp is not None and stamp["database"] != str(qmd_resources()[1]):
        stamp = None
    collections = []
    for facts, mtimes in rows:
        index_facts: IndexFacts | None = None
        if qmd_state == "available":
            info = details.get(facts.collection)
            if info is None:
                index_facts = IndexFacts(registered=False)
            else:
                unindexed, index_basis = _unindexed(mtimes, info, stamp, facts.collection, now)
                index_facts = IndexFacts(True, info.files, unindexed, index_basis)
        collections.append(replace(facts, index=index_facts))
    return Facts(
        tuple(collections),
        qmd=qmd_state,
        qmd_error=qmd_error,
        stamp_at=_from_ns(stamp["started_ns"]) if stamp else None,
        stamp_basis="cairn" if stamp else None,
        rg=shutil.which("rg") is not None,
    )


# ---------------------------------------------------------------------------
# Classifying (pure)
# ---------------------------------------------------------------------------


def export_command(source: str | None) -> str:
    """The command that exports every native item of ``source``."""
    found = next((s for s in SOURCES if s.source == source), None)
    if found is None:
        return FIX_SYNC
    return (
        f"cairn {found.source} export --all" if found.windowed else f"cairn {found.source} export"
    )


def _unexported_gap(
    c: CollectionFacts, now: datetime, gap: Callable[..., Gap], skew_note: str
) -> list[Gap]:
    """``export-lag`` from the native files written after their own export."""
    rows = c.unexported or ()
    if not rows:
        return []
    clamped = min(rows[0][0], now)
    # Each file is behind by its unexported interval: from its export's activity_at to
    # its mtime, or, with no export (or an export newer by the clock), from its mtime
    # to now.
    behind = [
        (min(m, now) - low).total_seconds()
        if low is not None and low < min(m, now)
        else (now - min(m, now)).total_seconds()
        for m, low in rows
    ]
    lag_s = int(max(behind))
    lows = [low for _, low in rows]
    low = None if any(x is None for x in lows) else min(x for x in lows if x is not None)
    reference = c.newest_activity_at or c.last_export_at
    severity = WARN if lag_s > EXPORT_GRACE_S else INFO
    sessions = f"{len(rows)} session" + ("" if len(rows) == 1 else "s")
    return [
        gap(
            "export-lag",
            severity,
            f"native activity at {_minute(clamped)} not exported for {sessions}, "
            f"newest export {_minute(reference)} ({duration(lag_s)} behind){skew_note}",
            FIX_SYNC,
            lag_s=lag_s,
            basis=c.native_basis,
            sessions=len(rows),
            interval=(low, clamped),
        )
    ]


def _store_gaps(c: CollectionFacts, now: datetime, named: set[str]) -> list[Gap]:
    def gap(kind: str, severity: str, message: str, fix: str | None, **extra) -> Gap:
        interval = extra.pop("interval", None)
        return Gap(kind, severity, STORE, c.collection, c.source, message, fix, extra, interval)

    gaps: list[Gap] = []
    if c.unreadable:
        # Recall reads a disabled collection's exports too, so this is checked first.
        gaps.append(
            gap(
                "unreadable-exports",
                WARN,
                f"{c.unreadable} exported files could not be read as sessions; recall "
                "skips them (cairn recall list warns about each one)",
                None,
                count=c.unreadable,
            )
        )
    if not c.enabled:
        severity = WARN if c.source in named else INFO
        last = c.newest_activity_at or c.last_export_at
        stop = f"exports stop at {_minute(last)}" if last else "nothing was exported"
        gaps.append(gap("source-disabled", severity, f"sync.enabled=false; {stop}", None))
        return gaps
    if c.store_error is not None:
        gaps.append(gap("store-missing", INFO, c.store_error, None))
    elif c.store is not None and not c.store_exists:
        gaps.append(gap("store-missing", INFO, f"store {c.store} not found", None))
    native = c.native_newest_at
    if native is None:
        return gaps
    skew = (native - now).total_seconds()
    clamped = min(native, now)
    skew_note = " (clock skew)" if skew > CLOCK_SKEW_S else ""
    if c.exported_docs == 0:
        gaps.append(
            gap(
                "never-exported",
                WARN,
                f"native files exist (newest {_minute(clamped)}) but nothing is exported"
                + skew_note,
                export_command(c.source) if c.source else FIX_SYNC,
                basis=c.native_basis,
                interval=(None, clamped),
            )
        )
        return gaps
    if c.unexported is not None:
        return gaps + _unexported_gap(c, now, gap, skew_note)
    reference = c.newest_activity_at if c.session else c.last_export_at
    reference = reference or c.last_export_at
    caught_up = c.last_export_at is not None and c.last_export_at >= native
    if reference is None or caught_up or native <= reference:
        return gaps
    lag_s = int((clamped - reference).total_seconds())
    if lag_s <= 0:
        return gaps
    severity = WARN if lag_s > EXPORT_GRACE_S and c.native_basis == "file" else INFO
    what = "native activity" if c.native_basis == "file" else "native store changed"
    note = "" if c.native_basis == "file" else "; a store mtime, not proof of a new session"
    gaps.append(
        gap(
            "export-lag",
            severity,
            f"{what} at {_minute(clamped)}, newest export {_minute(reference)} "
            f"({duration(lag_s)} behind{note}){skew_note}",
            FIX_SYNC,
            lag_s=lag_s,
            basis=c.native_basis,
            interval=(reference, clamped),
        )
    )
    return gaps


def _index_gaps(c: CollectionFacts, facts: Facts) -> list[Gap]:
    if not c.enabled or c.index is None:
        return []

    def gap(kind: str, severity: str, message: str, **extra) -> Gap:
        return Gap(kind, severity, INDEX, c.collection, c.source, message, FIX_SYNC, extra)

    idx = c.index
    if not idx.registered:
        if c.markdown_files:
            return [gap("collection-missing", WARN, "has markdown but is not a qmd collection")]
        return []
    if idx.unindexed_files:
        if idx.basis == "cairn":
            message = (
                f"{idx.unindexed_files} files changed after the last qmd update "
                f"({_minute(facts.stamp_at)})"
            )
        else:
            message = (
                f"at least {idx.unindexed_files} files changed after qmd last updated "
                "this collection"
            )
        return [gap("index-lag", WARN, message, count=idx.unindexed_files, basis=idx.basis)]
    if idx.files is not None and idx.files != c.markdown_files:
        severity = WARN if c.markdown_files > idx.files else INFO
        return [
            gap(
                "index-count",
                severity,
                f"{c.markdown_files} markdown files on disk, qmd indexes {idx.files}",
                store_files=c.markdown_files,
                qmd_files=idx.files,
            )
        ]
    return []


def _global_gaps(facts: Facts) -> list[Gap]:
    gaps: list[Gap] = []
    if facts.qmd == "missing":
        gaps.append(
            Gap(
                "index-unknown",
                WARN,
                INDEX,
                None,
                None,
                "qmd not found: topic search cannot run; temporal recall still works",
                "cairn doctor",
                {"qmd": "missing"},
            )
        )
    elif facts.qmd == "failed":
        gaps.append(
            Gap(
                "index-unknown",
                WARN,
                INDEX,
                None,
                None,
                f"qmd collection list failed: {facts.qmd_error}",
                "cairn doctor",
                {"qmd": "failed"},
            )
        )
    elif facts.qmd == "available":
        relative = [
            c.collection
            for c in facts.collections
            if c.enabled
            and c.markdown_files
            and c.index is not None
            and c.index.registered
            and c.index.basis != "cairn"
        ]
        if relative:
            gaps.append(
                Gap(
                    "index-stamp-missing",
                    INFO,
                    INDEX,
                    None,
                    None,
                    f"no qmd update recorded by cairn for {len(relative)} collections; "
                    "index-lag is a lower bound from qmd's Updated age, or unknown when "
                    "qmd prints no age cairn can read",
                    FIX_SYNC,
                    {"collections": relative},
                )
            )
    if facts.rg is False:
        gaps.append(
            Gap(
                "rg-missing",
                INFO,
                INDEX,
                None,
                None,
                "rg not on PATH: exact-match search is off",
                None,
            )
        )
    return gaps


def _in_window(gap: Gap, window: tuple[datetime, datetime]) -> bool:
    if gap.interval is None:
        return True  # unreadable, disabled, missing store: their dates are unknown
    start, end = window
    low, high = gap.interval
    # The unexported interval (low, high] intersects [start, end); a never-exported
    # or never-exported-session gap has no low end.
    return high is not None and high >= start and (low is None or low < end)


def _order(gap: Gap) -> tuple[int, int, str]:
    return (0 if gap.severity == WARN else 1, KINDS.index(gap.kind), gap.collection or "")


def classify(
    facts: Facts,
    now: datetime,
    *,
    window: tuple[datetime, datetime] | None = None,
    requested: Iterable[str] | None = None,
) -> list[Gap]:
    """The gaps in ``facts``, warn first, then by kind and collection. Pure.

    ``requested`` are the sources named with ``--source``: a disabled one is a warn.
    With ``window`` only store gaps that can hide a row in it are kept.
    """
    named = set(requested or ())
    gaps: list[Gap] = []
    for c in facts.collections:
        gaps.extend(_store_gaps(c, now, named))
        gaps.extend(_index_gaps(c, facts))
    gaps.extend(_global_gaps(facts))
    if window is not None:
        gaps = [g for g in gaps if g.scope == STORE and _in_window(g, window)]
    return sorted(gaps, key=_order)


def status_of(gaps: list[Gap]) -> str:
    return "degraded" if any(g.severity == WARN for g in gaps) else "clean"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _collection_json(c: CollectionFacts) -> dict:
    index = None
    if c.index is not None:
        index = {
            "registered": c.index.registered,
            "files": c.index.files,
            "unindexed_files": c.index.unindexed_files,
            "basis": c.index.basis,
        }
    return {
        "collection": c.collection,
        "source": c.source,
        "enabled": c.enabled,
        "store": str(c.store) if c.store is not None else None,
        "store_exists": c.store_exists,
        "exported_docs": c.exported_docs,
        "markdown_files": c.markdown_files,
        "unreadable": c.unreadable,
        "last_export_at": iso(c.last_export_at),
        "newest_activity_at": iso(c.newest_activity_at),
        "native_newest_at": iso(c.native_newest_at),
        "native_basis": c.native_basis,
        "index": index,
    }


def to_json(facts: Facts, gaps: list[Gap], now: datetime) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status_of(gaps),
        "checked_at": iso(now),
        "gaps": [g.to_json() for g in gaps],
        "collections": [_collection_json(c) for c in facts.collections],
        "index": {
            "qmd": facts.qmd,
            "stamp_at": iso(facts.stamp_at),
            "stamp_basis": facts.stamp_basis,
        },
        "rg": {"available": facts.rg},
    }


def render_text(facts: Facts, gaps: list[Gap]) -> str:
    lines = [g.line() for g in gaps]
    if not gaps:
        lines.append(f"status: clean (checked {len(facts.collections)} collections, qmd, rg)")
    else:
        warn = sum(g.severity == WARN for g in gaps)
        lines.append(f"status: {status_of(gaps)} ({warn} warn, {len(gaps) - warn} info)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coverage for recall list / digest
# ---------------------------------------------------------------------------


def skipped_gaps(skipped: Iterable[dict]) -> list[Gap]:
    """One ``unreadable-exports`` warn per source for the sessions a digest skipped.

    ``skipped`` are the digest's ``{"ref": "source:id", "reason": "legacy" | "corrupt"}``
    rows. Each is a session in the window the command could not read, so the
    result is never a clean miss.
    """
    by_source: dict[str, list[dict]] = {}
    for row in skipped:
        by_source.setdefault(str(row["ref"]).split(":", 1)[0], []).append(row)
    gaps = []
    for name, rows in sorted(by_source.items()):
        source = next((s for s in SOURCES if s.source == name), None)
        legacy = any(row.get("reason") == "legacy" for row in rows)
        gaps.append(
            Gap(
                "unreadable-exports",
                WARN,
                STORE,
                source.collection if source else None,
                name,
                f"{len(rows)} sessions in the window could not be digested "
                "(legacy or corrupt export; listed in skipped)",
                export_command(name) if legacy else None,
                {"count": len(rows), "skipped": [row["ref"] for row in rows]},
            )
        )
    return gaps


def coverage(
    config: CairnConfig,
    sources: Iterable[str],
    window: tuple[datetime, datetime],
    found: bool,
    *,
    skipped: Iterable[dict] = (),
    named: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """The store gaps that can hide a row from ``window`` for ``sources``. No QMD call.

    ``sources`` are the requested sources (``--source``, else the command's
    default). ``named`` are the ``--source`` values only: a disabled source is a warn
    when it was named, and info when it is only in the command's default list.
    ``found`` says whether the command returned any row. ``skipped`` are the sessions
    a digest could not read (:func:`skipped_gaps`).
    """
    now = now or datetime.now(UTC)
    sources = list(sources)
    facts = gather(config, sources, index=False, now=now)
    gaps = sorted(
        classify(facts, now, window=window, requested=named) + skipped_gaps(skipped),
        key=_order,
    )
    status = status_of(gaps)
    verdict = "found" if found else ("clean-miss" if status == "clean" else "degraded-miss")
    start, end = window
    return {
        "status": status,
        "verdict": verdict,
        "scope": STORE,
        "window": {"start": iso(start), "end": iso(end)},
        "sources": sources,
        "gaps": [g.to_json() for g in gaps],
    }


def coverage_note(cov: dict) -> str | None:
    """The one stderr line for a degraded coverage, else ``None``."""
    if cov["status"] != "degraded":
        return None
    warns = [g for g in cov["gaps"] if g["severity"] == WARN]
    first = warns[0]
    where = f"{first['collection']}: " if first["collection"] else ""
    more = len(cov["gaps"]) - 1
    tail = f"+{more} more; run cairn recall gaps" if more else "run cairn recall gaps"
    return f"Note: coverage degraded: {where}{first['message']} ({tail})"


__all__ = [
    "EXPORT_GRACE_S",
    "KINDS",
    "QMD_PROBE_TIMEOUT_S",
    "SCHEMA_VERSION",
    "CollectionFacts",
    "Facts",
    "Gap",
    "IndexFacts",
    "classify",
    "coverage",
    "coverage_note",
    "export_command",
    "gather",
    "render_text",
    "skipped_gaps",
    "status_of",
    "to_json",
]
