#!/usr/bin/env python3
"""Export Cursor IDE sessions to markdown.

Thin adapter over :mod:`cairn`. Cursor-specific parts only: read-only
access to the Cursor SQLite stores, composer/bubble extraction, and the Cursor
frontmatter layout.

Usage:
    cursor-sessions export (--today | --all | --since DATE)
    cursor-sessions list [--all] [--json]
    cursor-sessions note/close/log ...   (shared)
    cursor-sessions context
"""

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

from .. import cli
from ..config import get_config
from ..frontmatter import (
    head_close,
    head_open,
    resolve_title,
    title_line,
)
from ..projects import (
    project_from_file_uris,
    project_from_folder_path,
    repo_label,
)
from ..rendering import render_artifacts
from ..schema import SessionData
from ..timeutil import today_str
from .base import ConversationSource

COLLECTION = "cursor-sessions"
_config = get_config()
OUTPUT_DIR = _config.output_dir(COLLECTION)
_CURSOR_USER = _config.collection(COLLECTION).require_store()  # ~/.../Cursor/User
GLOBAL_DB = _CURSOR_USER / "globalStorage/state.vscdb"
WORKSPACE_STORAGE = _CURSOR_USER / "workspaceStorage"


# =============================================================================
# Database access
# =============================================================================


def open_global_db() -> sqlite3.Connection:
    """Open global state.vscdb read-only for safe concurrent reads."""
    return sqlite3.connect(f"file:{GLOBAL_DB}?mode=ro", uri=True)


def open_workspace_db(workspace_dir: Path) -> sqlite3.Connection | None:
    """Open a workspace state.vscdb read-only."""
    db_path = workspace_dir / "state.vscdb"
    if not db_path.exists():
        return None
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def build_workspace_map() -> dict[str, str]:
    """Map composerId → folder path by scanning all workspace dirs."""
    workspace_map: dict[str, str] = {}
    if not WORKSPACE_STORAGE.is_dir():
        return workspace_map

    for ws_dir in WORKSPACE_STORAGE.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            ws_data = json.loads(ws_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        folder_uri = ws_data.get("folder", "")
        if not folder_uri.startswith("file://"):
            continue
        folder_path = unquote(folder_uri[7:])

        conn = open_workspace_db(ws_dir)
        if not conn:
            continue
        try:
            cursor = conn.execute("SELECT value FROM ItemTable WHERE key = 'composer.composerData'")
            row = cursor.fetchone()
            if not row:
                continue
            composer_data = json.loads(row[0])
            for composer in composer_data.get("allComposers", []):
                cid = composer.get("composerId")
                if cid:
                    workspace_map[cid] = folder_path
        except (json.JSONDecodeError, sqlite3.Error):
            continue
        finally:
            conn.close()

    return workspace_map


# =============================================================================
# Composer / bubble extraction
# =============================================================================


def extract_composer_data(composer_json: dict, workspace_map: dict) -> dict | None:
    """Extract structured data from a composerData JSON entry (None to skip)."""
    composer_id = composer_json.get("composerId", "")
    if not composer_id:
        return None

    headers = composer_json.get("fullConversationHeadersOnly", [])
    if not headers:
        return None

    status = composer_json.get("status", "")

    created_ms = composer_json.get("createdAt", 0)
    updated_ms = composer_json.get("lastUpdatedAt", 0)

    created_at = (
        datetime.fromtimestamp(created_ms / 1000, tz=UTC).isoformat() if created_ms else None
    )
    updated_at = (
        datetime.fromtimestamp(updated_ms / 1000, tz=UTC).isoformat() if updated_ms else None
    )
    date = (
        datetime.fromtimestamp((created_ms or updated_ms) / 1000, tz=UTC).strftime("%Y-%m-%d")
        if (created_ms or updated_ms)
        else today_str()
    )

    unified_mode = composer_json.get("unifiedMode", "")
    model_config = composer_json.get("modelConfig", {})
    model_name = model_config.get("modelName", "") if isinstance(model_config, dict) else ""

    name = composer_json.get("name", "") or ""
    title = name.strip()[:100] if name.strip() else None

    branch = composer_json.get("branch", "")

    bubble_order = []  # [(type, bubbleId), ...] - modern path
    legacy_turns = []  # ordered inline turn dicts - legacy path
    total_messages = 0
    for header in headers:
        msg_type = header.get("type")
        bubble_id = header.get("bubbleId")
        if msg_type == 1:
            total_messages += 1
        if bubble_id:
            bubble_order.append((msg_type, bubble_id))

    if not bubble_order:
        conversation = composer_json.get("conversation")
        if isinstance(conversation, list):
            for turn in conversation:
                if isinstance(turn, dict):
                    legacy_turns.append(turn)
                    if turn.get("type") == 1:
                        total_messages += 1

    original_file_states = composer_json.get("originalFileStates", {})
    newly_created = composer_json.get("newlyCreatedFiles", [])
    files_modified = (
        list(original_file_states.keys()) if isinstance(original_file_states, dict) else []
    )
    files_created = []
    if isinstance(newly_created, list):
        for entry in newly_created:
            if isinstance(entry, str):
                files_created.append(entry)
            elif isinstance(entry, dict) and "path" in entry:
                files_created.append(entry["path"])

    created_set = set(files_created)
    files_modified = [f for f in files_modified if f not in created_set]

    project = None
    repo_subdir = None

    # Priority 1: workspaceIdentifier on the composer itself (newer Cursor / Agents window)
    folder_path = None
    wsid = composer_json.get("workspaceIdentifier")
    if isinstance(wsid, dict) and isinstance(wsid.get("uri"), dict):
        folder_path = wsid["uri"].get("fsPath") or wsid["uri"].get("path")

    # Priority 2: workspace allComposers map (legacy sessions)
    if not folder_path:
        folder_path = workspace_map.get(composer_id)
    if folder_path:
        project, repo_subdir = project_from_folder_path(folder_path)

    # Priority 3: file URI fallback
    if not project and files_modified:
        project, repo_subdir = project_from_file_uris(files_modified)
    if not project and files_created:
        project, repo_subdir = project_from_file_uris(files_created)

    return {
        "composer_id": composer_id,
        "date": date,
        "title": title,
        "mode": unified_mode or None,
        "model": model_name or None,
        "branch": branch or None,
        "messages": total_messages,
        "created_at": created_at,
        "last_activity": updated_at or created_at,
        "status": status or "active",
        "bubble_order": bubble_order,
        "legacy_turns": legacy_turns,
        "files_modified": files_modified,
        "files_created": files_created,
        "project": project,
        "repo_subdir": repo_subdir,
        "repo": repo_label(project, repo_subdir),
    }


def _bubble_field(bubble: dict, key: str) -> Any:
    """Read a content field, preferring the bubble top level then ``data``."""
    value = bubble.get(key)
    if value:
        return value
    data = bubble.get("data")
    return data.get(key) if isinstance(data, dict) else None


def _stringify(value) -> str | None:
    """Render a structured value as a JSON string, leaving plain strings alone."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _message_from_bubble(bubble: dict, msg_type: int | None) -> dict | None:
    """Convert one raw bubble into a normalized message dict (None if empty)."""
    role = "user" if msg_type == 1 else "assistant"

    raw_text = _bubble_field(bubble, "text")
    text = "" if raw_text is None else (raw_text if isinstance(raw_text, str) else str(raw_text))
    text = text.strip()

    thinking = None
    raw_thinking = _bubble_field(bubble, "thinking")
    if isinstance(raw_thinking, dict):
        t = raw_thinking.get("text")
        if isinstance(t, str) and t.strip():
            thinking = t

    code_blocks = []
    for raw in _bubble_field(bubble, "codeBlocks") or []:
        if not isinstance(raw, dict):
            continue
        uri = raw.get("uri")
        path = uri.get("_fsPath") if isinstance(uri, dict) else None
        content = raw.get("content")
        code_blocks.append(
            {
                "language": raw.get("languageId") or raw.get("language") or "",
                "path": path or None,
                "content": ""
                if content is None
                else (content if isinstance(content, str) else str(content)),
            }
        )

    tool_calls = []
    former = _bubble_field(bubble, "toolFormerData")
    if isinstance(former, dict) and former.get("name"):
        arguments = _stringify(former.get("params"))
        if not arguments:
            arguments = _stringify(former.get("rawArgs"))
        tool_calls.append(
            {
                "name": former.get("name"),
                "status": former.get("status") or None,
                "arguments": arguments,
                "result": _stringify(former.get("result")),
            }
        )

    if not (text or thinking or code_blocks or tool_calls):
        return None

    return {
        "role": role,
        "timestamp": None,
        "text": text,
        "thinking": thinking,
        "reasoning_recoverable": True,
        "tool_calls": tool_calls,
        "code_blocks": code_blocks,
    }


def fetch_bubbles(
    conn: sqlite3.Connection,
    composer_id: str,
    bubble_order: list,
    legacy_turns: list | None = None,
) -> list[dict]:
    """Fetch the full ordered transcript as normalized message dicts."""
    messages = []

    if bubble_order:
        for msg_type, bubble_id in bubble_order:
            key = f"bubbleId:{composer_id}:{bubble_id}"
            try:
                cursor = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,))
                row = cursor.fetchone()
            except sqlite3.Error:
                continue
            if not row or not row[0]:
                continue
            try:
                bubble = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(bubble, dict):
                continue
            msg = _message_from_bubble(bubble, msg_type)
            if msg:
                messages.append(msg)
        return messages

    for turn in legacy_turns or []:
        if not isinstance(turn, dict):
            continue
        msg = _message_from_bubble(turn, turn.get("type"))
        if msg:
            messages.append(msg)

    return messages


# =============================================================================
# Source adapter
# =============================================================================


class CursorSource(ConversationSource):
    name = "cursor-sessions"
    version = "2.0.0"
    output_dir = OUTPUT_DIR
    session_type = "cursor-session"
    collection = "cursor-sessions"
    list_default = "not-done"
    id_field = "composer_id"
    preserved_note = "exports"

    def list_mid_col(self, fm: dict) -> str:
        mode = fm.get("mode", "")
        return f"{(f' [{mode}]' if mode else ''):>10}"

    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        messages = data["messages_ordered"]
        user_messages = [m["text"] for m in messages if m.get("role") == "user" and m.get("text")]

        head = head_open(self.session_type, data["date"], self.id_field, data["composer_id"])
        if data.get("repo"):
            head.append(f"repo: {data['repo']}")
        if data.get("mode"):
            head.append(f"mode: {data['mode']}")
        if data.get("model"):
            head.append(f"model: {data['model']}")

        fallback = user_messages[0][:80].replace("\n", " ").strip() if user_messages else None
        title = resolve_title(data, existing_fm, fallback)
        head.append(title_line(title))

        if data.get("branch"):
            head.append(f"branch: {data['branch']}")

        head.extend(head_close(data.get("last_activity"), data["messages"]))

        body_extra = render_artifacts(data.get("files_created", []), data.get("files_modified", []))

        return self.assemble(
            head,
            body_extra,
            title,
            existing_fm,
            my_notes,
            messages,
            default_status=data.get("status", "active"),
        )

    # ---- export driver ----
    def export_composers(
        self, conn: sqlite3.Connection, date_filter_ms: int | None = None, quiet: bool = False
    ) -> int:
        # First pass: find composers that need exporting (cheap - no workspace map yet)
        to_export = []  # (composer_id, composer_json, existing_file)
        skipped = 0

        cursor = conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")

        for row in cursor:
            key = row[0]
            composer_id = key.split(":", 1)[1] if ":" in key else key
            try:
                composer_json = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                skipped += 1
                continue

            updated_ms = composer_json.get("lastUpdatedAt", 0)
            if date_filter_ms and updated_ms < date_filter_ms:
                skipped += 1
                continue

            existing_file = self.find_file(composer_id)
            if (
                existing_file
                and existing_file.exists()
                and existing_file.stat().st_mtime * 1000 >= updated_ms
            ):
                skipped += 1
                continue

            to_export.append((composer_id, composer_json, existing_file))

        if not to_export:
            if not quiet:
                print("  All sessions up to date, nothing to export")
            return 0

        if not quiet:
            print(f"  {len(to_export)} sessions need exporting, building workspace map...")
        workspace_map = build_workspace_map()
        if not quiet:
            print(f"  Mapped {len(workspace_map)} composers to workspaces")

        exported = 0
        for composer_id, composer_json, existing_file in to_export:
            data = extract_composer_data(composer_json, workspace_map)
            if not data:
                skipped += 1
                continue

            data["messages_ordered"] = fetch_bubbles(
                conn, composer_id, data["bubble_order"], data.get("legacy_turns")
            )

            output_file = self.export_one(composer_id, cast(SessionData, data), existing_file)
            if output_file is None:
                continue

            exported += 1
            if not quiet:
                print(f"  Exported: {output_file}")

        return exported

    def run_export(self, args: argparse.Namespace) -> int:
        if not GLOBAL_DB.exists():
            print(f"Error: Global DB not found: {GLOBAL_DB}", file=sys.stderr)
            return 1

        conn = open_global_db()
        try:
            cutoff = None if args.all else args.since
            date_filter_ms = int(cutoff * 1000) if cutoff is not None else None
            print(
                "Exporting all sessions..."
                if args.all
                else "Exporting sessions in the selected window..."
            )

            exported = self.export_composers(conn, date_filter_ms, quiet=args.quiet)
            print(f"\nExported {exported} sessions to {OUTPUT_DIR}")
        finally:
            conn.close()
        return 0


def main() -> None:
    """Console entry point - drive the Cursor source through the shared CLI."""
    cli.run(CursorSource())


if __name__ == "__main__":
    main()
