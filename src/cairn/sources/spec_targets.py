"""Dated spec targets: the host version each adapter's record shapes were checked on.

An adapter parses a format its host owns and changes without notice. Each entry
records the day the record shapes were last checked against a live store, the host
and its version on that machine, and the record ``type`` values the parser expects:
``conversational`` types should produce a message, ``ignored`` types are skipped on
purpose. The parser-drift guard (:mod:`cairn.sources.guards`) reads both sets, the
fixture tests compare the date, and ``cairn doctor`` prints the line.

Constants only. This module never reads the config, so ``cairn doctor`` and the gap
check import it without the store lookup the source modules do at import time.
Update an entry only after checking the shapes on a real store that day.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpecTarget:
    checked: str  # ISO date the record shapes were checked against a live store
    harness: str  # the host program, e.g. "Claude Code"
    harness_version: str  # the version on the machine that day ("unrecorded" if unknown)
    store: str  # what one unit of the native store is
    adapter_version: str  # the adapter's ``version`` when the check was made
    conversational: frozenset[str] = frozenset()
    ignored: frozenset[str] = frozenset()

    def describe(self) -> str:
        """``spec target 2026-10-30 (Claude Code 2.1.314)``."""
        return f"spec target {self.checked} ({self.harness} {self.harness_version})"


SPEC_TARGETS: dict[str, SpecTarget] = {
    "claude": SpecTarget(
        checked="2026-10-30",
        harness="Claude Code",
        harness_version="2.1.314",
        store="JSONL, one file per session; subagents under <session>/subagents/",
        adapter_version="2.0.0",
        conversational=frozenset({"user", "assistant"}),
        ignored=frozenset(
            {
                "agent-name",
                "ai-title",
                "api-request",
                "api-request-blob",
                "api-request-shape",
                "artifact-autoreact-ledger",
                "artifact-comment-monitor",
                "atis-latch",
                "attachment",
                "cost-state",
                "custom-title",
                "file-history-delta",
                "file-history-snapshot",
                "frame-link",
                "last-prompt",
                "mode",
                "permission-mode",
                "pr-link",
                "queue-operation",
                "summary",
                "system",
                "tools-used",
            }
        ),
    ),
    "codex": SpecTarget(
        checked="2026-10-30",
        harness="Codex",
        harness_version="0.165.0",
        store="JSONL rollout per thread fragment",
        adapter_version="2.0.0",
        conversational=frozenset({"response_item", "event_msg"}),
        ignored=frozenset(
            {
                "compacted",
                "inter_agent_communication_metadata",
                "session_meta",
                "token_usage_record",
                "turn_context",
                "world_state",
            }
        ),
    ),
    "antigravity": SpecTarget(
        checked="2026-10-30",
        harness="agy",
        harness_version="1.4.10",
        store="JSONL transcript per conversation under brain/<id>/",
        adapter_version="2.0.0",
        conversational=frozenset({"USER_INPUT", "PLANNER_RESPONSE"}),
        ignored=frozenset({"CHECKPOINT", "ERROR_MESSAGE", "GENERIC", "SYSTEM_MESSAGE"}),
    ),
    "cursor": SpecTarget(
        checked="2026-10-30",
        harness="Cursor",
        harness_version="unrecorded",
        store="SQLite cursorDiskKV rows, one composer per conversation",
        adapter_version="2.0.0",
        # A composer has no record types: each header names one bubble to fetch.
        conversational=frozenset({"bubble"}),
    ),
}


def spec_target_of(source: str) -> SpecTarget | None:
    return SPEC_TARGETS.get(source)


__all__ = ["SPEC_TARGETS", "SpecTarget", "spec_target_of"]
