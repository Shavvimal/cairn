"""Export receipts: when each source last finished an export, and last succeeded.

``cairn <source> export`` and ``sync`` write ``<state_home>/cairn/exports/<source>.json``
inside the operation lock (docs/routine-hardening-spec.md section 4.5). The catalog
cannot answer "when did this source last export": it is a cache, saved only when it
changed. A routine reads the receipts to warn about a digest source nobody refreshes.

A receipt never fails an export: a write error is a warning on stderr.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import state_home
from .storage import atomic_write


def receipt_path(source: str) -> Path:
    """``<state_home>/cairn/exports/<source>.json``."""
    return state_home() / "cairn" / "exports" / f"{source}.json"


def read(source: str) -> dict | None:
    """The receipt of ``source``, or ``None`` when it is missing or unreadable."""
    try:
        data = json.loads(receipt_path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(source: str, command: str, exit_code: object) -> None:
    """Record one finished ``export`` or ``sync`` of ``source``.

    ``last_ok_at`` moves only when ``exit_code`` is 0; every other field describes
    the latest run. An ``OSError`` is printed as a warning and swallowed.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    previous = read(source) or {}
    receipt = {
        "source": source,
        "last_command": command,
        "last_finished_at": now,
        "last_exit": exit_code,
        "last_ok_at": now if exit_code == 0 else previous.get("last_ok_at"),
        "cairn_version": __version__,
    }
    try:
        atomic_write(receipt_path(source), json.dumps(receipt, indent=2) + "\n")
    except OSError as exc:
        print(f"warning: could not write the export receipt for {source}: {exc}", file=sys.stderr)


def record(conversation_source: object, command: str, exit_code: object) -> None:
    """:func:`write` for a :class:`~cairn.sources.base.ConversationSource`, keyed by the
    registry name of its collection (``claude``, ``codex``, ...)."""
    from .registry import SOURCES

    collection = getattr(conversation_source, "collection", "")
    name = next((s.source for s in SOURCES if s.collection == collection), collection)
    if name:
        write(name, command, exit_code)


__all__ = ["read", "receipt_path", "record", "write"]
