"""The ``provenance`` front-matter key: who produced a document's body text.

One value per document, from a fixed set of four labels (no order, no rank):

- ``tool``: cairn exported it from the harness log of a session that a person drove.
- ``agent``: an agent wrote the prompts (a peer run, a subagent, an exchange document).
- ``imported``: mirrored from another application (a Granola meeting).
- ``user``: a person wrote it by hand.

The key sits in the exporter-owned head, directly before ``status:``, so every
mutation keeps it byte for byte and every re-render recomputes it. Documents written
before the key existed get a value derived from their front matter on read
(:func:`effective`); nothing is backfilled.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from . import frontmatter as fm_mod
from .digest_condense import INVOKE_PREAMBLE
from .mutations import _bare_link
from .registry import SOURCES
from .schema import Message, SessionData
from .store import messages_of

VALUES = ("user", "agent", "tool", "imported")
KEY = "provenance"


def _subagent_type(source: str) -> str:
    return next((s.subagent_type for s in SOURCES if s.source == source), "")


def _is_subagent_type(source: str, doc_type: object) -> bool:
    subagent = _subagent_type(source)
    return bool(subagent) and doc_type == subagent


def at_export(source: str, doc_type: object, messages: Iterable[Message]) -> str:
    """The value an export writes: subagent, Granola, invoke preamble, else ``tool``.

    Only the first user message with text counts: a person who resumes a peer session
    by hand leaves it ``agent``, and a preamble quoted later in a session leaves it
    ``tool``.
    """
    if _is_subagent_type(source, doc_type):
        return "agent"
    if source == "granola":
        return "imported"
    for message in messages:
        text = message.get("text") or ""
        if message.get("role") != "user" or not text.strip():
            continue
        return "agent" if INVOKE_PREAMBLE.match(text.lstrip()) else "tool"
    return "tool"


def head_lines(source: str, markdown: str, data: SessionData) -> list[str]:
    """The head lines an export adds after the shared metadata block."""
    try:
        doc_type = fm_mod.parse_frontmatter(markdown).get("type")
    except fm_mod.FrontmatterParseError:
        doc_type = None  # export_one refuses the document after the join
    return [f"{KEY}: {at_export(source, doc_type, messages_of(data))}"]


def derive(fm: dict, source: str) -> str:
    """The value of a document without a valid stored key, from front matter only."""
    if _is_subagent_type(source, fm.get("type")):
        return "agent"
    if source == "granola":
        return "imported"
    if fm.get("owner"):
        return "agent"
    links = fm.get("links")
    entries = [_bare_link(item) for item in links] if isinstance(links, list) else []
    if any(entry.startswith("parent ") for entry in entries):
        return "agent"
    return "tool"


def stored(fm: dict) -> str | None:
    """The stored value when it is one of :data:`VALUES`, else ``None``."""
    value = fm.get(KEY)
    return value if isinstance(value, str) and value in VALUES else None


def effective(fm: dict, source: str, path: Path | str, *, peer_body: bool = False) -> str:
    """The stored value, else the derived one; warn once when the stored one is invalid.

    ``peer_body`` is body evidence the caller already holds (the digest found the invoke
    preamble in the first user message with text): without a valid stored key it reads
    ``agent``.
    """
    value = stored(fm)
    if value is not None:
        return value
    derived = "agent" if peer_body else derive(fm, source)
    if KEY in fm:  # present but invalid, the empty scalar included
        print(
            f"Warning: {path}: provenance '{fm.get(KEY)}' is not one of "
            f"{', '.join(VALUES)}; reading it as {derived}",
            file=sys.stderr,
        )
    return derived
