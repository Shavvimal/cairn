"""``cairn agents list|wait``: one view over everything a session spawned (plan D3).

Rows cover peer runs started with ``cairn invoke`` (every ``thread.json`` under
``data_root/logs/invoke``), the Claude subagents of a Claude session and the Codex
subagents of a Codex thread. Each row reports the three health axes of
:mod:`cairn.health`: execution, activity with an age, and outcome. Everything is
read from native stores and run directories; nothing is created.

Claude subagent classification, in order:

1. ``meta.stoppedByUser`` -> exited, ``cancelled``.
2. ``meta.finished: true`` -> exited, ``succeeded``.
3. A ``task-notification`` for the agent in the parent transcript that is newer
   than the child's last record -> exited, with the notification's status.
4. No ``finished`` key in the meta and a final assistant ``end_turn`` (older Claude
   Code versions) -> exited, ``succeeded``.
5. Otherwise running, with activity from the child transcript's mtime; ``unknown``
   when both the parent and the child transcripts have been unchanged for longer
   than ``stall_after_s`` (a parent waiting quietly on a child that still writes
   is not evidence of anything).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import CairnConfig, ConfigError, get_config
from .harnesses import detect_harness, get_harness
from .health import Thresholds, activity_of, parse_iso

_SESSION_REF = re.compile(r"^(claude|codex|antigravity):\S+$")
_TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_TASK_STATUS = re.compile(r"<status>\s*([A-Za-z_-]+)\s*</status>")
_TAIL_BYTES = 256 * 1024
EXIT_STALLED = 3
EXIT_TIMEOUT = 124


@dataclass(frozen=True)
class _Thresholds:
    quiet_after_s: int
    stall_after_s: int


@dataclass
class Row:
    kind: str
    id: str
    parent: str | None
    title: str
    execution: str
    activity: str
    activity_age_s: float | None
    outcome: str | None
    started: str | None
    path: str | None


# ---------------------------------------------------------------------------
# Peer runs
# ---------------------------------------------------------------------------


def peer_rows(
    config: CairnConfig, session: str | None, everything: bool, include_exited: bool = False
) -> list[Row]:
    from .invoke import iter_run_dirs, observe

    rows: list[Row] = []
    for run_dir in iter_run_dirs(config):
        try:
            obs = observe(config, run_dir)
        except (OSError, ValueError) as exc:
            print(f"Warning: skipping {run_dir}: {exc}", file=sys.stderr)
            continue
        state = obs.state
        callers = {r.get("caller_session") for r in state.rounds}
        if session is not None:
            if session not in callers:
                continue
        elif not everything:
            continue
        if session is None and not include_exited and obs.health.execution == "exited":
            continue  # --all without a session lists what is still going
        round_ = obs.round_ or {}
        first = " ".join(str((state.rounds or [{}])[0].get("prompt") or "").split())
        rows.append(
            Row(
                kind=f"peer-{state.peer}",
                id=state.thread_id or run_dir.name,
                parent=round_.get("caller_session") or state.caller_session,
                title=first[:60],
                execution=obs.health.execution,
                activity=obs.health.activity,
                activity_age_s=obs.health.activity_age_s,
                outcome=obs.health.outcome,
                started=round_.get("started_at"),
                path=round_.get("events_path"),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Claude subagents
# ---------------------------------------------------------------------------


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return ""


def task_notifications(parent_jsonl: Path) -> dict[str, tuple[float, str]]:
    """``agentId -> (timestamp, status)`` of the newest task-notification per agent."""
    from .jsonl import iter_jsonl

    found: dict[str, tuple[float, str]] = {}
    for _, record in iter_jsonl(parent_jsonl):
        origin = record.get("origin")
        if record.get("type") != "user" or not isinstance(origin, dict):
            continue
        if origin.get("kind") != "task-notification":
            continue
        message = record.get("message")
        text = _text_of(message.get("content") if isinstance(message, dict) else None)
        stamp = parse_iso(record.get("timestamp"))
        if stamp is None:
            continue
        status = _TASK_STATUS.search(text)
        for agent_id in _TASK_ID.findall(text):
            if agent_id not in found or stamp >= found[agent_id][0]:
                found[agent_id] = (stamp, status.group(1).lower() if status else "completed")
    return found


def _notification_outcome(status: str) -> str:
    if status in ("completed", "complete", "success", "succeeded", "done"):
        return "succeeded"
    if status in ("killed", "stopped", "cancelled", "canceled", "interrupted"):
        return "cancelled"
    return "failed"


def _edge_records(path: Path) -> tuple[dict | None, dict | None]:
    """The first and the last parseable records of a JSONL file (reads two chunks)."""
    first: dict | None = None
    last: dict | None = None
    try:
        with path.open("rb") as handle:
            head = handle.readline()
            with contextlib.suppress(ValueError):
                first = json.loads(head)
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None, None
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("timestamp"):
            last = record
            break
    return (first if isinstance(first, dict) else None), last


def classify_claude_child(
    meta: dict,
    last_record: dict | None,
    child_mtime: float,
    notification: tuple[float, str] | None,
    parent_mtime: float,
    now: float,
    quiet_after_s: int,
    stall_after_s: int,
) -> tuple[str, str, float | None, str | None]:
    """``(execution, activity, activity_age_s, outcome)`` for one Claude subagent."""
    if meta.get("stoppedByUser") is True:
        return "exited", "none", None, "cancelled"
    if meta.get("finished") is True:
        return "exited", "none", None, "succeeded"
    last_time = parse_iso((last_record or {}).get("timestamp")) or child_mtime
    if notification is not None and notification[0] >= last_time:
        return "exited", "none", None, _notification_outcome(notification[1])
    message = (last_record or {}).get("message")
    end_turn = (
        (last_record or {}).get("type") == "assistant"
        and isinstance(message, dict)
        and message.get("stop_reason") == "end_turn"
    )
    if "finished" not in meta and end_turn:
        return "exited", "none", None, "succeeded"
    age = max(0.0, now - child_mtime)
    if now - parent_mtime > stall_after_s and age > stall_after_s:
        return "unknown", "none", None, "unknown"
    return "running", activity_of(age, _Thresholds(quiet_after_s, stall_after_s)), age, None


def claude_rows(config: CairnConfig, session_id: str) -> list[Row]:
    from .jsonl import find_session_jsonl, iter_subagent_files
    from .sources.claude import read_subagent_meta

    store = config.collection("claude-code-sessions").require_store()
    parent = find_session_jsonl(store, session_id)
    if parent is None:
        return []
    policy = config.invoke.claude
    notes = task_notifications(parent)
    parent_mtime = parent.stat().st_mtime
    now = time.time()
    rows: list[Row] = []
    for child in iter_subagent_files(parent):
        agent_id = child.stem[len("agent-") :]
        try:
            meta = read_subagent_meta(child)
        except ValueError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            meta = {}
        first, last = _edge_records(child)
        execution, activity, age, outcome = classify_claude_child(
            meta,
            last,
            child.stat().st_mtime,
            notes.get(agent_id),
            parent_mtime,
            now,
            policy.quiet_after_s,
            policy.stall_after_s,
        )
        rows.append(
            Row(
                kind="claude-subagent",
                id=f"agent-{agent_id}",
                parent=f"claude:{session_id}",
                title=str(meta.get("description") or meta.get("agentType") or ""),
                execution=execution,
                activity=activity,
                activity_age_s=age,
                outcome=outcome,
                started=(first or {}).get("timestamp"),
                path=str(child),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Codex subagents
# ---------------------------------------------------------------------------


_ABORT_CANCELLED = ("interrupted", "user", "cancelled", "canceled", "replaced")


TERMINAL = ("succeeded", "cancelled", "failed")


def codex_turn_state(rollout: Path) -> str:
    """The state of a rollout fragment's latest own turn.

    Reads the native ``event_msg`` markers after the replay boundary of a forked
    child (replayed parent history is skipped exactly as the exporter skips it).
    Returns ``succeeded`` (``task_complete``), ``cancelled`` (``turn_aborted`` for an
    interruption), ``failed`` (any other abort), ``open`` (a trailing
    ``task_started``), ``none`` (no own marker) or ``replaying`` (the replayed
    parent history has no resolved end, so nothing here is the child's own).
    """
    from .jsonl import iter_jsonl
    from .sources.codex_replay import ReplayCut

    own_id: str | None = None
    cut: ReplayCut | None = None
    latest = "none"
    for _, record in iter_jsonl(rollout):
        rtype = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if rtype == "session_meta":
            if own_id is None:
                own_id = str(payload.get("id") or "")
            elif payload.get("id") != own_id:
                cut = cut.with_meta() if cut else ReplayCut.start(own_id)
            continue
        if cut is not None and not cut.ended:
            cut = cut.observe(rtype, payload)
            if not cut.ended:
                continue
        if rtype != "event_msg":
            continue
        ptype = payload.get("type")
        if ptype == "task_started":
            latest = "open"
        elif ptype == "task_complete":
            latest = "succeeded"
        elif ptype == "turn_aborted":
            reason = str(payload.get("reason") or "").lower()
            latest = "cancelled" if reason in _ABORT_CANCELLED else "failed"
    if cut is not None and not cut.ended:
        return "replaying"
    return latest


def codex_turn_outcome(rollout: Path) -> str | None:
    """The terminal outcome of a fragment's latest own turn, else ``None``."""
    state = codex_turn_state(rollout)
    return state if state in TERMINAL else None


def classify_codex_child(
    fragments: list[tuple[str, dict | None]], age_s: float, thresholds: Thresholds
) -> tuple[str, str, float | None, str | None]:
    """``(execution, activity, age, outcome)`` for one child thread.

    ``fragments`` holds ``(turn state, parsed rollout or None)`` per rollout file of
    the thread, oldest first. The newest fragment with an own turn marker decides;
    without one, a parsed ``finished`` (an assistant final answer) counts only when
    the replay boundary resolved: an unresolved replay may carry the parent's final
    answer, so it is ``unknown``, never ``succeeded``.
    """
    running = ("running", activity_of(age_s, thresholds), age_s, None)
    decided = [state for state, _ in fragments if state not in ("none", "replaying")]
    if decided:
        last = decided[-1]
        return ("exited", "none", None, last) if last in TERMINAL else running
    parsed = [data for _, data in fragments if data is not None]
    if any(data.get("replay_unresolved") for data in parsed):
        return "unknown", "none", None, "unknown"
    if parsed and any(state == "replaying" for state, _ in fragments):
        return running
    if any(data.get("finished") for data in parsed):
        return "exited", "none", None, "succeeded"
    return running


def codex_rows(config: CairnConfig, thread_id: str) -> list[Row]:
    from .sources.codex import parse_rollout
    from .sources.codex_lineage import rollout_lineage

    store = config.collection("codex-sessions").require_store()
    if not store.is_dir():
        return []
    lineage = rollout_lineage(store)
    policy = config.invoke.codex
    now = time.time()
    # A resumed child thread spans several rollout files: one row per thread.
    threads: dict[str, list[Path]] = {}
    for child in lineage.children_of(thread_id):
        meta = lineage.meta_of(child)
        threads.setdefault(meta.id if meta else child.stem, []).append(child)
    rows: list[Row] = []
    for child_id, paths in threads.items():
        paths.sort(key=lambda path: path.stat().st_mtime)
        fragments: list[tuple[str, dict | None]] = []
        for path in paths:
            with contextlib.redirect_stdout(sys.stderr):
                data = parse_rollout(path)
            fragments.append((codex_turn_state(path), data))
        newest = paths[-1]
        age = max(0.0, now - newest.stat().st_mtime)
        execution, activity, shown_age, outcome = classify_codex_child(fragments, age, policy)
        meta = lineage.meta_of(paths[0])
        title = " ".join(p for p in (meta.role, meta.nickname) if p) if meta else ""
        first = next((data for _, data in fragments if data), None) or {}
        rows.append(
            Row(
                kind="codex-subagent",
                id=child_id,
                parent=f"codex:{thread_id}",
                title=title,
                execution=execution,
                activity=activity,
                activity_age_s=shown_age,
                outcome=outcome,
                started=first.get("first_timestamp"),
                path=str(newest),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Session resolution and rendering
# ---------------------------------------------------------------------------


class UsageError(Exception):
    pass


def resolve_session(explicit: str | None) -> str | None:
    if explicit:
        if not _SESSION_REF.match(explicit):
            raise UsageError(
                f"--session must look like claude:<id>, codex:<id> or antigravity:<id>: {explicit!r}"
            )
        return explicit
    try:
        name = detect_harness()
        native = get_harness(name).current_id() if name else None
    except ValueError:
        return None
    return f"{name}:{native}" if name and native else None


def collect(config: CairnConfig, session: str | None, everything: bool) -> list[Row]:
    rows = peer_rows(config, session, everything)
    if session is not None:
        source, native = session.split(":", 1)
        if source == "claude":
            rows += claude_rows(config, native)
        elif source == "codex":
            rows += codex_rows(config, native)
        # antigravity: no subagent transcripts are exported yet; peer rows cover it.
    return rows


def _age(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 120 else f"{seconds // 60}m"


def render(rows: list[Row]) -> str:
    if not rows:
        return "no peer runs or subagents"
    header = (
        "KIND",
        "ID",
        "PARENT",
        "TITLE",
        "EXECUTION",
        "ACTIVITY",
        "OUTCOME",
        "STARTED",
        "PATH",
    )
    table = [header]
    for row in rows:
        activity = row.activity + (f" {_age(row.activity_age_s)}" if row.activity_age_s else "")
        table.append(
            (
                row.kind,
                row.id[:24],
                (row.parent or "")[:24],
                row.title[:40],
                row.execution,
                activity,
                row.outcome or "",
                row.started or "",
                row.path or "",
            )
        )
    widths = [max(len(line[i]) for line in table) for i in range(len(header) - 1)]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line[:-1])) + "  " + line[-1]
        for line in table
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(config: CairnConfig, args: argparse.Namespace) -> int:
    # --all bypasses the implicitly detected host session; an explicit --session
    # still scopes the listing. Either way --all shows only rows not yet exited.
    session = None if args.all and not args.session else resolve_session(args.session)
    if session is None and not args.all:
        raise UsageError(
            "no session detected; pass --session claude:<id> | codex:<id>, or --all for "
            "every peer run still going"
        )
    while True:
        rows = collect(config, session, args.all)
        if args.all:
            rows = [row for row in rows if row.execution != "exited"]
        if args.json:
            print(json.dumps([asdict(row) for row in rows], ensure_ascii=False), flush=True)
        else:
            print(render(rows), flush=True)
        if not args.watch:
            return 0
        time.sleep(args.poll_s)
        if not args.json:
            print()


def _targets(config: CairnConfig, args: argparse.Namespace, session: str | None) -> list[Row]:
    if not args.ids:
        return collect(config, session, False)
    rows = (
        peer_rows(config, None, True, include_exited=True)
        if session is None
        else collect(config, session, False)
    )
    if session is not None:
        known = {row.id for row in rows}
        rows += [
            row for row in peer_rows(config, None, True, include_exited=True) if row.id not in known
        ]
    chosen: list[Row] = []
    for wanted in args.ids:
        matches = [row for row in rows if row.id.startswith(wanted)]
        if len(matches) != 1:
            raise UsageError(f"{wanted!r} matches {len(matches)} agents; give a unique id")
        chosen.append(matches[0])
    return chosen


def cmd_wait(config: CairnConfig, args: argparse.Namespace) -> int:
    session = resolve_session(args.session)
    if session is None and not args.ids:
        raise UsageError("no session detected; pass --session SOURCE:ID or agent ids")
    deadline = time.monotonic() + args.timeout_s if args.timeout_s else None
    wanted = [row.id for row in _targets(config, args, session)] if args.ids else None
    while True:
        rows = _targets(config, args, session)
        if wanted is not None:
            rows = [row for row in rows if row.id in wanted]
        if args.stall_after_s:
            stalled = [
                row
                for row in rows
                if row.execution in ("running", "orphaned")
                and (row.activity_age_s or 0) >= args.stall_after_s
            ]
            if stalled:
                for row in stalled:
                    print(
                        f"stalled: {row.kind} {row.id} silent for {_age(row.activity_age_s)}; "
                        "it keeps running until its own idle watchdog fires",
                        file=sys.stderr,
                    )
                _emit(rows, args.json)
                return EXIT_STALLED
        if all(row.execution in ("exited", "unknown") for row in rows):
            _emit(rows, args.json)
            failed = [row for row in rows if row.outcome != "succeeded"]
            for row in failed:
                print(f"not succeeded: {row.kind} {row.id}: {row.outcome}", file=sys.stderr)
            return 1 if failed else 0
        if deadline is not None and time.monotonic() >= deadline:
            _emit(rows, args.json)
            print(f"wait timed out after {args.timeout_s}s", file=sys.stderr)
            return EXIT_TIMEOUT
        time.sleep(args.poll_s)


def _emit(rows: list[Row], as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(row) for row in rows], ensure_ascii=False))
    else:
        print(render(rows))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn agents", description="Peer runs and subagents of a session, with health."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="list peer runs and subagents")
    listing.add_argument("--session", metavar="SOURCE:ID")
    listing.add_argument(
        "--all",
        action="store_true",
        help="only rows not yet exited; without --session, every peer run on this machine "
        "(the detected host session is ignored)",
    )
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--watch", action="store_true", help="re-render every --poll-s")
    listing.add_argument("--poll-s", type=float, default=5.0)
    wait = sub.add_parser("wait", help="block until every target exits")
    wait.add_argument("ids", nargs="*", metavar="ID")
    wait.add_argument("--session", metavar="SOURCE:ID")
    wait.add_argument("--timeout-s", type=float)
    wait.add_argument("--poll-s", type=float, default=5.0)
    wait.add_argument("--stall-after-s", type=float, help="exit 3 once a target is this silent")
    wait.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = get_config()
        if args.command == "list":
            return cmd_list(config, args)
        return cmd_wait(config, args)
    except UsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "Row",
    "classify_claude_child",
    "classify_codex_child",
    "codex_turn_outcome",
    "codex_turn_state",
    "collect",
    "main",
    "task_notifications",
]
