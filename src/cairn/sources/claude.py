#!/usr/bin/env python3
"""Sync Claude Code sessions to markdown.

Thin adapter over :mod:`cairn`. Only the Claude-specific parts live here -
discovery + parse of ``~/.claude/projects/*.jsonl`` and the Claude frontmatter /
body layout. Shared rendering, frontmatter, listing, mutations, and CLI wiring
come from cairn.

Usage:
    claude-sessions sync [--session-id ID] [--transcript PATH]
    claude-sessions export (--today | --all | FILE)
    claude-sessions resume (--pick | --active | FILE) [--fork]
    claude-sessions note TEXT [--session-id ID]
    claude-sessions close [TEXT] [--session-id ID]
    claude-sessions log [TEXT] [--status S] [--tags T] [--rating N]
    claude-sessions context
    claude-sessions list [--active | --all] [--json]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from .. import cli
from ..config import get_config
from ..frontmatter import (
    FrontmatterParseError,
    head_close,
    head_open,
    parse_frontmatter,
    resolve_title,
    title_line,
)
from ..jsonl import find_session_jsonl, iter_session_files, parse_jsonl, tool_result_text
from ..projects import (
    project_name_from_dir,
    repo_name_from_dir,
    repo_subdir_from_name,
)
from ..rendering import render_artifacts
from ..schema import SessionData
from ..timeutil import today_str
from .base import ConversationSource

COLLECTION = "claude-code-sessions"
_config = get_config()
OUTPUT_DIR = _config.output_dir(COLLECTION)
PROJECTS_BASE = _config.collection(COLLECTION).require_store()


# =============================================================================
# Discovery
# =============================================================================


def _get_all_session_files() -> list[Path]:
    """All non-empty JSONL session files under this source's configured store."""
    return iter_session_files(PROJECTS_BASE)


def find_jsonl(session_id: str) -> Path | None:
    """Find a JSONL file for a session_id across all project dirs."""
    return find_session_jsonl(PROJECTS_BASE, session_id)


def _project_for_jsonl(jsonl_path: Path) -> tuple[str, str]:
    """(project_group, repo_name) for a JSONL file based on its parent dir."""
    encoded = jsonl_path.parent.name
    return project_name_from_dir(encoded), repo_name_from_dir(encoded)


# =============================================================================
# Parsing
# =============================================================================

# Shared JSONL parsing lives in cairn.jsonl (reused by cairn.recall). Keep the
# private name as a thin alias so the export body below reads unchanged.
_tool_result_text = tool_result_text


def extract_file_operations(records: list[dict]) -> dict:
    """Extract files created and modified from toolUseResult records."""
    files_created = []
    files_modified = set()

    for record in records:
        result = record.get("toolUseResult", {})
        if not isinstance(result, dict):
            continue
        file_path = result.get("filePath")
        if not file_path:
            continue
        if result.get("type") == "create":
            if file_path not in files_created:
                files_created.append(file_path)
        elif result.get("structuredPatch") or result.get("oldString"):
            files_modified.add(file_path)

    files_modified = files_modified - set(files_created)
    return {"created": files_created, "modified": list(files_modified)}


def extract_session_data(records: list[dict]) -> dict:
    """Extract session metadata + ordered messages from JSONL records."""
    data: dict[str, Any] = {
        "session_id": None,
        "date": None,
        "title": None,
        "summary": None,
        "skills": [],
        "messages": 0,
        "user_messages": [],
        "messages_ordered": [],
        "first_timestamp": None,
        "last_timestamp": None,
        "files_created": [],
        "files_modified": [],
        "git_branch": None,
    }

    for record in records:
        # Skip subagent/warmup side-threads - not the main conversation.
        if record.get("isSidechain"):
            continue

        record_type = record.get("type")

        if record.get("sessionId") and not data["session_id"]:
            data["session_id"] = record["sessionId"]

        # Last non-null branch wins (a session can start on one branch and switch).
        if record.get("gitBranch"):
            data["git_branch"] = record["gitBranch"]

        if record_type == "user":
            timestamp = record.get("timestamp", "")
            if timestamp:
                if not data["date"]:
                    data["date"] = timestamp.split("T")[0]
                if not data["first_timestamp"]:
                    data["first_timestamp"] = timestamp
                data["last_timestamp"] = timestamp

        if record_type == "custom-title":
            custom_title = record.get("customTitle", "")
            if custom_title:
                data["title"] = custom_title.split("\n")[0].strip()[:100]

        if record_type == "summary":
            summary = record.get("summary", "")
            if summary:
                data["summary"] = summary

        timestamp = record.get("timestamp")

        if record_type == "user":
            data["messages"] += 1
            msg = record.get("message", {})
            content = msg.get("content", "")
            is_meta = bool(record.get("isMeta"))
            if content and isinstance(content, str):
                if not is_meta:
                    data["user_messages"].append(content)
                    data["messages_ordered"].append(
                        {
                            "role": "user",
                            "timestamp": timestamp,
                            "text": content,
                        }
                    )
            elif isinstance(content, list):
                user_texts = []
                tool_turns = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype == "text":
                        text = item.get("text", "")
                        if text and not is_meta:
                            data["user_messages"].append(text)
                            user_texts.append(text)
                    elif itype == "tool_result":
                        tool_turns.append(
                            {
                                "role": "tool",
                                "timestamp": timestamp,
                                "text": _tool_result_text(item.get("content")),
                            }
                        )
                joined = "\n".join(user_texts).strip()
                if joined:
                    data["messages_ordered"].append(
                        {
                            "role": "user",
                            "timestamp": timestamp,
                            "text": joined,
                        }
                    )
                data["messages_ordered"].extend(tool_turns)

        if record_type == "assistant":
            msg = record.get("message", {})
            contents = msg.get("content", [])
            if isinstance(contents, list):
                texts = []
                thinking_parts = []
                reasoning_recoverable = True
                tool_calls = []
                for item in contents:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype == "text":
                        texts.append(item.get("text", ""))
                    elif itype == "thinking":
                        cleartext = (item.get("thinking") or "").strip()
                        if cleartext:
                            thinking_parts.append(cleartext)
                        elif item.get("signature"):
                            # Encrypted/redacted reasoning (empty text + signature).
                            reasoning_recoverable = False
                    elif itype == "tool_use":
                        tool_calls.append(
                            {
                                "name": item.get("name", ""),
                                "arguments": json.dumps(item.get("input", {})),
                                "status": None,
                                "result": None,
                            }
                        )
                    if item.get("name") == "Skill":
                        skill_input = item.get("input", {})
                        skill_name = skill_input.get("skill", "")
                        if skill_name and skill_name not in data["skills"]:
                            data["skills"].append(skill_name)
                text = "\n".join(t for t in texts if t).strip()
                if text or thinking_parts or tool_calls:
                    data["messages_ordered"].append(
                        {
                            "role": "assistant",
                            "timestamp": timestamp,
                            "text": text,
                            "thinking": "\n".join(thinking_parts) if thinking_parts else None,
                            "reasoning_recoverable": reasoning_recoverable,
                            "tool_calls": tool_calls,
                        }
                    )

    if not data["title"] and data["user_messages"]:
        first_msg = data["user_messages"][0]
        data["title"] = first_msg.replace("\n", " ").strip()[:80]

    if not data["date"]:
        data["date"] = today_str()

    file_ops = extract_file_operations(records)
    data["files_created"] = file_ops["created"]
    data["files_modified"] = file_ops["modified"]

    return data


# =============================================================================
# Source adapter
# =============================================================================


class ClaudeSource(ConversationSource):
    name = "claude-sessions"
    version = "2.0.0"
    output_dir = OUTPUT_DIR
    session_type = "claude-session"
    collection = "claude-code-sessions"
    list_default = "active"
    session_env_var = "CLAUDE_SESSION_ID"
    id_field = "session_id"
    preserved_note = "syncs"

    # ---- rendering ----
    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        head = head_open(self.session_type, data["date"], self.id_field, data["session_id"])
        if data.get("repo"):
            head.append(f"repo: {data['repo']}")
        if data.get("git_branch"):
            head.append(f"git_branch: {data['git_branch']}")

        title = resolve_title(data, existing_fm)
        head.append(title_line(title))

        if data["summary"]:
            summary_escaped = data["summary"].replace('"', '\\"').replace("\n", " ")
            head.append(f'summary: "{summary_escaped}"')

        if data["skills"]:
            head.append("skills:")
            for skill in sorted(data["skills"]):
                head.append(f"  - {skill}")

        head.extend(head_close(data.get("last_timestamp"), data["messages"]))

        body_extra = []
        if data["summary"]:
            body_extra.extend(["## Summary", "", data["summary"], ""])
        if data["skills"]:
            body_extra.append("## Skills Used")
            body_extra.append("")
            for skill in sorted(data["skills"]):
                body_extra.append(f"- {skill}")
            body_extra.append("")

        body_extra.extend(
            render_artifacts(data.get("files_created", []), data.get("files_modified", []))
        )

        return self.assemble(
            head, body_extra, title, existing_fm, my_notes, data["messages_ordered"]
        )

    # ---- per-session sync ----
    def sync_session(
        self,
        session_id: str,
        transcript_path: str | None = None,
        quiet: bool = False,
        project: str | None = None,
    ) -> Path | None:
        if session_id.startswith("agent-"):
            return None

        jsonl_path = Path(transcript_path) if transcript_path else find_jsonl(session_id)
        if not jsonl_path:
            return None

        if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
            return None

        # Skip if output is already up to date.
        existing_file = self.find_file(session_id)
        if (
            existing_file
            and existing_file.exists()
            and existing_file.stat().st_mtime >= jsonl_path.stat().st_mtime
        ):
            return existing_file

        repo = None
        if not project:
            project, repo = _project_for_jsonl(jsonl_path)
        else:
            repo = repo_name_from_dir(jsonl_path.parent.name)

        repo_subdir = repo_subdir_from_name(project, repo) if project and repo else None

        records = parse_jsonl(jsonl_path)
        if not records:
            return None

        data = extract_session_data(records)
        data["session_id"] = session_id
        data["repo"] = repo
        data["project"] = project
        data["repo_subdir"] = repo_subdir

        output_file = self.export_one(session_id, cast(SessionData, data), existing_file)
        if output_file is None:
            return None

        if not quiet:
            print(f"Synced: {output_file}")
        return output_file

    # ---- export ----
    def extend_export_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("file", nargs="?", help="export one specific session file")

    def run_export(self, args: argparse.Namespace) -> int:
        if args.file:
            sessions = [Path(args.file)]
        elif args.all:
            sessions = _get_all_session_files()
            print(f"Found {len(sessions)} total sessions")
        else:
            cutoff = args.since
            sessions = [f for f in _get_all_session_files() if f.stat().st_mtime >= cutoff]
            print(f"Found {len(sessions)} sessions in the selected window")

        for session_file in sessions:
            session_id = session_file.stem
            self.sync_session(session_id, str(session_file), quiet=args.quiet)
        return 0

    # ---- claude-only extras: sync (hook) + resume ----
    def add_extra_parsers(
        self, subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser
    ) -> None:
        p_sync = subparsers.add_parser(
            "sync", parents=[common], help="Sync session (hook or explicit)"
        )
        p_sync.add_argument("--session-id", help="Session ID")
        p_sync.add_argument("--transcript", help="Transcript file path")
        p_sync.set_defaults(func=self.cmd_sync)

        p_resume = subparsers.add_parser("resume", parents=[common], help="Resume a session")
        p_resume.add_argument("--pick", "-p", action="store_true", help="Interactive picker")
        p_resume.add_argument("--active", "-a", action="store_true", help="Most recent active")
        p_resume.add_argument("--fork", "-f", action="store_true", help="Fork instead of continue")
        p_resume.add_argument("--all", action="store_true", help="Show all (not just active)")
        p_resume.add_argument("file", nargs="?", help="Markdown file to resume from")
        p_resume.set_defaults(func=self.cmd_resume)

    def cmd_sync(self, args: argparse.Namespace) -> int:
        session_id = args.session_id
        transcript_path = args.transcript
        if not session_id:
            # Hook invocation: read the session/transcript from the hook's JSON stdin.
            # A malformed or absent payload is non-fatal - we simply skip the sync.
            try:
                hook_input = json.loads(sys.stdin.read())
                session_id = hook_input.get("session_id")
                transcript_path = transcript_path or hook_input.get("transcript_path")
            except (OSError, json.JSONDecodeError):
                pass
        if not session_id:
            return 0
        self.sync_session(session_id, transcript_path, quiet=args.quiet)
        return 0

    def cmd_resume(self, args: argparse.Namespace) -> int:
        from ..listing import interactive_pick

        target_file = None
        if args.file:
            target_file = Path(args.file).expanduser()
            if not target_file.exists():
                print(f"Error: File not found: {target_file}", file=sys.stderr)
                return 1
        elif args.active:
            active = self.active_sessions()
            if not active:
                print("No active sessions found.")
                return 1
            target_file = active[0][0]
            print(f"Most recent active: {target_file.name}")
        elif args.pick:
            sessions = self.all_sessions() if args.all else self.active_sessions()
            target_file = interactive_pick(sessions)
            if not target_file:
                print("No session selected.")
                return 0
        else:
            print("Error: Specify --pick, --active, or a file", file=sys.stderr)
            return 2

        content = target_file.read_text(encoding="utf-8")
        try:
            fm = parse_frontmatter(content)
        except FrontmatterParseError as e:
            print(f"Error: cannot parse frontmatter in {target_file}: {e}", file=sys.stderr)
            return 1
        session_id = fm.get("session_id")
        if not session_id:
            print(f"Error: No session_id in {target_file}", file=sys.stderr)
            return 1

        cmd = ["claude", "--resume", session_id]
        if args.fork:
            cmd.append("--fork-session")
        print(f"Resuming: {session_id}")
        os.execvp("claude", cmd)


def main() -> None:
    """Console entry point - drive the Claude source through the shared CLI."""
    cli.run(ClaudeSource())


if __name__ == "__main__":
    main()
