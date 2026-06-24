"""Frontmatter parsing, preserved-field handling, and shared document assembly.

Two halves of every exported session file are source-agnostic:

1. The **preserved tail** of the frontmatter - ``status``, ``tags``, ``rating``,
   ``comments``, ``projects`` - which is round-tripped identically across syncs
   so user edits survive. Every adapter emits these the same way.
2. The **My Notes** block and the trailing ``## Conversation`` body.

Each adapter owns only its frontmatter *head* (the per-source fields like
``session_id``/``composer_id``, ``repo``, ``model`` …) and any body sections
between the title and My Notes (Summary/Skills/Artifacts). It delegates the rest
to the helpers here so the shared format stays in one place.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

# Fields preserved across re-syncs so manual edits survive: the tail fields are
# re-emitted by preserved_tail(); ``title`` is preserved separately via
# resolve_title() in each adapter's head. (Informational - not used as control flow.)
PRESERVED_FIELDS = {"comments", "projects", "status", "tags", "rating", "title"}
PRESERVED_SECTION = "## My Notes"

_DOCUMENT_RE = re.compile(r"\A(---\n)(.*?)(\n---\n)(.*)\Z", re.DOTALL)
_TAIL_KEYS = ("status:", "tags:", "rating:", "comments:", "projects:")


class FrontmatterParseError(ValueError):
    """A frontmatter block contains YAML this parser cannot faithfully round-trip.

    Raised instead of silently mis-parsing constructs outside the small block-style
    dialect cairn emits (flow sequences, flow mappings, folded scalars, nested
    mappings). Failing loudly here is what protects the round-trip guarantee - a
    silently dropped or mangled field would corrupt a user's preserved edits.
    """


def _escape_scalar(value: str) -> str:
    r"""Escape ``"`` as ``\"`` for a double-quoted frontmatter scalar.

    Quote-only (backslashes are left as-is) so it is the exact inverse of
    :func:`_unescape_scalar`: a title round-trips to a fixed point instead of
    accumulating a backslash on every sync, and existing files - which were
    written under this same quote-only scheme - re-export byte-for-byte.
    """
    return value.replace('"', '\\"')


def _unescape_scalar(value: str) -> str:
    r"""Reverse :func:`_escape_scalar` - collapse ``\"`` back to ``"``."""
    return value.replace('\\"', '"')


def parse_frontmatter(content: str) -> dict:
    """Parse the leading frontmatter block into a dict.

    Supports exactly the block-style dialect cairn emits: ``key: value`` scalars
    (optionally double-quoted), block sequences (``key:`` followed by ``  - item``
    lines), empty sequences (``key: []``), and block scalars (``key: |``).

    Returns ``{}`` when there is no frontmatter block (so callers scanning a tree
    can treat a non-session file as "no fields"). Raises :class:`FrontmatterParseError`
    on YAML this parser cannot faithfully round-trip - flow sequences/mappings,
    folded scalars, nested mappings - rather than silently mis-parsing them.
    """
    frontmatter: dict = {}
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        return frontmatter

    fm_text = match.group(1)
    current_key = None
    current_array = None
    multiline_value: list[str] = []
    in_multiline = False

    for lineno, line in enumerate(fm_text.split("\n"), 1):
        # Continuation of a block scalar (``key: |``).
        if in_multiline:
            if line.startswith("  ") or line == "":
                multiline_value.append(line[2:] if line.startswith("  ") else "")
                continue
            frontmatter[current_key] = "\n".join(multiline_value).rstrip()
            in_multiline = False
            multiline_value = []

        # Block sequence item.
        if line.startswith("  - "):
            if current_array is None:
                raise FrontmatterParseError(
                    f"frontmatter line {lineno}: list item without a preceding key: {line!r}"
                )
            if not isinstance(frontmatter.get(current_array), list):
                frontmatter[current_array] = []
            frontmatter[current_array].append(line[4:].strip())
            continue

        if not line.strip():
            continue

        # Any other indentation is a construct we don't model (e.g. a nested mapping).
        if line.startswith(" "):
            raise FrontmatterParseError(
                f"frontmatter line {lineno}: unsupported indented construct: {line!r}"
            )

        if ":" not in line:
            raise FrontmatterParseError(
                f"frontmatter line {lineno}: expected 'key: value', got {line!r}"
            )

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key

        if value == "|":
            in_multiline = True
            multiline_value = []
            current_array = None
        elif value == "" or value == "[]":
            current_array = key
            frontmatter[key] = [] if value == "[]" else value
        elif value[0] in "[{" or value == ">":
            raise FrontmatterParseError(
                f"frontmatter line {lineno}: flow/folded YAML is unsupported; "
                f"use block style: {line!r}"
            )
        else:
            current_array = None
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = _unescape_scalar(value[1:-1])
            frontmatter[key] = value

    if in_multiline:
        frontmatter[current_key] = "\n".join(multiline_value).rstrip()

    return frontmatter


def extract_my_notes_section(content: str) -> str | None:
    """Extract the '## My Notes' section from existing content."""
    if PRESERVED_SECTION not in content:
        return None
    pattern = rf"({re.escape(PRESERVED_SECTION)}.*?)(?=\n## |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).rstrip()
    return None


def resolve_title(
    data: Mapping[str, Any], existing_fm: dict | None, fallback: str | None = None
) -> str:
    """Pick the title: preserved frontmatter wins, then parsed, then fallback."""
    title = existing_fm.get("title") if existing_fm else None
    if not title or title == "Untitled Session":
        title = data.get("title") or fallback or "Untitled Session"
    return title


def title_line(title: str) -> str:
    """The ``title: "..."`` frontmatter line, with quotes and backslashes escaped.

    Symmetric with :func:`parse_frontmatter`'s unescaping, so titles round-trip
    instead of accumulating backslashes on every sync.
    """
    return f'title: "{_escape_scalar(title)}"'


def head_open(session_type: str, date: str, id_field: str, id_value: str) -> list[str]:
    """The opening frontmatter lines every adapter emits: fence, type, date, id.

    ``id_field`` is the per-source id key (``session_id`` / ``composer_id`` /
    ``meeting_id``). The adapter appends its own fields (repo, model, title …) after.
    """
    return ["---", f"type: {session_type}", f"date: {date}", f"{id_field}: {id_value}"]


def head_close(last_activity: str | None, message_count: int | None = None) -> list[str]:
    """The closing head lines: optional ``messages:`` then ``last_activity:``.

    Centralizes the ``last_activity`` default so every adapter falls back to "now"
    identically. ``message_count`` is omitted for sources without a turn count
    (e.g. meeting transcripts), which then emit only ``last_activity``.
    """
    lines = []
    if message_count is not None:
        lines.append(f"messages: {message_count}")
    lines.append(f"last_activity: {last_activity or datetime.now(UTC).isoformat()}")
    return lines


def preserved_tail(existing_fm: dict | None, default_status: str = "active") -> list[str]:
    """The identical frontmatter tail every source emits.

    Order: status, tags, rating, comments, projects. Values come from the
    existing file's frontmatter (round-tripped) so manual edits survive a sync.
    """
    lines = []

    status = existing_fm.get("status", default_status) if existing_fm else default_status
    lines.append(f"status: {status}")

    tags = existing_fm.get("tags", []) if existing_fm else []
    if isinstance(tags, list) and tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {tag}")
    else:
        lines.append("tags: []")

    rating = existing_fm.get("rating") if existing_fm else None
    if rating is not None and rating != "null":
        lines.append(f"rating: {rating}")
    else:
        lines.append("rating: null")

    comments = existing_fm.get("comments", "") if existing_fm else ""
    if comments:
        lines.append("comments: |")
        for comment_line in comments.split("\n"):
            lines.append(f"  {comment_line}")
    else:
        lines.append('comments: ""')

    projects = existing_fm.get("projects", []) if existing_fm else []
    if isinstance(projects, list) and projects:
        lines.append("projects:")
        for proj in projects:
            lines.append(f"  - {proj}")
    else:
        lines.append("projects: []")

    return lines


def rewrite_preserved_tail(content: str, mutate: Callable[[dict], None]) -> str | None:
    """Apply ``mutate`` to the parsed frontmatter, then rewrite ONLY the preserved
    tail in place - head fields and body stay byte-for-byte untouched.

    This is how in-place mutations (note/close/log) edit a synced file. The tail is
    re-emitted via :func:`preserved_tail` - the same function the exporter uses - so
    a mutated file is formatted identically to a freshly exported one, and a stray
    ``status:`` / ``tags:`` in the conversation body can never be clobbered.

    Returns the new content, or ``None`` if ``content`` has no frontmatter block.
    """
    match = _DOCUMENT_RE.match(content)
    if not match:
        return None
    open_fence, inner, close_fence, body = match.groups()

    fm = parse_frontmatter(content)
    mutate(fm)

    lines = inner.split("\n")
    tail_start = next(
        (i for i, line in enumerate(lines) if line.startswith(_TAIL_KEYS)), len(lines)
    )
    new_tail = preserved_tail(fm, default_status=fm.get("status") or "active")
    new_inner = "\n".join(lines[:tail_start] + new_tail)
    return f"{open_fence}{new_inner}{close_fence}{body}"


def notes_block(my_notes: str | None, preserved_note: str = "syncs") -> list[str]:
    """The My Notes section: the preserved block if present, else the placeholder."""
    if my_notes:
        return [my_notes, ""]
    return [
        "## My Notes",
        "",
        f"<!-- Add your notes here. This section is preserved across {preserved_note}. -->",
        "",
    ]


__all__ = [
    "PRESERVED_FIELDS",
    "PRESERVED_SECTION",
    "FrontmatterParseError",
    "extract_my_notes_section",
    "head_close",
    "head_open",
    "notes_block",
    "parse_frontmatter",
    "preserved_tail",
    "resolve_title",
    "rewrite_preserved_tail",
    "title_line",
]
