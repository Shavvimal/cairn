"""Normalized conversation schema - the shared contract every adapter targets.

Mirrors the conversation_provenance_service design (PR #2199): each source
parses its native store into this one shape, and the decoupled renderer
(:mod:`cairn.rendering`) consumes it.

The payloads are plain ``dict``\\ s rather than constructed objects - this is a
deliberate, documented exception to "model everything", taken so the renderer
emits byte-identical output to the pre-refactor scripts. The ``TypedDict``\\ s
below type those dict payloads (zero runtime cost) so the contract is still
checkable by a type checker without changing the data on the hot path.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ToolCallData(TypedDict, total=False):
    name: str
    arguments: str | None
    status: str | None
    result: str | None


class CodeBlockData(TypedDict, total=False):
    content: str
    language: str
    path: str | None


class Message(TypedDict, total=False):
    role: str  # "user" | "assistant" | "tool"
    text: str
    timestamp: str | None
    thinking: str | None
    reasoning_recoverable: bool  # False marks encrypted/dropped reasoning (renderer flags it)
    tool_calls: list[ToolCallData]
    code_blocks: list[CodeBlockData]


# Parsed YAML frontmatter has dynamic keys (type, date, session_id, status, …),
# so it is an open string-keyed mapping rather than a fixed TypedDict.
Frontmatter = dict[str, Any]


class SessionData(TypedDict, total=False):
    """The per-source parsed-session dict consumed by ``render_document``/``export_one``.

    A single ``total=False`` superset of the keys every adapter produces (Claude,
    Codex, Cursor, Granola). Like :data:`Frontmatter` it stays a plain ``dict`` on
    the hot path - the TypedDict only lets a checker verify ``data["date"]`` and
    friends instead of treating them as ``Any``. Each adapter populates the subset
    relevant to its source; absent keys are read with ``.get(...)``.
    """

    # identity / location
    date: str
    session_id: str
    composer_id: str
    meeting_id: str
    project: str | None
    repo: str | None
    repo_subdir: str | None
    cwd: str | None
    folder: str | None
    # vcs / model / tool metadata
    git_branch: str | None
    branch: str | None
    commit: str | None
    model: str | None
    mode: str | None
    cli_version: str | None
    # body / content
    # ``messages`` is overloaded by source: a turn *count* (claude, cursor - feeds the
    # ``messages:`` frontmatter line) or the message *list* (codex - feeds the body).
    # ``messages_ordered`` is always the list when a source keeps the two separate.
    messages: Any
    messages_ordered: list[Message]
    user_messages: list[str]
    message_count: int
    summary: str
    skills: list[str]
    files_created: list[str]
    files_modified: list[str]
    # timestamps (raw ISO strings as found in the source store)
    first_timestamp: str | None
    last_timestamp: str | None
    last_activity: str | None
    time: str
    # status / titling
    status: str
    title: str
    # meeting-specific (Granola)
    duration_min: int | None
    attendees: list[Any]
    creator: Any  # an attendee mapping ({name, email}); rendered via format_attendee
    notes_markdown: str
    summary_markdown: str
    transcript_markdown: str
    # cursor sqlite plumbing
    bubble_order: list[str]
    legacy_turns: list[dict] | None
    cache: dict[str, Any]


__all__ = [
    "CodeBlockData",
    "Frontmatter",
    "Message",
    "SessionData",
    "ToolCallData",
]
