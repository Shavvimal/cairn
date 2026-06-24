#!/usr/bin/env python3
"""Export Codex CLI sessions to markdown.

Thin adapter over :mod:`cairn`. Codex-specific parts only: discovery +
parse of ``~/.codex/sessions/**/rollout-*.jsonl`` (including the resume/fork
fragment de-duplication) and the Codex frontmatter layout.

Usage:
    codex-sessions export (--today | --all | --since DATE)
    codex-sessions list [--all] [--json]
    codex-sessions note/close/log ...   (shared)
    codex-sessions context
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import cli
from ..config import get_config
from ..frontmatter import (
    head_close,
    head_open,
    resolve_title,
    title_line,
)
from ..jsonl import iter_jsonl
from ..projects import project_from_folder_path, repo_label
from ..schema import SessionData
from ..timeutil import today_str
from .base import ConversationSource

COLLECTION = "codex-sessions"
_config = get_config()
OUTPUT_DIR = _config.output_dir(COLLECTION)
SESSIONS_DIR = _config.collection(COLLECTION).require_store()


# =============================================================================
# Rollout parsing
# =============================================================================


def _content_text(content) -> str:
    """Join text from a list of content items (output_text / input_text)."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in ("output_text", "input_text"):
            text = item.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _reasoning_summary(summary) -> str:
    """Pull cleartext text out of a reasoning ``summary`` list, if any."""
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    parts = []
    for item in summary:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def parse_rollout(file_path: Path) -> dict | None:
    """Parse a Codex rollout JSONL into structured session data (None if unusable)."""
    data: dict[str, Any] = {
        "session_id": None,
        "cwd": None,
        "date": None,
        "model": None,
        "cli_version": None,
        "branch": None,
        "commit": None,
        "first_timestamp": None,
        "last_timestamp": None,
        "user_messages": [],
        "messages": [],
    }

    pending_calls = {}  # call_id -> index of the assistant message carrying it

    for _, record in iter_jsonl(file_path):
        rtype = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        ts = record.get("timestamp") or payload.get("timestamp")
        if ts:
            if not data["first_timestamp"]:
                data["first_timestamp"] = ts
            data["last_timestamp"] = ts

        if rtype == "session_meta":
            data["session_id"] = payload.get("id")
            data["cwd"] = payload.get("cwd")
            data["cli_version"] = payload.get("cli_version")
            meta_ts = payload.get("timestamp") or ts
            if meta_ts:
                data["date"] = meta_ts.split("T")[0]
            git = payload.get("git")
            if isinstance(git, dict):
                data["branch"] = git.get("branch") or data["branch"]
                data["commit"] = git.get("commit_hash") or data["commit"]
            if payload.get("model"):
                data["model"] = payload["model"]
            continue

        if rtype == "turn_context":
            if payload.get("model"):
                data["model"] = payload["model"]
            continue

        if rtype == "event_msg":
            ptype = payload.get("type")
            if ptype in ("token_count", "rate_limits", "task_started", "task_complete"):
                continue
            if ptype == "user_message":
                message = payload.get("message", "")
                if isinstance(message, str) and message.strip():
                    text = message.strip()
                    data["user_messages"].append(text)
                    data["messages"].append(
                        {
                            "role": "user",
                            "timestamp": ts,
                            "text": text,
                            "tool_calls": [],
                        }
                    )
            continue

        if rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message":
                if payload.get("role") == "assistant":
                    text = _content_text(payload.get("content"))
                    if text:
                        data["messages"].append(
                            {
                                "role": "assistant",
                                "timestamp": ts,
                                "text": text,
                                "tool_calls": [],
                            }
                        )
                continue
            if ptype == "function_call":
                call_id = payload.get("call_id")
                name = payload.get("name", "tool")
                arguments = payload.get("arguments")
                if arguments is None:
                    arguments = payload.get("rawArgs")
                tool_call = {
                    "name": name,
                    "arguments": arguments,
                    "status": None,
                    "result": None,
                }
                data["messages"].append(
                    {
                        "role": "assistant",
                        "timestamp": ts,
                        "text": "",
                        "tool_calls": [tool_call],
                    }
                )
                if call_id:
                    pending_calls[call_id] = len(data["messages"]) - 1
                continue
            if ptype == "function_call_output":
                call_id = payload.get("call_id")
                output = payload.get("output", "")
                if isinstance(output, dict):
                    output = output.get("output", "")
                if not isinstance(output, str):
                    try:
                        output = json.dumps(output)
                    except (TypeError, ValueError):
                        output = str(output)
                idx = pending_calls.pop(call_id, None) if call_id else None
                if idx is not None:
                    data["messages"][idx]["tool_calls"][0]["result"] = output
                else:
                    data["messages"].append(
                        {
                            "role": "tool",
                            "timestamp": ts,
                            "text": "",
                            "tool_calls": [
                                {
                                    "name": "tool",
                                    "arguments": None,
                                    "status": None,
                                    "result": output,
                                }
                            ],
                        }
                    )
                continue
            if ptype == "reasoning":
                summary = payload.get("summary")
                thinking = _reasoning_summary(summary)
                if thinking:
                    data["messages"].append(
                        {
                            "role": "assistant",
                            "timestamp": ts,
                            "text": "",
                            "thinking": thinking,
                            "tool_calls": [],
                        }
                    )
                elif payload.get("encrypted_content"):
                    data["messages"].append(
                        {
                            "role": "assistant",
                            "timestamp": ts,
                            "text": "",
                            "reasoning_recoverable": False,
                            "tool_calls": [],
                        }
                    )
                continue
            continue

    if not data["session_id"]:
        return None

    if not data["date"]:
        if data["first_timestamp"]:
            data["date"] = data["first_timestamp"].split("T")[0]
        else:
            data["date"] = today_str()

    project, repo_subdir = (None, None)
    if data["cwd"]:
        project, repo_subdir = project_from_folder_path(data["cwd"])

    data["project"] = project
    data["repo_subdir"] = repo_subdir
    data["repo"] = repo_label(project, repo_subdir)
    data["message_count"] = len(data["user_messages"])

    title = None
    if data["user_messages"]:
        title = data["user_messages"][0].replace("\n", " ").strip()[:80]
    data["title"] = title or "Untitled Session"

    return data


def _get_all_rollout_files() -> list[Path]:
    """All non-empty rollout JSONL files under ~/.codex/sessions/."""
    if not SESSIONS_DIR.is_dir():
        return []
    return [f for f in SESSIONS_DIR.rglob("rollout-*.jsonl") if f.stat().st_size > 0]


# =============================================================================
# Source adapter
# =============================================================================


class CodexSource(ConversationSource):
    name = "codex-sessions"
    version = "2.0.0"
    output_dir = OUTPUT_DIR
    session_type = "codex-session"
    collection = "codex-sessions"
    list_default = "not-done"
    id_field = "session_id"
    preserved_note = "exports"

    def short_id(self, session_id: str) -> str:
        """Collision-resistant short id: 8 readable (time-ordered) hex + 4 random
        hex from the UUIDv7 tail, so sessions started seconds apart don't collide."""
        head = session_id.replace("-", "")[:8]
        tail = session_id.split("-")[-1][:4]
        return f"{head}{tail}"

    def list_mid_col(self, fm: dict) -> str:
        model = fm.get("model", "")
        return f"{(f' [{model}]' if model else ''):>12}"

    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        head = head_open(self.session_type, data["date"], self.id_field, data["session_id"])
        if data.get("repo"):
            head.append(f"repo: {data['repo']}")
        if data.get("branch"):
            head.append(f"branch: {data['branch']}")
        if data.get("commit"):
            head.append(f"commit: {data['commit']}")
        if data.get("model"):
            head.append(f"model: {data['model']}")

        title = resolve_title(data, existing_fm)
        head.append(title_line(title))
        head.extend(head_close(data.get("last_timestamp"), data["message_count"]))

        return self.assemble(head, [], title, existing_fm, my_notes, data["messages"])

    # ---- export driver ----
    def export_rollouts(self, date_filter: float | None = None, quiet: bool = False) -> int:
        rollouts = _get_all_rollout_files()
        exported = 0

        # Codex resume/fork writes a *new* rollout per resume but keeps the same
        # session_meta.id, so several files map to one logical session. Keep the
        # most complete fragment (most user messages, then latest activity / mtime).
        best: dict[str, tuple] = {}  # session_id -> (score, source_mtime, data)
        for rollout in rollouts:
            data = parse_rollout(rollout)
            if not data:
                continue
            source_mtime = rollout.stat().st_mtime
            score = (
                len(data["user_messages"]),
                len(data.get("messages", [])),
                data.get("last_timestamp") or "",
                source_mtime,
            )
            prev = best.get(data["session_id"])
            if prev is None or score > prev[0]:
                best[data["session_id"]] = (score, source_mtime, data)

        for session_id, (_score, source_mtime, data) in best.items():
            if date_filter and source_mtime < date_filter:
                continue

            existing_file = self.find_file(session_id)
            output_file = existing_file or self.output_path(
                session_id, data["date"], data.get("project"), data.get("repo_subdir")
            )

            if output_file.exists() and output_file.stat().st_mtime >= source_mtime:
                continue

            if self.export_one(session_id, data, existing_file) is None:
                continue

            exported += 1
            if not quiet:
                print(f"  Exported: {output_file}")

        if exported == 0 and not quiet:
            print("  All sessions up to date, nothing to export")

        return exported

    def run_export(self, args: argparse.Namespace) -> int:
        if not SESSIONS_DIR.is_dir():
            print(f"Error: Sessions dir not found: {SESSIONS_DIR}", file=sys.stderr)
            return 1

        date_filter = None if args.all else args.since
        print(
            "Exporting all sessions..."
            if args.all
            else "Exporting sessions in the selected window..."
        )

        exported = self.export_rollouts(date_filter, quiet=args.quiet)
        print(f"\nExported {exported} sessions to {OUTPUT_DIR}")
        return 0


def main() -> None:
    """Console entry point - drive the Codex source through the shared CLI."""
    cli.run(CodexSource())


if __name__ == "__main__":
    main()
