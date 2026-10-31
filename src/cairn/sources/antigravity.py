#!/usr/bin/env python3
"""Export Google Antigravity (``agy``) conversations to markdown.

Thin adapter over :mod:`cairn`. Antigravity-specific parts only: discovery and
parse of the per-conversation JSONL transcripts agy writes under
``<store>/brain/<conversation-id>/.system_generated/logs/``, the conversation
metadata in ``<store>/conversation_summaries.db``, and the frontmatter layout.
The store is the agy app-data directory (``~/.gemini/antigravity-cli`` for the
CLI; the IDE and the Antigravity 2.0 app keep the same layout under their own
directories).

Transcript records (agy 1.4.6): one JSON object per step with ``step_index``,
``source`` (``USER_EXPLICIT`` | ``MODEL`` | ``SYSTEM``), ``type`` (``USER_INPUT`` |
``PLANNER_RESPONSE`` | ``GENERIC`` | ``SYSTEM_MESSAGE``), ``status`` (``DONE`` |
``ERROR`` | ``RUNNING``), ``created_at``, ``content``, ``thinking``, ``tool_calls``
(``[{name, args}]``), token counts, ``error`` and ``media``. A ``PLANNER_RESPONSE``
with N tool calls is followed by N ``GENERIC`` steps carrying the results in order.
``transcript_full.jsonl`` is untruncated; ``transcript.jsonl`` is the compact copy.

Usage:
    antigravity-sessions export (--all | --since DATE)
    antigravity-sessions list [--all] [--json]
    antigravity-sessions note/close/log ...   (shared)
    antigravity-sessions resume ID
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

from .. import cli
from ..config import get_config
from ..frontmatter import (
    FrontmatterParseError,
    _escape_scalar,
    head_close,
    head_open,
    read_frontmatter_head,
    resolve_title,
    title_line,
)
from ..harnesses.antigravity import conversation_transcript, transcript_conversation_id
from ..jsonl import JsonlStats, iter_jsonl
from ..paths import session_dir_of, subagent_path
from ..projects import project_from_folder_path, repo_label
from ..schema import SessionData, SubagentRef
from ..store import file_revision, is_current
from ..timeutil import today_str
from .base import ConversationSource
from .guards import DRIFT_KINDS, ERROR
from .spec_targets import SPEC_TARGETS

COLLECTION = "antigravity-sessions"
_config = get_config()
OUTPUT_DIR = _config.output_dir(COLLECTION)
STORE = _config.collection(COLLECTION).require_store()
BRAIN_DIR = STORE / "brain"
SUMMARIES_DB = STORE / "conversation_summaries.db"

RUN_STATUS_PREFIX = "CASCADE_RUN_STATUS_"
TITLE_CHARS = 80


# =============================================================================
# conversation_summaries.db
# =============================================================================


@dataclass(frozen=True)
class Summary:
    """One row of agy's conversation index (the parts the export uses)."""

    conversation_id: str
    title: str | None
    step_count: int | None
    last_modified: float | None  # POSIX seconds
    workspace_uris: tuple[str, ...]
    run_status: str | None  # ``CASCADE_RUN_STATUS_`` stripped: IDLE, RUNNING, ...
    agent_name: str | None
    parent_id: str | None
    nesting_depth: int


_SUMMARY_COLUMNS = (
    "conversation_id",
    "title",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
)


def _posix(value: object) -> float | None:
    """``2026-10-23 09:36:38.485988+00:00`` (agy's SQLite text) as POSIX seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _uris(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        items = json.loads(value)
    except ValueError:
        return ()
    return tuple(item for item in items if isinstance(item, str)) if isinstance(items, list) else ()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _run_status(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return text[len(RUN_STATUS_PREFIX) :] if text.startswith(RUN_STATUS_PREFIX) else text


def read_summaries(path: Path = SUMMARIES_DB) -> dict[str, Summary]:
    """Every conversation agy indexes, by id. A missing or unreadable index is a
    warning, not a failure: the transcripts alone still export."""
    if not path.is_file():
        return {}
    query = f"select {', '.join(_SUMMARY_COLUMNS)} from conversation_summaries"
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(query).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        print(f"Warning: cannot read {path}: {exc}", file=sys.stderr)
        return {}
    summaries: dict[str, Summary] = {}
    for row in rows:
        values = dict(zip(_SUMMARY_COLUMNS, row, strict=True))
        conversation_id = _text(values["conversation_id"])
        if conversation_id is None:
            continue
        summaries[conversation_id] = Summary(
            conversation_id=conversation_id,
            title=_text(values["title"]),
            step_count=values["step_count"] if isinstance(values["step_count"], int) else None,
            last_modified=_posix(values["last_modified_time"]),
            workspace_uris=_uris(values["workspace_uris"]),
            run_status=_run_status(values["status"]),
            agent_name=_text(values["agent_name"]),
            parent_id=_text(values["parent_conversation_id"]),
            nesting_depth=values["nesting_depth"]
            if isinstance(values["nesting_depth"], int)
            else 0,
        )
    return summaries


# =============================================================================
# Transcript parsing
# =============================================================================

_WRAPPER_BLOCKS = re.compile(
    r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE_PREAMBLE)>.*?</\1>\s*", re.S
)
_RESULT_PREAMBLE = re.compile(r"\A(?:(?:Created|Completed) At: [^\n]*\n)+")


def _unwrap(tag: str, content: str) -> str | None:
    """The body of the last ``<TAG>…</TAG>`` block in ``content`` (agy's preambles
    mention the tag name before the block itself), or ``None`` without one."""
    end = content.rfind(f"</{tag}>")
    if end < 0:
        return None
    start = content.rfind(f"<{tag}>", 0, end)
    if start < 0:
        return None
    return content[start + len(tag) + 2 : end].strip("\n").strip()


def user_text(content: object) -> str:
    """The prompt the user typed: the ``<USER_REQUEST>`` body, without the metadata
    blocks agy appends (local time, settings changes)."""
    if not isinstance(content, str):
        return ""
    inner = _unwrap("USER_REQUEST", content)
    if inner is not None:
        return inner
    return _WRAPPER_BLOCKS.sub("", content).strip()


def system_text(content: object) -> str:
    """The body of a host-injected message, without agy's ``<SYSTEM_MESSAGE>`` wrapper
    and the preamble that says it was not sent by the user."""
    if not isinstance(content, str):
        return ""
    inner = _unwrap("SYSTEM_MESSAGE", content)
    return inner if inner is not None else content.strip()


def result_text(content: object) -> str:
    """A tool result without agy's ``Created At`` / ``Completed At`` preamble."""
    if not isinstance(content, str):
        return ""
    return _RESULT_PREAMBLE.sub("", content).rstrip()


def _media_lines(media: object) -> list[str]:
    if not isinstance(media, list):
        return []
    lines = []
    for item in media:
        if isinstance(item, dict) and item.get("uri"):
            lines.append(f"[media {item.get('mime_type') or 'file'}]({item['uri']})")
    return lines


def _call_status(status: object) -> str:
    return {"ERROR": "error", "RUNNING": "running"}.get(str(status or ""), "done")


def parse_transcript(path: Path, conversation_id: str, stats: JsonlStats | None = None) -> dict:
    """Parse one transcript JSONL into the normalised session dict (no metadata yet).

    ``stats`` (optional) receives the line counts for the parser-drift guard.
    """
    data: dict[str, Any] = {
        "session_id": conversation_id,
        "cwd": None,
        "date": None,
        "first_timestamp": None,
        "last_timestamp": None,
        "user_messages": [],
        "messages": [],
        "steps": 0,
        "errors": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
        "finished": True,
    }
    messages: list[dict] = data["messages"]
    pending: list[tuple[int, int]] = []  # (message index, tool index) awaiting a result
    for _, record in iter_jsonl(path, stats=stats):
        if not isinstance(record, dict):
            continue
        data["steps"] += 1
        rtype = str(record.get("type") or "")
        status = str(record.get("status") or "")
        ts = record.get("created_at") if isinstance(record.get("created_at"), str) else None
        if ts:
            data["first_timestamp"] = data["first_timestamp"] or ts
            data["last_timestamp"] = ts
        if status == "RUNNING":
            data["finished"] = False
        if status == "ERROR":
            data["errors"] += 1
        for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                data["usage"][key] += value
        content = record.get("content")
        error = record.get("error") if isinstance(record.get("error"), str) else None

        if rtype == "USER_INPUT":
            text = user_text(content)
            data["user_messages"].append(text)
            messages.append({"role": "user", "timestamp": ts, "text": text, "tool_calls": []})
            continue

        if rtype == "PLANNER_RESPONSE":
            text = content.strip() if isinstance(content, str) else ""
            if error and status == "ERROR":
                text = f"{text}\n\nError: {error}".strip()
            text = "\n".join([text, *_media_lines(record.get("media"))]).strip()
            message: dict[str, Any] = {
                "role": "assistant",
                "timestamp": ts,
                "text": text,
                "tool_calls": [],
            }
            thinking = record.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                message["thinking"] = thinking.strip()
            calls = record.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                arguments = call.get("args")
                message["tool_calls"].append(
                    {
                        "name": str(call.get("name") or "tool"),
                        "arguments": (
                            json.dumps(arguments, ensure_ascii=False)
                            if arguments is not None
                            else None
                        ),
                        "status": None,
                        "result": None,
                    }
                )
                pending.append((len(messages), len(message["tool_calls"]) - 1))
            messages.append(message)
            continue

        if rtype == "SYSTEM_MESSAGE":
            text = system_text(content)
            if text:
                messages.append({"role": "system", "timestamp": ts, "text": text, "tool_calls": []})
            continue

        # GENERIC (a tool result) and anything newer agy adds: pair it with the
        # oldest unresolved call, else keep it as a standalone tool turn.
        result = result_text(content)
        if error and status == "ERROR" and error not in result:
            result = f"{result}\n{error}".strip()
        result = "\n".join([result, *_media_lines(record.get("media"))]).strip()
        if pending:
            index, position = pending.pop(0)
            call = messages[index]["tool_calls"][position]
            call["result"] = result or None
            call["status"] = _call_status(status)
            continue
        if result:
            messages.append(
                {
                    "role": "tool",
                    "timestamp": ts,
                    "text": "",
                    "tool_calls": [
                        {
                            "name": rtype.lower() or "tool",
                            "arguments": None,
                            "status": _call_status(status),
                            "result": result,
                        }
                    ],
                }
            )
    for index, position in pending:
        messages[index]["tool_calls"][position]["status"] = "pending"
    return data


def _workspace_path(uris: tuple[str, ...]) -> str | None:
    for uri in uris:
        if uri.startswith("file://"):
            return unquote(uri[len("file://") :])
        if uri.startswith("/"):
            return uri
    return None


def finish(data: dict, summary: Summary | None) -> dict:
    """Derived fields: date, cwd/project/repo, title, status and lineage."""
    if not data["date"]:
        if data["first_timestamp"]:
            data["date"] = data["first_timestamp"].split("T")[0]
        elif summary and summary.last_modified:
            data["date"] = (
                datetime.fromtimestamp(summary.last_modified).astimezone().date().isoformat()
            )
        else:
            data["date"] = today_str()
    data["cwd"] = _workspace_path(summary.workspace_uris) if summary else None
    project, repo_subdir = (None, None)
    if data["cwd"]:
        project, repo_subdir = project_from_folder_path(data["cwd"])
    data["project"] = project
    data["repo_subdir"] = repo_subdir
    data["repo"] = repo_label(project, repo_subdir)
    data["message_count"] = len(data["user_messages"])
    data["run_status"] = summary.run_status if summary else None
    data["agent_name"] = summary.agent_name if summary else None
    data["parent_session_id"] = summary.parent_id if summary else None
    data["spawn_depth"] = summary.nesting_depth if summary and summary.parent_id else None
    title = summary.title if summary else None
    data["title"] = title or prompt_title(data) or "Untitled Session"
    return data


def prompt_title(data: Mapping[str, Any]) -> str | None:
    """The fallback title: the first prompt, flattened and capped."""
    if not data.get("user_messages"):
        return None
    return data["user_messages"][0].replace("\n", " ").strip()[:TITLE_CHARS] or None


def _revision(transcript: Path, summary: Summary | None) -> str:
    """The native snapshot identity: the transcript file plus the index row (a title
    or status change after the last step must re-render the document)."""
    file_rev = file_revision(transcript)
    if summary is None:
        return file_rev
    row = (summary.title, summary.last_modified, summary.run_status, summary.parent_id)
    return hashlib.sha256(f"{file_rev}|{row!r}".encode()).hexdigest()


# =============================================================================
# Rendering helpers
# =============================================================================

_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9._-]+")


def _scalar_line(key: str, value: object) -> str:
    """``key: value``, double-quoted unless the value is a plain token."""
    text = str(value)
    if _PLAIN_SCALAR.fullmatch(text):
        return f"{key}: {text}"
    return f'{key}: "{_escape_scalar(text.replace(chr(10), " "))}"'


def artifact_lines(conversation_id: str, brain_dir: Path = BRAIN_DIR) -> list[str]:
    """``## Artifacts``: the user-facing documents agy wrote for this conversation
    (``brain/<id>/*.md`` with their ``.metadata.json`` summary)."""
    folder = brain_dir / conversation_id
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.md")):
        summary = ""
        meta = path.with_name(path.name + ".metadata.json")
        if meta.is_file():
            try:
                summary = str(json.loads(meta.read_text(encoding="utf-8")).get("summary") or "")
            except (OSError, ValueError, AttributeError):
                summary = ""
        label = path.name.replace("[", "\\[").replace("]", "\\]")
        rows.append(
            f"- [{label}]({path.as_uri()})" + (f" - {' '.join(summary.split())}" if summary else "")
        )
    return ["## Artifacts", "", *rows, ""] if rows else []


def _subagent_ref(child_id: str, child: dict, child_doc: Path, parent_doc: Path) -> SubagentRef:
    rel = Path(os.path.relpath(child_doc, parent_doc.parent)).as_posix()
    return {
        "agent_id": child_id,
        "title": child.get("title") or "Untitled Session",
        "agent_type": child.get("agent_name"),
        "finished": bool(child.get("finished")),
        "path": rel,
    }


def _parent_revision(file_rev: str, refs: list[SubagentRef]) -> str:
    rows = sorted(
        (
            ref["agent_id"],
            ref.get("title"),
            ref.get("agent_type"),
            ref.get("finished"),
            ref.get("path"),
        )
        for ref in refs
    )
    return hashlib.sha256(f"{file_rev}|{rows!r}".encode()).hexdigest()


def _subagent_index(refs: list[SubagentRef]) -> list[str]:
    if not refs:
        return []
    lines = ["## Subagents", ""]
    for ref in refs:
        title = ref.get("title", "").replace("[", "\\[").replace("]", "\\]")
        rel = ref.get("path", "")
        link = rel if rel.startswith("../") else f"./{rel}"
        parts = [f"`{ref['agent_id']}`"]
        if ref.get("agent_type"):
            parts.append(str(ref["agent_type"]))
        parts.append("finished" if ref.get("finished") else "running")
        lines.append(f"- [{title}]({link}) - {', '.join(parts)}")
    lines.append("")
    return lines


# =============================================================================
# Source adapter
# =============================================================================


class AntigravitySource(ConversationSource):
    name = "antigravity-sessions"
    version = "2.0.0"
    spec_target = SPEC_TARGETS["antigravity"]
    output_dir = OUTPUT_DIR
    session_type = "antigravity-session"
    subagent_type = "antigravity-subagent"
    collection = COLLECTION
    list_default = "not-done"
    id_field = "session_id"
    preserved_note = "exports"

    def list_mid_col(self, fm: dict) -> str:
        status = fm.get("run_status", "")
        return f"{(f' [{status}]' if status else ''):>12}"

    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        child = bool(data.get("parent_session_id"))
        head = head_open(
            self.subagent_type if child else self.session_type,
            data["date"],
            self.id_field,
            data["session_id"],
        )
        if data.get("repo"):
            head.append(f"repo: {data['repo']}")
        if data.get("run_status"):
            head.append(_scalar_line("run_status", data["run_status"]))
        if data.get("agent_name"):
            head.append(_scalar_line("agent", data["agent_name"]))
        usage = data.get("usage") or {}
        if any(usage.values()):
            head.append(
                f'tokens: "in={usage.get("input_tokens", 0)} out={usage.get("output_tokens", 0)} '
                f'cached={usage.get("cache_read_tokens", 0)}"'
            )
        if data.get("steps"):
            head.append(f"steps: {data['steps']}")
        if data.get("errors"):
            head.append(f"step_errors: {data['errors']}")
        if child:
            head.append(_scalar_line("parent_session_id", data["parent_session_id"]))
            if data.get("spawn_depth") is not None:
                head.append(f"spawn_depth: {data['spawn_depth']}")
        title = resolve_title(data, existing_fm)
        # agy names a conversation asynchronously, often after the first export. A
        # document still titled by its first prompt takes agy's title; any other
        # existing title is a user's edit and stays.
        if existing_fm and existing_fm.get("title") == prompt_title(data) and data.get("title"):
            title = data["title"]
        head.append(title_line(title))
        head.extend(head_close(data.get("last_timestamp"), data["message_count"]))

        body_extra = _subagent_index(data.get("subagents") or [])
        body_extra += artifact_lines(data["session_id"])
        default_status = "done" if child and data.get("finished") else None
        return self.assemble(
            head, body_extra, title, existing_fm, my_notes, data["messages"], default_status
        )

    # ---- discovery ----
    @staticmethod
    def conversations() -> dict[str, Path]:
        """``conversation_id -> transcript`` for every brain dir that has one."""
        if not BRAIN_DIR.is_dir():
            return {}
        found = {}
        for folder in sorted(BRAIN_DIR.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            transcript = conversation_transcript(STORE, folder.name)
            if transcript is not None:
                found[folder.name] = transcript
        return found

    def _parse(
        self,
        transcripts: dict[str, Path],
        summaries: dict[str, Summary],
        since: float | None = None,
    ) -> tuple[dict[str, dict], list[str]]:
        """Parse each transcript once, stamping its native revision. A transcript
        that cannot be read, or that the parser-drift guard refuses, is reported
        and listed in ``failed``. With ``since`` (the ``--since`` cutoff), a refused
        conversation whose activity time is before it is held quietly: its finding
        is stored, not printed, and it does not fail the run."""
        parsed: dict[str, dict] = {}
        failed: list[str] = []
        for conversation_id, transcript in transcripts.items():
            summary = summaries.get(conversation_id)
            stats = JsonlStats()
            try:
                revision = _revision(transcript, summary)
                data = finish(parse_transcript(transcript, conversation_id, stats), summary)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                print(f"Error parsing {transcript}: {exc}", file=sys.stderr)
                failed.append(conversation_id)
                continue
            top_level = not data.get("parent_session_id")
            activity = (
                summary.last_modified if summary and summary.last_modified else None
            ) or transcript.stat().st_mtime
            loud = since is None or activity >= since
            finding = self.classify_parse(
                conversation_id, transcript, stats, data, top_level=top_level, loud=loud
            )
            if finding is None:
                self.findings.clear(f"{self.source_name}:{conversation_id}", DRIFT_KINDS)
            elif finding.severity == ERROR:
                if loud:
                    failed.append(conversation_id)
                continue
            data["source_revision"] = revision
            data["_mtime"] = activity
            parsed[conversation_id] = data
        return parsed, failed

    @staticmethod
    def _depth(conversation_id: str, parsed: dict[str, dict]) -> int:
        seen = {conversation_id}
        depth = 0
        parent = parsed[conversation_id].get("parent_session_id")
        while parent and parent in parsed and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = parsed[parent].get("parent_session_id")
        return depth

    def _safe_target(self, session_id: str, planned: Path, claimed: dict[Path, str]) -> Path:
        holder = claimed.get(planned)
        if holder is None and planned.exists():
            try:
                holder = read_frontmatter_head(planned).get(self.id_field)
            except FrontmatterParseError:
                holder = session_id  # export_one refuses it and records the finding
        if holder is not None and holder != session_id:
            digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
            planned = planned.with_stem(planned.stem + "-" + digest)
        return planned

    def _plan_targets(self, parsed: dict[str, dict], order: list[str]) -> dict[str, Path]:
        """Document path per conversation: an existing record's path, else the child
        folder of the parent's document, else the canonical path."""
        targets: dict[str, Path] = {}
        claimed: dict[Path, str] = {}
        for sid in order:
            data = parsed[sid]
            record = self.find_record(sid)
            if record is not None:
                targets[sid] = record.path
                claimed[record.path] = sid
                continue
            parent = data.get("parent_session_id")
            parent_doc = targets.get(parent) if parent else None
            if parent and parent_doc is None:
                parent_record = self.find_record(parent)
                parent_doc = parent_record.path if parent_record else None
            if parent_doc is not None:
                planned = subagent_path(session_dir_of(parent_doc), self.short_id(sid))
            else:
                planned = self.output_path(
                    sid, data["date"], data.get("project"), data.get("repo_subdir")
                )
            targets[sid] = self._safe_target(sid, planned, claimed)
            claimed[targets[sid]] = sid
        return targets

    def export_conversations(
        self,
        date_filter: float | None = None,
        quiet: bool = False,
        parsed: dict[str, dict] | None = None,
    ) -> int:
        if parsed is None:
            parsed, _failed = self._parse(self.conversations(), read_summaries())
        children: dict[str, list[str]] = {}
        for sid, data in parsed.items():
            parent = data.get("parent_session_id")
            if parent and parent != sid:
                children.setdefault(parent, []).append(sid)
        # Parents before children, so a child lands in its parent's folder.
        order = sorted(parsed, key=lambda sid: (self._depth(sid, parsed), sid))
        targets = self._plan_targets(parsed, order)

        exported = 0
        for sid in order:
            data = parsed[sid]
            kids = sorted(children.get(sid, []))
            newest = max([data["_mtime"]] + [parsed[kid]["_mtime"] for kid in kids])
            if date_filter and newest < date_filter:
                continue
            if kids:
                data = dict(data)
                refs = [_subagent_ref(kid, parsed[kid], targets[kid], targets[sid]) for kid in kids]
                data["subagents"] = refs
                data["source_revision"] = _parent_revision(data["source_revision"], refs)
            if is_current(self.find_record(sid), data["source_revision"]):
                continue
            payload = {key: value for key, value in data.items() if key != "_mtime"}
            output_file = self.export_one(sid, cast(SessionData, payload), targets[sid])
            if output_file is None:
                continue
            exported += 1
            if not quiet:
                print(f"  Exported: {output_file}")
        if exported == 0 and not quiet:
            print("  All conversations up to date, nothing to export")
        return exported

    def _family(self, conversation_id: str, summaries: dict[str, Summary]) -> set[str]:
        """The root ancestor of ``conversation_id`` and every descendant of that root,
        so every ``## Subagents`` index this export renders is complete."""
        root = conversation_id
        seen = {root}
        while (
            (summary := summaries.get(root)) and summary.parent_id and summary.parent_id not in seen
        ):
            root = summary.parent_id
            seen.add(root)
        family = {root}
        queue = [root]
        while queue:
            current = queue.pop()
            for sid, summary in summaries.items():
                if summary.parent_id == current and sid not in family:
                    family.add(sid)
                    queue.append(sid)
        return family

    def export_target(self, session_id: str, transcript_path: str | None) -> Path:
        transcript = conversation_transcript(STORE, session_id)
        given = Path(transcript_path) if transcript_path else None
        if given is not None:
            if transcript_conversation_id(given) != session_id:
                raise ValueError("transcript path does not belong to the requested conversation")
            if not given.is_file():
                raise ValueError(f"Antigravity transcript unavailable: {given}")
            # The hook names the compact copy; the untruncated sibling is the export input.
            full = given.with_name("transcript_full.jsonl")
            given = full if full.is_file() else given
            if transcript is None or given.resolve() != transcript.resolve():
                transcript = given
        if transcript is None:
            raise ValueError(f"Antigravity transcript unavailable for {session_id}")
        summaries = read_summaries()
        transcripts = {
            sid: path
            for sid, path in self.conversations().items()
            if sid in self._family(session_id, summaries)
        }
        transcripts[session_id] = transcript
        parsed, failed = self._parse(transcripts, summaries)
        if session_id not in parsed:
            raise self.drift_error({session_id}) or ValueError(
                f"No readable Antigravity transcript for {session_id}"
            )
        self.export_conversations(quiet=True, parsed=parsed)
        drift = self.drift_error()
        if drift is not None:
            raise drift
        if failed:
            raise ValueError(
                f"Antigravity export for {session_id} incomplete; unreadable: {', '.join(failed)}"
            )
        result = self.find_file(session_id)
        if result is None:
            raise ValueError(f"Antigravity export failed for {session_id}")
        return result

    def add_extra_parsers(
        self, subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser
    ) -> None:
        from ..resume import add_source_parser

        add_source_parser(subparsers, common, "antigravity")

    def run_export(self, args: argparse.Namespace) -> int:
        if not STORE.is_dir():
            print(f"Error: Antigravity app-data dir not found: {STORE}", file=sys.stderr)
            return 1
        if not BRAIN_DIR.is_dir():
            # agy creates brain/ with the first conversation; until then there is nothing.
            print(f"No conversations yet ({BRAIN_DIR} does not exist)")
            return 0
        date_filter = None if args.all else args.since
        print(
            "Exporting all conversations..."
            if args.all
            else "Exporting conversations in the selected window..."
        )
        parsed, failed = self._parse(self.conversations(), read_summaries(), date_filter)
        exported = self.export_conversations(date_filter, quiet=args.quiet, parsed=parsed)
        print(f"\nExported {exported} conversations to {OUTPUT_DIR}")
        if failed:
            print(f"Error: {len(failed)} transcript(s) failed to parse", file=sys.stderr)
            return 1
        return 0


def main() -> None:
    """Console entry point - drive the Antigravity source through the shared CLI."""
    cli.run(AntigravitySource())


if __name__ == "__main__":
    main()
