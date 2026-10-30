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

The report never repairs anything: each gap names the command that fixes it.
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
from .harnesses.antigravity import conversation_transcript
from .qmd import CollectionInfo, collection_details, read_stamp
from .registry import SOURCES, Source
from .runtime import qmd_resources
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


def _newest(paths: Iterable[Path], skip: tuple[str, ...]) -> float | None:
    """The largest mtime among non-empty regular files, skipping the live session."""
    newest: float | None = None
    for path in paths:
        if skip and any(token in str(path) for token in skip):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat_mod.S_ISREG(st.st_mode) or st.st_size == 0:
            continue
        if newest is None or st.st_mtime > newest:
            newest = st.st_mtime
    return newest


def _scandir(path: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as entries:
            return list(entries)
    except OSError:
        return []


def _claude_files(store: Path) -> Iterable[Path]:
    for project in _scandir(store):
        if project.is_dir(follow_symlinks=False):
            for entry in _scandir(Path(project.path)):
                if entry.name.endswith(".jsonl"):
                    yield Path(entry.path)


def _walk_files(root: Path, accept: Callable[[str], bool]) -> Iterable[Path]:
    for directory, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            if accept(name):
                yield Path(directory) / name


def _antigravity_files(store: Path) -> Iterable[Path]:
    for entry in _scandir(store / "brain"):
        if entry.is_dir(follow_symlinks=False):
            transcript = conversation_transcript(store, entry.name)
            if transcript is not None:
                yield transcript


def _store_files(store: Path, names: Iterable[str]) -> Iterable[Path]:
    return (store / name for name in names)


def _granola_files(store: Path) -> Iterable[Path]:
    for entry in _scandir(store):
        if entry.name.startswith("cache-v") and entry.name.endswith(".json"):
            yield Path(entry.path)


# source -> (probe over the store, basis). ``store`` basis: the mtime changes
# without a new session (a database or cache rewrite), so its lag is never a warn.
NativeProbe = Callable[[Path], Iterable[Path]]
PROBES: dict[str, tuple[NativeProbe, str]] = {
    "claude": (_claude_files, "file"),
    "codex": (
        lambda store: _walk_files(
            store, lambda n: n.startswith("rollout-") and n.endswith(".jsonl")
        ),
        "file",
    ),
    "antigravity": (_antigravity_files, "file"),
    "cursor": (
        lambda store: _store_files(
            store, ("globalStorage/state.vscdb", "globalStorage/state.vscdb-wal")
        ),
        "store",
    ),
    "granola": (_granola_files, "store"),
}


def _native(source: str | None, coll: Collection) -> tuple[datetime | None, str, bool, str | None]:
    """``(native_newest_at, basis, store_exists, store_error)`` for one collection."""
    skip = _current_session_ids()
    if source == "service-docs":
        roots = [s.path for s in coll.service_sources]
        missing = [str(r) for r in roots if not r.is_dir()]
        newest = _newest(
            (p for r in roots for p in _walk_files(r, lambda n: n.endswith(".md"))), skip
        )
        error = f"service source {', '.join(missing)} not found" if missing else None
        return (
            datetime.fromtimestamp(newest, UTC) if newest is not None else None,
            "file",
            not missing,
            error,
        )
    if coll.store is None or source not in PROBES:
        return None, "none", False, None
    store = coll.store
    try:
        os.stat(store)
    except FileNotFoundError:
        return None, "none", False, None
    except OSError as exc:
        return None, "none", False, str(exc)
    if not os.access(store, os.R_OK | os.X_OK) and store.is_dir():
        return None, "none", False, f"store {store} is not readable"
    probe, basis = PROBES[source]
    newest = _newest(probe(store), skip)
    return (
        datetime.fromtimestamp(newest, UTC) if newest is not None else None,
        basis,
        True,
        None,
    )


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def _source_of(collection: str) -> Source | None:
    return next((s for s in SOURCES if s.collection == collection), None)


def _exports(output_dir: Path, source: Source | None) -> tuple[dict, list[int]]:
    """Export facts plus every markdown file's ``mtime_ns`` (for the index count)."""
    if source is not None and source.session_type:
        catalog = load_catalog(output_dir)
        catalog.save()
        mtimes = [entry.mtime_ns for entry in catalog.entries.values()]
        newest: datetime | None = None
        exported = 0
        for entry in catalog.entries.values():
            fm = entry.frontmatter
            if fm.get("type") not in source.session_types:
                continue
            exported += 1
            stamp = _utc(fm.get("activity_at")) or _utc(fm.get("started_at"))
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
        facts = {
            "markdown_files": len(catalog.entries) + len(catalog.errors),
            "exported_docs": exported,
            "unreadable": len(catalog.errors),
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
    return facts, mtimes


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
        exports, mtimes = _exports(config.output_dir(name), source)
        native_at, basis, exists, error = (
            _native(source.source if source else None, coll)
            if coll.sync.enabled
            else (None, "none", coll.store is not None and coll.store.exists(), None)
        )
        rows.append(
            (
                CollectionFacts(
                    collection=name,
                    source=source.source if source else None,
                    enabled=coll.sync.enabled,
                    store=coll.store,
                    store_exists=exists,
                    store_error=error,
                    session=bool(source and source.session_type),
                    native_newest_at=native_at,
                    native_basis=basis,
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
            details = collection_details(binary, QMD_PROBE_TIMEOUT_S)
        except (RuntimeError, OSError) as exc:
            qmd_state, qmd_error = "failed", str(exc)
        else:
            if not details:
                qmd_state, qmd_error = "failed", "`qmd collection list` printed no qmd:// block"
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


def _store_gaps(c: CollectionFacts, now: datetime, named: set[str]) -> list[Gap]:
    def gap(kind: str, severity: str, message: str, fix: str | None, **extra) -> Gap:
        interval = extra.pop("interval", None)
        return Gap(kind, severity, STORE, c.collection, c.source, message, fix, extra, interval)

    gaps: list[Gap] = []
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
    if c.unreadable:
        gaps.append(
            gap(
                "unreadable-exports",
                WARN,
                f"{c.unreadable} exported files could not be parsed; recall skips them "
                "(cairn recall list prints each one)",
                None,
                count=c.unreadable,
            )
        )
    native = c.native_newest_at
    if native is None:
        return gaps
    skew = (native - now).total_seconds()
    clamped = min(native, now)
    skew_note = " (clock skew)" if skew > CLOCK_SKEW_S else ""
    if c.exported_docs == 0:
        fix = f"cairn {c.source} export --all" if c.source else FIX_SYNC
        gaps.append(
            gap(
                "never-exported",
                WARN,
                f"native files exist (newest {_minute(clamped)}) but nothing is exported"
                + skew_note,
                fix,
                basis=c.native_basis,
                interval=(None, clamped),
            )
        )
        return gaps
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
            and c.index.basis == "qmd-relative"
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
                    "index-lag is a lower bound from qmd's Updated age",
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
    if gap.kind == "never-exported":
        return high is not None and high >= start
    # The unexported interval (low, high] intersects [start, end).
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


def coverage(
    config: CairnConfig,
    sources: Iterable[str],
    window: tuple[datetime, datetime],
    found: bool,
    *,
    now: datetime | None = None,
) -> dict:
    """The store gaps that can hide a row from ``window`` for ``sources``. No QMD call.

    ``sources`` are the requested sources (``--source``, else the command's
    default). ``found`` says whether the command returned any row.
    """
    now = now or datetime.now(UTC)
    sources = list(sources)
    facts = gather(config, sources, index=False, now=now)
    gaps = classify(facts, now, window=window, requested=sources)
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
    "gather",
    "render_text",
    "status_of",
    "to_json",
]
