"""Temporal recall and bounded expansion over Cairn's shared exported store."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from .config import ConfigError, get_config
from .registry import SOURCES
from .store import SessionStore, bounded_content
from .timeutil import parse_date_range

DEFAULT_MIN_MSGS = 3
DEFAULT_MAX_MSGS = 50


def refresh(sources: list[str] | None = None) -> bool:
    """Export only, without QMD. All history allows old resumed sessions/schema upgrades.

    Sources retain their incremental mtime/schema checks. Failures are surfaced;
    callers must not mistake the retained, potentially stale store for fresh data.
    """
    config = get_config()
    success = True
    for source in SOURCES:
        coll = config.collections.get(source.collection)
        if not source.session_type or coll is None or not coll.sync.enabled:
            continue
        if sources and source.source not in sources:
            continue
        result = subprocess.run(
            [sys.executable, "-m", "cairn", source.source, "export", "--all", "--quiet"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            print(
                f"Refresh failed for {source.source}; store may be stale:\n{result.stderr[-2000:]}",
                file=sys.stderr,
            )
            success = False
    return success


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cairn recall",
        description="Recall exported sessions across sources (UTC date windows).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="list sessions with activity in a date window")
    listing.add_argument("date_expr", nargs="+", metavar="DATE")
    listing.add_argument("--min-msgs", type=int, default=DEFAULT_MIN_MSGS)
    listing.add_argument("--json", action="store_true")
    listing.add_argument(
        "--refresh", action="store_true", help="refresh exports first, without embedding"
    )
    listing.add_argument(
        "--include-subagents",
        action="store_true",
        help="also list subagent transcripts (hidden by default; --min-msgs skips them)",
    )
    expand = sub.add_parser("expand", help="read an exported session, including annotations")
    expand.add_argument("session_id", help="source:full-id or unique prefix")
    expand.add_argument("--max-msgs", type=int, default=DEFAULT_MAX_MSGS)
    expand.add_argument("--max-chars", type=int, default=30000)
    for command in (listing, expand):
        projects = command.add_mutually_exclusive_group()
        projects.add_argument("--project", help="exact project/repo label or working directory")
        projects.add_argument("--all-projects", action="store_true")
        command.add_argument(
            "--source", action="append", choices=[s.source for s in SOURCES if s.session_type]
        )
    args = parser.parse_args(argv)
    try:
        store = SessionStore(get_config())
        if args.command == "list":
            start, end = parse_date_range(" ".join(args.date_expr))
            if args.min_msgs < 0:
                raise ValueError("--min-msgs must be nonnegative")
            if args.refresh and not refresh(args.source):
                return 1
        records = store.records(args.source)
        # An explicit ID is global unless a project filter is requested. Listing
        # defaults strictly to cwd; absence of project metadata never broadens it.
        project = args.project or (
            os.getcwd() if args.command == "list" and not args.all_projects else None
        )
        if project:
            records = [r for r in records if r.matches_project(project)]
        if args.command == "expand":
            if args.max_msgs < 1 or args.max_chars < 1:
                raise ValueError("expansion limits must be positive")
            record = store.resolve(args.session_id, records)
            print(f"Session: {record.reference}\nFile: {record.path}\n")
            print(bounded_content(record, args.max_msgs, args.max_chars))
            if record.user_messages is not None:
                print(f"\n{record.user_messages} user messages total")
            return 0
        if not args.include_subagents:
            records = [r for r in records if not r.is_subagent]
        # A subagent has one user message (its prompt), so --min-msgs, a filter for
        # conversations, would hide every child the caller asked to see.
        selected = [
            r
            for r in records
            if r.in_window(start, end)
            and (r.is_subagent or r.user_messages is None or r.user_messages >= args.min_msgs)
        ]
        selected.sort(key=lambda r: r.started_at or datetime.min.replace(tzinfo=UTC))
        output = [
            {
                "source": r.source,
                "session_id": r.session_id,
                "reference": r.reference,
                "time": r.started_at.isoformat() if r.started_at else None,
                "msgs": r.user_messages,
                "total_messages": r.total_messages,
                "title": r.title,
                "project": r.project,
                "cwd": r.cwd,
                "precision": r.precision,
                "path": str(r.path),
                "type": r.session_type,
                "parent": r.parent_id,
            }
            for r in selected
        ]
        if args.json:
            print(json.dumps(output, indent=2))
        else:
            print(
                f"Sessions with recorded activity: {start.date()} to {end.date()} (exclusive), UTC"
            )
            for row in output:
                parent = row["parent"]
                lineage = f" (subagent of {str(parent)[:8]})" if parent else ""
                print(
                    f"{row['reference']}  {row['time']}  {row['title']}  "
                    f"[{row['precision']}]{lineage}"
                )
            print(f"{len(output)} sessions")
        limited = sum(r.precision != "messages" or r.user_messages is None for r in selected)
        if limited:
            print(
                f"Note: {limited} records have reduced time/count precision; only recorded dates/endpoints match. Refresh upgrades available native data.",
                file=sys.stderr,
            )
        return 0
    except (ValueError, ConfigError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
