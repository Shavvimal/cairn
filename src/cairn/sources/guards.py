"""The parser-drift guard: a source file holds records but renders no messages.

A host can change its transcript format without notice. Before this guard the
change showed up as missing or empty documents, and nobody saw it until a recall
missed. :func:`check_parse` classifies one parse from the line counts that
:func:`cairn.jsonl.iter_jsonl` collects and the messages the adapter produced:

========================  ========  =============================================
kind                      severity  effect
========================  ========  =============================================
``unreadable-lines``      error     no line parses as JSON; nothing is written
``no-session-id``         error     records but no session id; nothing is written
``zero-messages``         error     conversational or unknown records, 0 messages
``unparseable-document``  error     the existing document cannot be round-tripped
``empty``                 info      only ignored record types; today's behaviour
``no-user-turns``         warn      a top-level session with no user message
========================  ========  =============================================

Error and warn findings print on stderr, persist in
``<data_root>/exporter/findings.json`` (:class:`FindingsStore`) until a clean parse
clears them, and show in ``cairn sync`` and ``cairn doctor``. Info findings are
recorded only so the gap check does not count the file as missing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..jsonl import JsonlStats
from ..schema import SessionData
from ..storage import atomic_write, lock_path, locked
from ..store import file_revision, messages_of
from .spec_targets import SpecTarget

ERROR = "error"
WARN = "warn"
INFO = "info"
FINDINGS_SCHEMA = 1
MAX_RECORD_TYPES = 20
REF_CHARS = 8
DRIFT_KINDS = ("unreadable-lines", "no-session-id", "zero-messages", "empty", "no-user-turns")
DOCUMENT_KIND = "unparseable-document"


class ParserDrift(ValueError):
    """An export refused to write because the parse looks like format drift."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Finding:
    source: str
    native_id: str
    path: str
    kind: str
    severity: str
    message: str
    detail: str = ""
    bytes: int = 0
    lines: int = 0
    records: int = 0
    malformed: int = 0
    record_types: dict[str, int] = field(default_factory=dict)
    messages: int = 0
    user: int = 0
    assistant: int = 0
    file_revision: str | None = None
    adapter_version: str = ""
    spec_target: str | None = None
    first_seen: str = ""
    last_seen: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.native_id}"

    @property
    def ref(self) -> str:
        """``source:<first 8 chars of the id>``, the short form every report prints."""
        return f"{self.source}:{self.native_id[:REF_CHARS]}"

    @property
    def line(self) -> str:
        """The stderr line: ``Error: ...`` or ``Warning: ...``."""
        return ("Warning: " if self.severity == WARN else "Error: ") + self.message

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> Finding:
        """Build a finding from its stored form; ``TypeError`` names a bad shape."""
        if not isinstance(raw, dict):
            raise TypeError(f"finding is a {type(raw).__name__}, not an object")
        known = {f.name: f.type for f in fields(cls)}
        for name, value in raw.items():
            allowed = _FIELD_TYPES.get(str(known.get(name)))
            if allowed is not None and (isinstance(value, bool) or not isinstance(value, allowed)):
                raise TypeError(f"finding field {name!r} is a {type(value).__name__}")
        return cls(**{k: v for k, v in raw.items() if k in known})


# Stored field types, by the annotation string ``fields()`` reports.
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "dict[str, int]": (dict,),
    "str | None": (str, type(None)),
}


def _tail(adapter_version: str, target: SpecTarget | None) -> str:
    spec = target.describe() if target is not None else "no spec target"
    return f"adapter {adapter_version}, {spec}"


def _revision(path: Path) -> str | None:
    try:
        return file_revision(path)
    except OSError:
        return None


def _renders(message: Mapping[str, Any]) -> bool:
    """True when the message renders a body: text, thinking, tool calls, code blocks
    or the unrecoverable-reasoning marker (see :func:`cairn.rendering.render_conversation`)."""
    return bool(
        message.get("text")
        or message.get("thinking")
        or message.get("tool_calls")
        or message.get("code_blocks")
        or message.get("reasoning_recoverable") is False
    )


def _unknown_kinds(
    stats: JsonlStats, conversational: frozenset[str], ignored: frozenset[str]
) -> list[str]:
    """Finer kinds under a wrapper type that neither set names.

    A wrapper is the top-level part of a conversational name with a slash (Codex
    ``event_msg``, ``response_item``). Under a wrapper the finer kind decides, so a
    renamed payload type (``response_item/message_v2``) is unknown like a renamed
    top-level type. A wrapper record whose kind cannot be read counts as
    ``<wrapper>/?``.
    """
    wrappers = sorted({t.split("/", 1)[0] for t in conversational if "/" in t})
    unknown: list[str] = []
    for wrapper in wrappers:
        sub = {k: n for k, n in stats.kinds.items() if k.split("/", 1)[0] == wrapper}
        unknown += sorted(k for k in sub if k not in conversational and k not in ignored)
        if stats.types[wrapper] > sum(sub.values()):
            unknown.append(f"{wrapper}/?")
    return unknown


def check_parse(
    *,
    source: str,
    native_id: str,
    path: Path,
    stats: JsonlStats,
    data: SessionData | dict | None,
    target: SpecTarget | None,
    adapter_version: str,
    top_level: bool = True,
) -> Finding | None:
    """Classify one parse; ``None`` means a clean parse (it clears an open finding).

    ``data`` is ``None`` when the adapter could not build a session at all (Codex: no
    ``session_meta`` id). Messages are what the document would render
    (:func:`cairn.store.messages_of`); user and assistant are counted by role, and
    only those two roles count as rendered for the zero-messages check. A message
    counts only when it renders something (:func:`_renders`): an adapter that still
    knows a record type but no longer finds its text appends empty turns.
    """
    messages = messages_of(data) if data else []  # type: ignore[arg-type]
    user = sum(1 for m in messages if m.get("role") == "user" and _renders(m))
    assistant = sum(1 for m in messages if m.get("role") == "assistant" and _renders(m))
    # Tool, system and agent turns alone are not a conversation: a parser that
    # files every record it no longer knows as a tool result still drifted.
    rendered = user + assistant
    conversational = target.conversational if target else frozenset()
    ignored = target.ignored if target else frozenset()
    # A conversational name is a top-level type ("user") or a finer kind
    # ("event_msg/user_message"); its top-level part is then a known type.
    conv_records = sum(stats.types[t] + stats.kinds[t] for t in conversational)
    known = ignored | {t.split("/", 1)[0] for t in conversational}
    unknown = sorted(t for t in stats.types if t not in known)
    unknown += _unknown_kinds(stats, conversational, ignored)
    tail = _tail(adapter_version, target)
    where = f"{source}:{native_id}"

    if stats.bytes > 0 and stats.records == 0 and stats.malformed > 0:
        kind, severity = "unreadable-lines", ERROR
        detail = f"0 of {stats.lines} lines parse as JSON ({stats.bytes} bytes)"
        message = f"parser drift: {where} {kind}: {detail} in {path}; {tail}; see 'cairn doctor'"
    elif data is None:
        if stats.records == 0:
            return None
        kind, severity = "no-session-id", ERROR
        detail = f"no session id in {stats.records} records ({stats.bytes} bytes)"
        message = f"parser drift: {where} {kind}: {detail} in {path}; {tail}; see 'cairn doctor'"
    elif not rendered and (conv_records or unknown):
        kind, severity = "zero-messages", ERROR
        detail = f"rendered 0 messages from {stats.records} records ({stats.bytes} bytes)"
        if unknown:
            detail += f"; unknown types: {', '.join(unknown[:5])}"
        message = (
            f"parser drift: {where} {kind}: rendered 0 messages from {stats.records} records "
            f"({stats.bytes} bytes) in {path}; {tail}; see 'cairn doctor'"
        )
    elif not rendered:
        kind, severity = "empty", INFO
        detail = f"{stats.records} records, all of ignored types"
        message = f"{where} {kind}: {detail} in {path}"
    elif top_level and assistant and not user:
        kind, severity = "no-user-turns", WARN
        detail = f"{assistant} assistant messages, 0 user messages"
        message = f"parser drift: {where} {kind}: {detail} in {path}; {tail}"
    else:
        return None
    return Finding(
        source=source,
        native_id=native_id,
        path=str(path),
        kind=kind,
        severity=severity,
        message=message,
        detail=detail,
        bytes=stats.bytes,
        lines=stats.lines,
        records=stats.records,
        malformed=stats.malformed,
        record_types=dict(stats.types.most_common(MAX_RECORD_TYPES)),
        messages=len(messages),
        user=user,
        assistant=assistant,
        file_revision=_revision(path),
        adapter_version=adapter_version,
        spec_target=target.checked if target else None,
    )


def document_finding(
    source: str,
    native_id: str,
    path: Path,
    reason: str,
    adapter_version: str,
    target: SpecTarget | None,
) -> Finding:
    """An existing document whose frontmatter cannot be parsed blocks its re-export."""
    return Finding(
        source=source,
        native_id=native_id,
        path=str(path),
        kind=DOCUMENT_KIND,
        severity=ERROR,
        message=(
            f"refusing to re-export {source}:{native_id}: existing document {path} "
            f"has unparseable frontmatter ({reason}); fix or move it"
        ),
        detail=f"existing document has unparseable frontmatter ({reason})",
        adapter_version=adapter_version,
        spec_target=target.checked if target else None,
    )


@dataclass
class ExportReport:
    """What one export run wrote and found, for ``export --json``."""

    written: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add_written(self, path: Path) -> None:
        if str(path) not in self.written:
            self.written.append(str(path))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    def as_dict(
        self, source: str, exit_code: int, adapter_version: str, target: SpecTarget | None
    ) -> dict:
        return {
            "schema_version": 1,
            "source": source,
            "exit": exit_code,
            "written": list(self.written),
            "failed": len({f.key for f in self.errors}),
            "findings": [
                {"ref": f.ref, "kind": f.kind, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
            "exporter_version": adapter_version,
            "spec_target": (
                {
                    "checked": target.checked,
                    "harness": target.harness,
                    "harness_version": target.harness_version,
                }
                if target
                else None
            ),
        }


def findings_path(data_root: Path) -> Path:
    return data_root / "exporter" / "findings.json"


class FindingsStore:
    """``<data_root>/exporter/findings.json``: open findings, keyed ``source:native_id``.

    Read once per process. Written only on a change (a new finding, a changed kind,
    a cleared one), as a read-modify-write under the findings lock, so a clean parse
    of an id with no entry costs no I/O. Entries whose source path no longer exists
    are dropped on every read. Readers (doctor, the gap check) take no lock.
    """

    def __init__(self, data_root: Path):
        self.path = findings_path(data_root)
        self._cache: dict[str, Finding] | None = None
        self._stale: set[str] = set()  # keys on disk whose file is gone

    def _read(self) -> dict[str, Finding]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("the file is not a JSON object")
            if raw.get("schema_version") != FINDINGS_SCHEMA:
                raise ValueError(f"schema_version is not {FINDINGS_SCHEMA}")
            entries = raw.get("findings")
            if not isinstance(entries, dict):
                raise TypeError("findings is not an object")
            loaded = {key: Finding.from_dict(value) for key, value in entries.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(
                f"Warning: ignoring unreadable {self.path} ({exc}); it is rewritten on the next change",
                file=sys.stderr,
            )
            return {}
        # A finding whose file was moved or deleted no longer holds anything: the
        # user followed "fix or move it", or the host pruned the transcript.
        kept = {key: f for key, f in loaded.items() if Path(f.path).exists()}
        self._stale = set(loaded) - set(kept)
        return kept

    @property
    def findings(self) -> dict[str, Finding]:
        if self._cache is None:
            self._cache = self._read()
        return self._cache

    def _write(self, key: str, finding: Finding | None) -> None:
        with locked(lock_path(self.path.parent, "findings")):
            current = self._read()
            if finding is None:
                current.pop(key, None)
            else:
                current[key] = finding
            payload = {
                "schema_version": FINDINGS_SCHEMA,
                "findings": {k: f.as_dict() for k, f in sorted(current.items())},
            }
            atomic_write(self.path, json.dumps(payload, indent=1) + "\n")
            self._cache = current

    def record(self, finding: Finding) -> None:
        """Store ``finding`` unless the same kind is already open for its id."""
        previous = self.findings.get(finding.key)
        if previous is not None and previous.kind == finding.kind:
            return
        now = _now()
        finding.first_seen = previous.first_seen if previous else now
        finding.last_seen = now
        self._write(finding.key, finding)

    def clear(self, key: str, kinds: tuple[str, ...] | None = None) -> None:
        """Drop the open finding for ``key`` (only when its kind is in ``kinds``)."""
        open_finding = self.findings.get(key)
        if open_finding is not None and (kinds is None or open_finding.kind in kinds):
            self._write(key, None)
        elif key in self._stale:
            self._write(key, None)  # drop the entry whose file is gone from disk too

    def open_findings(self, severities: tuple[str, ...] = (ERROR, WARN)) -> list[Finding]:
        """Open findings of ``severities``, oldest first."""
        found = [f for f in self.findings.values() if f.severity in severities]
        return sorted(found, key=lambda f: (f.first_seen, f.key))

    def held(self, source: str) -> set[str]:
        """Ids the gap check must not count as missing (error and info findings)."""
        return {
            f.native_id
            for f in self.findings.values()
            if f.source == source and f.severity in (ERROR, INFO)
        }


__all__ = [
    "ERROR",
    "INFO",
    "WARN",
    "ExportReport",
    "Finding",
    "FindingsStore",
    "ParserDrift",
    "check_parse",
    "document_finding",
    "findings_path",
]
