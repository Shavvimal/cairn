"""The anchored frame walker: exact message boundaries in an exported session.

An exported chat session ends with a ``## Conversation`` section in which every
message is written as ``<!-- cairn:turn N -->``, then ``N - 2`` characters of body,
then a blank separator line (see :func:`cairn.rendering.render_conversation`). ``N``
is a length, not an id: two bodies of equal length carry the same ``N``.

This module walks those lengths from the conversation heading to the end of the file
and never searches for marker text, because a body can quote a marker or a heading.
It is the provenance layer of ``cairn recall digest`` and ``cairn recall frame``: a
frame's ordinal, offset and hash come from here and nowhere else.

Unlike :func:`cairn.store.bounded_content`, which is a preview and may resynchronise,
the walker anchors on a ``## Conversation`` line, requires an exact walk to the end of
the file, and checks the role header and the blank separator of every frame. A file
that fails is classified (legacy or corrupt), never guessed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import _date

MARKER = re.compile(r"<!-- cairn:turn (\d+) -->\n")
# A body that is only a header (no text, no tools) loses its trailing newline to the
# renderer's rstrip, so the header may also end the body.
HEADER = re.compile(r"### (User|Assistant|Tool|Agent|System|Message)(?: - ([^\n]*))?(?:\n|\Z)")
HEADING = "## Conversation\n"
_FIRST_MARKER = "\n<!-- cairn:turn "


class LegacyExport(ValueError):
    """The file has no framed conversation; its messages have no reliable boundaries."""


class CorruptExport(ValueError):
    """A framed conversation whose length walk does not reach the end of the file."""

    def __init__(self, position: int):
        super().__init__(f"frame walk failed at character {position}")
        self.position = position


@dataclass(frozen=True)
class Frame:
    """One message of an exported session, located by the walk.

    ``offset`` is the marker's position relative to the anchoring ``## Conversation``
    line, so it survives front matter rewrites and My Notes edits. ``body`` runs from
    the role header to the end of the message, separator excluded.
    """

    ordinal: int
    offset: int
    length: int
    role: str
    ts_raw: str | None
    ts: datetime | None
    body: str


def read_text(path: Path) -> str:
    """Read an export exactly as written: frame lengths count ``\\r\\n`` as two characters."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def sha1_8(body: str) -> str:
    """The first 8 hex digits of SHA-1 over the body: a change detector, not a control."""
    return hashlib.sha1(body.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def utc_iso(value: datetime) -> str:
    """``2026-03-28T23:30:00Z``; fractional seconds are kept only when present."""
    value = value.astimezone(UTC)
    if not value.microsecond:
        spec = "seconds"
    elif not value.microsecond % 1000:
        spec = "milliseconds"
    else:
        spec = "microseconds"
    return value.isoformat(timespec=spec).replace("+00:00", "Z")


def _walk(text: str, anchor: int) -> tuple[list[Frame] | None, int]:
    """Frames from ``anchor`` to the end of the file, or ``None`` and the failing position."""
    pos = anchor + len(HEADING)
    if pos == len(text):
        return [], pos  # "## Conversation" with no messages
    if text[pos] != "\n":
        return None, pos
    pos += 1
    frames: list[Frame] = []
    while True:
        match = MARKER.match(text, pos)  # anchored: the marker must start exactly here
        if match is None:
            return None, pos
        length = int(match[1])
        start = match.end()
        end = start + length
        if length < 2 or end > len(text) + 1:
            return None, pos
        body = text[start : end - 2]
        header = HEADER.match(body)
        if header is None:
            return None, pos
        last = end == len(text) + 1  # a real export ends "body\n" with no blank line
        if (text[end - 2 :] != "\n") if last else (text[end - 2 : end] != "\n\n"):
            return None, pos
        ts_raw = header[2]
        frames.append(
            Frame(
                ordinal=len(frames),
                offset=match.start() - anchor,
                length=length,
                role=header[1].lower(),
                ts_raw=ts_raw,
                ts=_date(ts_raw),
                body=body,
            )
        )
        if last or end == len(text):  # or a document with one extra "\n"
            return frames, pos
        pos = end


def locate(text: str) -> tuple[int, list[Frame]]:
    """Find the conversation heading from which an exact walk consumes the rest of the file.

    Candidates are ``## Conversation`` lines in file order; one inside My Notes or a
    summary fails the walk and the next is tried. Raises :class:`LegacyExport` when
    no heading is followed by a marker, :class:`CorruptExport` (with the first
    candidate's failing position) when none walks.
    """
    candidates = []
    index = text.find(HEADING)
    while index != -1:
        after = index + len(HEADING)
        if (index == 0 or text[index - 1] == "\n") and (
            text.startswith(_FIRST_MARKER, after) or after == len(text)
        ):
            candidates.append(index)
        index = text.find(HEADING, index + 1)
    if not candidates:
        raise LegacyExport("legacy export without frames")
    first_failure = None
    for anchor in candidates:
        frames, position = _walk(text, anchor)
        if frames is not None:
            return anchor, frames
        if first_failure is None:
            first_failure = position
    raise CorruptExport(first_failure if first_failure is not None else 0)


def effective_times(frames: list[Frame]) -> list[datetime | None]:
    """Each frame's timestamp, inherited from the nearest earlier frame when it has none.

    A leading run without timestamps takes the first later one; a session without any
    has no effective times. Only the window test uses these; the sidecar keeps ``null``.
    """
    result: list[datetime | None] = []
    current = None
    for frame in frames:
        current = frame.ts or current
        result.append(current)
    first = next((ts for ts in result if ts is not None), None)
    return [ts if ts is not None else first for ts in result]


__all__ = [
    "HEADER",
    "HEADING",
    "MARKER",
    "CorruptExport",
    "Frame",
    "LegacyExport",
    "effective_times",
    "locate",
    "read_text",
    "sha1_8",
    "utc_iso",
]
