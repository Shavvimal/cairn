"""``cairn routine``: scheduled peer runs under launchd (docs/routine-spec.md section 6).

``install`` writes a LaunchAgent that runs ``cairn routine run NAME`` daily at the
routine's local ``at`` time; ``run`` is the engine in :mod:`cairn.routine_run`;
``list`` and ``status`` read the stamps and ``last-run.json``; ``uninstall`` and
``kickstart`` drive ``launchctl``. Every ``launchctl`` call goes through
:func:`launchctl`, which the tests replace.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import routine_plan
from .config import (
    NAME_RE,
    NAME_RULE,
    CairnConfig,
    ConfigError,
    RoutineSpec,
    get_config,
    routine_paths,
)
from .routine_plan import (
    ATTEMPT_CAP,
    RoutineError,
    attempt_dir,
    next_attempt,
    read_json,
    render_day,
    stamp_path,
    window,
)
from .storage import atomic_write

STATUS_HISTORY_DAYS = 30


def is_macos() -> bool:
    return sys.platform == "darwin"


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``launchctl ARGS``; a missing binary is exit 127."""
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(["launchctl", *args], 127, "", str(exc))


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _paths(config: CairnConfig, name: str) -> tuple[Path, Path, Path, str]:
    """``(state_dir, log, plist, label)`` for routine ``name``."""
    paths = routine_paths(config, name)
    return Path(paths["state_dir"]), Path(paths["log"]), Path(paths["plist"]), str(paths["label"])


def _routine(config: CairnConfig, name: str) -> RoutineSpec:
    spec = config.routines.get(name)
    if spec is None:
        known = ", ".join(sorted(config.routines)) or "(none)"
        raise RoutineError(f"routine {name!r} is not in routines; known: {known}")
    return spec


def _check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise RoutineError(f"routine name {name!r} is invalid: {NAME_RULE}")


# ---------------------------------------------------------------------------
# launchd plist and environment (6.1, 6.7)
# ---------------------------------------------------------------------------


def environment_block(config: CairnConfig) -> dict[str, str]:
    """The background environment without pinned agent homes, plus ``XDG_STATE_HOME``
    when the installing shell sets it (the run must find the same state dir)."""
    from .runtime import background_environment

    environment = background_environment(config, pin_agent_homes=False)
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        environment["XDG_STATE_HOME"] = str(Path(state).resolve())
    return environment


def cairn_invocation() -> list[str]:
    from .admin import _cairn_invocation

    return _cairn_invocation()


def render_plist(
    config: CairnConfig, spec: RoutineSpec, environment: dict[str, str], invocation: list[str]
) -> bytes:
    """The LaunchAgent plist of section 6.1 (``plistlib`` escapes the XML)."""
    _state, log, _plist, label = _paths(config, spec.name)
    hour, minute = (int(part) for part in spec.at.split(":"))
    payload = {
        "EnvironmentVariables": environment,
        "Label": label,
        "ProcessType": "Background",
        "ProgramArguments": [*invocation, "routine", "run", spec.name],
        "RunAtLoad": False,
        "StandardErrorPath": str(log),
        "StandardOutPath": str(log),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def crontab_line(config: CairnConfig, spec: RoutineSpec) -> str:
    """The cron equivalent printed by ``install`` off macOS (``%`` escaped for cron)."""
    _state, log, _plist, _label = _paths(config, spec.name)
    hour, minute = spec.at.split(":")
    argv = [
        "/usr/bin/env",
        *[f"{key}={value}" for key, value in environment_block(config).items()],
        *cairn_invocation(),
        "routine",
        "run",
        spec.name,
    ]
    command = f"{shlex.join(argv)} >> {shlex.quote(str(log))} 2>&1".replace("%", r"\%")
    return f"{int(minute)} {int(hour)} * * * {command} # cairn routine {spec.name}"


def install(config: CairnConfig, name: str) -> int:
    spec = _routine(config, name)
    if not is_macos():
        print(
            "cairn routine install is launchd-only in this version; add this line with "
            "'crontab -e' to run it under cron:",
            file=sys.stderr,
        )
        print(crontab_line(config, spec))
        return 2
    routine_plan.check_inputs(spec)
    yesterday = routine_plan.utc_today() - timedelta(days=1)
    number = next_attempt(config, name, yesterday)
    render_day(config, spec, yesterday, attempt_dir(config, name, yesterday, number))

    environment = environment_block(config)
    search = environment.get("PATH", "")
    if shutil.which(spec.peer, path=search) is None:
        print(f"warning: {spec.peer} is not on the recorded PATH ({search})", file=sys.stderr)
    head = spec.validate[0]
    if not Path(head).expanduser().is_absolute() and shutil.which(head, path=search) is None:
        print(f"warning: validate command {head!r} is not on the recorded PATH", file=sys.stderr)

    _state, log, plist, label = _paths(config, name)
    text = render_plist(config, spec, environment, cairn_invocation()).decode("utf-8")
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    launchctl("bootout", f"{_domain()}/{label}")  # not loaded is fine: re-install replaces
    atomic_write(plist, text)
    result = launchctl("bootstrap", _domain(), str(plist))
    if result.returncode != 0:
        print(
            f"launchctl bootstrap failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}",
            file=sys.stderr,
        )
        return 1
    print(f"Installed {label}: daily at {spec.at} local, log {log}")
    return 0


def uninstall(config: CairnConfig, name: str) -> int:
    _check_name(name)
    if not is_macos():
        print("cairn routine uninstall is launchd-only in this version", file=sys.stderr)
        return 2
    state, _log, plist, label = _paths(config, name)
    booted = launchctl("bootout", f"{_domain()}/{label}").returncode == 0
    present = plist.is_file()
    if present:
        plist.unlink()
    if not (booted or present):
        print(f"{label} is not installed; nothing to remove")
        return 0
    print(f"Removed {label} (stamps kept in {state})")
    return 0


def kickstart(config: CairnConfig, name: str, force_restart: bool) -> int:
    _check_name(name)
    if not is_macos():
        print("cairn routine kickstart is launchd-only in this version", file=sys.stderr)
        return 2
    _state, _log, _plist, label = _paths(config, name)
    target = f"{_domain()}/{label}"
    if launchctl("print", target).returncode != 0:
        print(f"{label} is not loaded; run 'cairn routine install {name}'", file=sys.stderr)
        return 1
    # Without -k launchd leaves a running instance alone; the routine lock makes a
    # second start harmless. -k kills a running invoke mid-round (its spend is lost).
    result = launchctl("kickstart", *(["-k"] if force_restart else []), target)
    if result.returncode != 0:
        print(
            f"launchctl kickstart failed (exit {result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    print(f"Started {label}; follow it with 'cairn routine status {name}'")
    return 0


# ---------------------------------------------------------------------------
# list / status (6.3, 6.4)
# ---------------------------------------------------------------------------


def install_state(config: CairnConfig, name: str) -> str:
    """``yes`` (plist on disk and loaded), ``unloaded``, ``no`` or ``n/a`` (not macOS)."""
    if not is_macos():
        return "n/a"
    _state, _log, plist, label = _paths(config, name)
    if not plist.is_file():
        return "no"
    return "yes" if launchctl("print", f"{_domain()}/{label}").returncode == 0 else "unloaded"


def day_rows(spec: RoutineSpec, state: Path, today: date) -> list[dict]:
    """One row per window day, plus any older stamped day of the last 30, newest first."""
    current = window(today, spec.catch_up_days)
    days = set(current)
    for path in [*state.glob("done-*.json"), *state.glob("failed-*.json")]:
        try:
            day = date.fromisoformat(path.stem.split("-", 1)[1])
        except ValueError:
            continue
        if timedelta(0) < today - day <= timedelta(days=STATUS_HISTORY_DAYS):
            days.add(day)
    rows: list[dict] = []
    for day in sorted(days, reverse=True):
        done = read_json(stamp_path(state, "done", day))
        failed = read_json(stamp_path(state, "failed", day))
        row: dict = {"day": day.isoformat()}
        if done is not None:
            row.update(state=done.get("status"), attempt=done.get("attempt"))
            row.update(cost_usd=done.get("cost_usd"), attempt_dir=done.get("attempt_dir"))
        elif failed is not None:
            row.update(state="failed" if day in current else "expired")
            row.update(reason=failed.get("reason"), attempts=failed.get("attempts"))
            row.update(attempt_dir=failed.get("last_attempt_dir"))
        else:
            row.update(state="pending" if day in current else "expired")
        rows.append(row)
    return rows


def _row_text(row: dict) -> str:
    state = row["state"]
    if state in ("done", "empty"):
        cost = "n/a" if row.get("cost_usd") is None else f"{row['cost_usd']:.2f}"
        return f"{row['day']} {state} attempt {row.get('attempt')} cost {cost}"
    if row.get("reason"):
        return (
            f"{row['day']} {state} ({row['reason']}) attempts {row.get('attempts')}/{ATTEMPT_CAP}"
        )
    return f"{row['day']} {state}"


def summary(config: CairnConfig, spec: RoutineSpec) -> dict:
    state, _log, _plist, _label = _paths(config, spec.name)
    last = read_json(state / "last-run.json") or {}
    today = routine_plan.utc_today()
    pending = sum(
        1
        for day in window(today, spec.catch_up_days)
        if not stamp_path(state, "done", day).is_file()
    )
    failed = sum(
        1 for day in window(today, spec.catch_up_days) if stamp_path(state, "failed", day).is_file()
    )
    return {
        "name": spec.name,
        "peer": spec.peer,
        "profile": spec.profile,
        "at": spec.at,
        "installed": install_state(config, spec.name),
        "last": last.get("finished_at"),
        "exit": last.get("exit"),
        "pending": pending,
        "failed": failed,
    }


def list_routines(config: CairnConfig, as_json: bool) -> int:
    rows = [summary(config, spec) for spec in config.routines.values()]
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no routines configured")
        return 0
    for row in rows:
        print(
            f"{row['name']}  peer={row['peer']} profile={row['profile']} at={row['at']} "
            f"installed={row['installed']} last={row['last'] or 'never'} "
            f"exit={'-' if row['exit'] is None else row['exit']} "
            f"pending={row['pending']} failed={row['failed']}"
        )
    return 0


def _log_tail(log: Path, lines: int) -> list[str]:
    try:
        return log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def status(config: CairnConfig, name: str, as_json: bool, lines: int) -> int:
    spec = _routine(config, name)
    state, log, plist, label = _paths(config, name)
    base = (
        spec.profile
        if spec.profile not in config.invoke.for_peer(spec.peer).profiles
        else config.invoke.for_peer(spec.peer).profiles[spec.profile].base
    )
    rows = day_rows(spec, state, routine_plan.utc_today())
    last_done = next((r for r in rows if r["state"] in ("done", "empty")), None)
    latest = next((r.get("attempt_dir") for r in rows if r.get("attempt_dir")), None)
    report: dict = {
        "routine": name,
        "config": {
            "peer": spec.peer,
            "profile": spec.profile,
            "profile_base": base,
            "model": spec.model,
            "at": spec.at,
            "sources": list(spec.sources),
            "catch_up_days": spec.catch_up_days,
            "timeout_s": spec.timeout_s,
            "budget_usd_per_day": spec.budget_usd_per_day,
        },
        "paths": {
            "prompt_file": str(spec.prompt_file),
            "cd": str(spec.cd),
            "outputs": {key: str(path) for key, path in spec.outputs.items()},
            "state_dir": str(state),
            "log": str(log),
            "plist": str(plist),
            "label": label,
            # Per attempt: prompt.md, prepare.out (prepare's stdout, which the peer
            # reads), invoke-result.json, invoke.stderr, validate.out, validate.err.
            "attempts": str(config.data_root / "routines" / name),
            "latest_prepare_out": str(Path(latest) / "prepare.out") if latest else None,
        },
        "installed": install_state(config, name),
        "last_run": read_json(state / "last-run.json"),
        "days": rows,
        "last_cost_usd": last_done.get("cost_usd") if last_done else None,
        "log_tail": _log_tail(log, lines),
    }
    if as_json:
        print(json.dumps(report, indent=2))
        return 0
    cfg = report["config"]
    print(f"routine {name}")
    print(
        f"  peer {spec.peer}, profile {spec.profile} (base {base}), "
        f"model {spec.model or '(policy default)'}, at {spec.at} local"
    )
    print(
        f"  sources {','.join(spec.sources) or '(none)'}, catch-up {spec.catch_up_days}d, "
        f"timeout {cfg['timeout_s'] or '(policy default)'}, "
        f"budget {cfg['budget_usd_per_day'] or 'none'}"
    )
    print("paths:")
    for key in ("prompt_file", "cd", "state_dir", "log", "plist", "attempts"):
        print(f"  {key:12} = {report['paths'][key]}")
    for key, path in report["paths"]["outputs"].items():
        print(f"  outputs.{key} = {path}")
    if report["paths"]["latest_prepare_out"]:
        print(f"  prepare.out  = {report['paths']['latest_prepare_out']}")
    print(f"installed: {report['installed']}")
    last = report["last_run"]
    if last:
        print(
            f"last run: {last.get('finished_at')} exit {last.get('exit')} "
            f"(done {last.get('done')}, empty {last.get('empty')}, failed {last.get('failed')}, "
            f"deferred {last.get('deferred')})"
            + (f" error: {last['error']}" if last.get("error") else "")
        )
    else:
        print("last run: never")
    print("days:")
    for row in rows:
        print(f"  {_row_text(row)}")
    cost = report["last_cost_usd"]
    print(f"last cost: {'n/a' if cost is None else f'{cost:.2f}'}")
    print(f"log tail ({log}):")
    for line in report["log_tail"]:
        print(f"  {line}")
    return 0


# ---------------------------------------------------------------------------
# doctor (8.2, 8.3)
# ---------------------------------------------------------------------------


def doctor_checks(config: CairnConfig) -> list[tuple[str, str, str]]:
    """``(level, label, detail)`` lines for ``cairn doctor``; level ``ok`` or ``warn``."""
    lines: list[tuple[str, str, str]] = []
    today = routine_plan.utc_today()
    for name, spec in config.routines.items():
        label = f"routine [{name}]"
        state, _log, _plist, launchd_label = _paths(config, name)
        installed = install_state(config, name)
        if installed == "n/a":
            lines.append(("warn", f"{label} launchd", "launchd only; not installed on this OS"))
        elif installed == "yes":
            lines.append(
                ("ok", f"{label} launchd", f"{launchd_label} loaded, daily at {spec.at} local")
            )
        elif installed == "unloaded":
            lines.append(
                (
                    "warn",
                    f"{label} launchd",
                    f"plist present but not loaded; run 'cairn routine install {name}'",
                )
            )
        else:
            lines.append(
                ("warn", f"{label} launchd", f"not installed; run 'cairn routine install {name}'")
            )

        last = read_json(state / "last-run.json")
        if last is None:
            lines.append(("warn", f"{label} last run", "never ran"))
        else:
            head = f"{last.get('finished_at')} exit {last.get('exit')}"
            if last.get("exit") == 0:
                detail = (
                    f"{head} (done {last.get('done', 0)}, empty {last.get('empty', 0)}, "
                    f"failed {last.get('failed', 0)})"
                )
                lines.append(("ok", f"{label} last run", detail))
            elif last.get("error"):
                lines.append(("warn", f"{label} last run", f"{head} ({last['error']})"))
            else:
                failures = ", ".join(
                    f"{f.get('day')} {f.get('reason')}" for f in last.get("failures") or []
                )
                detail = f"{head} (failed {last.get('failed', 0)}" + (
                    f": {failures})" if failures else ")"
                )
                lines.append(("warn", f"{label} last run", detail))

        days = window(today, spec.catch_up_days)
        pending, failed = [], []
        for day in days:
            if stamp_path(state, "done", day).is_file():
                continue
            stamp = read_json(stamp_path(state, "failed", day))
            if stamp is None:
                pending.append(day.isoformat())
            else:
                failed.append(
                    f"{day.isoformat()} ({stamp.get('reason')}, "
                    f"{stamp.get('attempts', 0)}/{ATTEMPT_CAP} attempts)"
                )
        if not pending and not failed:
            span = f"{days[0].isoformat()}..{days[-1].isoformat()}"
            lines.append(("ok", f"{label} stamps", f"window {span} all done"))
        else:
            parts = []
            if pending:
                parts.append(f"pending {', '.join(pending)}")
            if failed:
                parts.append(f"failed {', '.join(failed)}")
            lines.append(("warn", f"{label} stamps", "; ".join(parts)))

        journal = spec.outputs.get("journal_dir")
        if journal is not None:
            lines.append(_journal_line(config, label, journal))
    return lines


def _journal_line(config: CairnConfig, label: str, journal: Path) -> tuple[str, str, str]:
    expected = config.data_root / "journal"
    where = f"{label} journal"
    if journal.resolve() != expected.resolve():
        return (
            "warn",
            where,
            f"journal_dir {journal} is outside {expected}; qmd does not index it through cairn",
        )
    collection = config.collections.get("journal")
    if collection is None:
        return (
            "warn",
            where,
            f"journal_dir {journal} has no 'journal' collection in config; "
            "add collections.journal to index it",
        )
    if not collection.sync.enabled:
        return (
            "warn",
            where,
            f"journal_dir {journal}: the 'journal' collection is disabled (sync.enabled=false)",
        )
    return ("ok", where, f"{journal} is the 'journal' collection")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"day must be YYYY-MM-DD, got {raw!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn routine", description="Scheduled peer runs (launchd, macOS)"
    )
    sub = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    p = sub.add_parser("install", help="add or replace the launchd job (macOS)")
    p.add_argument("name", metavar="NAME")
    p = sub.add_parser("run", help="run every due day now")
    p.add_argument("name", metavar="NAME")
    p.add_argument("--day", type=_day, help="run only this UTC day (YYYY-MM-DD)")
    p.add_argument("--force", action="store_true", help="run even a done or capped day")
    p.add_argument("--dry-run", action="store_true", help="print what would run; run nothing")
    p = sub.add_parser("list", help="every configured routine")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status", help="config, paths, stamps, last run, cost, log tail")
    p.add_argument("name", metavar="NAME")
    p.add_argument("--json", action="store_true")
    p.add_argument("--lines", type=int, default=20, help="log tail lines (default 20)")
    p = sub.add_parser("uninstall", help="remove the launchd job; stamps are kept")
    p.add_argument("name", metavar="NAME")
    p = sub.add_parser("kickstart", help="ask launchd to start the job now")
    p.add_argument("name", metavar="NAME")
    p.add_argument(
        "--force-restart",
        action="store_true",
        help="kill a running instance first (launchctl kickstart -k; its spend is lost)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = get_config()
        if args.action == "install":
            return install(config, args.name)
        if args.action == "run":
            from .routine_run import run_routine

            spec = _routine(config, args.name)
            return run_routine(config, spec, day=args.day, force=args.force, dry_run=args.dry_run)
        if args.action == "list":
            return list_routines(config, args.json)
        if args.action == "status":
            return status(config, args.name, args.json, args.lines)
        if args.action == "uninstall":
            return uninstall(config, args.name)
        return kickstart(config, args.name, args.force_restart)
    except (RoutineError, ConfigError) as exc:
        print(f"cairn routine: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "doctor_checks",
    "environment_block",
    "install",
    "kickstart",
    "launchctl",
    "main",
    "render_plist",
    "status",
    "uninstall",
]
