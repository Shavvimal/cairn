"""Queries over canonical exported Markdown, never over native source stores."""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .catalog import Catalog, load_catalog
from .config import CairnConfig
from .frontmatter import _escape_scalar, parse_frontmatter
from .registry import SOURCES
from .schema import Message, SessionData
from .timeutil import parse_iso

SCHEMA_VERSION = 1


def messages_of(data: SessionData) -> list[Message]:
    value = data.get("messages_ordered", data.get("messages", []))
    return value if isinstance(value, list) else []


def metadata_lines(source: str, native_id: str, data: SessionData) -> list[str]:
    messages = messages_of(data)
    times = sorted({ts for m in messages if (ts := m.get("timestamp"))})
    # Day granularity is all recall needs; every turn header in the body keeps the
    # full timestamp, so the frontmatter stays small even for thousand-turn sessions.
    days = sorted({d.date().isoformat() for ts in times if (d := _date(ts)) is not None})
    first = data.get("first_timestamp") or data.get("created_at")
    last = data.get("last_timestamp") or data.get("last_activity")
    values: dict[str, object] = {
        "cairn_schema": SCHEMA_VERSION,
        "source": source,
        "native_id": native_id,
        "source_revision": data.get("source_revision"),
        "cwd": data.get("cwd") or data.get("folder"),
        "project": data.get("project"),
        "started_at": first or (times[0] if times else None),
        "activity_at": last or (times[-1] if times else None),
        "time_precision": "messages" if times else ("endpoints" if first or last else "unknown"),
        "total_messages": len(messages) if source != "granola" else None,
        "user_message_count": sum(
            m.get("role") == "user" and bool(m.get("text", "").strip()) for m in messages
        )
        if source != "granola"
        else None,
    }
    lines = [
        f'{k}: "{_escape_scalar(str(v).replace(chr(10), " "))}"'
        for k, v in values.items()
        if v is not None
    ]
    lines.append("activity_dates:" if days else "activity_dates: []")
    lines.extend(f"  - {day}" for day in days)
    return lines


def file_revision(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(
        f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    ).hexdigest()


def needs_upgrade(record: SessionRecord | None) -> bool:
    return record is None or record.metadata.get("cairn_schema") != str(SCHEMA_VERSION)


def is_current(record: SessionRecord | None, revision: str) -> bool:
    """Annotation writes must never advance the native source checkpoint."""
    return not needs_upgrade(record) and (
        record is not None and record.metadata.get("source_revision") == revision
    )


def _number(value: object) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except ValueError:
        return None


def _date(value: object) -> datetime | None:
    result = parse_iso(value) if isinstance(value, str) else None
    return result.astimezone(UTC) if result else None


def _utc_day(value: object) -> datetime | None:
    """A bare ``YYYY-MM-DD`` as UTC midnight (never interpreted in local time)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class SessionRecord:
    source: str
    session_id: str
    path: Path
    title: str
    project: str | None
    cwd: str | None
    started_at: datetime | None
    activity: tuple[datetime, ...]
    precision: str
    total_messages: int | None
    user_messages: int | None
    metadata: dict

    @property
    def reference(self) -> str:
        return f"{self.source}:{self.session_id}"

    def in_window(self, start: datetime, end: datetime) -> bool:
        return any(start <= stamp < end for stamp in self.activity)

    def matches_project(self, project: str) -> bool:
        if project in (self.project, self.metadata.get("repo")):
            return True
        return bool(
            self.cwd
            and Path(self.cwd).expanduser().resolve() == Path(project).expanduser().resolve()
        )


def native_id_of(fm: dict, id_field: str) -> str | None:
    native_id = fm.get("native_id") or fm.get(id_field)
    return native_id if isinstance(native_id, str) and native_id else None


def _activity_days(fm: dict) -> tuple[datetime, ...]:
    days = fm.get("activity_dates")
    if isinstance(days, list):
        return tuple(sorted({d for s in days if (d := _utc_day(s)) is not None}))
    # Files written by the pre-release branch carry every timestamp; reduce to days
    # so their intermediate activity is not lost (their source_revision is unchanged,
    # so a re-export will not rewrite them and the native data may be gone).
    stamps = fm.get("activity_timestamps")
    if isinstance(stamps, list):
        return tuple(
            sorted(
                {
                    d.replace(hour=0, minute=0, second=0, microsecond=0)
                    for s in stamps
                    if (d := _date(s)) is not None
                }
            )
        )
    return ()


def record_from_frontmatter(
    path: Path, fm: dict, source: str, id_field: str, session_type: str
) -> SessionRecord | None:
    """Build a record from already-parsed frontmatter (``None`` if not a session)."""
    if fm.get("type") != session_type:
        return None
    native_id = native_id_of(fm, id_field)
    if native_id is None:
        return None
    if fm.get("source", source) != source:
        raise ValueError("source disagrees with collection")
    activity = _activity_days(fm)
    first = _date(fm.get("started_at"))
    precision = "messages"
    if not activity:
        # Only endpoints prove activity; never interpolate the intervening days.
        last = _date(fm.get("activity_at"))
        if not fm.get("cairn_schema"):
            last = _date(fm.get("last_activity"))
        activity = tuple(t for t in (first, last) if t is not None)
        precision = "endpoints"
        date = _date(fm.get("date"))
        if date is not None and first is None and fm.get("time_precision") != "unknown":
            activity = (*activity, date)
            precision = "date"
    return SessionRecord(
        source,
        native_id,
        path,
        str(fm.get("title") or "Untitled"),
        fm.get("project") or None,
        fm.get("cwd") or None,
        first or (min(activity) if activity else None),
        activity,
        precision,
        _number(fm.get("total_messages")),
        _number(fm.get("user_message_count")),
        fm,
    )


def read_record(path: Path, source: str, id_field: str, session_type: str) -> SessionRecord | None:
    """Parse one file in full and build its record (the catalog-free path)."""
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    return record_from_frontmatter(path, fm, source, id_field, session_type)


def collection_records(
    catalog: Catalog, source: str, id_field: str, session_type: str
) -> list[SessionRecord]:
    """Every session record in a refreshed catalog; unreadable files are reported."""
    result = []
    for rel, message in sorted(catalog.errors.items()):
        print(f"Warning: skipping {catalog.path_of(rel)}: {message}", file=sys.stderr)
    for path, fm in catalog.items():
        try:
            record = record_from_frontmatter(path, fm, source, id_field, session_type)
        except ValueError as exc:
            print(f"Warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        if record:
            result.append(record)
    return result


class SessionStore:
    def __init__(self, config: CairnConfig):
        self.config = config

    def records(self, sources: list[str] | None = None) -> list[SessionRecord]:
        result = []
        for source in SOURCES:
            if not source.session_type or (sources and source.source not in sources):
                continue
            if source.collection not in self.config.collections:
                continue
            catalog = load_catalog(self.config.output_dir(source.collection))
            result.extend(
                collection_records(catalog, source.source, source.id_field, source.session_type)
            )
            catalog.save()
        return result

    def resolve(self, reference: str, records: list[SessionRecord] | None = None) -> SessionRecord:
        return resolve(reference, self.records() if records is None else records)


def resolve(reference: str, records: list[SessionRecord]) -> SessionRecord:
    source, prefix = reference.split(":", 1) if ":" in reference else (None, reference)
    candidates = [
        r
        for r in records
        if (source is None or r.source == source) and r.session_id.startswith(prefix)
    ]
    exact = [r for r in candidates if r.session_id == prefix]
    candidates = exact or candidates
    if not candidates:
        raise ValueError(f"no session found matching {reference!r}")
    if len(candidates) > 1:
        raise ValueError(
            f"ambiguous session {reference!r}; choose " + ", ".join(r.reference for r in candidates)
        )
    return candidates[0]


def bounded_content(record: SessionRecord, max_messages: int, max_chars: int) -> str:
    """Read a bounded prefix. Length-framed turns cannot be forged by headings.

    Legacy Markdown has no reliable message boundaries, so it is bounded by
    characters only. We do not reinterpret arbitrary headings as transcript data.
    """
    with record.path.open(encoding="utf-8") as handle:
        content = handle.read(max_chars + 1)
    marker = re.compile(r"<!-- cairn:turn (\d+) -->\n")
    position = count = 0
    while match := marker.search(content, position):
        if count >= max_messages:
            content = content[: match.start()] + "\n… truncated (message limit)\n"
            break
        position = match.end() + int(match[1])
        count += 1
    if len(content) > max_chars:
        content = content[:max_chars] + "\n… truncated (character limit)\n"
    return content
