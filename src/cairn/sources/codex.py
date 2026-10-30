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
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, cast

from .. import cli
from ..config import get_config
from ..frontmatter import (
    _escape_scalar,
    head_close,
    head_open,
    read_frontmatter_head,
    resolve_title,
    title_line,
)
from ..jsonl import iter_jsonl
from ..paths import session_dir_of, subagent_path
from ..projects import project_from_folder_path, repo_label
from ..schema import SessionData, SubagentRef
from ..store import file_revision, is_current
from ..timeutil import today_str
from .base import ConversationSource
from .codex_lineage import Lineage, rollout_lineage, spawn_fields
from .codex_replay import LIVE_CHILD_WINDOW_S, REPLAY_HEURISTIC_VERSION, ReplayCut
from .spec_targets import SPEC_TARGETS

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


def _tool_output_text(output: object, items_as_text: bool) -> str:
    """Normalise a tool result: ``{"output": ...}``, ``input_text`` lists, or JSON.

    ``items_as_text`` joins a list of text items (``custom_tool_call_output``); a
    ``function_call_output`` list keeps its JSON rendering.
    """
    if isinstance(output, dict):
        output = output.get("output", "")
    if items_as_text and isinstance(output, list):
        text = _content_text(output)
        if text or not output:
            return text
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output)
    except (TypeError, ValueError):
        return str(output)


def _append_tool_call(
    messages: list[dict],
    pending: dict[str, int],
    call_id: object,
    name: str,
    arguments: object,
    timestamp: str | None,
) -> None:
    """One assistant turn carrying a tool call; its result arrives later by ``call_id``."""
    tool_call = {"name": name, "arguments": arguments, "status": None, "result": None}
    messages.append(
        {"role": "assistant", "timestamp": timestamp, "text": "", "tool_calls": [tool_call]}
    )
    if isinstance(call_id, str) and call_id:
        pending[call_id] = len(messages) - 1


def _resolve_tool_output(
    messages: list[dict],
    pending: dict[str, int],
    call_id: object,
    output: str,
    timestamp: str | None,
) -> None:
    """Attach a result to its call, or keep it as a standalone tool turn."""
    idx = pending.pop(call_id, None) if isinstance(call_id, str) and call_id else None
    if idx is not None:
        messages[idx]["tool_calls"][0]["result"] = output
        return
    messages.append(
        {
            "role": "tool",
            "timestamp": timestamp,
            "text": "",
            "tool_calls": [{"name": "tool", "arguments": None, "status": None, "result": output}],
        }
    )


def _agent_message_text(payload: dict) -> str:
    """``from <author> to <recipient>`` plus the plaintext header; the body is encrypted."""
    content = payload.get("content")
    items = content if isinstance(content, list) else []
    header = _content_text(items)
    encrypted = any(
        isinstance(item, dict)
        and (item.get("type") == "encrypted_content" or item.get("encrypted_content"))
        for item in items
    )
    author = payload.get("author") or "unknown"
    recipient = payload.get("recipient") or "unknown"
    body = "\n".join(part for part in (header, "[encrypted]" if encrypted else "") if part)
    return f"from {author} to {recipient}" + (f"\n\n{body}" if body else "")


def _sandbox_label(policy: object) -> str | None:
    if isinstance(policy, dict):
        policy = policy.get("type")
    return policy if isinstance(policy, str) and policy else None


def _subagent_title(data: dict) -> str | None:
    """``<agent_type> <agent_nickname>: <agent_path>`` from the parts that exist."""
    who = " ".join(part for part in (data.get("agent_type"), data.get("agent_nickname")) if part)
    where = data.get("agent_path")
    if who and where:
        return f"{who}: {where}"
    return who or where or None


def _scan_rollout(file_path: Path, honour_replay: bool) -> tuple[dict | None, bool]:
    """One pass over a rollout. Returns ``(data, still_replaying_at_eof)``.

    The first ``session_meta`` names the thread. A second one (a forked child) starts
    the replayed parent history, which :class:`ReplayCut` skips when ``honour_replay``;
    otherwise later metas are ignored and every record counts.
    """
    data: dict[str, Any] = {
        "session_id": None,
        "cwd": None,
        "date": None,
        "model": None,
        "cli_version": None,
        "branch": None,
        "commit": None,
        "originator": None,
        "thread_source": None,
        "forked_from_id": None,
        "parent_session_id": None,
        "spawn_depth": None,
        "agent_type": None,
        "agent_nickname": None,
        "agent_path": None,
        "effort": None,
        "sandbox": None,
        "approval_policy": None,
        "finished": False,
        "first_timestamp": None,
        "last_timestamp": None,
        "user_messages": [],
        "messages": [],
    }

    pending_calls: dict[str, int] = {}  # call_id -> index of the assistant message carrying it
    turn_id = ""
    user_occurrences: dict[tuple[str, str], dict[str, int]] = {}
    cut: ReplayCut | None = None

    def user_message(text: str, timestamp: str | None, origin: str) -> None:
        # Older clients mirror a prompt in event_msg and response_item. Keep the
        # larger occurrence count, not both copies; repeated prompts still count.
        counts = user_occurrences.setdefault((turn_id, text), {"event": 0, "response": 0})
        counts[origin] += 1
        other = "response" if origin == "event" else "event"
        if counts[origin] <= counts[other]:
            return
        data["user_messages"].append(text)
        data["messages"].append(
            {"role": "user", "timestamp": timestamp, "text": text, "tool_calls": []}
        )

    for _, record in iter_jsonl(file_path):
        rtype = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        ts = record.get("timestamp") or payload.get("timestamp")

        if rtype == "session_meta" and data["session_id"]:
            # A later meta is replayed history (or a duplicate); the first meta won.
            if honour_replay and payload.get("id") != data["session_id"]:
                cut = cut.with_meta() if cut else ReplayCut.start(data["session_id"])
            continue
        if cut is not None and not cut.ended:
            cut = cut.observe(rtype, payload)
            if not cut.ended:
                continue

        if ts:
            if not data["first_timestamp"]:
                data["first_timestamp"] = ts
            data["last_timestamp"] = ts

        if rtype == "session_meta":
            data["session_id"] = payload.get("id")
            data["cwd"] = payload.get("cwd")
            data["cli_version"] = payload.get("cli_version")
            for key in ("originator", "thread_source", "forked_from_id"):
                value = payload.get(key)
                data[key] = value if isinstance(value, str) and value else None
            data.update(spawn_fields(payload))
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
            turn_id = payload.get("turn_id") or turn_id
            if payload.get("model"):
                data["model"] = payload["model"]
            effort = payload.get("effort")
            if isinstance(effort, str) and effort:
                data["effort"] = effort
            sandbox = _sandbox_label(payload.get("sandbox_policy"))
            if sandbox:
                data["sandbox"] = sandbox
            approval = payload.get("approval_policy")
            if isinstance(approval, str) and approval:
                data["approval_policy"] = approval
            continue

        if rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "task_started":
                turn_id = payload.get("turn_id") or turn_id
                data["finished"] = False
            if ptype in ("token_count", "rate_limits", "task_started", "task_complete"):
                continue
            if ptype == "user_message":
                message = payload.get("message", "")
                if isinstance(message, str) and message.strip():
                    user_message(message.strip(), ts, "event")
            continue

        if rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message":
                if payload.get("role") == "user":
                    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                    kinds = metadata.get("content_item_kinds")
                    content = payload.get("content")
                    if kinds is not None:
                        # New clients serialize harness context as user messages
                        # too, but label actual user input explicitly.
                        if not isinstance(content, list) or len(content) != len(kinds):
                            continue
                        content = [
                            c for c, k in zip(content, kinds, strict=True) if k == "user.text"
                        ]
                    text = _content_text(content)
                    if text and not text.startswith(
                        ("<environment_context>", "# AGENTS.md instructions for ", "<skill>")
                    ):
                        turn_id = metadata.get("turn_id") or turn_id
                        user_message(text, ts, "response")
                if payload.get("role") == "assistant":
                    if payload.get("phase") == "final_answer":
                        data["finished"] = True
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
            if ptype in ("function_call", "custom_tool_call"):
                if ptype == "custom_tool_call":
                    arguments = payload.get("input")
                else:
                    arguments = payload.get("arguments")
                    if arguments is None:
                        arguments = payload.get("rawArgs")
                _append_tool_call(
                    data["messages"],
                    pending_calls,
                    payload.get("call_id"),
                    payload.get("name", "tool"),
                    arguments,
                    ts,
                )
                continue
            if ptype in ("function_call_output", "custom_tool_call_output"):
                text_output = _tool_output_text(
                    payload.get("output", ""), items_as_text=ptype == "custom_tool_call_output"
                )
                _resolve_tool_output(
                    data["messages"], pending_calls, payload.get("call_id"), text_output, ts
                )
                continue
            if ptype == "agent_message":
                data["messages"].append(
                    {
                        "role": "agent",
                        "timestamp": ts,
                        "text": _agent_message_text(payload),
                        "tool_calls": [],
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
        return None, False
    return data, cut is not None and not cut.ended


def _finish_rollout(data: dict) -> dict:
    """Derived fields shared by every parse outcome: date, project, counts, title."""
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
    elif data.get("parent_session_id"):
        title = _subagent_title(data)
    data["title"] = title or "Untitled Session"
    return data


def parse_rollout(file_path: Path) -> dict | None:
    """Parse a Codex rollout JSONL into structured session data (None if unusable).

    A forked child whose replayed parent history has no recognisable end is a live
    child when the file changed within :data:`LIVE_CHILD_WINDOW_S` (``None``: skipped
    this run, no error). An older one exports the whole file with
    ``replay_unresolved: True`` and a warning. Replay problems never raise.
    """
    data, replaying = _scan_rollout(file_path, honour_replay=True)
    if data is None:
        return None
    if replaying:
        try:
            age = time.time() - file_path.stat().st_mtime
        except OSError:
            return None
        if age < LIVE_CHILD_WINDOW_S:
            return None
        print(
            f"Warning: {file_path}: replayed parent history has no recognisable end "
            f"({REPLAY_HEURISTIC_VERSION} rules); exporting it whole with replay_unresolved",
            file=sys.stderr,
        )
        data, _ = _scan_rollout(file_path, honour_replay=False)
        if data is None:
            return None
        data["replay_unresolved"] = True
    return _finish_rollout(data)


def _get_all_rollout_files() -> list[Path]:
    """All non-empty rollout JSONL files under ~/.codex/sessions/."""
    if not SESSIONS_DIR.is_dir():
        return []
    return [f for f in SESSIONS_DIR.rglob("rollout-*.jsonl") if f.stat().st_size > 0]


# Head keys from session_meta / turn_context, then the lineage keys of a child.
_CONTEXT_KEYS = (
    "originator",
    "thread_source",
    "effort",
    "sandbox",
    "approval_policy",
    "forked_from_id",
)
_CHILD_KEYS = ("parent_session_id", "spawn_depth", "agent_type", "agent_nickname", "agent_path")
_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9._-]+")


def _scalar_line(key: str, value: object) -> str:
    """``key: value``, double-quoted unless the value is a plain token."""
    text = str(value)
    if _PLAIN_SCALAR.fullmatch(text) and key not in ("agent_nickname", "agent_path"):
        return f"{key}: {text}"
    return f'{key}: "{_escape_scalar(text.replace(chr(10), " "))}"'


def _depth(session_id: str, best: dict[str, tuple[float, dict]]) -> int:
    """Ancestors of ``session_id`` present in this run (cycle-safe)."""
    seen = {session_id}
    depth = 0
    parent = best[session_id][1].get("parent_session_id")
    while parent and parent in best and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = best[parent][1].get("parent_session_id")
    return depth


def _subagent_ref(
    child_id: str, child: dict, targets: dict[str, Path], parent_doc: Path
) -> SubagentRef:
    rel = Path(os.path.relpath(targets[child_id], parent_doc.parent)).as_posix()
    return {
        "agent_id": child_id,
        "title": child.get("title") or "Untitled Session",
        "agent_type": child.get("agent_type"),
        "finished": bool(child.get("finished")),
        "path": rel,
    }


def _parent_revision(file_rev: str, refs: list[SubagentRef]) -> str:
    """The parent's revision folds in the index it renders (link paths included), so a
    new, retitled, finished or relocated child re-renders an unchanged parent rollout."""
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


def _family(lineage: Lineage, session_id: str) -> list[str]:
    """Thread ids of ``session_id``'s whole family: its root ancestor and every
    transitive descendant of that root (cycle-safe)."""
    root = session_id
    seen = {root}
    while (parent := lineage.parent_of(root)) and parent not in seen:
        seen.add(parent)
        root = parent
    family = [root]
    queue = [root]
    visited = {root}
    while queue:
        for child in lineage.children_of(queue.pop()):
            meta = lineage.meta_of(child)
            if meta is not None and meta.id not in visited:
                visited.add(meta.id)
                family.append(meta.id)
                queue.append(meta.id)
    return family


def _subagent_index(refs: list[SubagentRef]) -> list[str]:
    """``## Subagents`` lines for a parent document (empty without children)."""
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


class CodexSource(ConversationSource):
    name = "codex-sessions"
    version = "2.0.0"
    spec_target = SPEC_TARGETS["codex"]
    output_dir = OUTPUT_DIR
    session_type = "codex-session"
    subagent_type = "codex-subagent"
    collection = "codex-sessions"
    list_default = "not-done"
    id_field = "session_id"
    session_env_var = "CODEX_THREAD_ID"
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
        child = bool(data.get("parent_session_id"))
        head = head_open(
            self.subagent_type if child else self.session_type,
            data["date"],
            self.id_field,
            data["session_id"],
        )
        if data.get("repo"):
            head.append(f"repo: {data['repo']}")
        if data.get("branch"):
            head.append(f"branch: {data['branch']}")
        if data.get("commit"):
            head.append(f"commit: {data['commit']}")
        if data.get("model"):
            head.append(f"model: {data['model']}")
        keys = _CONTEXT_KEYS + (_CHILD_KEYS if child else ())
        head.extend(_scalar_line(key, data.get(key)) for key in keys if data.get(key))
        if data.get("replay_unresolved"):
            head.append("replay_unresolved: true")

        title = resolve_title(data, existing_fm)
        head.append(title_line(title))
        head.extend(head_close(data.get("last_timestamp"), data["message_count"]))

        body_extra = _subagent_index(data.get("subagents") or [])
        default_status = "done" if child and data.get("finished") else None
        return self.assemble(
            head, body_extra, title, existing_fm, my_notes, data["messages"], default_status
        )

    # ---- export driver ----
    @staticmethod
    def _parse_rollouts(rollouts: list[Path]) -> tuple[list[tuple[Path, dict]], list[Path]]:
        """Parse each rollout exactly once, stamping its native revision.

        Returns ``(parsed, failed)``: a file that cannot be decoded is reported on
        stderr and listed in ``failed`` so the others still export.
        """
        parsed = []
        failed = []
        for rollout in rollouts:
            try:
                revision = file_revision(rollout)
                data = parse_rollout(rollout)
            except (OSError, ValueError) as exc:
                print(f"Error parsing {rollout}: {exc}", file=sys.stderr)
                failed.append(rollout)
                continue
            if data:
                data["source_revision"] = revision
                parsed.append((rollout, data))
        return parsed, failed

    @staticmethod
    def _best_fragments(parsed: list[tuple[Path, dict]]) -> dict[str, tuple[float, dict]]:
        """Pick the most complete fragment per logical session.

        Codex resume/fork writes a *new* rollout per resume but keeps the same
        session_meta.id, so several files map to one logical session. Keep the
        most complete fragment (most user messages, then latest activity / mtime).
        A forked subagent keys on its own id (the first meta), so it never merges
        with its parent.
        """
        best: dict[str, tuple] = {}  # session_id -> (score, source_mtime, data)
        for rollout, data in parsed:
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
        return {sid: (mtime, data) for sid, (_score, mtime, data) in best.items()}

    def _safe_target(self, session_id: str, planned: Path, claimed: dict[Path, str]) -> Path:
        """``planned``, or its collision-suffixed form when another session already
        holds it on disk or earlier in this run (mirrors :meth:`export_one`), so the
        parent index links where the child is really written."""
        holder = claimed.get(planned)
        if holder is None and planned.exists():
            holder = read_frontmatter_head(planned).get(self.id_field)
        if holder is not None and holder != session_id:
            digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
            planned = planned.with_stem(planned.stem + "-" + digest)
        return planned

    def _plan_targets(
        self, best: dict[str, tuple[float, dict]], order: list[str]
    ) -> dict[str, Path]:
        """Document path per session: an existing record's path, else the child folder
        of the parent's (planned or existing) document, else the canonical path; new
        paths get the same collision suffix :meth:`export_one` would give them."""
        targets: dict[str, Path] = {}
        claimed: dict[Path, str] = {}
        for sid in order:
            data = best[sid][1]
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

    def export_rollouts(
        self,
        date_filter: float | None = None,
        quiet: bool = False,
        parsed: list[tuple[Path, dict]] | None = None,
    ) -> int:
        if parsed is None:
            parsed, _failed = self._parse_rollouts(_get_all_rollout_files())
        best = self._best_fragments(parsed)
        children: dict[str, list[str]] = {}
        for sid, (_mtime, data) in best.items():
            parent = data.get("parent_session_id")
            if parent and parent != sid:
                children.setdefault(parent, []).append(sid)
        # Parents before children, so a child lands in its parent's folder.
        order = sorted(best, key=lambda sid: (_depth(sid, best), sid))
        targets = self._plan_targets(best, order)

        exported = 0
        for session_id in order:
            source_mtime, data = best[session_id]
            kids = sorted(children.get(session_id, []))
            newest = max([source_mtime] + [best[kid][0] for kid in kids])
            if date_filter and newest < date_filter:
                continue
            if kids:
                data = dict(data)
                refs = [
                    _subagent_ref(kid, best[kid][1], targets, targets[session_id]) for kid in kids
                ]
                data["subagents"] = refs
                data["source_revision"] = _parent_revision(data["source_revision"], refs)

            record = self.find_record(session_id)
            if is_current(record, data["source_revision"]):
                continue

            output_file = self.export_one(session_id, cast(SessionData, data), targets[session_id])
            if output_file is None:
                continue

            exported += 1
            if not quiet:
                print(f"  Exported: {output_file}")

        if exported == 0 and not quiet:
            print("  All sessions up to date, nothing to export")

        return exported

    def export_target(self, session_id: str, transcript_path: str | None) -> Path:
        path = Path(transcript_path) if transcript_path else None
        candidates = [p for p in SESSIONS_DIR.rglob("*.jsonl") if session_id in p.name]
        if path:
            if not path.is_file():
                raise ValueError(f"Codex transcript unavailable: {path}")
            if path not in candidates:
                candidates.append(path)
        if not candidates:
            raise ValueError(f"Codex transcript unavailable for {session_id}")
        # Every document this call rewrites renders an index, so load the whole family:
        # the root above the target (each ancestor's index is refreshed) and every
        # transitive descendant of that root (no index renders from a partial set).
        lineage = rollout_lineage(SESSIONS_DIR)
        for thread_id in _family(lineage, session_id):
            candidates += [p for p in lineage.paths_of(thread_id) if p not in candidates]
        # One parse per candidate; the event's own transcript must carry the event's id.
        parsed, failed = self._parse_rollouts(candidates)
        own = [(p, d) for p, d in parsed if d["session_id"] == session_id]
        if path and path not in failed and not any(p == path for p, _ in own):
            raise ValueError("transcript session ID does not match requested session")
        if not own and not failed:
            raise ValueError(f"No readable Codex transcript for {session_id}")
        self.export_rollouts(quiet=True, parsed=parsed)
        if failed:
            # Readable documents are written; fail so the durable hook job retries.
            names = ", ".join(str(p) for p in failed)
            raise ValueError(f"Codex export for {session_id} incomplete; unreadable: {names}")
        result = self.find_file(session_id)
        if result is None:
            raise ValueError(f"Codex export failed for {session_id}")
        return result

    def add_extra_parsers(
        self, subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser
    ) -> None:
        from ..resume import add_source_parser

        add_source_parser(subparsers, common, "codex")

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

        parsed, failed = self._parse_rollouts(_get_all_rollout_files())
        exported = self.export_rollouts(date_filter, quiet=args.quiet, parsed=parsed)
        print(f"\nExported {exported} sessions to {OUTPUT_DIR}")
        if failed:
            print(f"Error: {len(failed)} rollout(s) failed to parse", file=sys.stderr)
            return 1
        return 0


def main() -> None:
    """Console entry point - drive the Codex source through the shared CLI."""
    cli.run(CodexSource())


if __name__ == "__main__":
    main()
