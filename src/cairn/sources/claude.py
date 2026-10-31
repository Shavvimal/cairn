#!/usr/bin/env python3
"""Sync Claude Code sessions to markdown.

Thin adapter over :mod:`cairn`. Only the Claude-specific parts live here -
discovery + parse of ``~/.claude/projects/*.jsonl`` (and each session's
``subagents/`` transcripts, exported as ``claude-subagent`` documents in the
parent's session folder) and the Claude frontmatter / body layout. Shared rendering, frontmatter, listing, mutations, and CLI wiring
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
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast

from .. import cli
from ..config import get_config
from ..frontmatter import (
    FrontmatterParseError,
    _escape_scalar,
    head_close,
    head_open,
    parse_frontmatter,
    resolve_title,
    title_line,
)
from ..jsonl import (
    JsonlStats,
    find_session_jsonl,
    find_subagent_jsonl,
    iter_jsonl,
    iter_session_files,
    iter_subagent_files,
    newest_mtime,
    subagent_meta_path,
    subagent_parent_jsonl,
    tool_result_text,
    workflow_run_of,
)
from ..paths import session_dir_of, subagent_path
from ..projects import (
    project_name_from_dir,
    repo_name_from_dir,
    repo_subdir_from_name,
)
from ..rendering import render_artifacts
from ..schema import Message, SessionData, SubagentRef
from ..store import file_revision, is_current
from ..timeutil import today_str
from .base import ConversationSource
from .guards import ParserDrift
from .spec_targets import SPEC_TARGETS

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


def _in_subagent_layout(jsonl_path: Path) -> bool:
    """True for ``<sid>/subagents/[workflows/wf_x/]agent-*.jsonl`` (not a legacy
    top-level ``agent-*.jsonl`` side-thread, which is never exported)."""
    try:
        subagent_parent_jsonl(jsonl_path)
    except ValueError:
        return False
    return True


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

# Child transcripts carry bulky request-replay records (a third of the largest
# file) that the extractor never reads; skip them before JSON decoding.
_CHILD_SKIP_PREFIXES = ('"type":"api-request',)

# A peer message's content wraps the plaintext body; strip the wrapper when the
# record has no ``origin.body`` (older Claude Code versions).
_PEER_PREAMBLE = re.compile(r"\A\s*Another Claude session sent a message[^:\n]*:\s*")
_PEER_WRAPPER = re.compile(r"</?(?:cross-session-message|agent-message)\b[^>]*>")


class _FileOps:
    """Files created and modified, accumulated from ``toolUseResult`` records."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.modified: set[str] = set()

    def add(self, record: dict) -> None:
        result = record.get("toolUseResult", {})
        if not isinstance(result, dict):
            return
        file_path = result.get("filePath")
        if not file_path:
            return
        if result.get("type") == "create":
            if file_path not in self.created:
                self.created.append(file_path)
        elif result.get("structuredPatch") or result.get("oldString"):
            self.modified.add(file_path)

    def result(self) -> dict:
        return {"created": self.created, "modified": list(self.modified - set(self.created))}


def extract_file_operations(records: Iterable[dict]) -> dict:
    """Extract files created and modified from toolUseResult records."""
    ops = _FileOps()
    for record in records:
        ops.add(record)
    return ops.result()


def _content_text(content: object) -> str:
    """Plain text of a message ``content`` (a string or a list of text parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _peer_message(record: dict, timestamp: str | None) -> Message | None:
    """An ``### Agent`` turn for a message another Claude session sent, else ``None``."""
    origin = record.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return None
    body = origin.get("body")
    if not isinstance(body, str) or not body.strip():
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        body = _PEER_WRAPPER.sub("", _PEER_PREAMBLE.sub("", _content_text(content))).strip()
    sender = origin.get("from") or "unknown"
    name = origin.get("name")
    label = f"from agent {sender} ({name})" if name else f"from agent {sender}"
    return {"role": "agent", "timestamp": timestamp, "text": f"{label}:\n\n{body}"}


def extract_session_data(records: Iterable[dict], *, sidechain: bool = False) -> dict:
    """Extract session metadata + ordered messages from JSONL records in one pass.

    ``records`` can be a generator (the largest subagent transcript is 68 MB), so
    every field, file operations included, is collected in a single iteration.
    ``sidechain=True`` parses a subagent transcript: its ``isSidechain`` records are
    kept, ``sessionId`` becomes ``parent_session_id`` and ``session_id`` becomes
    ``agent-<agentId>``. ``finished`` is True when the last assistant record ended
    its turn (``stop_reason == "end_turn"``).
    """
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
        "finished": False,
    }
    file_ops = _FileOps()
    agent_id: str | None = None
    last_stop: str | None = None

    for record in records:
        # Skip subagent/warmup side-threads - not the main conversation.
        if record.get("isSidechain") and not sidechain:
            continue

        file_ops.add(record)
        record_type = record.get("type")

        if record.get("sessionId") and not data["session_id"]:
            data["session_id"] = record["sessionId"]
        if sidechain and agent_id is None and isinstance(record.get("agentId"), str):
            agent_id = record["agentId"]

        # Last non-null branch wins (a session can start on one branch and switch).
        if record.get("cwd"):
            data["cwd"] = record["cwd"]

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
            # A message from another agent is never user input: it renders as an
            # Agent turn and must be caught before the isMeta skip drops it.
            peer = _peer_message(record, timestamp)
            if peer is not None:
                data["messages_ordered"].append(peer)
                continue
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
            last_stop = msg.get("stop_reason") if isinstance(msg, dict) else None
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

    ops = file_ops.result()
    data["files_created"] = ops["created"]
    data["files_modified"] = ops["modified"]
    data["finished"] = last_stop == "end_turn"

    if sidechain:
        data["agent_id"] = agent_id
        data["parent_session_id"] = data["session_id"]
        data["session_id"] = f"agent-{agent_id}" if agent_id else None

    return data


def _records(
    path: Path, skip_prefixes: tuple[str, ...] = (), stats: JsonlStats | None = None
) -> Iterator[dict]:
    """Stream the records of a transcript (malformed lines skipped, counted in ``stats``)."""
    return (record for _, record in iter_jsonl(path, skip_prefixes, stats))


# =============================================================================
# Subagents
# =============================================================================


def read_subagent_meta(agent_jsonl: Path) -> dict:
    """The ``agent-<id>.meta.json`` beside a child transcript.

    A missing meta (seen once in 1067 children) yields ``{}``; a meta that is not
    a JSON object raises ``ValueError`` so a corrupt file is never read as empty.
    """
    path = subagent_meta_path(agent_jsonl)
    if not path.exists():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed subagent meta {path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError(f"malformed subagent meta {path}: not a JSON object")
    return meta


def _opt_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def subagent_fields(
    meta: dict, agent_id: str, parent_id: str | None, workflow_run: str | None
) -> dict:
    """Map a child's camelCase meta onto its document head keys."""
    return {
        "agent_id": agent_id,
        "parent_session_id": parent_id,
        "agent_type": _opt_str(meta.get("agentType")),
        "description": _opt_str(meta.get("description")),
        "tool_use_id": _opt_str(meta.get("toolUseId")),
        "spawn_depth": _opt_int(meta.get("spawnDepth")),
        "request_shape": _opt_str(meta.get("requestShape")),
        "model": _opt_str(meta.get("model")),
        "workflow_run": workflow_run,
        "workflow_phase": _opt_str(meta.get("workflowPhase")),
        "parent_agent_id": _opt_str(meta.get("parentAgentId")),
        "is_fork": meta.get("isFork") is True,
        "stopped_by_user": meta.get("stoppedByUser") is True,
    }


def _agent_id_of(agent_jsonl: Path) -> str:
    return agent_jsonl.stem.removeprefix("agent-")


def subagent_index(children: list[Path], parent_doc: Path | None) -> list[SubagentRef]:
    """The parent's ``## Subagents`` rows, read from the children's metas only.

    ``parent_doc`` gives each row its link (``./<parent stem>/agent-<id>.md``); pass
    ``None`` before the parent's path is known and fill it with :func:`_link_index`.

    A malformed meta never blocks the family: its row says the meta is unreadable,
    and :meth:`ClaudeSource.sync_children` reports that child's error, so the parent
    and every healthy sibling still export before the run fails.
    """
    refs: list[SubagentRef] = []
    for child in children:
        agent_id = _agent_id_of(child)
        try:
            meta = read_subagent_meta(child)
        except ValueError:
            meta, unreadable = {}, True
        else:
            unreadable = False
        fields = subagent_fields(meta, agent_id, None, workflow_run_of(child))
        if unreadable:
            title = f"agent {agent_id} (meta unreadable)"
        else:
            title = fields["description"] or f"agent {agent_id}"
        refs.append(
            {
                "agent_id": agent_id,
                "title": title,
                "agent_type": fields["agent_type"],
                "request_shape": fields["request_shape"],
                "workflow_run": fields["workflow_run"],
                "finished": meta.get("finished") is True,
                "stopped_by_user": fields["stopped_by_user"],
                "path": "",
            }
        )
    if parent_doc is not None:
        _link_index(refs, parent_doc)
    return refs


def _link_index(refs: list[SubagentRef], parent_doc: Path) -> None:
    for ref in refs:
        ref["path"] = f"./{parent_doc.stem}/agent-{ref['agent_id']}.md"


def parent_revision(jsonl: Path, index: list[SubagentRef]) -> str:
    """The parent's native revision: its transcript plus the index it renders.

    Every field of every row is hashed through one normalised form (the exact refs
    that render the index, link included), so any visible change to the index -
    a new child, a title, type, request shape, workflow run, finish or stop -
    re-renders the parent even when the parent transcript did not change.
    """
    rows = sorted(
        (json.dumps(dict(ref), sort_keys=True) for ref in index),
    )
    payload = file_revision(jsonl) + json.dumps(rows)
    return hashlib.sha256(payload.encode()).hexdigest()


def child_revision(agent_jsonl: Path) -> str:
    """A child's native revision: its transcript plus its meta, when one exists."""
    payload = file_revision(agent_jsonl)
    meta = subagent_meta_path(agent_jsonl)
    if meta.exists():
        payload += file_revision(meta)
    return hashlib.sha256(payload.encode()).hexdigest()


def _index_lines(refs: list[SubagentRef]) -> list[str]:
    if not refs:
        return []
    lines = ["## Subagents", ""]
    for ref in refs:
        title = _one_line(ref.get("title", "")).replace("[", "\\[").replace("]", "\\]")
        if ref.get("workflow_run"):
            shape = f"workflow {ref['workflow_run']}"
        else:
            shape = ref.get("request_shape") or "unknown"
        if ref.get("stopped_by_user"):
            state = "stopped"
        elif ref.get("finished"):
            state = "done"
        else:
            state = "running"
        agent_type = ref.get("agent_type") or "unknown"
        lines.append(
            f"- [{title}]({ref['path']}) - `agent-{ref['agent_id']}`, {agent_type}, {shape}, {state}"
        )
    lines.append("")
    return lines


def _quoted(key: str, value: str) -> str:
    return f'{key}: "{_escape_scalar(_one_line(value))}"'


def _one_line(value: str) -> str:
    """Flatten CR/LF so a free-text value stays one frontmatter or list line."""
    return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


# =============================================================================
# Source adapter
# =============================================================================


class ClaudeSource(ConversationSource):
    name = "claude-sessions"
    version = "2.0.0"
    spec_target = SPEC_TARGETS["claude"]
    output_dir = OUTPUT_DIR
    session_type = "claude-session"
    subagent_type = "claude-subagent"
    collection = "claude-code-sessions"
    list_default = "active"
    session_env_var = "CLAUDE_CODE_SESSION_ID"
    id_field = "session_id"
    preserved_note = "syncs"

    def find_file(self, session_id: str) -> Path | None:
        """A session document, or for ``memory:<label>`` the project decisions document
        that ``cairn claude memory import`` writes (so ``decide``, ``supersede`` and
        ``forget`` reach its rows)."""
        from .. import decisions

        label = session_id.removeprefix(f"{decisions.MEMORY_SOURCE}:")
        if label != session_id:
            if not decisions.MEMORY_LABEL_RE.fullmatch(label):
                return None
            path = decisions.memory_document(get_config(), label)
            return path if path.is_file() else None
        return super().find_file(session_id)

    # ---- rendering ----
    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        if data.get("agent_id"):
            return self._render_subagent(data, existing_fm, my_notes)
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
        body_extra.extend(_index_lines(data.get("subagents", [])))

        return self.assemble(
            head, body_extra, title, existing_fm, my_notes, data["messages_ordered"]
        )

    def _render_subagent(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        """A ``claude-subagent`` document: lineage head, parent link, conversation."""
        extra = cast(dict[str, Any], data)  # keys outside SessionData (see _sync_child)
        head = head_open(self.subagent_type, data["date"], self.id_field, data["session_id"])
        parent_id = data.get("parent_session_id")
        head.append(f"parent_session_id: {parent_id}")
        head.append(f"agent_id: {data['agent_id']}")
        for key in ("agent_type", "description"):
            value = data.get(key)
            if value:
                head.append(_quoted(key, str(value)))
        if data.get("tool_use_id"):
            head.append(f"tool_use_id: {data['tool_use_id']}")
        if data.get("spawn_depth") is not None:
            head.append(f"spawn_depth: {data['spawn_depth']}")
        if data.get("request_shape"):
            head.append(f"request_shape: {data['request_shape']}")
        if data.get("model"):
            head.append(_quoted("model", str(data["model"])))
        if data.get("workflow_run"):
            head.append(f"workflow_run: {data['workflow_run']}")
        if data.get("workflow_phase"):
            head.append(_quoted("workflow_phase", str(data["workflow_phase"])))
        if data.get("parent_agent_id"):
            head.append(f"parent_agent_id: {data['parent_agent_id']}")
        if data.get("is_fork"):
            head.append("is_fork: true")
        if data.get("stopped_by_user"):
            head.append("stopped_by_user: true")
        if extra.get("agent_cwd"):
            head.append(_quoted("agent_cwd", str(extra["agent_cwd"])))
        if data.get("repo"):
            head.append(f"repo: {data['repo']}")
        if data.get("git_branch"):
            head.append(f"git_branch: {data['git_branch']}")

        title = resolve_title(data, existing_fm)
        head.append(title_line(title))
        head.extend(head_close(data.get("last_timestamp"), data["messages"]))

        body_extra: list[str] = []
        parent_file = extra.get("parent_document")
        if parent_file:
            body_extra.extend([f"Parent: [{parent_id}](../{parent_file})", ""])
        body_extra.extend(
            render_artifacts(data.get("files_created", []), data.get("files_modified", []))
        )

        done = bool(data.get("finished") or data.get("stopped_by_user"))
        if done and existing_fm and existing_fm.get("status") == "active":
            # The child finished since the last export: "active" was the export's
            # default for a running child, not a user's choice, so it moves on.
            existing_fm = {**existing_fm, "status": "done"}
        return self.assemble(
            head,
            body_extra,
            title,
            existing_fm,
            my_notes,
            data["messages_ordered"],
            default_status="done" if done else None,
        )

    # ---- per-session sync ----
    def sync_session(
        self,
        session_id: str,
        transcript_path: str | None = None,
        quiet: bool = False,
        project: str | None = None,
    ) -> Path | None:
        """Sync one parent session (or, for an ``agent-<id>`` id, one subagent).

        A parent's revision folds in the ``## Subagents`` index it renders, so the
        parent re-renders when a child appears, is retitled, finishes or is stopped.
        The children themselves are written by :meth:`sync_children`.
        """
        if session_id.startswith("agent-"):
            return self.sync_subagent_by_id(session_id[len("agent-") :], transcript_path, quiet)

        jsonl_path = Path(transcript_path) if transcript_path else find_jsonl(session_id)
        if not jsonl_path:
            return None

        if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
            return None

        # Skip if output is already up to date (catalog lookup: one stat, no reads).
        record = self.find_record(session_id)
        existing_file = record.path if record else None
        # Link the rows before hashing when the document exists, so the revision
        # covers the exact index that renders.
        index = subagent_index(iter_subagent_files(jsonl_path), existing_file)
        revision = parent_revision(jsonl_path, index)
        if is_current(record, revision):
            return existing_file

        repo = None
        if not project:
            project, repo = _project_for_jsonl(jsonl_path)
        else:
            repo = repo_name_from_dir(jsonl_path.parent.name)

        repo_subdir = repo_subdir_from_name(project, repo) if project and repo else None

        stats = JsonlStats()
        data = extract_session_data(_records(jsonl_path, stats=stats))
        if not self.guard_parse(session_id, jsonl_path, stats, data):
            raise self.refusal(session_id)
        if not data["messages_ordered"] and not data.get("session_id"):
            return None
        if data.get("session_id") and data["session_id"] != session_id:
            raise ValueError("transcript session ID does not match requested session")
        data["session_id"] = session_id
        data["source_revision"] = revision
        data["repo"] = repo
        data["project"] = project
        data["repo_subdir"] = repo_subdir
        data["subagents"] = index

        planned = existing_file or self.output_path(session_id, data["date"], project, repo_subdir)
        # The stored revision must hash the index exactly as written (links
        # included), or the next run would see a different revision and re-render.
        _link_index(index, planned)
        data["source_revision"] = parent_revision(jsonl_path, index)
        output_file = self.export_one(session_id, cast(SessionData, data), existing_file)
        if output_file is not None and index and output_file != planned:
            # export_one moved the document off a colliding path: relink the index
            # to the folder beside the file that was actually written.
            _link_index(index, output_file)
            data["source_revision"] = parent_revision(jsonl_path, index)
            output_file = self.export_one(session_id, cast(SessionData, data), output_file)
        if output_file is None:
            return None

        if not quiet:
            print(f"Synced: {output_file}")
        return output_file

    def sync_children(
        self,
        parent_jsonl: Path,
        parent_id: str,
        parent_doc: Path,
        project: str | None,
        repo: str | None,
        repo_subdir: str | None,
        quiet: bool,
    ) -> tuple[int, list[str]]:
        """Write every stale child of a parent into the parent's session folder.

        Returns ``(exported, errors)``. A child that fails (wrong ``sessionId``,
        malformed meta, unreadable file) is reported in ``errors`` and the rest
        still export.
        """
        exported = 0
        errors: list[str] = []
        for child in iter_subagent_files(parent_jsonl):
            try:
                _path, written = self._sync_child(
                    child, parent_id, parent_doc, project, repo, repo_subdir, quiet
                )
            except (OSError, ValueError) as exc:
                errors.append(f"agent-{_agent_id_of(child)}: {exc}")
                continue
            exported += written
        return exported, errors

    def _sync_child(
        self,
        child: Path,
        parent_id: str,
        parent_doc: Path,
        project: str | None,
        repo: str | None,
        repo_subdir: str | None,
        quiet: bool,
    ) -> tuple[Path, bool]:
        """Export one child if stale: ``(document path, written)``."""
        agent_id = _agent_id_of(child)
        child_id = f"agent-{agent_id}"
        planned = subagent_path(session_dir_of(parent_doc), agent_id)
        revision = child_revision(child)
        if is_current(self.record_at(planned), revision):
            return planned, False

        meta = read_subagent_meta(child)
        stats = JsonlStats()
        data = extract_session_data(_records(child, _CHILD_SKIP_PREFIXES, stats), sidechain=True)
        if not self.guard_parse(child_id, child, stats, data, top_level=False):
            raise self.refusal(child_id)
        if data.get("parent_session_id") != parent_id:
            raise ValueError(
                f"transcript sessionId {data.get('parent_session_id')!r} "
                f"does not match parent {parent_id!r}"
            )
        if data.get("agent_id") not in (None, agent_id):
            raise ValueError(f"transcript agentId {data['agent_id']!r} does not match file")
        fields = subagent_fields(meta, agent_id, parent_id, workflow_run_of(child))
        transcript_finished = data["finished"]
        data.update(fields)
        data["finished"] = (
            meta["finished"] is True if "finished" in meta else bool(transcript_finished)
        )
        data["session_id"] = child_id
        first_prompt = data["user_messages"][0] if data["user_messages"] else ""
        data["title"] = fields["description"] or first_prompt.split("\n")[0].strip()[:80] or None

        # Lineage and location come from the parent, never from the child's own cwd
        # (subagents often run in another repo or a worktree).
        parent = self.record_at(parent_doc)
        parent_cwd = parent.cwd if parent else None
        child_cwd = data.get("cwd")
        if parent_cwd:
            data["cwd"] = parent_cwd
            if child_cwd and child_cwd != parent_cwd:
                data["agent_cwd"] = child_cwd
        data["git_branch"] = parent.metadata.get("git_branch") if parent else None
        data["repo"] = repo
        data["project"] = project
        data["repo_subdir"] = repo_subdir
        data["source_revision"] = revision
        data["parent_document"] = parent_doc.name

        output_file = self.export_one(child_id, cast(SessionData, data), planned)
        if output_file is None:
            raise ValueError(f"cannot round-trip existing document {planned}")
        if not quiet:
            print(f"Synced: {output_file}")
        return output_file, True

    def sync_family(
        self, session_id: str, transcript_path: str | None, quiet: bool
    ) -> tuple[Path | None, int, list[str]]:
        """Sync a parent, then its children: ``(parent doc, children written, errors)``."""
        parent_doc = self.sync_session(session_id, transcript_path, quiet=quiet)
        if parent_doc is None:
            return None, 0, []
        jsonl_path = Path(transcript_path) if transcript_path else find_jsonl(session_id)
        if jsonl_path is None:
            return parent_doc, 0, []
        project, repo = _project_for_jsonl(jsonl_path)
        repo_subdir = repo_subdir_from_name(project, repo) if project and repo else None
        exported, errors = self.sync_children(
            jsonl_path, session_id, parent_doc, project, repo, repo_subdir, quiet
        )
        return parent_doc, exported, errors

    def sync_subagent_by_id(
        self, agent_id: str, transcript_path: str | None, quiet: bool
    ) -> Path | None:
        """Sync one subagent: refresh its parent (and so the index), then the child."""
        if transcript_path:
            child = Path(transcript_path)
            if child.name != f"agent-{agent_id}.jsonl":
                raise ValueError(f"transcript {child} is not agent-{agent_id}.jsonl")
            parent_jsonl = subagent_parent_jsonl(child)
        else:
            found = find_subagent_jsonl(PROJECTS_BASE, agent_id)
            if found is None:
                return None
            child, parent_jsonl = found
        if not child.exists() or child.stat().st_size == 0:
            return None
        if not parent_jsonl.exists():
            raise ValueError(f"parent transcript missing for agent-{agent_id}: {parent_jsonl}")
        parent_id = parent_jsonl.stem
        parent_doc = self.sync_session(parent_id, str(parent_jsonl), quiet=True)
        if parent_doc is None:
            raise ValueError(f"parent session {parent_id} could not be exported")
        project, repo = _project_for_jsonl(parent_jsonl)
        repo_subdir = repo_subdir_from_name(project, repo) if project and repo else None
        path, _written = self._sync_child(
            child, parent_id, parent_doc, project, repo, repo_subdir, quiet
        )
        return path

    def export_target(self, session_id: str, transcript_path: str | None) -> Path:
        """Hook entry point: a parent id syncs the family, an ``agent-`` id one child.

        Child failures raise after the parent is written, so the durable hook job
        records a failure and retries instead of reporting a partial success.
        """
        if session_id.startswith("agent-"):
            result = self.sync_session(session_id, transcript_path, quiet=True)
            errors: list[str] = []
        else:
            result, _exported, errors = self.sync_family(session_id, transcript_path, quiet=True)
        if result is None:
            raise ValueError(f"Claude transcript unavailable for {session_id}")
        if errors:
            raise ValueError(
                f"exported {result} but {len(errors)} subagent(s) failed: " + "; ".join(errors)
            )
        return result

    # ---- export ----
    def extend_export_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("file", nargs="?", help="export one specific session file")

    def run_export(self, args: argparse.Namespace) -> int:
        if not args.file and not PROJECTS_BASE.is_dir():
            print(f"Error: Sessions dir not found: {PROJECTS_BASE}", file=sys.stderr)
            return 1
        if args.file:
            sessions = [Path(args.file)]
        elif args.all:
            sessions = _get_all_session_files()
            print(f"Found {len(sessions)} total sessions")
        else:
            # A background subagent can finish long after its parent went quiet.
            cutoff = args.since
            sessions = [f for f in _get_all_session_files() if newest_mtime(f) >= cutoff]
            print(f"Found {len(sessions)} sessions in the selected window")

        failed = False
        for session_file in sessions:
            session_id = session_file.stem
            if session_id.startswith("agent-") and not _in_subagent_layout(session_file):
                continue  # legacy top-level side-thread, intentionally not exported
            try:
                if session_id.startswith("agent-"):
                    child_doc = self.sync_subagent_by_id(
                        session_id[len("agent-") :], str(session_file), quiet=args.quiet
                    )
                    failed = failed or child_doc is None
                    continue
                parent_doc, _exported, errors = self.sync_family(
                    session_id, str(session_file), quiet=args.quiet
                )
            except ParserDrift:
                failed = True  # guard_parse already printed the finding
                continue
            except (OSError, ValueError) as exc:
                print(f"Error exporting {session_id}: {exc}", file=sys.stderr)
                failed = True
                continue
            if parent_doc is None:
                failed = True
            for error in errors:
                print(f"Error exporting {session_id} subagent {error}", file=sys.stderr)
                failed = True
        return int(failed)

    # ---- claude-only extras: sync (hook), resume and memory import ----
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

        from ..claude_memory import add_parser as add_memory_parser

        add_memory_parser(subparsers, common, self)

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
        try:
            if session_id.startswith("agent-"):
                self.sync_session(session_id, transcript_path, quiet=args.quiet)
                return 0
            _parent, _exported, errors = self.sync_family(session_id, transcript_path, args.quiet)
        except ParserDrift:
            return 1  # guard_parse already printed the finding
        for error in errors:
            print(f"Error exporting {session_id} subagent {error}", file=sys.stderr)
        return int(bool(errors))

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
        if fm.get("type") == self.subagent_type:
            print(
                "Error: cannot resume a subagent transcript; resume its parent "
                f"claude:{fm.get('parent_session_id')}",
                file=sys.stderr,
            )
            return 1
        session_id = fm.get("session_id")
        if not session_id:
            print(f"Error: No session_id in {target_file}", file=sys.stderr)
            return 1

        from ..harnesses import get_harness

        cmd = get_harness("claude").resume(session_id, args.fork)
        print(f"Resuming: {session_id}")
        os.execvp("claude", cmd)


def build_parser() -> argparse.ArgumentParser:
    """The ``cairn claude`` parser, built without running a command."""
    return cli.build_parser(ClaudeSource(), prog="cairn claude")


def main() -> None:
    """Console entry point - drive the Claude source through the shared CLI."""
    cli.run(ClaudeSource())


if __name__ == "__main__":
    main()
