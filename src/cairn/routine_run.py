"""``cairn routine run``: the per-day run engine (docs/routine-spec.md sections 3, 6.2, 7).

A run takes the routine's lock without blocking, works out which UTC days still need
work, refreshes the configured session exports once for the whole window, then for
each day (oldest first) renders the prompt, runs ``prepare``, ``cairn invoke`` and
``validate``. Only a ``validate`` exit 0 (done) or 3 (done, empty) writes the day's
done stamp. The first failed day stops the run; the remaining days are deferred.

docs/routine-hardening-spec.md adds the pause and disabled markers, ``running.json``,
the stale-source check, the terminal hook after each stamp, the routine-level breaker
and the run report (``last-run.json`` and ``--json``).

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

from . import __version__, routine_plan, routine_state
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
from .routine_state import SCHEMA_VERSION, STALE_SOURCE_S, parse_time
from .storage import atomic_write, lock_path, locked

HOOK_TIMEOUT_S = 10
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
                errors="replace",
                env=env,
                cwd=cwd,
            )
        return run_capped(argv, timeout=timeout, env=env, cwd=cwd)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{exc}\n")
    except PermissionError as exc:
        return subprocess.CompletedProcess(argv, 126, "", f"{exc}\n")


def run_hook(
    argv: list[str], *, stdin_text: str, env: dict[str, str], cwd: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the terminal hook with :data:`HOOK_TIMEOUT_S` and ``stdin_text`` on stdin.

    A missing executable is exit 127 and a refused one 126; a timeout kills the
    process group and raises ``TimeoutExpired``. The tests replace this function.
    """
    try:
        return run_capped(argv, timeout=HOOK_TIMEOUT_S, env=env, cwd=cwd, input=stdin_text)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{exc}\n")
    except PermissionError as exc:
        return subprocess.CompletedProcess(argv, 126, "", f"{exc}\n")


def counted_attempts(history: list[dict]) -> int:
    """The attempts in ``history`` that count toward :data:`ATTEMPT_CAP`."""
    return sum(
        1
        for h in history
        if h.get("counted", True) and h.get("reason") not in (*UNCOUNTED_REASONS, "done", "empty")
    )


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

    Echoes to stdout only on a TTY (under launchd stdout is this same file), and
    never with ``--json``, whose stdout holds the report alone.
    """

    def __init__(self, path: Path, routine: str, run_id: str, *, echo: bool = True) -> None:
        self.path, self.routine, self.run_id = path, routine, run_id
        self.echo = echo

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
        if self.echo and sys.stdout.isatty():
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

    def __init__(
        self,
        config: CairnConfig,
        spec: RoutineSpec,
        *,
        as_json: bool = False,
        stale_sources: str | None = None,
    ) -> None:
        self.config, self.spec = config, spec
        self.sdir = state_dir(config, spec.name)
        self.run_id = run_id_now()
        self.started_at = now_iso()
        self.as_json = as_json
        self.stale_mode = stale_sources or spec.stale_sources
        self.log = RunLog(log_path(config, spec.name), spec.name, self.run_id, echo=not as_json)
        self.days: list[date] = []
        self.rows: dict[date, dict] = {}
        self.stale: list[dict] = []
        self.breaker = {
            "consecutive_failed_runs": routine_state.breaker_count(self.sdir),
            "disabled": (self.sdir / routine_state.DISABLED_FILE).exists(),
        }
        self.counts = {"done": 0, "empty": 0, "failed": 0, "deferred": 0}
        self.failures: list[dict] = []
        self.costs: list[float] = []
        # History of a done day that a forced re-run superseded (its stamp is gone).
        self._carried: dict[date, list[dict]] = {}

    # ---- stamps ---------------------------------------------------------

    def _prior_history(self, day: date) -> list[dict]:
        if day in self._carried:
            return list(self._carried[day])
        return prior_history(self.sdir, day)

    def spent(self, day: date) -> float:
        """The cost of ``day``'s earlier attempts; a ``null`` cost counts as 0."""
        return sum(float(h.get("cost_usd") or 0) for h in self._prior_history(day))

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
        attempts = counted_attempts(history)
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
        self.hook(self.row(day, "failed", reason, attempt, attempts, cost, where))
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
        self.hook(self.row(day, status, None, attempt, counted_attempts(history), cost, where))
        return status

    # ---- report rows and the terminal hook (hardening spec 4.6, 5) -------

    def row(
        self,
        day: date,
        outcome: str,
        reason: str | None,
        attempt: int | None = None,
        attempts: int | None = None,
        cost: float | None = None,
        where: Path | None = None,
    ) -> dict:
        """Record and return the report row of ``day``."""
        row = {
            "day": day.isoformat(),
            "outcome": outcome,
            "reason": reason,
            "attempt": attempt,
            "attempts": attempts,
            "cost_usd": cost,
            "attempt_dir": str(where) if where else None,
            "hook": None,
        }
        self.rows[day] = row
        return row

    def hook(self, row: dict) -> None:
        """Run ``terminal_hook`` once for a stamp just written; log it, never raise.

        Its result lands in ``row["hook"]`` only: no stamp, count, exit code or
        breaker strike depends on it.
        """
        argv = self.spec.terminal_hook
        if not argv:
            return
        env = child_environment()
        env.update(
            CAIRN_ROUTINE=self.spec.name,
            CAIRN_ROUTINE_DAY=row["day"],
            CAIRN_ROUTINE_ATTEMPT_DIR=row["attempt_dir"] or "",
            CAIRN_ROUTINE_OUTCOME=row["outcome"],
            CAIRN_ROUTINE_REASON=row["reason"] or "",
            CAIRN_ROUTINE_ATTEMPT="" if row["attempt"] is None else str(row["attempt"]),
            CAIRN_ROUTINE_RUN_ID=self.run_id,
        )
        payload = json.dumps({**row, "routine": self.spec.name, "run_id": self.run_id})
        start = time.monotonic()
        code: int | str
        try:
            result = run_hook(list(argv), stdin_text=payload + "\n", env=env, cwd=str(self.spec.cd))
            code = "missing" if result.returncode == 127 else result.returncode
            stderr = result.stderr or ""
        except subprocess.TimeoutExpired as exc:
            code, stderr = "timeout", _as_text(exc.stderr)
        except Exception as exc:  # a hook must never break the run
            code, stderr = "error", f"{type(exc).__name__}: {exc}"
        seconds = round(time.monotonic() - start, 1)
        row["hook"] = {"exit": code, "seconds": seconds}
        self.log.event("hook", day=row["day"], outcome=row["outcome"], exit=code, seconds=seconds)
        if code != 0:
            self.log.stderr(stderr)

    # ---- one day ----------------------------------------------------------

    def run_day(self, day: date) -> str:
        """Run one day; returns ``done``, ``empty`` or the failure reason."""
        spec, iso = self.spec, day.isoformat()
        number = next_attempt(self.config, spec.name, day)
        where = attempt_dir(self.config, spec.name, day, number)
        started_at = now_iso()
        self.log.event("day-start", day=iso, attempt=number)
        done = stamp_path(self.sdir, "done", day)
        if done.is_file():
            # A forced re-run of a done day: the old completion no longer describes the
            # current output, so it moves into history until this attempt validates.
            self._carried[day] = prior_history(self.sdir, day)
            done.unlink()
            self.log.event("day-superseded", day=iso, attempt=number)
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

    def stale_sources(self) -> list[dict]:
        """Digest sources outside ``spec.sources`` last exported over 24 h ago (4.5)."""
        from . import export_receipt
        from .catalog import catalog_path
        from .digest import DIGEST_SOURCES
        from .registry import BY_NAME

        now = datetime.now(UTC)
        stale: list[dict] = []
        for source in DIGEST_SOURCES:
            collection = BY_NAME[source].collection
            policy = self.config.collections.get(collection)
            if source in self.spec.sources or policy is None or not policy.sync.enabled:
                continue
            last: datetime | None = None
            basis = "none"
            output_dir = self.config.output_dir(collection)
            receipt = export_receipt.read(source, output_dir)
            if receipt is not None:
                last, basis = parse_time(receipt.get("last_ok_at")), "receipt"
            else:
                try:
                    mtime = catalog_path(output_dir).stat().st_mtime
                except OSError:
                    pass
                else:
                    last, basis = datetime.fromtimestamp(mtime, UTC), "catalog-mtime"
            age_s = None if last is None else max((now - last).total_seconds(), 0.0)
            if age_s is not None and age_s <= STALE_SOURCE_S:
                continue
            entry = {
                "source": source,
                "last_export_at": None if last is None else last.isoformat(timespec="seconds"),
                "age_h": None if age_s is None else round(age_s / 3600, 1),
                "basis": basis,
            }
            stale.append(entry)
            where = f"routine {self.spec.name}: source {source}"
            fix = f"add it to routines.{self.spec.name}.sources or run 'cairn sync'"
            if last is None:
                print(f"warning: {where} has no export record; {fix}", file=sys.stderr)
            else:
                print(
                    f"warning: {where} was last exported {entry['last_export_at']} "
                    f"({entry['age_h']} h ago); {fix}",
                    file=sys.stderr,
                )
            self.log.event("stale-source", source=source, age_h=entry["age_h"], basis=basis)
        return stale

    def status(self, code: int) -> str:
        """``failed``, ``partial``, ``ok`` or ``clean`` for a finished run (5)."""
        good = self.counts["done"] + self.counts["empty"]
        bad = self.counts["failed"] + self.counts["deferred"]
        if code == 2:
            return "failed"
        if not self.days:
            return "clean"
        if good and bad:
            return "partial"
        return "failed" if bad else "ok"

    def update_breaker(self, status: str, finished_at: str) -> None:
        """Reset on a done or empty day; strike on a failed run with a counted failure."""
        if self.counts["done"] + self.counts["empty"]:
            routine_state.reset_breaker(self.sdir)
        elif status == "failed":
            counted = [f for f in self.failures if f["reason"] not in UNCOUNTED_REASONS]
            if counted:
                marker = routine_state.strike(
                    self.sdir, self.run_id, finished_at, counted[0]["reason"]
                )
                if marker is not None:
                    self.log.event("disabled", reason=marker["reason"])
                    print(
                        routine_state.skip_line(self.spec.name, "disabled", marker),
                        file=sys.stderr,
                    )
        self.breaker = {
            "consecutive_failed_runs": routine_state.breaker_count(self.sdir),
            "disabled": (self.sdir / routine_state.DISABLED_FILE).exists(),
        }

    def report(
        self,
        code: int,
        status: str,
        finished_at: str,
        *,
        reason: str | None = None,
        error: str | None = None,
    ) -> dict:
        """The run report: ``last-run.json`` and the ``--json`` output (5)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "routine": self.spec.name,
            "status": status,
            "reason": reason,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "exit": code,
            "days": [d.isoformat() for d in self.days],
            **self.counts,
            "cost_usd": round(sum(self.costs), 6) if self.costs else None,
            "error": error,
            "failures": self.failures,
            "stale_sources": self.stale,
            "breaker": self.breaker,
            "rows": [self.rows[day] for day in sorted(self.rows)],
        }

    def emit(self, report: dict) -> None:
        if self.as_json:
            print(json.dumps(report, indent=2))

    def skipped(self, reason: str) -> int:
        """A run that did nothing (lock held, paused or disabled): exit 0, and
        ``last-run.json`` keeps the last real run."""
        self.emit(self.report(0, "skipped", now_iso(), reason=reason))
        return 0

    def finish(self, code: int, error: str | None = None, *, end_line: bool = True) -> int:
        finished = now_iso()
        status = self.status(code)
        self.update_breaker(status, finished)
        report = self.report(code, status, finished, error=error)
        write_json(self.sdir / routine_state.LAST_RUN_FILE, report)
        if end_line:
            self.log.event("end", exit=code, **self.counts, cost_usd=report["cost_usd"])
        self.emit(report)
        return code

    def body(self, today: date, day: date | None, force: bool) -> int:
        days, skipped = compute_days(self.spec, self.sdir, today, day, force)
        for skipped_day, reason in skipped:
            self.log.event("day-skipped", day=skipped_day.isoformat(), reason=reason)
            self.row(skipped_day, "skipped", reason)
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
            if self.stale_mode != "ignore":
                self.stale = self.stale_sources()
                if self.stale and self.stale_mode == "refuse":
                    for pending in days:
                        self.fail_day(pending, None, None, "stale-source")
                    return self.finish(1)
            for index, current in enumerate(days):
                outcome = self.run_day(current)
                if outcome in ("done", "empty", "budget-exhausted"):
                    continue
                for rest in days[index + 1 :]:
                    self.counts["deferred"] += 1
                    self.log.event("day-deferred", day=rest.isoformat())
                    self.row(rest, "deferred", None)
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
    as_json: bool = False,
    stale_sources: str | None = None,
) -> int:
    """``cairn routine run``: 0 all done/empty (or lock held, paused, disabled, or
    nothing due), 1 a day failed or was deferred, 2 a usage, config or render error."""
    today = routine_plan.utc_today()
    if day is not None and day >= today:
        raise RoutineError(f"day {day.isoformat()} has not ended in UTC")
    if dry_run and as_json:
        raise RoutineError("--json does not combine with --dry-run")
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
    run = Run(config, spec, as_json=as_json, stale_sources=stale_sources)
    blocked = skip_on_marker(run)
    if blocked is not None:
        return blocked
    run.sdir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(lock_path(run.sdir, "routine"), blocking=False))
        except BlockingIOError:
            run.log.event("skipped-lock")
            if sys.stdout.isatty() and not as_json:
                print(f"routine {spec.name}: skipped, another run holds the lock")
            return run.skipped("lock-held")
        # Check again under the lock: the run that held it may have just tripped the
        # breaker, or a pause may have landed between the first check and the lock.
        blocked = skip_on_marker(run)
        if blocked is not None:
            return blocked
        # Rotate under the lock, so a second start never renames the log mid-run.
        run.log.rotate()
        running = routine_state.write_running(run.sdir, run.run_id, run.started_at)
        try:
            code = run.body(today, day, force)
        except BaseException as exc:
            # Keep the evidence: status must read interrupted-run, not the previous run.
            routine_state.mark_interrupted(running, type(exc).__name__)
            raise
        running.unlink(missing_ok=True)
        return code


def skip_on_marker(run: Run) -> int | None:
    """Skip the run when a pause or disabled marker exists; ``None`` when none does.

    --force does not bypass a marker: 'cairn routine resume' is the one way out.
    """
    found = routine_state.blocking_marker(run.sdir)
    if found is None:
        return None
    kind, marker = found
    run.log.event(f"skipped-{kind}", reason=marker.get("reason") or routine_state.UNREADABLE)
    print(routine_state.skip_line(run.spec.name, kind, marker), file=sys.stderr)
    return run.skipped(kind)


def dry_run_routine(
    config: CairnConfig, spec: RoutineSpec, today: date, day: date | None, force: bool
) -> int:
    """Print what a run would do. No lock, no directories, no stamps, no children."""
    sdir = state_dir(config, spec.name)
    found = routine_state.blocking_marker(sdir)
    if found is not None:
        print(routine_state.skip_line(spec.name, *found))
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
    "run_hook",
    "run_routine",
]
