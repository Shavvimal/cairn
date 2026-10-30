"""Temporal recall and bounded expansion over Cairn's shared exported store."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from . import digest, provenance, verify
from . import recall_gaps as gaps
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


def _owner(fm: dict) -> str | None:
    """The stored ``owner`` label, or ``None`` (blank and ``null`` included)."""
    return provenance.owner_of(fm)


def _add_verify_parser(sub) -> None:
    """``recall verify``: check a journal's citations and quotes against a digest."""
    command = sub.add_parser(
        "verify",
        help="check a journal's [SOURCE:ID fN] citations and quotes against a digest",
        description=(
            "Check that every [SOURCE:ID fN] citation in FILE names a frame the digest kept, "
            "unchanged since, and that every quoted span is in a cited frame. Exit 0 when "
            "everything verifies, 4 when a check failed, 1 on an error."
        ),
    )
    command.add_argument("file", metavar="FILE", help="the journal (any Markdown file)")
    command.add_argument(
        "--digest", required=True, metavar="DIR", help="a directory written by recall digest"
    )
    command.add_argument("--json", action="store_true")
    command.add_argument(
        "--fix", action="store_true", help="move failing blocks under ## Unverified in FILE"
    )


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
    listing.add_argument(
        "--coverage",
        action="store_true",
        help='with --json, print {"sessions": [...], "coverage": {...}} instead of a bare array',
    )
    expand = sub.add_parser("expand", help="read an exported session, including annotations")
    expand.add_argument("session_id", help="source:full-id or unique prefix")
    expand.add_argument("--max-msgs", type=int, default=DEFAULT_MAX_MSGS)
    expand.add_argument("--max-chars", type=int, default=30000)
    digest_cmd = sub.add_parser(
        "digest",
        help="condense one UTC day of sessions, with a frames.jsonl sidecar per session",
        description=(
            "Condense one UTC day of exported sessions into per-session digests. "
            "DATE: today, yesterday, YYYY-MM-DD, 'N days ago' or 'last <weekday>'."
        ),
    )
    digest_cmd.add_argument("date_expr", nargs="+", metavar="DATE")
    digest_cmd.add_argument("--min-msgs", type=int, default=1)
    digest_cmd.add_argument("--include-subagents", action="store_true")
    digest_cmd.add_argument(
        "--include-routine-sessions",
        action="store_true",
        help="keep sessions owned by a routine (owner routine:NAME); excluded by default",
    )
    digest_cmd.add_argument(
        "--out", help="output directory (default: $XDG_CACHE_HOME/cairn/digests/DATE)"
    )
    digest_cmd.add_argument("--json", action="store_true")
    digest_cmd.add_argument("--max-chars", type=int, default=digest.DEFAULT_MAX_CHARS)
    digest_cmd.add_argument(
        "--refresh", action="store_true", help="refresh exports first, without embedding"
    )
    for command in (listing, digest_cmd):
        command.add_argument(
            "--provenance",
            action="append",
            choices=provenance.VALUES,
            help="keep only documents with this provenance value (repeatable)",
        )
    frame = sub.add_parser("frame", help="print one exported frame and check its digest hash")
    frame.add_argument("session_id", help="source:full-id or unique prefix")
    frame.add_argument("ordinal", type=int, help="0-based frame index in the whole session")
    frame.add_argument("--frames", help="the .frames.jsonl sidecar the citation came from")
    gaps_cmd = sub.add_parser(
        "gaps",
        help="what recall cannot see right now: export lag, index lag, disabled sources, rg",
        description=(
            "Report coverage gaps computed from config, native store stats, the exported "
            "store and one 'qmd collection list' call. Exit 0 whether clean or degraded; "
            "the status is the last line. Takes no lock, so a sync running at the same "
            "time can show a transient index-lag."
        ),
    )
    gaps_cmd.add_argument("--json", action="store_true")
    gaps_cmd.add_argument(
        "--source", action="append", choices=[s.source for s in SOURCES], metavar="SOURCE"
    )
    _add_verify_parser(sub)
    session_sources = [s.source for s in SOURCES if s.session_type]
    for command in (listing, expand, digest_cmd, frame):
        projects = command.add_mutually_exclusive_group()
        projects.add_argument("--project", help="exact project/repo label or working directory")
        projects.add_argument("--all-projects", action="store_true")
        command.add_argument(
            "--source",
            action="append",
            choices=list(digest.DIGEST_SOURCES) if command is digest_cmd else session_sources,
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "gaps":
            return _gaps(args)
        store = SessionStore(get_config())
        if args.command == "verify":
            return verify.run_verify(args, store)
        sources = args.source
        if args.command in ("list", "digest"):
            if args.command == "digest":
                start, end = digest.one_day(" ".join(args.date_expr))
                if args.max_chars < 1:
                    raise ValueError("--max-chars must be positive")
                sources = args.source or list(digest.DIGEST_SOURCES)
            else:
                start, end = parse_date_range(" ".join(args.date_expr))
            if args.min_msgs < 0:
                raise ValueError("--min-msgs must be nonnegative")
            if args.refresh and not refresh(sources):
                return 1
        records = store.records(sources)
        # An explicit ID is global unless a project filter is requested. Listing and
        # digest default strictly to cwd; absence of project metadata never broadens it.
        project = args.project or (
            os.getcwd() if args.command in ("list", "digest") and not args.all_projects else None
        )
        if project:
            records = [r for r in records if r.matches_project(project)]
        if args.command == "digest":
            requested = list(sources or digest.DIGEST_SOURCES)
            return digest.run_digest(
                args,
                records,
                (start, end),
                coverage=lambda found, skipped: gaps.coverage(
                    store.config,
                    requested,
                    (start, end),
                    found,
                    skipped=skipped,
                    named=args.source,
                ),
            )
        if args.command == "frame":
            return digest.run_frame(args, store.resolve(args.session_id, records))
        if args.command == "expand":
            if args.max_msgs < 1 or args.max_chars < 1:
                raise ValueError("expansion limits must be positive")
            record = store.resolve(args.session_id, records)
            value = provenance.effective(record.metadata, record.source, record.path)
            owner = _owner(record.metadata)
            print(f"Session: {record.reference}\nFile: {record.path}")
            print(f"Provenance: {value}" + (f" (owner {owner})" if owner else "") + "\n")
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
        values = {r.path: provenance.effective(r.metadata, r.source, r.path) for r in selected}
        if args.provenance:
            selected = [r for r in selected if values[r.path] in args.provenance]
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
                "provenance": values[r.path],
                "owner": _owner(r.metadata),
            }
            for r in selected
        ]
        cov = None
        if args.coverage or not args.json:
            requested = args.source or session_sources
            cov = gaps.coverage(
                store.config, requested, (start, end), bool(output), named=args.source
            )
        if args.json:
            payload = {"sessions": output, "coverage": cov} if args.coverage else output
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"Sessions with recorded activity: {start.date()} to {end.date()} (exclusive), UTC"
            )
            for row in output:
                parent = row["parent"]
                lineage = f" (subagent of {str(parent)[:8]})" if parent else ""
                owner = f" owner={row['owner']}" if row["owner"] else ""
                print(
                    f"{row['reference']}  {row['time']}  {row['title']}  "
                    f"[{row['precision']}] {row['provenance']}{owner}{lineage}"
                )
            print(f"{len(output)} sessions")
        limited = sum(r.precision != "messages" or r.user_messages is None for r in selected)
        if limited:
            print(
                f"Note: {limited} records have reduced time/count precision; only recorded dates/endpoints match. Refresh upgrades available native data.",
                file=sys.stderr,
            )
        if cov is not None and (note := gaps.coverage_note(cov)):
            print(note, file=sys.stderr)
        return 0
    except (ValueError, ConfigError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _gaps(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    facts = gaps.gather(get_config(), args.source, now=now)
    found = gaps.classify(facts, now, requested=args.source)
    if args.json:
        print(json.dumps(gaps.to_json(facts, found, now), indent=2))
    else:
        print(gaps.render_text(facts, found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
