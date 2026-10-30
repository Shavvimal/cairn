"""``cairn recall verify``: check the citations and quotes of a journal against a digest.

A journal (any Markdown file an agent wrote from ``cairn recall digest``) cites a frame
as ``[SOURCE:ID fN]``. Verify resolves each citation to a frame the digest kept, checks
that the export still holds that frame unchanged (the ``recall frame`` hash check), and
checks that every quoted span is a substring of a frame its block cites. With ``--fix``
it moves every failing Markdown block under ``## Unverified``. It calls no model and
does not judge uncited prose.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import digest
from .frames import Frame, locate, read_text
from .frontmatter import FrontmatterParseError, parse_frontmatter
from .storage import atomic_write, document_lock, locked
from .store import SessionRecord, SessionStore, resolve

SCHEMA_VERSION = 1
# A citation names one frame of one session: ``[claude:3f2a9c1e f11]``. The id is a full
# session id or a unique prefix of at least 8 characters; ``(?!\()`` keeps Markdown link
# text out. Layer users import this constant rather than copying the pattern.
CITATION_RE = re.compile(
    r"\[(?P<source>[a-z][a-z0-9-]*):(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{7,})"
    r" f(?P<ordinal>[0-9]+)\](?!\()"
)
# Anything shaped like an attempt at a citation; where CITATION_RE does not match at
# the same position it is a ``malformed-citation``.
NEAR_CITATION_RE = re.compile(r"\[(?:[a-z][a-z0-9-]*:[^\]\s]*\s+)?f[0-9]+[^\]]*\]")
QUOTE_MIN_CHARS = 15
UNVERIFIED_HEADING = "## Unverified"
REASONS = (
    "malformed-citation",
    "session-not-in-digest",
    "ambiguous-session",
    "sidecar-missing",
    "bad-ordinal",
    "frame-not-in-digest",
    "export-unreadable",
    "hash-mismatch",
    "quote-not-found",
    "quote-uncited",
)
EXIT_FAILED = 4

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_TABLE_ROW = re.compile(r"^ {0,3}\|")
# A table delimiter row (``--- | :---:``), with or without outer pipes; it makes the line
# before it a table header, so a table without outer pipes still splits into rows.
_DELIMITER_CELL = r"[ \t]*:?-+:?[ \t]*"
_TABLE_DELIMITER = re.compile(
    rf"^ {{0,3}}(?:\|?{_DELIMITER_CELL}(?:\|{_DELIMITER_CELL})+\|?|\|{_DELIMITER_CELL}\|)[ \t]*$"
)
_BLOCKQUOTE = re.compile(r"^ {0,3}>")
_BLOCKQUOTE_PREFIX = re.compile(r"^ {0,3}> ?")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|[0-9]{1,9}[.)])(?:[ \t]|$)")
_BACKTICKS = re.compile(r"`+")
# Curly double quotes (U+201C, U+201D, U+201E) and single quotes (U+2018, U+2019).
_PUNCTUATION = {0x201C: '"', 0x201D: '"', 0x201E: '"', 0x2018: "'", 0x2019: "'"}
_QUOTE_PAIRS = {'"': '"', chr(0x201C): chr(0x201D)}  # straight, and curly open/close
_DETAIL_QUOTE_CHARS = 120


@dataclass
class Block:
    """A Markdown block: 0-based first and last line, kind, original and masked text."""

    first: int
    last: int
    kind: str  # heading, item, table, quote or paragraph
    text: str = ""
    masked: str = ""
    reasons: list[str] = field(default_factory=list)
    attached_to: Block | None = None

    def line_of(self, position: int) -> int:
        """The 1-based file line of a character position in this block."""
        return self.first + 1 + self.masked.count("\n", 0, position)


@dataclass(frozen=True)
class Session:
    """One digest file: its full ref and the sidecar that locates its frames."""

    ref: str
    sidecar: Path


@dataclass
class Resolved:
    """A citation checked against the digest and the export; ``reason`` is ``None``
    when it verified, and then ``body`` is the cited frame as exported."""

    ref: str | None
    ordinal: int
    reason: str | None = None
    detail: str = ""
    body: str = ""


def normalise(text: str) -> str:
    """NFC, straight quotes, one space per whitespace run, stripped; case is kept."""
    text = unicodedata.normalize("NFC", text).translate(_PUNCTUATION)
    return " ".join(text.split())


def _clip(text: str) -> str:
    if len(text) <= _DETAIL_QUOTE_CHARS:
        return text
    return text[: _DETAIL_QUOTE_CHARS - 3] + "..."


def mask_code(text: str) -> str:
    """``text`` with every inline code span (matching runs of backticks) blanked to
    spaces; newlines stay, so positions and line numbers are unchanged."""
    chars = list(text)
    position = 0
    while opening := _BACKTICKS.search(text, position):
        width = len(opening[0])
        closing = None
        for candidate in _BACKTICKS.finditer(text, opening.end()):
            if len(candidate[0]) == width:
                closing = candidate
                break
        if closing is None:
            position = opening.end()
            continue
        for index in range(opening.start(), closing.end()):
            if chars[index] != "\n":
                chars[index] = " "
        position = closing.end()
    return "".join(chars)


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def body_range(lines: list[str]) -> tuple[int, int, list[bool]]:
    """The checked lines ``[start, end)`` and, per line, whether it is fenced code.

    ``start`` skips a leading ``---`` front matter block; ``end`` is the last line that
    is exactly ``## Unverified`` outside a fence, so text moved there is not checked.
    """
    start = 0
    if lines and _bare(lines[0]) == "---":
        for index in range(1, len(lines)):
            if _bare(lines[index]) in ("---", "..."):
                start = index + 1
                break
    fenced = [False] * len(lines)
    fence: str | None = None
    end = len(lines)
    for index in range(start, len(lines)):
        line = _bare(lines[index])
        if fence is not None:
            fenced[index] = True
            stripped = line.strip()
            if (
                stripped.startswith(fence)
                and set(stripped) == {fence[0]}
                and len(line) - len(line.lstrip(" ")) <= 3
            ):
                fence = None
            continue
        opening = _FENCE_OPEN.match(line)
        # A backtick fence's info string holds no backtick (CommonMark 4.5), so a line
        # that starts with ```code``` is inline code, not a fence that never closes.
        if opening and not (opening[1][0] == "`" and "`" in line[opening.end() :]):
            fenced[index] = True
            fence = opening[1]
            continue
        if line == UNVERIFIED_HEADING:
            end = index
    return start, end, fenced


def _table_lines(lines: list[str], start: int, end: int, fenced: list[bool]) -> set[int]:
    """Lines of tables found by their delimiter row: the header, the delimiter and every
    following line up to a blank, fenced, heading or ``>`` line."""
    rows: set[int] = set()
    for index in range(start + 1, end):
        header = _bare(lines[index - 1])
        if (
            fenced[index]
            or fenced[index - 1]
            or "|" not in header
            or not _TABLE_DELIMITER.match(_bare(lines[index]))
        ):
            continue
        rows.update((index - 1, index))
        for row in range(index + 1, end):
            line = _bare(lines[row])
            if fenced[row] or not line.strip() or _HEADING.match(line) or _BLOCKQUOTE.match(line):
                break
            rows.add(row)
    return rows


def split_blocks(lines: list[str]) -> list[Block]:
    """The blocks of the checked part of ``lines`` (journal-verify spec 4.2)."""
    start, end, fenced = body_range(lines)
    table = _table_lines(lines, start, end, fenced)
    blocks: list[Block] = []
    current: Block | None = None
    for index in range(start, end):
        line = _bare(lines[index])
        if fenced[index] or not line.strip():
            current = None
            continue
        if _HEADING.match(line):
            kind = "heading"
        elif index in table or _TABLE_ROW.match(line):
            kind = "table"
        elif _BLOCKQUOTE.match(line):
            kind = "quote"
        elif _LIST_ITEM.match(line):
            kind = "item"
        else:
            kind = "continuation"
        joins = current is not None and (
            (kind == "quote" and current.kind == "quote")
            or (kind == "continuation" and current.kind in ("item", "paragraph"))
        )
        if joins and current is not None:
            current.last = index
            continue
        current = Block(index, index, "paragraph" if kind == "continuation" else kind)
        blocks.append(current)
        if kind in ("heading", "table"):
            current = None
    for block in blocks:
        block.text = "".join(lines[block.first : block.last + 1])
        block.masked = mask_code(block.text)
    return blocks


def _closing(masked: str, char: str, closer: str, after: int) -> int:
    """The position of the mark that closes a quote opened just before ``after``, or -1.
    A straight ``"`` closes only after a non-space character."""
    close = masked.find(closer, after)
    if char != '"':
        return close
    while close >= 0 and masked[close - 1].isspace():
        close = masked.find(closer, close + 1)
    return close


def inline_quotes(block: Block) -> list[tuple[int, str]]:
    """``(position, text)`` of each ``"..."`` or curly-quoted span outside inline code.

    A straight ``"`` opens a quote only at the start or after a space or an opening
    bracket, and before a non-space (so ``12"`` is an inch mark). An opener with no
    closer is skipped; the scan goes on, so later quotes are still checked.
    """
    found = []
    position = 0
    masked = block.masked
    while position < len(masked):
        char = masked[position]
        closer = _QUOTE_PAIRS.get(char)
        if closer is None or (char == '"' and not _opens(masked, position)):
            position += 1
            continue
        close = _closing(masked, char, closer, position + 1)
        if close < 0:
            position += 1
            continue
        found.append((position, block.text[position + 1 : close]))
        position = close + 1
    return found


def _opens(masked: str, position: int) -> bool:
    """Whether the straight ``"`` at ``position`` can open a quote."""
    before = masked[position - 1] if position else " "
    after = masked[position + 1] if position + 1 < len(masked) else " "
    return (before.isspace() or before in "([{-/") and not after.isspace()


def blockquote_text(block: Block) -> str:
    """A ``>`` block's text with the marker and one following space removed per line."""
    return "".join(
        _BLOCKQUOTE_PREFIX.sub("", line, count=1) for line in block.text.splitlines(True)
    )


class Verifier:
    """Resolves citations against one digest directory and the exported store."""

    def __init__(self, directory: Path, store: SessionStore):
        self.directory = directory
        self.store = store
        self.sessions = self._index()
        self._sidecars: dict[Path, dict[int, dict] | str] = {}
        self._exports: dict[str, tuple[str, int, list[Frame]] | str] = {}
        self._records: list[SessionRecord] | None = None
        self._cache: dict[tuple[str, str, int], Resolved] = {}

    def _index(self) -> list[Session]:
        """Every digest file's ``ref`` and sidecar path (spec 4.1)."""
        if not (self.directory / digest.OUT_MARKER).is_file():
            raise ValueError(f"not a digest directory: {self.directory} has no {digest.OUT_MARKER}")
        sessions = []
        for path in sorted(self.directory.iterdir()):
            if not (
                path.is_file()
                and path.name.endswith(".md")
                and digest._DIGEST_FILE.match(path.name)
            ):
                continue
            try:
                ref = parse_frontmatter(path.read_text(encoding="utf-8")).get("ref")
            except (OSError, ValueError, FrontmatterParseError) as exc:
                print(f"Warning: skipping {path}: {exc}", file=sys.stderr)
                continue
            if not isinstance(ref, str) or ":" not in ref:
                print(f"Warning: skipping {path}: no ref in its front matter", file=sys.stderr)
                continue
            sessions.append(
                Session(ref, path.with_name(path.name[: -len(".md")] + ".frames.jsonl"))
            )
        return sessions

    def check(self, source: str, prefix: str, ordinal: int) -> Resolved:
        key = (source, prefix, ordinal)
        if key not in self._cache:
            self._cache[key] = self._check(source, prefix, ordinal)
        return self._cache[key]

    def _check(self, source: str, prefix: str, ordinal: int) -> Resolved:
        cited = f"{source}:{prefix}"
        candidates = [
            s
            for s in self.sessions
            if s.ref.split(":", 1)[0] == source and s.ref.split(":", 1)[1].startswith(prefix)
        ]
        exact = [s for s in candidates if s.ref == cited]
        candidates = exact or candidates
        if not candidates:
            return Resolved(
                None,
                ordinal,
                "session-not-in-digest",
                f"no session in {self.directory} matches {cited}",
            )
        if len(candidates) > 1:
            refs = ", ".join(s.ref for s in candidates)
            return Resolved(
                None,
                ordinal,
                "ambiguous-session",
                f"{cited} matches {len(candidates)} sessions ({refs}); cite the full ref",
            )
        session = candidates[0]
        rows = self._sidecar(session.sidecar)
        if isinstance(rows, str):
            return Resolved(session.ref, ordinal, "sidecar-missing", rows)
        row = rows.get(ordinal)
        if row is None:
            return Resolved(
                session.ref,
                ordinal,
                "bad-ordinal",
                f"ordinal {ordinal} not in {session.sidecar.name} ({len(rows)} frames)",
            )
        if row.get("kept") is not True:
            return Resolved(
                session.ref,
                ordinal,
                "frame-not-in-digest",
                f"f{ordinal} of {session.ref} is not in the digest body (kept: false)",
            )
        export = self._export(session.ref)
        if isinstance(export, str):
            return Resolved(session.ref, ordinal, "export-unreadable", export)
        text, anchor, frames = export
        if ordinal >= len(frames):
            return Resolved(
                session.ref,
                ordinal,
                "hash-mismatch",
                f"{session.ref} frame {ordinal} changed since the digest (the export now has "
                f"{len(frames)} frames); the export was re-rendered",
            )
        frame = frames[ordinal]
        if not digest.frame_unchanged(text, anchor, frame, row):
            return Resolved(
                session.ref,
                ordinal,
                "hash-mismatch",
                digest.changed_detail(session.ref, ordinal, row, frame),
            )
        return Resolved(session.ref, ordinal, body=frame.body)

    def _sidecar(self, path: Path) -> dict[int, dict] | str:
        if path not in self._sidecars:
            if not path.is_file():
                self._sidecars[path] = f"no sidecar {path.name} in {self.directory}"
            else:
                try:
                    self._sidecars[path] = digest.load_sidecar(path)
                except (OSError, ValueError) as exc:
                    self._sidecars[path] = f"cannot read {path.name}: {exc}"
        return self._sidecars[path]

    def _export(self, ref: str) -> tuple[str, int, list[Frame]] | str:
        if ref not in self._exports:
            try:
                if self._records is None:
                    self._records = self.store.records()
                record = resolve(ref, self._records)
                text = read_text(record.path)
                anchor, frames = locate(text)
                self._exports[ref] = (text, anchor, frames)
            except (OSError, ValueError) as exc:  # no session, LegacyExport, CorruptExport
                self._exports[ref] = f"{ref}: {exc}"
        return self._exports[ref]


@dataclass
class Report:
    """The counts and failures of one run; the shape of ``--json`` (spec 5.1)."""

    citations: dict = field(default_factory=lambda: {"total": 0, "verified": 0, "failed": 0})
    quotes: dict = field(
        default_factory=lambda: {
            "total": 0,
            "verified": 0,
            "failed": 0,
            "unchecked": 0,
            "skipped_short": 0,
        }
    )
    failures: list[dict] = field(default_factory=list)

    def fail(
        self,
        block: Block,
        line: int,
        kind: str,
        reason: str,
        text: str,
        detail: str,
        ref=None,
        ordinal=None,
    ) -> None:
        if reason not in block.reasons:
            block.reasons.append(reason)
        self.failures.append(
            {
                "line": line,
                "block": [block.first + 1, block.last + 1],
                "kind": kind,
                "reason": reason,
                "text": text,
                "ref": ref,
                "ordinal": ordinal,
                "detail": detail,
            }
        )


def _check_quote(
    report: Report, block: Block, line: int, raw: str, cited: list[Resolved], attempts: int
) -> None:
    """Check one quote against the frames its citations resolved to (spec 4.4)."""
    quote = normalise(CITATION_RE.sub("", raw))
    if not quote:
        return
    report.quotes["total"] += 1
    shown = f'"{_clip(quote)}"'
    if not cited and not attempts:
        report.quotes["failed"] += 1
        report.fail(
            block, line, "quote", "quote-uncited", raw, f"{shown} has no citation in its block"
        )
        return
    good = [c for c in cited if c.reason is None]
    if not good:
        report.quotes["unchecked"] += 1
        return
    if any(quote in normalise(c.body) for c in good):
        report.quotes["verified"] += 1
        return
    report.quotes["failed"] += 1
    places = " or ".join(f"f{c.ordinal} of {c.ref}" for c in good)
    one = good[0] if len(good) == 1 else None
    report.fail(
        block,
        line,
        "quote",
        "quote-not-found",
        raw,
        f"{shown} is not in {places}",
        ref=one.ref if one else None,
        ordinal=one.ordinal if one else None,
    )


def check_blocks(blocks: list[Block], verifier: Verifier) -> Report:
    """Run every citation and quote check over ``blocks`` in file order."""
    report = Report()
    cited: dict[int, list[Resolved]] = {}
    attempts: dict[int, int] = {}
    for index, block in enumerate(blocks):
        resolved: list[Resolved] = []
        malformed = 0
        for near in NEAR_CITATION_RE.finditer(block.masked):
            if CITATION_RE.match(block.masked, near.start()) or block.masked.startswith(
                "(", near.end()
            ):
                continue
            malformed += 1
            report.citations["total"] += 1
            report.citations["failed"] += 1
            report.fail(
                block,
                block.line_of(near.start()),
                "citation",
                "malformed-citation",
                near[0],
                f"{near[0]} is not a citation; write [SOURCE:ID fN] with an id of at least 8 characters",
            )
        for match in CITATION_RE.finditer(block.masked):
            report.citations["total"] += 1
            result = verifier.check(match["source"], match["id"], int(match["ordinal"]))
            resolved.append(result)
            if result.reason is None:
                report.citations["verified"] += 1
                continue
            report.citations["failed"] += 1
            report.fail(
                block,
                block.line_of(match.start()),
                "citation",
                result.reason,
                match[0],
                result.detail,
                ref=result.ref,
                ordinal=result.ordinal,
            )
        cited[index], attempts[index] = resolved, malformed + len(resolved)
        if block.kind == "quote":
            own, tries = resolved, attempts[index]
            if not tries and index > 0:
                before = blocks[index - 1]
                if block.first - before.last <= 2:
                    own, tries = cited[index - 1], attempts[index - 1]
                    if tries:
                        block.attached_to = before
            raw = blockquote_text(block)
            _check_quote(report, block, block.first + 1, raw, own, tries)
            continue
        for position, raw in inline_quotes(block):
            if len(normalise(CITATION_RE.sub("", raw))) < QUOTE_MIN_CHARS:
                report.quotes["skipped_short"] += 1
                continue
            _check_quote(report, block, block.line_of(position), raw, resolved, attempts[index])
    return report


def fix_text(lines: list[str], blocks: list[Block]) -> tuple[str, int]:
    """The file with every failing block (and any block quote attached to one) moved
    under ``## Unverified``, and the number of blocks moved (spec 4.5)."""
    moving = [
        b for b in blocks if b.reasons or (b.attached_to is not None and b.attached_to.reasons)
    ]
    if not moving:
        return "".join(lines), 0
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    removed: set[int] = set()
    for block in moving:
        removed.update(range(block.first, block.last + 1))
        after = block.last + 1
        if after < len(lines) and not lines[after].strip() and after not in removed:
            removed.add(after)
    text = "".join(line for index, line in enumerate(lines) if index not in removed)
    _, end, _ = body_range(lines)
    if text and not text.endswith("\n"):
        text += newline
    if end == len(lines):
        text += f"{newline}{UNVERIFIED_HEADING}{newline}{newline}"
    elif not text.endswith(f"{newline}{newline}"):
        text += newline
    for block in moving:
        reasons = list(block.reasons)
        if block.attached_to is not None:
            reasons += [r for r in block.attached_to.reasons if r not in reasons]
        text += (
            f"<!-- cairn:unverified line={block.first + 1} reason={','.join(reasons)} -->{newline}"
        )
        body = block.text
        if not body.endswith("\n"):
            body += newline
        text += body + newline
    return text, len(moving)


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _replace(path: Path, snapshot: str, text: str, shown: str) -> None:
    """Write the fixed journal unless it changed since ``snapshot`` was read.

    Under the document lock, so cairn writers do not interleave; the re-read catches an
    editor that saved meanwhile. A symlink is written through and the mode is kept.
    """
    target = path.resolve()
    with locked(document_lock(target)):
        if _read(target) != snapshot:
            raise ValueError(f"{shown} changed while verify ran; nothing written, run it again")
        atomic_write(target, text, mode=stat.S_IMODE(target.stat().st_mode))


def run_verify(args: argparse.Namespace, store: SessionStore) -> int:
    """``cairn recall verify FILE --digest DIR [--json] [--fix]``.

    Exit 0 when everything verifies (or ``--fix`` moved every failure), 4 when at
    least one check failed without ``--fix``; errors raise and exit 1. Never 3, which
    a routine ``validate`` reads as an empty day.
    """
    path = Path(args.file).expanduser()
    if not path.exists():
        raise ValueError(f"no such file: {args.file}")
    directory = Path(args.digest).expanduser()
    verifier = Verifier(directory, store)
    snapshot = _read(path)
    lines = snapshot.splitlines(keepends=True)
    blocks = split_blocks(lines)
    report = check_blocks(blocks, verifier)
    moved = 0
    if args.fix and report.failures:
        if not os.access(path, os.W_OK):
            raise PermissionError(f"cannot write {args.file}: permission denied")
        text, moved = fix_text(lines, blocks)
        _replace(path, snapshot, text, args.file)
    if report.citations["total"] == 0 and report.quotes["total"] == 0:
        print(f"Warning: {args.file} has no citations", file=sys.stderr)
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "file": str(path.absolute()),
                    "digest": str(directory.absolute()),
                    "ok": not report.failures,
                    "citations": report.citations,
                    "quotes": report.quotes,
                    "failures": report.failures,
                    "moved": moved,
                },
                indent=2,
            )
        )
    else:
        for failure in report.failures:
            print(f"{args.file}:{failure['line']}: {failure['reason']}: {failure['detail']}")
        c, q = report.citations, report.quotes
        print(
            f"verified {c['verified']}/{c['total']} citations, {q['verified']}/{q['total']} quotes "
            f"({q['skipped_short']} short quotes not checked); {len(report.failures)} failures"
        )
        if moved:
            print(f"moved {moved} blocks to {UNVERIFIED_HEADING} in {args.file}")
    if report.failures and not args.fix:
        return EXIT_FAILED
    return 0


__all__ = [
    "CITATION_RE",
    "NEAR_CITATION_RE",
    "QUOTE_MIN_CHARS",
    "REASONS",
    "SCHEMA_VERSION",
    "normalise",
    "run_verify",
    "split_blocks",
]
