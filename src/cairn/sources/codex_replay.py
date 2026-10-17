"""Where a forked Codex child's replayed parent history ends.

Version tag: ``codex-cli 0.160``. This is a heuristic over an undocumented file
layout, kept in one module so a format change touches one place.

Evidence (100 of 100 forked child rollouts on the author's machine, codex-cli 0.160):

* A child spawned with ``fork_context`` writes two ``session_meta`` lines: its own
  first, then the parent's, with nothing between them. The parent's history is then
  replayed after the second meta.
* The replayed records are stamped at fork time, so their timestamps are not older
  than the child's own meta and cannot locate the cut.
* The child's own history starts at the first ``event_msg task_started`` whose
  ``turn_id`` sorts at or after the child's thread id. Both are UUIDv7, so the 48-bit
  millisecond prefix orders them in time (rule 1, held in 100 of 100 files).
* In 95 of 100 files a ``thread_settings_applied`` event (its ``thread_id`` is the
  child's) and a developer message containing ``<multi_agent_role>`` sit directly
  before that turn, with no rendered record between the marker and the cut (rule 2).
  Both rules stay active and the first signal wins: an ancestor's UUIDv7 turn ids
  must not hide the child's own marker when the child's turn ids are not UUIDv7.
* Nested forks repeat the layout: one more ``session_meta`` and one more marker group
  per level, and rule 1 still compares against the first meta's id.

When neither rule fires before end of file the caller decides: a recently written file
is a live child that has no own records yet (skip it this run); an older one exports
the whole file with ``replay_unresolved: true`` (rule 3). Nothing here raises.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

REPLAY_HEURISTIC_VERSION = "codex-cli 0.160"
# A rollout still replaying at end of file and modified more recently than this is
# treated as a live child (the run appends to it) and skipped without an error.
LIVE_CHILD_WINDOW_S = 600

_ROLE_MARKER = "<multi_agent_role>"


def uuid7_key(value: object) -> tuple[int, str] | None:
    """Sort key ``(48-bit unix-ms timestamp, canonical string)`` for a UUIDv7, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if parsed.version != 7:
        return None
    return parsed.int >> 80, str(parsed)


def _content_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


@dataclass(frozen=True)
class ReplayCut:
    """Immutable replay state: each :meth:`observe` returns the next state.

    ``rule`` is ``None`` while the parent history is still replaying; once set, the
    record passed to the :meth:`observe` call that set it is the child's first own
    record and every later record belongs to the child.
    """

    child_id: str
    child_key: tuple[int, str] | None
    metas: int = 1  # replayed session_meta lines seen (1 for a direct fork)
    groups: int = 0  # marker groups seen (one per fork level)
    in_group: bool = False  # the previous record was a marker
    rule: str | None = None

    @classmethod
    def start(cls, child_id: str) -> ReplayCut:
        """State right after the second ``session_meta`` of child ``child_id``."""
        return cls(child_id, uuid7_key(child_id))

    @property
    def ended(self) -> bool:
        return self.rule is not None

    def with_meta(self) -> ReplayCut:
        """Another replayed ``session_meta`` (a nested fork): one more marker group."""
        return replace(self, metas=self.metas + 1, in_group=False)

    def observe(self, rtype: object, payload: dict) -> ReplayCut:
        """Advance over one record; the result's ``rule`` is set when the cut is here."""
        if self.ended:
            return self
        ptype = payload.get("type")
        if rtype == "event_msg" and ptype == "task_started":
            turn_key = uuid7_key(payload.get("turn_id"))
            if turn_key is not None and self.child_key is not None and turn_key >= self.child_key:
                return replace(self, rule="uuid7-turn", in_group=False)
            return replace(self, in_group=False)
        # No rule locks the other out: whichever boundary signal fires first wins, so a
        # UUIDv7 ancestor turn never hides the child's own marker. In the sampled files
        # the marker sits directly before the rule-1 turn with nothing rendered between.
        marker = self._marker(rtype, ptype, payload)
        if marker is None:
            return replace(self, in_group=False) if self.in_group else self
        if marker == "exact":
            return replace(self, rule="thread-settings", in_group=True)
        groups = self.groups if self.in_group else self.groups + 1
        if groups >= self.metas and marker == "own-level":
            return replace(self, groups=groups, rule="role-marker", in_group=True)
        return replace(self, groups=groups, in_group=True)

    def _marker(self, rtype: object, ptype: object, payload: dict) -> str | None:
        if rtype == "event_msg" and ptype == "thread_settings_applied":
            thread_id = payload.get("thread_id")
            if thread_id == self.child_id:
                return "exact"
            return "other-thread" if thread_id else "own-level"
        if (
            rtype == "response_item"
            and ptype == "message"
            and payload.get("role") == "developer"
            and _ROLE_MARKER in _content_text(payload.get("content"))
        ):
            return "own-level"
        return None


__all__ = ["LIVE_CHILD_WINDOW_S", "REPLAY_HEURISTIC_VERSION", "ReplayCut", "uuid7_key"]
