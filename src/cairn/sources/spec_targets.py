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
    # Record types that should render a message. A name with a slash is a finer
    # kind a source counts through ``JsonlStats.kind_of`` (Codex).
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
        # event_msg and response_item also carry control payloads (task_started,
        # thread_settings_applied, developer and context messages), so the
        # conversational names are the finer kinds the parser renders.
        conversational=frozenset(
            {
                "event_msg/user_message",
                "response_item/message/assistant",
                "response_item/function_call",
                "response_item/custom_tool_call",
            }
        ),
        # Under event_msg and response_item the finer kind decides: a payload type
        # in neither set is unknown (drift when nothing renders), so the control
        # and context kinds measured on the live store are listed here.
        ignored=frozenset(
            {
                "compacted",
                "inter_agent_communication_metadata",
                "session_meta",
                "token_usage_record",
                "turn_context",
                "world_state",
                "event_msg/agent_message",
                "event_msg/agent_reasoning",
                "event_msg/collab_agent_spawn_end",
                "event_msg/collab_waiting_end",
                "event_msg/context_compacted",
                "event_msg/error",
                "event_msg/exec_command_end",
                "event_msg/item_completed",
                "event_msg/mcp_tool_call_end",
                "event_msg/patch_apply_end",
                "event_msg/rate_limits",
                "event_msg/task_complete",
                "event_msg/task_started",
                "event_msg/thread_name_updated",
                "event_msg/thread_settings_applied",
                "event_msg/token_count",
                "event_msg/turn_aborted",
                "event_msg/web_search_end",
                "response_item/additional_tools",
                "response_item/agent_message",
                "response_item/custom_tool_call_output",
                "response_item/function_call_output",
                "response_item/message/developer",
                "response_item/message/user",
                "response_item/reasoning",
                "response_item/web_search_call",
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
        # One bubble alone is an aborted request (no text), not drift.
        conversational=frozenset({"bubble"}),
        ignored=frozenset({"single-bubble"}),
    ),
}


def spec_target_of(source: str) -> SpecTarget | None:
    return SPEC_TARGETS.get(source)


__all__ = ["SPEC_TARGETS", "SpecTarget", "spec_target_of"]
