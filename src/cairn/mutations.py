"""In-place frontmatter mutations on already-exported session files.

These operate purely on the synced markdown (comments, status, tags, rating),
so they are source-agnostic. The caller supplies a ``find_file(session_id)``
callable that knows the source's short-id scheme; everything else is shared.

Every mutation goes through :func:`cairn.frontmatter.rewrite_preserved_tail`,
which rewrites only the preserved tail of the frontmatter block via the same
``preserved_tail`` emitter the exporter uses. That means a mutation can never
touch the conversation body (a stray ``status:`` in a transcript is safe) and a
mutated file is byte-for-byte what a fresh export would produce.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

from .frontmatter import FrontmatterParseError, rewrite_preserved_tail

# The session lifecycle vocabulary. Declared here, at the mutation boundary, so an
# invalid status is rejected no matter which caller writes it (not only the CLI).
VALID_STATUSES: Final[frozenset[str]] = frozenset({"active", "done", "blocked", "handoff"})

FindFile = Callable[[str], "Path | None"]


def is_valid_status(status: str) -> bool:
    """Whether ``status`` is a recognised session lifecycle status (see VALID_STATUSES)."""
    return status in VALID_STATUSES


def invalid_status_message(status: str) -> str:
    """Canonical error text for a status outside VALID_STATUSES.

    Single source for the wording so every caller (the ``log`` CLI pre-check and
    :func:`set_session_status`) rejects an invalid status identically.
    """
    valid = ", ".join(sorted(VALID_STATUSES))
    return f"invalid status {status!r}. Must be one of: {valid}"


def _mutate_file(find_file: FindFile, session_id: str, mutate: Callable[[dict], None]) -> bool:
    """Locate the session file, apply ``mutate`` to its frontmatter, write it back.

    Returns False (with a message on stderr) if the file is missing, has no
    frontmatter block, has unparseable frontmatter, or its short id is ambiguous -
    we never silently no-op.
    """
    try:
        session_file = find_file(session_id)
    except FileExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return False
    if not session_file or not session_file.exists():
        print(f"Error: Session file not found for {session_id[:8]}", file=sys.stderr)
        return False

    content = session_file.read_text(encoding="utf-8")
    try:
        new_content = rewrite_preserved_tail(content, mutate)
    except FrontmatterParseError as e:
        print(f"Error: cannot parse frontmatter in {session_file.name}: {e}", file=sys.stderr)
        return False
    if new_content is None:
        print(f"Error: No frontmatter block in {session_file.name}", file=sys.stderr)
        return False

    session_file.write_text(new_content, encoding="utf-8")
    return True


def add_comment(find_file: FindFile, session_id: str, text: str) -> bool:
    """Append a timestamped comment to a session's frontmatter."""
    entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {text}"

    def mutate(fm: dict) -> None:
        existing = fm.get("comments") or ""
        fm["comments"] = f"{existing}\n{entry}" if existing else entry

    if _mutate_file(find_file, session_id, mutate):
        print(f"Added comment: {entry}")
        return True
    return False


def set_session_status(find_file: FindFile, session_id: str, status: str) -> bool:
    """Set the session status in frontmatter (must be one of VALID_STATUSES)."""
    if not is_valid_status(status):
        print(f"Error: {invalid_status_message(status)}", file=sys.stderr)
        return False

    def mutate(fm: dict) -> None:
        fm["status"] = status

    return _mutate_file(find_file, session_id, mutate)


def set_session_rating(find_file: FindFile, session_id: str, rating: int) -> bool:
    """Set the session rating in frontmatter (1-10)."""
    if not 1 <= rating <= 10:
        print(f"Error: Rating must be 1-10, got {rating}", file=sys.stderr)
        return False

    def mutate(fm: dict) -> None:
        fm["rating"] = rating

    return _mutate_file(find_file, session_id, mutate)


def set_session_tags(find_file: FindFile, session_id: str, tags: list[str]) -> bool:
    """Set the session tags in frontmatter.

    Newlines are stripped from each tag so a tag value can never inject a new
    top-level frontmatter key.
    """
    clean = [t.replace("\n", " ").strip() for t in tags]
    clean = [t for t in clean if t]

    def mutate(fm: dict) -> None:
        fm["tags"] = clean

    return _mutate_file(find_file, session_id, mutate)


__all__ = [
    "VALID_STATUSES",
    "add_comment",
    "invalid_status_message",
    "is_valid_status",
    "set_session_rating",
    "set_session_status",
    "set_session_tags",
]
