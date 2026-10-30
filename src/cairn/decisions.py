"""The ``## Decisions`` table of a session document: grammar, parser, writer and reader.

A session document can hold one markdown table of claims directly after ``## My
Notes``::

    ## Decisions

    | # | claim | kind | confidence | since | revisit_by | source |
    |---|-------|------|------------|-------|------------|--------|
    | 1 | One PR per layer | decision | high | 2026-10-28 |  | [claude:3f2a9c1e f12] |

A row's ``#`` is its stable local id: cairn never renumbers a row and never reuses a
number. Its global id is ``<source>:<native_id>#<n>``. A corrected row is kept and
struck in place, ``~~<claim>~~[ <marker>]``. This module writes the marker
``superseded by #M`` only. The reader keeps any other marker exactly as written and
classifies such a row as ``struck``, so a later writer can add its own marker without a
change to the format. Readers hide struck rows by default.

Writes go through :func:`mutate_table`: under the document lock it parses the table,
applies an edit to the rows, rewrites only the lines of changed rows, inserts new rows
after the last one, and leaves every other byte of the file as it was. The bytes from
the conversation anchor to the end of the file never change, so frame offsets and
digest sidecars stay valid. Re-export keeps the section through
:func:`cairn.frontmatter.extract_annotations`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .frontmatter import (
    DECISIONS_SECTION,
    FrontmatterParseError,
    _bare_line,
    _fence_token,
    parse_frontmatter,
    scan_annotations,
)
from .mutations import FindFile, locate
from .storage import atomic_write, document_lock, locked

if TYPE_CHECKING:
    from .sources.base import ConversationSource
    from .store import SessionRecord, SessionStore

# --------------------------------------------------------------------------
# Grammar
# --------------------------------------------------------------------------

HEADING: Final = DECISIONS_SECTION
HEADER: Final = "| # | claim | kind | confidence | since | revisit_by | source |"
SEPARATOR: Final = "|---|-------|------|------------|-------|------------|--------|"
SEPARATOR_RE: Final = re.compile(r"^\|(?:\s*:?-+:?\s*\|){7}$")
CANONICAL_ROW_RE: Final = re.compile(
    r"^\| (?P<n>[1-9][0-9]{0,8})"
    r" \| (?P<claim>(?:[^|\\]|\\.)+)"
    r" \| (?P<kind>decision|fact|commitment|hunch)"
    r" \| (?P<confidence>high|medium|low)"
    r" \| (?P<since>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r" \| (?P<revisit_by>[0-9]{4}-[0-9]{2}-[0-9]{2})?"
    r" \| (?P<source>(?:[^|\\]|\\.)*) \|$"
)
CELL_RE: Final = re.compile(r"\s*((?:[^|\\]|\\.)*?)\s*\|")
# The struck claim is any text without ``~~``; it may start or end with one ``~``.
STRUCK_RE: Final = re.compile(r"^~~(?P<claim>~?(?:[^~]~?)*)~~(?: (?P<marker>\S.*))?$")
# Row numbers have at most nine digits, so ``int()`` never meets an oversized string.
SUPERSEDED_RE: Final = re.compile(r"^superseded by #(?P<by>[1-9][0-9]{0,8})$")
SOURCE_RE: Final = re.compile(
    r"^(?:supersedes #(?P<supersedes>[1-9][0-9]{0,8})(?![0-9]))?\s*(?P<rest>.*)$"
)
# A frame citation, ``[<source>:<shortid> f<N>]`` with N a 0-based frame ordinal. The
# one definition of this grammar in the decisions code; every check below uses it.
CITATION_RE: Final = re.compile(
    r"\[(?:claude|codex|antigravity|cursor|granola):[A-Za-z0-9][A-Za-z0-9._-]* f(?:0|[1-9][0-9]*)\]"
)
_CITATIONS_RE: Final = re.compile(rf"^{CITATION_RE.pattern}(?: {CITATION_RE.pattern})*$")
_DATE_RE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_NUMBER_RE: Final = re.compile(r"^[1-9][0-9]{0,8}$")
MAX_ROW_NUMBER: Final = 999_999_999
# Every character str.splitlines() breaks a line at; a claim turns each into a space.
_LINE_BREAKS_RE: Final = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
_TURN_PREFIX: Final = "<!-- cairn:turn "

KINDS: Final = ("decision", "fact", "commitment", "hunch")
CONFIDENCES: Final = ("high", "medium", "low")
DEFAULT_KIND: Final = "decision"
DEFAULT_CONFIDENCE: Final = "medium"
MAX_CLAIM_CHARS: Final = 500
# The conversation heading each source's reader stops at; meetings read to the end.
_HEADINGS: Final[dict[str, str | None]] = {"granola": None}
_CONVERSATION: Final = "## Conversation"


class DecisionError(ValueError):
    """A refused table write; the message is printed after ``Error: ``."""


class TableProblem(DecisionError):
    """The table does not parse; ``line`` is 1-based in the file."""

    def __init__(self, line: int, reason: str):
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


class MissingNotes(DecisionError):
    """The document has no ``## My Notes`` section to hold the table."""


class MissingRow(DecisionError):
    """The row number an edit names is not in the table."""

    def __init__(self, number: int):
        super().__init__(f"no row #{number}")
        self.number = number


@dataclass(frozen=True)
class Problem:
    line: int
    reason: str


@dataclass(frozen=True)
class DecisionRow:
    """One table row. ``claim`` and ``source`` are unescaped; ``claim`` holds neither
    the ``~~`` strike nor the marker. ``suffix`` is the marker text after the closing
    ``~~`` (empty when there is none). ``line`` is the 1-based file line the row was
    read from, 0 for a new row, and takes no part in equality."""

    number: int
    claim: str
    kind: str = DEFAULT_KIND
    confidence: str = DEFAULT_CONFIDENCE
    since: str = ""
    revisit_by: str = ""
    source: str = ""
    struck: bool = False
    suffix: str = ""
    line: int = field(default=0, compare=False)

    @property
    def marker(self) -> str | None:
        return (self.suffix or None) if self.struck else None

    @property
    def superseded_by(self) -> int | None:
        match = SUPERSEDED_RE.match(self.suffix) if self.struck else None
        return int(match["by"]) if match else None

    @property
    def state(self) -> str:
        """``active``, ``superseded`` (struck by a supersede), or ``struck`` (any other
        marker, or none)."""
        if not self.struck:
            return "active"
        return "superseded" if self.superseded_by is not None else "struck"

    @property
    def supersedes(self) -> int | None:
        match = SOURCE_RE.match(self.source)
        return int(match["supersedes"]) if match and match["supersedes"] else None

    @property
    def citations(self) -> list[str]:
        return CITATION_RE.findall(self.source)

    def overdue(self, as_of: date) -> bool:
        """An active row whose ``revisit_by`` is strictly before ``as_of``."""
        return (
            self.state == "active"
            and bool(self.revisit_by)
            and date.fromisoformat(self.revisit_by) < as_of
        )

    def claim_cell(self) -> str:
        if not self.struck:
            return escape_cell(self.claim)
        marker = f" {escape_cell(self.suffix)}" if self.suffix else ""
        return f"~~{escape_cell(self.claim)}~~{marker}"

    def cells(self) -> list[str]:
        return [
            str(self.number),
            self.claim_cell(),
            self.kind,
            self.confidence,
            self.since,
            self.revisit_by,
            escape_cell(self.source),
        ]


def escape_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


def unescape_cell(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def render_row(row: DecisionRow) -> str:
    """The canonical line of one row, without a line break."""
    return "| " + " | ".join(row.cells()) + " |"


def render(rows: list[DecisionRow]) -> list[str]:
    """The canonical table: header, separator and one line per row (no heading)."""
    return [HEADER, SEPARATOR, *(render_row(row) for row in rows)]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _cells(text: str) -> list[str] | None:
    if len(text) < 2 or not text.startswith("|") or not text.endswith("|"):
        return None
    cells, pos = [], 1
    while pos < len(text):
        match = CELL_RE.match(text, pos)
        if match is None:
            return None
        cells.append(match[1])
        pos = match.end()
    return cells


def _real_date(value: str) -> bool:
    if not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_row(text: str, line: int = 0) -> DecisionRow | Problem:
    """One table line (outer whitespace ignored) as a row, or the problem with it."""
    cells = _cells(text.strip())
    if cells is None:
        return Problem(line, "expected a table row")
    if len(cells) != 7:
        return Problem(line, f"expected 7 cells, got {len(cells)}")
    number, claim_cell, kind, confidence, since, revisit_by, source = cells
    if not _NUMBER_RE.match(number):
        if number.isdigit() and len(number) > 9:
            return Problem(line, f"row number must be at most {MAX_ROW_NUMBER}")
        return Problem(line, f"row number must be a positive integer, got {number!r}")
    claim_text = unescape_cell(claim_cell)
    struck_match = STRUCK_RE.match(claim_text)
    if struck_match:
        claim, struck, suffix = struck_match["claim"], True, struck_match["marker"] or ""
    elif "~~" in claim_text:
        return Problem(line, "claim holds '~~' outside a strike")
    else:
        claim, struck, suffix = claim_text, False, ""
    if not claim.strip():
        return Problem(line, "claim is empty")
    if kind not in KINDS:
        return Problem(line, f"unknown kind {kind!r}; use one of {', '.join(KINDS)}")
    if confidence not in CONFIDENCES:
        return Problem(
            line, f"unknown confidence {confidence!r}; use one of {', '.join(CONFIDENCES)}"
        )
    if not _real_date(since):
        return Problem(line, f"since must be a date YYYY-MM-DD, got {since!r}")
    if revisit_by and not _real_date(revisit_by):
        return Problem(line, f"revisit_by must be a date YYYY-MM-DD, got {revisit_by!r}")
    return DecisionRow(
        int(number),
        claim,
        kind,
        confidence,
        since,
        revisit_by,
        unescape_cell(source),
        struck,
        suffix,
        line,
    )


def parse_table(lines: list[str], span: tuple[int, int]) -> tuple[list[DecisionRow], list[Problem]]:
    """The rows of the section ``lines[span[0]:span[1]]`` (``span[0]`` is the heading).

    ``lines`` may keep their line breaks. Lenient about whitespace, strict about values:
    a bad header or separator makes the whole table a problem; a bad row is a problem
    and is left out, the other rows are kept. Line numbers are 1-based.
    """
    start, end = span
    rows: list[DecisionRow] = []
    problems: list[Problem] = []
    expect = "header"
    for index in range(start + 1, end):
        text = _bare_line(lines[index]).strip()
        if not text:
            continue
        number = index + 1
        if expect == "header":
            if text != HEADER:
                return [], [Problem(number, "expected the table header")]
            expect = "separator"
            continue
        if expect == "separator":
            if not SEPARATOR_RE.match(text):
                return [], [Problem(number, "expected the table separator")]
            expect = "rows"
            continue
        parsed = parse_row(text, number)
        if isinstance(parsed, Problem):
            problems.append(parsed)
        elif any(row.number == parsed.number for row in rows):
            problems.append(Problem(number, f"duplicate row #{parsed.number}"))
        else:
            rows.append(parsed)
    if expect != "rows":
        return [], [Problem(start + 1, "the section has no table header and separator")]
    numbers = {row.number for row in rows}
    kept = []
    for row in rows:
        target = row.superseded_by
        if target is not None and target not in numbers:
            problems.append(
                Problem(
                    row.line, f"row #{row.number} is superseded by #{target}, which does not exist"
                )
            )
        else:
            kept.append(row)
    problems.sort(key=lambda problem: problem.line)
    return kept, problems


def parse(text: str, *, conversation_heading: str | None = _CONVERSATION) -> list[DecisionRow]:
    """The rows of a document's Decisions section; ``[]`` when it has none.

    Raises :class:`TableProblem` for the first problem, so a caller never acts on a
    table it read only in part.
    """
    lines = text.splitlines(keepends=True)
    _notes, section = scan_annotations(lines, conversation_heading=conversation_heading)
    if section is None:
        return []
    rows, problems = parse_table(lines, section)
    if problems:
        raise TableProblem(problems[0].line, problems[0].reason)
    return rows


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

Edit = Callable[[list[DecisionRow]], list[DecisionRow]]


def _ending(line: str) -> str:
    return line[len(_bare_line(line)) :]


def rewrite(
    text: str, edit: Edit, *, conversation_heading: str | None = _CONVERSATION
) -> tuple[str, list[DecisionRow]]:
    """Apply ``edit`` to the table of ``text``; return the new text and the new rows.

    ``edit`` gets the parsed rows and returns the full new list. A row whose number is
    already in the table replaces that row's line only when it changed; a row with a
    new number goes after the last row. Rows cannot be removed or share a number. When
    the document has no section yet, one is inserted at the end of My Notes, with the
    line break of the ``## My Notes`` line. Every other line keeps its exact bytes.
    """
    lines = text.splitlines(keepends=True)
    notes, section = scan_annotations(lines, conversation_heading=conversation_heading)
    if notes is None:
        raise MissingNotes("no ## My Notes section")
    rows: list[DecisionRow] = []
    if section is not None:
        rows, problems = parse_table(lines, section)
        if problems:
            raise TableProblem(problems[0].line, problems[0].reason)
    new_rows = edit(list(rows))
    numbers = [row.number for row in new_rows]
    if len(set(numbers)) != len(numbers):
        raise DecisionError("an edit of ## Decisions must keep row numbers unique")
    old = {row.number: row for row in rows}
    if old.keys() - set(numbers):
        raise DecisionError("an edit of ## Decisions cannot remove a row")
    newline = _ending(lines[notes[0]]) or "\n"
    for row in new_rows:
        before = old.get(row.number)
        if before is not None and row != before:
            index = before.line - 1
            lines[index] = render_row(row) + (_ending(lines[index]) or newline)
    added = [render_row(row) + newline for row in new_rows if row.number not in old]
    if not added:
        return "".join(lines), new_rows
    if section is not None:
        start, end = section
        last = max(i for i in range(start, end) if _bare_line(lines[i]).strip())
        if not _ending(lines[last]):
            lines[last] += newline
        lines[last + 1 : last + 1] = added
        return "".join(lines), new_rows
    start, end = notes
    position = end
    while position > start + 1 and not _bare_line(lines[position - 1]).strip():
        position -= 1
    if not _ending(lines[position - 1]):
        lines[position - 1] += newline
    block = [newline, HEADING + newline, newline, HEADER + newline, SEPARATOR + newline, *added]
    if position == end and end < len(lines):
        block.append(newline)  # a blank line before the heading that follows
    lines[position:position] = block
    return "".join(lines), new_rows


@dataclass(frozen=True)
class Written:
    """The outcome of a table write."""

    path: Path
    native_id: str
    title: str | None
    project: str | None
    rows: list[DecisionRow]


def apply(
    find_file: FindFile,
    session_id: str,
    edit: Edit,
    *,
    source: str,
    conversation_heading: str | None = _CONVERSATION,
    id_field: str = "session_id",
) -> Written | None:
    """Locate the session file and rewrite its table under the document lock.

    Returns ``None`` (with the reason on stderr) when the file is missing or
    ambiguous, has no My Notes, has a table that does not parse, or ``edit`` refuses.
    Nothing is written in those cases.
    """
    path = locate(find_file, session_id)
    if path is None:
        return None
    with locked(document_lock(path)):
        # Keep CRLF and every other byte exactly: frame lengths count them.
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        try:
            new_text, rows = rewrite(text, edit, conversation_heading=conversation_heading)
        except MissingNotes:
            print(
                f"Error: {path.name} has no ## My Notes section; "
                f"re-export it with cairn {source} export --all",
                file=sys.stderr,
            )
            return None
        except TableProblem as exc:
            print(f"Error: cannot parse ## Decisions in {path.name}: {exc}", file=sys.stderr)
            return None
        except MissingRow as exc:
            print(f"Error: no row #{exc.number} in ## Decisions of {path.name}", file=sys.stderr)
            return None
        except DecisionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return None
        if new_text != text:
            atomic_write(path, new_text)
    try:
        fm = parse_frontmatter(text)
    except FrontmatterParseError:
        fm = {}
    native_id = fm.get("native_id") or fm.get(id_field) or session_id.split(":", 1)[-1]
    project = fm.get("project") or None
    return Written(path, str(native_id), fm.get("title") or None, project, rows)


def mutate_table(
    find_file: FindFile,
    session_id: str,
    edit: Edit,
    *,
    source: str,
    conversation_heading: str | None = _CONVERSATION,
    id_field: str = "session_id",
) -> bool:
    """:func:`apply`, reduced to whether the write happened."""
    return (
        apply(
            find_file,
            session_id,
            edit,
            source=source,
            conversation_heading=conversation_heading,
            id_field=id_field,
        )
        is not None
    )


# --------------------------------------------------------------------------
# The decide and supersede verbs
# --------------------------------------------------------------------------


def today() -> date:
    """The UTC date: ``since`` of a new row and the default ``--as-of``."""
    return datetime.now(UTC).date()


def normalise_claim(words: list[str]) -> str:
    """Words joined by one space; every line break ``str.splitlines`` knows (CR, LF,
    VT, FF, U+001C-U+001E, U+0085, U+2028, U+2029) becomes a space; runs collapse."""
    text = _LINE_BREAKS_RE.sub(" ", " ".join(words))
    return re.sub(r" {2,}", " ", text).strip()


def claim_error(claim: str) -> str | None:
    if not claim:
        return "claim is empty"
    if "~~" in claim:
        return "claim must not contain '~~' (it marks a struck row)"
    if len(claim) > MAX_CLAIM_CHARS:
        return f"claim is longer than {MAX_CLAIM_CHARS} characters (got {len(claim)})"
    return None


def revisit_error(value: str, since: date) -> str | None:
    if not value:
        return None
    if _real_date(value) and date.fromisoformat(value) >= since:
        return None
    return f"--revisit-by must be YYYY-MM-DD on or after {since.isoformat()}, got '{value}'"


def citations_error(value: str) -> str | None:
    if not value or _CITATIONS_RE.match(value):
        return None
    return (
        "--source must be frame citations like [claude:3f2a9c1e f12], "
        f"separated by spaces, got '{value}'"
    )


def cmd_decide(source: ConversationSource, session_id: str, args: argparse.Namespace) -> int:
    """``decide`` (``args.number`` absent) and ``supersede N``; the session is resolved."""
    target: int | None = getattr(args, "number", None)
    claim = normalise_claim(args.claim)
    since = today()
    revisit_by = (args.revisit_by or "").strip()
    cites = (args.source or "").strip()
    error = claim_error(claim) or revisit_error(revisit_by, since) or citations_error(cites)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    struck_number: list[int] = []

    def edit(rows: list[DecisionRow]) -> list[DecisionRow]:
        number = max((row.number for row in rows), default=0) + 1
        if number > MAX_ROW_NUMBER:
            raise DecisionError(f"no row number is left after #{MAX_ROW_NUMBER}")
        if target is None:
            new = DecisionRow(
                number,
                claim,
                args.kind or DEFAULT_KIND,
                args.confidence,
                since.isoformat(),
                revisit_by,
                cites,
            )
            return [*rows, new]
        old = next((row for row in rows if row.number == target), None)
        if old is None:
            raise MissingRow(target)
        if old.struck:
            raise DecisionError(f"row #{target} is already struck ({old.marker or 'no marker'})")
        struck_number.append(target)
        struck = replace(old, struck=True, suffix=f"superseded by #{number}")
        cell = f"supersedes #{target}" + (f" {cites}" if cites else "")
        new = DecisionRow(
            number,
            claim,
            args.kind or old.kind,
            args.confidence,
            since.isoformat(),
            revisit_by,
            cell,
        )
        return [struck if row.number == target else row for row in rows] + [new]

    written = apply(
        source.find_file,
        session_id,
        edit,
        source=source.source_name,
        conversation_heading=source.conversation_heading,
        id_field=source.id_field,
    )
    if written is None:
        return 1
    row = written.rows[-1]
    session = f"{source.source_name}:{written.native_id}"
    if getattr(args, "json", False):
        data = row_json(
            row,
            session=session,
            as_of=since,
            title=written.title,
            project=written.project,
            path=written.path,
        )
        print(json.dumps({"ref": data["ref"], "path": str(written.path), "row": data}, indent=2))
    elif not getattr(args, "quiet", False):
        short = f"{source.source_name}:{written.native_id[:8]}"
        prefix = f"Struck #{struck_number[0]}; added" if struck_number else "Added"
        print(f"{prefix} #{row.number} to {short}: {row.claim}")
    return 0


# --------------------------------------------------------------------------
# Reading: recall decisions and the digest overdue list
# --------------------------------------------------------------------------


def _read_prefix(path: Path, heading: str | None) -> str:
    """The text before the frame anchor, read line by line.

    The anchor is the first ``heading`` line outside a fence that is followed by a
    blank line and a turn marker, or that ends the file: the rule of
    :func:`cairn.frames.locate`. The whole file when ``heading`` is ``None`` or no
    anchor is found.
    """
    parts: list[str] = []
    fence: str | None = None
    anchor = -1  # index in ``parts`` of a candidate heading line
    with path.open(encoding="utf-8", newline="") as handle:
        for line in handle:
            if anchor >= 0:
                seen = len(parts) - anchor  # lines read since the heading
                if seen == 1 and line == "\n":
                    parts.append(line)
                    continue
                if seen == 2 and line.startswith(_TURN_PREFIX):
                    return "".join(parts[:anchor])
                anchor = -1
            bare = _bare_line(line)
            token = _fence_token(bare)
            if token:
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
            elif fence is None and heading is not None and line == heading + "\n":
                anchor = len(parts)
            parts.append(line)
    if anchor >= 0 and len(parts) - anchor == 1:
        return "".join(parts[:anchor])  # the heading ends the file
    return "".join(parts)


def read_table(path: Path, heading: str | None) -> tuple[list[DecisionRow], list[Problem]]:
    """The rows of the document at ``path``, read only up to its frame anchor."""
    lines = _read_prefix(path, heading).splitlines(keepends=True)
    _notes, section = scan_annotations(lines, conversation_heading=heading)
    if section is None:
        return [], []
    return parse_table(lines, section)


def row_json(
    row: DecisionRow,
    *,
    session: str,
    as_of: date,
    title: str | None,
    project: str | None,
    path: Path,
) -> dict:
    return {
        "ref": f"{session}#{row.number}",
        "session": session,
        "number": row.number,
        "claim": row.claim,
        "kind": row.kind,
        "confidence": row.confidence,
        "since": row.since,
        "revisit_by": row.revisit_by or None,
        "source": row.source,
        "citations": row.citations,
        "supersedes": row.supersedes,
        "state": row.state,
        "marker": row.marker,
        "superseded_by": row.superseded_by,
        "overdue": row.overdue(as_of),
        "title": title,
        "project": project,
        "path": str(path),
    }


def collect(
    records: list[SessionRecord],
) -> tuple[list[tuple[SessionRecord, DecisionRow]], list[dict]]:
    """Every parsed row of ``records`` and every problem, warned on stderr."""
    found: list[tuple[SessionRecord, DecisionRow]] = []
    problems: list[dict] = []
    for record in records:
        try:
            rows, issues = read_table(record.path, _HEADINGS.get(record.source, _CONVERSATION))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Warning: {record.path}: {exc}", file=sys.stderr)
            problems.append({"path": str(record.path), "line": None, "reason": str(exc)})
            continue
        for issue in issues:
            print(f"Warning: {record.path}: line {issue.line}: {issue.reason}", file=sys.stderr)
            problems.append({"path": str(record.path), "line": issue.line, "reason": issue.reason})
        found.extend((record, row) for row in rows)
    found.sort(key=lambda item: (item[1].since, item[0].reference, item[1].number))
    return found, problems


def _json(record: SessionRecord, row: DecisionRow, as_of: date) -> dict:
    return row_json(
        row,
        session=record.reference,
        as_of=as_of,
        title=record.title,
        project=record.project,
        path=record.path,
    )


def overdue_rows(records: list[SessionRecord], as_of: date) -> list[dict]:
    """The JSON rows of every active row whose ``revisit_by`` is before ``as_of``."""
    found, _problems = collect(records)
    return [_json(record, row, as_of) for record, row in found if row.overdue(as_of)]


def _as_of(value: str | None) -> date:
    if value is None:
        return today()
    if not _real_date(value):
        raise ValueError(f"--as-of must be YYYY-MM-DD, got '{value}'")
    return date.fromisoformat(value)


def add_recall_parser(sub: argparse._SubParsersAction, session_sources: list[str]) -> None:
    """Register ``cairn recall decisions`` on the recall subparsers."""
    cmd = sub.add_parser(
        "decisions",
        help="list ## Decisions rows across sessions; --overdue for rows due for review",
        description=(
            "List the rows of every session's ## Decisions table. DATE filters on the "
            "row's since date. Struck rows are hidden unless --include-struck."
        ),
    )
    cmd.add_argument("date_expr", nargs="*", metavar="DATE")
    cmd.add_argument(
        "--overdue", action="store_true", help="only active rows with revisit_by before --as-of"
    )
    cmd.add_argument("--include-struck", action="store_true", help="also list struck rows")
    cmd.add_argument("--as-of", metavar="YYYY-MM-DD", help="the date --overdue uses (UTC today)")
    projects = cmd.add_mutually_exclusive_group()
    projects.add_argument("--project", help="exact project/repo label or working directory")
    projects.add_argument("--all-projects", action="store_true")
    cmd.add_argument("--source", action="append", choices=session_sources)
    cmd.add_argument("--json", action="store_true")


def run_recall(args: argparse.Namespace, store: SessionStore) -> int:
    """``cairn recall decisions``. ValueError (exit 1 in the caller) on a bad date."""
    from .timeutil import parse_date_range

    as_of = _as_of(args.as_of)
    window = parse_date_range(" ".join(args.date_expr)) if args.date_expr else None
    records = store.records(args.source)
    project = args.project or (None if args.all_projects else os.getcwd())
    if project:
        records = [r for r in records if r.matches_project(project)]
    found, problems = collect(records)
    selected = []
    for record, row in found:
        if not args.include_struck and row.struck:
            continue
        if args.overdue and not row.overdue(as_of):
            continue
        if window is not None:
            since = datetime.fromisoformat(row.since).replace(tzinfo=UTC)
            if not window[0] <= since < window[1]:
                continue
        selected.append((record, row))
    if args.json:
        data = {
            "as_of": as_of.isoformat(),
            "decisions": [_json(record, row, as_of) for record, row in selected],
            "problems": problems,
        }
        print(json.dumps(data, indent=2))
        return 0
    for record, row in selected:
        ref = f"{record.source}:{record.session_id[:8]}#{row.number}"
        revisit = f"revisit {row.revisit_by}" if row.revisit_by else "revisit -"
        claim = row.claim if not row.struck else f"~~{row.claim}~~ [{row.marker or 'struck'}]"
        line = f"{ref}  {row.kind}  {row.confidence}  {row.since}  {revisit}  {claim}"
        print(line + ("  OVERDUE" if row.overdue(as_of) else ""))
    print(f"{len(selected)} decisions")
    return 0


__all__ = [
    "CANONICAL_ROW_RE",
    "CITATION_RE",
    "CONFIDENCES",
    "HEADER",
    "HEADING",
    "KINDS",
    "SEPARATOR",
    "STRUCK_RE",
    "SUPERSEDED_RE",
    "DecisionError",
    "DecisionRow",
    "Problem",
    "TableProblem",
    "add_recall_parser",
    "apply",
    "cmd_decide",
    "mutate_table",
    "overdue_rows",
    "parse",
    "parse_row",
    "parse_table",
    "read_table",
    "render",
    "render_row",
    "rewrite",
    "row_json",
    "run_recall",
]
