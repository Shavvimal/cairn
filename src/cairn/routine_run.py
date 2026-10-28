"""``cairn routine run``: the per-day run engine (docs/routine-spec.md sections 3, 6.2, 7).

A run takes the routine's lock without blocking, works out which UTC days still need
work, refreshes the configured session exports once for the whole window, then for
each day (oldest first) renders the prompt, runs ``prepare``, ``cairn invoke`` and
``validate``. Only a ``validate`` exit 0 (done) or 3 (done, empty) writes the day's
done stamp. The first failed day stops the run; the remaining days are deferred.

Every child process goes through :func:`run_command`, which the tests replace.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__, routine_plan
from .config import CairnConfig, RoutineSpec
from .exchange import now_iso
from .procs import run_capped
from .routine_plan import (
    ATTEMPT_CAP,
    DEPTH_ENV,
    EXIT_EMPTY,
    UNCOUNTED_REASONS,
    RoutineError,
    attempt_dir,
    child_environment,
    compute_days,
    invoke_argv,
    log_path,
    next_attempt,
    parse_result,
    prior_history,
    read_json,
    refresh_argv,
    refresh_since,
    render_day,
    result_cost,
    spent_on,
    stamp_path,
    state_dir,
    write_json,
)
from .storage import atomic_write, lock_path, locked

PREPARE_TIMEOUT_S = 600
VALIDATE_TIMEOUT_S = 900
REFRESH_TIMEOUT_S = 600
MAX_LOG_BYTES = 5 * 1024 * 1024
STDERR_TAIL_LINES = 20
PREPARE_STDERR_HEAD, PREPARE_STDERR_TAIL = 40, 15
_QUOTE_RE = re.compile(r'[\s="]')


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


def run_command(
    argv: list[str], *, timeout: float | None, env: dict[str, str], cwd: str | None
) -> subprocess.CompletedProcess[str]:
    """Run one child with captured text output.

    ``timeout`` kills the whole process group and raises ``TimeoutExpired``; ``None``
    waits for the child (invoke has its own watchdogs). A missing executable returns
    exit 127, as a shell would.
    """
    try:
        if timeout is None:
            return subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                cwd=cwd,
            )
        return run_capped(argv, timeout=timeout, env=env, cwd=cwd)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{exc}\n")
    except PermissionError as exc:
        return subprocess.CompletedProcess(argv, 126, "", f"{exc}\n")


# ---------------------------------------------------------------------------
# Log (6.2.4)
# ---------------------------------------------------------------------------


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(round(value, 4))
    text = str(value)
    return json.dumps(text) if not text or _QUOTE_RE.search(text) else text


class RunLog:
    """Appends ``<time> routine=<name> run=<id> event=<event> k=v ...`` lines.

    Echoes to stdout only on a TTY: under launchd stdout is this same file.
    """

    def __init__(self, path: Path, routine: str, run_id: str) -> None:
        self.path, self.routine, self.run_id = path, routine, run_id

    def rotate(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_LOG_BYTES:
                self.path.replace(self.path.with_suffix(".log.1"))
        except OSError:
            pass

    def write(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if sys.stdout.isatty():
            print(line)

    def event(self, event: str, **fields: object) -> None:
        pairs = "".join(f" {key}={_format_value(value)}" for key, value in fields.items())
        self.write(f"{now_iso()} routine={self.routine} run={self.run_id} event={event}{pairs}")

    def stderr(self, text: str | None, head: int = 0, tail: int = STDERR_TAIL_LINES) -> None:
        """Log a child's stderr: its last ``tail`` lines, or ``head`` + ``tail`` lines."""
        lines = (text or "").strip().splitlines()
        if head and len(lines) > head + tail + 1:
            lines = [*lines[:head], f"... ({len(lines) - head - tail} more lines)", *lines[-tail:]]
        elif not head:
            lines = lines[-tail:]
        for line in lines:
            self.write(f"      [stderr] {line}")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _as_text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


@dataclass
class Child:
    code: int | None  # None after a timeout
    stdout: str
    stderr: str
    seconds: float


def run_child(argv: list[str], timeout: float | None, env: dict[str, str], cwd: Path) -> Child:
    start = time.monotonic()
    try:
        result = run_command(argv, timeout=timeout, env=env, cwd=str(cwd))
    except subprocess.TimeoutExpired as exc:
        return Child(None, _as_text(exc.stdout), _as_text(exc.stderr), time.monotonic() - start)
    return Child(
        result.returncode, result.stdout or "", result.stderr or "", time.monotonic() - start
    )


def run_id_now() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


class Run:
    """One ``cairn routine run NAME`` under the routine lock."""

    def __init__(self, config: CairnConfig, spec: RoutineSpec) -> None:
        self.config, self.spec = config, spec
        self.sdir = state_dir(config, spec.name)
        self.run_id = run_id_now()
        self.started_at = now_iso()
        self.log = RunLog(log_path(config, spec.name), spec.name, self.run_id)
        self.days: list[date] = []
        self.counts = {"done": 0, "empty": 0, "failed": 0, "deferred": 0}
        self.failures: list[dict] = []
        self.costs: list[float] = []

    # ---- stamps ---------------------------------------------------------

    def _prior_history(self, day: date) -> list[dict]:
        return prior_history(self.sdir, day)

    def spent(self, day: date) -> float:
        return spent_on(self.sdir, day)

    def _cost(self, cost: float | None) -> None:
        if cost is not None:
            self.costs.append(cost)

    def fail_day(
        self,
        day: date,
        attempt: int | None,
        where: Path | None,
        reason: str,
        *,
        cost: float | None = None,
        exits: tuple[int | None, int | None, int | None] = (None, None, None),
    ) -> str:
        """Write the failed stamp (and ``attempt.json``) and log ``day-failed``."""
        history = self._prior_history(day)
        entry: dict = {
            "attempt": attempt,
            "reason": reason,
            "cost_usd": cost,
            "finished_at": now_iso(),
        }
        if reason in UNCOUNTED_REASONS:
            entry["counted"] = False
        history.append(entry)
        attempts = sum(
            1
            for h in history
            if h.get("counted", True) and h.get("reason") not in UNCOUNTED_REASONS
        )
        previous = read_json(stamp_path(self.sdir, "failed", day)) or {}
        total = sum(float(h.get("cost_usd") or 0) for h in history)
        write_json(
            stamp_path(self.sdir, "failed", day),
            {
                "routine": self.spec.name,
                "day": day.isoformat(),
                "status": "failed",
                "reason": reason,
                "attempts": attempts,
                "last_attempt_dir": str(where) if where else previous.get("last_attempt_dir"),
                "prepare_exit": exits[0],
                "invoke_exit": exits[1],
                "validate_exit": exits[2],
                "cost_usd_total": round(total, 6),
                "updated_at": entry["finished_at"],
                "run_id": self.run_id,
                "cairn_version": __version__,
                "history": history,
            },
        )
        if where is not None and where.is_dir():
            write_json(where / "attempt.json", entry)
        self._cost(cost)
        self.counts["failed"] += 1
        self.failures.append({"day": day.isoformat(), "reason": reason})
        self.log.event(
            "day-failed",
            day=day.isoformat(),
            reason=reason,
            attempts=attempts,
            cost_usd=cost,
        )
        return reason

    def done_day(
        self,
        day: date,
        attempt: int,
        where: Path,
        status: str,
        started_at: str,
        *,
        thread_id: str | None = None,
        cost: float | None = None,
        exits: tuple[int | None, int | None, int | None] = (None, None, None),
    ) -> str:
        """Write the done stamp, drop the failed stamp and log ``day-done``."""
        history = self._prior_history(day)
        finished = now_iso()
        entry = {"attempt": attempt, "reason": status, "cost_usd": cost, "finished_at": finished}
        day_total = sum(float(h.get("cost_usd") or 0) for h in history) + (cost or 0)
        write_json(
            stamp_path(self.sdir, "done", day),
            {
                "routine": self.spec.name,
                "day": day.isoformat(),
                "status": status,
                "attempt": attempt,
                "attempt_dir": str(where),
                "peer": self.spec.peer,
                "profile": self.spec.profile,
                "thread_id": thread_id,
                "prepare_exit": exits[0],
                "invoke_exit": exits[1],
                "validate_exit": exits[2],
                "cost_usd": cost,
                "cost_usd_day_total": round(day_total, 6),
                "started_at": started_at,
                "finished_at": finished,
                "run_id": self.run_id,
                "cairn_version": __version__,
                "history": history,
            },
        )
        write_json(where / "attempt.json", entry)
        stamp_path(self.sdir, "failed", day).unlink(missing_ok=True)
        self._cost(cost)
        self.counts[status] += 1
        self.log.event(
            "day-done", day=day.isoformat(), status=status, attempt=attempt, cost_usd=cost
        )
        return status

    # ---- one day ----------------------------------------------------------

    def run_day(self, day: date) -> str:
        """Run one day; returns ``done``, ``empty`` or the failure reason."""
        spec, iso = self.spec, day.isoformat()
        number = next_attempt(self.config, spec.name, day)
        where = attempt_dir(self.config, spec.name, day, number)
        started_at = now_iso()
        self.log.event("day-start", day=iso, attempt=number)
        rendered = render_day(self.config, spec, day, where)  # RoutineError: exit 2
        try:
            for path in (*spec.outputs.values(), where, where / "digest"):
                path.mkdir(parents=True, exist_ok=True)
            atomic_write(where / "prompt.md", rendered.prompt)
            if rendered.profile_args is not None:
                write_json(where / "profile-args.json", rendered.profile_args)
        except OSError as exc:
            self.log.event("error", message=f"{iso}: {exc}")
            return self.fail_day(day, number, None, "render-error")

        remaining = None
        if spec.budget_usd_per_day is not None:
            remaining = spec.budget_usd_per_day - self.spent(day)
            if remaining <= 0:
                return self.fail_day(day, number, where, "budget-exhausted")

        env = child_environment(spec.name, day, where)
        prepare_exit: int | None = None
        if rendered.prepare is not None:
            child = run_child(rendered.prepare, PREPARE_TIMEOUT_S, env, spec.cd)
            # The peer reads the prepare output from this file, whatever the exit.
            atomic_write(where / "prepare.out", child.stdout)
            prepare_exit = child.code
            self.log.event(
                "prepare",
                day=iso,
                exit="timeout" if child.code is None else child.code,
                seconds=round(child.seconds, 1),
            )
            self.log.stderr(child.stderr, head=PREPARE_STDERR_HEAD, tail=PREPARE_STDERR_TAIL)
            if child.code is None:
                return self.fail_day(day, number, where, "prepare-timeout")
            if child.code == EXIT_EMPTY:
                return self.done_day(
                    day, number, where, "empty", started_at, exits=(prepare_exit, None, None)
                )
            if child.code != 0:
                return self.fail_day(
                    day,
                    number,
                    where,
                    f"prepare-exit-{child.code}",
                    exits=(prepare_exit, None, None),
                )

        argv = invoke_argv(
            spec, where, config_profile=rendered.profile_args is not None, remaining_usd=remaining
        )
        child = run_child(argv, None, env, spec.cd)
        atomic_write(where / "invoke-result.json", child.stdout)
        atomic_write(where / "invoke.stderr", child.stderr)
        result = parse_result(child.stdout)
        cost = result_cost(result)
        invoke_exit = child.code if child.code is not None else 1
        self.log.event(
            "invoke",
            day=iso,
            exit=invoke_exit,
            thread=(result or {}).get("thread_id"),
            outcome=(result or {}).get("outcome"),
            cost_usd=cost,
            seconds=round(child.seconds, 1),
        )
        if invoke_exit != 0:
            self.log.stderr(child.stderr)
            return self.fail_day(
                day,
                number,
                where,
                f"invoke-exit-{invoke_exit}",
                cost=cost,
                exits=(prepare_exit, invoke_exit, None),
            )

        child = run_child(rendered.validate, VALIDATE_TIMEOUT_S, env, spec.cd)
        atomic_write(where / "validate.out", child.stdout)
        atomic_write(where / "validate.err", child.stderr)
        verdict = {0: "valid", EXIT_EMPTY: "empty"}.get(
            -1 if child.code is None else child.code, "invalid"
        )
        self.log.event(
            "validate",
            day=iso,
            exit="timeout" if child.code is None else child.code,
            result=verdict,
            seconds=round(child.seconds, 1),
        )
        exits = (prepare_exit, invoke_exit, child.code)
        thread_id = (result or {}).get("thread_id")
        if child.code in (0, EXIT_EMPTY):
            status = "done" if child.code == 0 else "empty"
            return self.done_day(
                day, number, where, status, started_at, thread_id=thread_id, cost=cost, exits=exits
            )
        self.log.stderr(child.stderr)
        reason = "validate-timeout" if child.code is None else f"validate-exit-{child.code}"
        return self.fail_day(day, number, where, reason, cost=cost, exits=exits)

    # ---- the whole run --------------------------------------------------

    def refresh(self, since: str) -> bool:
        env = child_environment()
        for source in self.spec.sources:
            child = run_child(refresh_argv(source, since), REFRESH_TIMEOUT_S, env, self.spec.cd)
            self.log.event(
                "refresh",
                source=source,
                since=since,
                exit="timeout" if child.code is None else child.code,
                seconds=round(child.seconds, 1),
            )
            if child.code != 0:
                self.log.stderr(child.stderr)
                return False
        return True

    def finish(self, code: int, error: str | None = None, *, end_line: bool = True) -> int:
        cost = round(sum(self.costs), 6) if self.costs else None
        write_json(
            self.sdir / "last-run.json",
            {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "finished_at": now_iso(),
                "exit": code,
                "days": [d.isoformat() for d in self.days],
                **self.counts,
                "cost_usd": cost,
                "error": error,
                "failures": self.failures,
            },
        )
        if end_line:
            self.log.event("end", exit=code, **self.counts, cost_usd=cost)
        return code

    def body(self, today: date, day: date | None, force: bool) -> int:
        days, skipped = compute_days(self.spec, self.sdir, today, day, force)
        for skipped_day, reason in skipped:
            self.log.event("day-skipped", day=skipped_day.isoformat(), reason=reason)
        if not days:
            self.log.event("nothing-due")
            return self.finish(0, end_line=False)
        self.days = days
        self.log.event(
            "start",
            days=",".join(d.isoformat() for d in days),
            force=force,
            version=__version__,
        )
        try:
            # A template or input defect fails every day the same way: catch it before
            # the refresh, so it costs no export and no attempt.
            first = days[0]
            render_day(
                self.config,
                self.spec,
                first,
                attempt_dir(
                    self.config,
                    self.spec.name,
                    first,
                    next_attempt(self.config, self.spec.name, first),
                ),
            )
            if self.spec.sources and not self.refresh(refresh_since(days)):
                for pending in days:
                    self.fail_day(pending, None, None, "incomplete-input")
                return self.finish(1)
            for index, current in enumerate(days):
                outcome = self.run_day(current)
                if outcome in ("done", "empty", "budget-exhausted"):
                    continue
                for rest in days[index + 1 :]:
                    self.counts["deferred"] += 1
                    self.log.event("day-deferred", day=rest.isoformat())
                break
        except RoutineError as exc:
            self.log.event("error", message=str(exc))
            print(str(exc), file=sys.stderr)
            return self.finish(2, str(exc))
        return self.finish(1 if self.counts["failed"] or self.counts["deferred"] else 0)


def inside_peer_depth() -> int:
    try:
        return int(os.environ.get(DEPTH_ENV) or 0)
    except ValueError:
        return 1


def run_routine(
    config: CairnConfig,
    spec: RoutineSpec,
    *,
    day: date | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> int:
    """``cairn routine run``: 0 all done/empty (or lock held, or nothing due),
    1 a day failed or was deferred, 2 a usage, config or render error."""
    today = routine_plan.utc_today()
    if day is not None and day >= today:
        raise RoutineError(f"day {day.isoformat()} has not ended in UTC")
    if force and day is None:
        print("warning: --force without --day re-runs every day in the window", file=sys.stderr)
    if dry_run:
        return dry_run_routine(config, spec, today, day, force)
    depth = inside_peer_depth()
    if depth >= 1:
        raise RoutineError(
            f"routine {spec.name}: refusing to run inside a peer ({DEPTH_ENV}={depth}); "
            f"start it from a shell or with 'cairn routine kickstart {spec.name}'"
        )
    run = Run(config, spec)
    run.sdir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(lock_path(run.sdir, "routine"), blocking=False))
        except BlockingIOError:
            run.log.event("skipped-lock")
            if sys.stdout.isatty():
                print(f"routine {spec.name}: skipped, another run holds the lock")
            return 0
        # Rotate under the lock, so a second start never renames the log mid-run.
        run.log.rotate()
        return run.body(today, day, force)


def dry_run_routine(
    config: CairnConfig, spec: RoutineSpec, today: date, day: date | None, force: bool
) -> int:
    """Print what a run would do. No lock, no directories, no stamps, no children."""
    sdir = state_dir(config, spec.name)
    days, skipped = compute_days(spec, sdir, today, day, force)
    for skipped_day, reason in skipped:
        print(f"day {skipped_day.isoformat()} skipped ({reason})")
    if not days:
        print(f"routine {spec.name}: nothing due")
        return 0
    first = days[0]
    render_day(
        config,
        spec,
        first,
        attempt_dir(config, spec.name, first, next_attempt(config, spec.name, first)),
    )
    for source in spec.sources:
        argv = refresh_argv(source, refresh_since(days))
        print(f"refresh: {shlex.join(argv)}   (once, for the whole window)")
    for current in days:
        number = next_attempt(config, spec.name, current)
        where = attempt_dir(config, spec.name, current, number)
        rendered = render_day(config, spec, current, where)
        remaining = None
        if spec.budget_usd_per_day is not None:
            remaining = spec.budget_usd_per_day - spent_on(sdir, current)
        print(f"day {current.isoformat()} attempt {number} (would create {where})")
        print(f"prepare: {shlex.join(rendered.prepare) if rendered.prepare else '(none)'}")
        if remaining is not None and remaining <= 0:
            print("invoke:  (skipped: budget-exhausted)")
        else:
            argv = invoke_argv(
                spec,
                where,
                config_profile=rendered.profile_args is not None,
                remaining_usd=remaining,
            )
            print(f"invoke:  {shlex.join(argv)}")
        if rendered.profile_args is not None:
            print(f"profile args: {shlex.join(rendered.profile_args)}")
        print(f"validate: {shlex.join(rendered.validate)}")
        size = len(rendered.prompt.encode("utf-8"))
        print(f"--- rendered prompt ({size} bytes) ---")
        print(rendered.prompt.rstrip("\n"))
        print("--- end ---")
    return 0


__all__ = [
    "ATTEMPT_CAP",
    "RoutineError",
    "compute_days",
    "dry_run_routine",
    "invoke_argv",
    "refresh_argv",
    "refresh_since",
    "render_day",
    "run_command",
    "run_routine",
]
