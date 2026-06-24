"""``cairn recall`` - temporal recall over Claude Code's native JSONL sessions.

``cairn recall list <date>`` lists sessions in a date window, read straight from the
Claude projects store - so it sees the newest activity even before a session is
exported to markdown. ``cairn recall expand <id>`` prints a condensed transcript.

This is the chronological "what did I do <when>" view (topic search is qmd's job).
It is the one piece of real logic the ``/cairn:recall`` skill drives; the skill itself
is a thin invoker of this command + ``qmd``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import CairnConfig, ConfigError, claude_slug, get_config
from .jsonl import iter_jsonl, parse_jsonl
from .projects import project_name_from_dir
from .timeutil import parse_date_range, parse_iso

COLLECTION = "claude-code-sessions"
# Claude Code's own convention; used when no cairn config relocates the store.
_DEFAULT_STORE = Path.home() / ".claude" / "projects"

# Min user-message count for a session to count as signal (filters trivial/aborted ones).
DEFAULT_MIN_MSGS = 3
# Max messages `expand` prints before truncating, to keep a transcript scannable.
DEFAULT_MAX_MSGS = 50
# How many leading lines the fast scan checks the timestamp within before bailing early.
_EARLY_EXIT_LINES = 5
# Title length cap in the list view.
_TITLE_MAX = 80
# A user message must clear this length (after cleaning) to seed a title / count as content.
_MIN_CONTENT_LEN = 5

# System wrappers injected by the harness - not human-written, so stripped before
# deriving a title or showing a message.
_STRIP_PATTERNS = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.DOTALL),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL),
    re.compile(
        r"<command-name>.*?</command-name>\s*<command-message>.*?</command-message>\s*"
        r"(?:<command-args>.*?</command-args>)?",
        re.DOTALL,
    ),
    re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL),
    re.compile(r"<teammate-message[^>]*>.*?</teammate-message>", re.DOTALL),
]
_SLASH_COMMAND = re.compile(r"^/\w+\s*$")


def _maybe_config() -> CairnConfig | None:
    """The cairn config if one is set up, else None.

    Recall reads Claude Code's own store, which has a fixed default location, so a
    cairn config is optional enrichment here (it can relocate the store and supply
    project grouping). Justified §8 degradation: when no config exists recall still
    works on the default store with raw project-dir labels - it never silently
    produces a *wrong* window, only a less-decorated one.
    """
    try:
        return get_config()
    except ConfigError:
        return None


def _store(cfg: CairnConfig | None) -> Path:
    """The Claude projects store from config, else the well-known default."""
    if cfg is not None:
        coll = cfg.collections.get(COLLECTION)
        if coll is not None and coll.store is not None:
            return coll.store
    return _DEFAULT_STORE


def _project_label(cfg: CairnConfig | None, encoded_dir: str) -> str:
    """Human project name for a session's encoded dir (raw name if no config)."""
    if cfg is None:
        return encoded_dir
    return project_name_from_dir(encoded_dir)


def _clean(text: object) -> str:
    """Strip harness system tags, leaving human-written content."""
    if not isinstance(text, str):
        return ""
    for pat in _STRIP_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def _message_text(content: object) -> str:
    """Text from a user message's content (a string, or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _is_user_message(record: dict) -> bool:
    return record.get("type") == "user" and record.get("message", {}).get("role") == "user"


def _derive_title(first_user_msg: str) -> str:
    """First meaningful line of the first user message, trimmed to a title."""
    line = re.sub(r"^#+\s*", "", first_user_msg.split("\n")[0].strip())
    if line.startswith("## Continue:"):
        m = re.match(r"## Continue:\s*(.+?)(?:\n|$)", first_user_msg)
        if m:
            line = m.group(1).strip()
    if len(line) > _TITLE_MAX:
        line = line[: _TITLE_MAX - 3] + "..."
    return line if len(line) >= 3 else "Untitled"


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _project_dirs(store: Path, project: str | None, all_projects: bool) -> list[Path]:
    """Project dirs to scan. Raises ValueError if an explicit --project is not found."""
    if project:
        encoded = store / claude_slug(project)
        if encoded.is_dir():
            return [encoded]
        literal = Path(project).expanduser()
        if literal.is_dir():
            return [literal]
        raise ValueError(f"project path not found: {project}")
    if not store.is_dir():
        return []
    if not all_projects:
        cwd_dir = store / claude_slug(os.getcwd())
        if cwd_dir.is_dir():
            return [cwd_dir]
    return [d for d in store.iterdir() if d.is_dir()]


def _scan_metadata(path: Path, start: datetime, end: datetime) -> dict | None:
    """Fast metadata scan: read leading lines for timestamp, id, and message count.

    Returns None when the session falls outside ``[start, end)`` or can't be read -
    this is enumeration, so an unreadable file is skipped (best-effort, like
    :func:`cairn.jsonl.parse_jsonl`), never an error.
    """
    session_id = path.stem
    start_time: datetime | None = None
    first_user_msg: str | None = None
    user_msg_count = 0

    try:
        for i, obj in iter_jsonl(path):
            if obj.get("sessionId"):
                session_id = obj["sessionId"]

            if not start_time:
                start_time = parse_iso(obj.get("timestamp"))

            if _is_user_message(obj):
                user_msg_count += 1
                if first_user_msg is None:
                    cleaned = _clean(_message_text(obj["message"].get("content", "")))
                    if len(cleaned) >= _MIN_CONTENT_LEN and not _SLASH_COMMAND.match(cleaned):
                        first_user_msg = cleaned

            # Bail early once we know the start is well outside the window.
            if (
                start_time
                and i < _EARLY_EXIT_LINES
                and (start_time >= end or start_time < start - timedelta(days=1))
            ):
                return None
    except (OSError, UnicodeDecodeError):
        return None

    if start_time is None or start_time < start or start_time >= end:
        return None

    return {
        "session_id": session_id,
        "start_time": start_time,
        "user_msg_count": user_msg_count,
        "file_size": path.stat().st_size,
        "title": _derive_title(first_user_msg) if first_user_msg else "Untitled",
        "filepath": path,
    }


def _cmd_list(args: argparse.Namespace, cfg: CairnConfig | None, store: Path) -> int:
    start, end = parse_date_range(args.date_expr)  # ValueError -> caught in main (fail loud)
    dirs = _project_dirs(store, args.project, args.all_projects)

    sessions: list[dict] = []
    noise = 0
    for proj_dir in dirs:
        for path in proj_dir.glob("*.jsonl"):
            try:  # coarse mtime prefilter (with a day of slack) before the line scan
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if mtime < start - timedelta(days=1):
                continue
            meta = _scan_metadata(path, start, end)
            if meta is None:
                continue
            if meta["user_msg_count"] < args.min_msgs:
                noise += 1
                continue
            meta["project"] = _project_label(cfg, path.parent.name)
            sessions.append(meta)

    sessions.sort(key=lambda s: s["start_time"])

    if getattr(args, "json", False):
        print(
            json.dumps(
                [
                    {
                        "session_id": s["session_id"],
                        "time": s["start_time"].isoformat(),
                        "msgs": s["user_msg_count"],
                        "size": s["file_size"],
                        "title": s["title"],
                        "project": s["project"],
                    }
                    for s in sessions
                ],
                indent=2,
            )
        )
        return 0

    if end - start <= timedelta(days=1):
        header = start.strftime("%Y-%m-%d (%A)")
    else:
        header = f"{start.strftime('%Y-%m-%d')} to {(end - timedelta(days=1)).strftime('%Y-%m-%d')}"
    print(f"\nSessions for {header}\n")

    if not sessions:
        print("No sessions found.")
        if noise:
            print(f"({noise} filtered as noise - try --min-msgs 1)")
        return 0

    print(f" {'#':>2}  {'Time':5}  {'Msgs':>4}  {'Size':>6}  {'Project':12}  First Message")
    print(f" {'--':>2}  {'-----':5}  {'----':>4}  {'------':>6}  {'-------':12}  -------------")
    for i, s in enumerate(sessions, 1):
        print(
            f" {i:2}  {s['start_time'].strftime('%H:%M')}  {s['user_msg_count']:4}  "
            f"{_format_size(s['file_size']):>6}  {s['project'][:12]:12}  {s['title'][:60]}"
        )

    print(f"\n{len(sessions)} sessions" + (f" ({noise} filtered as noise)" if noise else ""))
    print("\nSession IDs (for expand):")
    for i, s in enumerate(sessions, 1):
        print(f"  {i:2}. {s['session_id'][:8]}")
    return 0


def _find_session(dirs: list[Path], session_id: str) -> Path | None:
    """Locate a transcript by id prefix across the given project dirs."""
    target = session_id.lower()
    for proj_dir in dirs:
        for path in proj_dir.glob("*.jsonl"):
            if path.stem.lower().startswith(target):
                return path
    return None


def _cmd_expand(args: argparse.Namespace, store: Path) -> int:
    dirs = _project_dirs(store, args.project, args.all_projects)
    target = _find_session(dirs, args.session_id)
    if target is None:
        # An explicitly requested session that can't be found is an error, not an
        # empty success (§8) - the caller asked for *this* session.
        print(f"Error: no session found matching {args.session_id!r}", file=sys.stderr)
        return 1

    print(f"\nSession: {target.stem}")
    print(f"File: {target}\n")

    msg_count = 0
    for record in parse_jsonl(target):
        ts_label = ""
        dt = parse_iso(record.get("timestamp"))
        if dt:
            ts_label = dt.strftime("%H:%M")

        msg = record.get("message", {})
        if _is_user_message(record):
            cleaned = _clean(_message_text(msg.get("content", "")))
            if len(cleaned) < _MIN_CONTENT_LEN or _SLASH_COMMAND.match(cleaned):
                continue
            msg_count += 1
            if msg_count > args.max_msgs:
                print(f"\n... truncated at {args.max_msgs} messages (use --max-msgs to show more)")
                break
            display = cleaned if len(cleaned) <= 200 else cleaned[:197] + "..."
            print(f"[{ts_label}] USER: {display.replace(chr(10), chr(10) + '    ')}")
        elif record.get("type") == "assistant" and msg.get("role") == "assistant":
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    first_line = block.get("text", "").split("\n")[0][:120]
                    if first_line.strip():
                        print(f"  [{ts_label}] ASST: {first_line}")
                    break
                if block.get("type") == "tool_use":
                    print(f"  [{ts_label}] TOOL: {block.get('name', '?')}")

    print(f"\n{msg_count} user messages total")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cairn recall",
        description="Temporal recall over Claude Code sessions (native JSONL).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p_list = sub.add_parser("list", help="list sessions in a date window")
    p_list.add_argument(
        "date_expr",
        nargs="+",
        metavar="DATE",
        help="today | yesterday | YYYY-MM-DD | 'N days ago' | 'last N days' | "
        "'this week' | 'last week' | 'last <weekday>'",
    )
    p_list.add_argument("--project", metavar="PATH", help="scan only this project path")
    p_list.add_argument("--all-projects", action="store_true", help="scan every project")
    p_list.add_argument(
        "--min-msgs", type=int, default=DEFAULT_MIN_MSGS, help="min user messages (default: 3)"
    )
    p_list.add_argument("--json", action="store_true", help="emit JSON on stdout")

    p_expand = sub.add_parser("expand", help="show a condensed transcript for a session")
    p_expand.add_argument("session_id", metavar="SESSION_ID", help="session id (prefix match)")
    p_expand.add_argument("--project", metavar="PATH", help="scan only this project path")
    p_expand.add_argument("--all-projects", action="store_true", help="scan every project")
    p_expand.add_argument(
        "--max-msgs", type=int, default=DEFAULT_MAX_MSGS, help="max messages to show (default: 50)"
    )

    args = parser.parse_args(argv)
    if args.command == "list":
        args.date_expr = " ".join(args.date_expr)

    cfg = _maybe_config()
    store = _store(cfg)
    try:
        if args.command == "list":
            return _cmd_list(args, cfg, store)
        return _cmd_expand(args, store)
    except ValueError as e:  # bad date expression / unknown --project
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
