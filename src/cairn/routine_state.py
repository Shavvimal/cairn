"""Routine state from files only: markers, the breaker and the status classifier
(docs/routine-hardening-spec.md sections 4.1 to 4.4).

Everything here reads or writes files in the routine's state directory. Nothing
calls ``launchctl``, a database or the network, so ``cairn routine status`` can
name a state and an exit code while launchd is wedged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, routine_plan
from .config import CairnConfig, RoutineSpec, routine_paths
from .health import identity_alive, process_identity
from .routine_plan import ATTEMPT_CAP, RoutineError, read_json, stamp_path, write_json

SCHEMA_VERSION = 1
# A healthy laptop sees gaps up to about one day plus its sleep: launchd fires a missed
# calendar job once at wake. Two cadences still report one fully missed day.
STALE_AFTER_S = 2 * 86_400
STALE_SOURCE_S = 24 * 3_600  # a digest source older than this is stale
BREAKER_RUNS = 3  # consecutive failed runs (with a counted failure) that disable a routine
REASON_MAX = 200
UNREADABLE = "(unreadable marker)"

PAUSED_FILE = "paused.json"
DISABLED_FILE = "disabled.json"
BREAKER_FILE = "breaker.json"
RUNNING_FILE = "running.json"
LAST_RUN_FILE = "last-run.json"
RESUME_FILES = (PAUSED_FILE, DISABLED_FILE, BREAKER_FILE)

STATES = ("disabled", "paused", "running", "never_run", "stale", "fresh")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_time(value: object) -> datetime | None:
    """An aware ISO-8601 timestamp, or ``None`` when it cannot be parsed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Markers (4.2)
# ---------------------------------------------------------------------------


def read_marker(path: Path) -> dict | None:
    """A marker object; a corrupt marker still counts as present (fail closed)."""
    if not path.exists():
        return None
    return read_json(path) or {"reason": UNREADABLE}


def check_reason(raw: str) -> str:
    reason = raw.strip()
    if not reason or "\n" in reason or "\r" in reason or len(reason) > REASON_MAX:
        raise RoutineError(f"--reason must be non-empty, one line, at most {REASON_MAX} characters")
    return reason


def pause(sdir: Path, reason: str) -> bool:
    """Write ``paused.json``; ``True`` when the routine was already paused.

    A second pause keeps the first ``paused_at`` and replaces the reason.
    """
    path = sdir / PAUSED_FILE
    already = path.exists()
    previous = (read_json(path) or {}) if already else {}
    paused_at = previous.get("paused_at") or now_iso()
    write_json(path, {"reason": reason, "paused_at": paused_at, "cairn_version": __version__})
    return already


def resume(sdir: Path) -> list[str]:
    """Remove the pause and disabled markers and the breaker; the names removed."""
    removed = []
    for name in RESUME_FILES:
        path = sdir / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return removed


def skip_line(name: str, kind: str, marker: dict) -> str:
    """The stderr line a paused or disabled routine prints on every run."""
    reason = marker.get("reason") or UNREADABLE
    if kind == "paused":
        return (
            f"routine {name}: paused since {marker.get('paused_at') or 'unknown'} ({reason}); "
            f"run 'cairn routine resume {name}' to resume"
        )
    return (
        f"routine {name}: disabled since {marker.get('disabled_at') or 'unknown'} ({reason}); "
        f"fix the cause, then run 'cairn routine resume {name}'"
    )


def blocking_marker(sdir: Path) -> tuple[str, dict] | None:
    """``("disabled", marker)`` or ``("paused", marker)``; disabled wins."""
    for kind, filename in (("disabled", DISABLED_FILE), ("paused", PAUSED_FILE)):
        marker = read_marker(sdir / filename)
        if marker is not None:
            return kind, marker
    return None


# ---------------------------------------------------------------------------
# Running marker (4.3)
# ---------------------------------------------------------------------------


def write_running(sdir: Path, run_id: str, started_at: str) -> Path:
    identity = process_identity(os.getpid())
    path = sdir / RUNNING_FILE
    write_json(
        path,
        {
            "pid": os.getpid(),
            "start_token": identity[1] if identity else None,
            "run_id": run_id,
            "started_at": started_at,
        },
    )
    return path


def running_alive(marker: dict) -> bool:
    pid = marker.get("pid")
    token = marker.get("start_token")
    return identity_alive(pid if isinstance(pid, int) else None, token if token else None)


# ---------------------------------------------------------------------------
# Breaker (4.4)
# ---------------------------------------------------------------------------


def breaker_count(sdir: Path) -> int:
    data = read_json(sdir / BREAKER_FILE) or {}
    count = data.get("consecutive_failed_runs")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def reset_breaker(sdir: Path) -> None:
    (sdir / BREAKER_FILE).unlink(missing_ok=True)


def strike(sdir: Path, run_id: str, finished_at: str, reason: str) -> dict | None:
    """Add one failed run to ``breaker.json``; the ``disabled.json`` marker when the
    count reaches :data:`BREAKER_RUNS`, else ``None``."""
    count = breaker_count(sdir) + 1
    runs = list((read_json(sdir / BREAKER_FILE) or {}).get("runs") or [])
    runs = [*runs, {"run_id": run_id, "finished_at": finished_at, "reason": reason}]
    runs = runs[-BREAKER_RUNS:]
    write_json(sdir / BREAKER_FILE, {"consecutive_failed_runs": count, "runs": runs})
    if count < BREAKER_RUNS:
        return None
    reasons = ", ".join(str(run.get("reason")) for run in runs)
    marker = {
        "reason": f"{count} consecutive failed runs: {reasons}",
        "disabled_at": finished_at,
        "runs": runs,
        "cairn_version": __version__,
    }
    write_json(sdir / DISABLED_FILE, marker)
    return marker


# ---------------------------------------------------------------------------
# Classification (4.1)
# ---------------------------------------------------------------------------


@dataclass
class Classification:
    state: str
    attention: list[str] = field(default_factory=list)
    paused: dict | None = None
    disabled: dict | None = None
    last_run_age_s: float | None = None

    @property
    def exit(self) -> int:
        """0 fresh or running, 1 needs attention, 2 paused or disabled."""
        if self.state in ("disabled", "paused"):
            return 2
        if self.state in ("never_run", "stale") or self.attention:
            return 1
        return 0

    def as_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "state": self.state,
            "exit": self.exit,
            "attention": list(self.attention),
            "paused": self.paused,
            "disabled": self.disabled,
            "last_run_age_s": self.last_run_age_s,
            "stale_after_s": STALE_AFTER_S,
        }


def _stamp_attention(spec: RoutineSpec, sdir: Path) -> list[str]:
    found: set[str] = set()
    for day in routine_plan.window(routine_plan.utc_today(), spec.catch_up_days):
        if stamp_path(sdir, "done", day).is_file():
            continue
        failed = stamp_path(sdir, "failed", day)
        if not failed.is_file():
            continue
        stamp = read_json(failed)
        if stamp is None:
            found.add("corrupt-stamp")
        elif routine_plan.attempts_used(stamp) >= ATTEMPT_CAP:
            found.add("attempts-exhausted")
        else:
            found.add("failed-day")
    return [code for code in ("failed-day", "attempts-exhausted", "corrupt-stamp") if code in found]


def classify(
    config: CairnConfig, spec: RoutineSpec, now: datetime | None = None, *, macos: bool
) -> Classification:
    """Name the routine's state (first match wins) and every attention code."""
    now = now or datetime.now(UTC)
    paths = routine_paths(config, spec.name)
    sdir = Path(paths["state_dir"])
    attention = _stamp_attention(spec, sdir)

    last_path = sdir / LAST_RUN_FILE
    last = read_json(last_path)
    age: float | None = None
    if last is None and last_path.exists():
        attention.append("last-run-exit-unknown")
    elif last is not None:
        if last.get("exit") != 0:
            attention.append(f"last-run-exit-{last.get('exit')}")
        finished = parse_time(last.get("finished_at"))
        if finished is not None:
            age = (now - finished).total_seconds()
        else:
            try:
                age = now.timestamp() - last_path.stat().st_mtime
            except OSError:
                age = None
        if age is not None:
            age = round(max(age, 0.0), 1)  # a clock skew never reads as stale

    running = read_marker(sdir / RUNNING_FILE)
    alive = running is not None and running_alive(running)
    if running is not None and not alive:
        attention.append("interrupted-run")
    if macos and not Path(paths["plist"]).is_file():
        attention.append("launchd-missing")
    if last is not None and last.get("stale_sources"):
        attention.append("stale-source")

    disabled = read_marker(sdir / DISABLED_FILE)
    paused = read_marker(sdir / PAUSED_FILE)
    if disabled is not None:
        state = "disabled"
    elif paused is not None:
        state = "paused"
    elif alive:
        state = "running"
    elif last is None:
        state = "never_run"
    elif age is not None and age > STALE_AFTER_S:
        state = "stale"
    else:
        state = "fresh"
    return Classification(state, attention, paused, disabled, age)


__all__ = [
    "BREAKER_RUNS",
    "SCHEMA_VERSION",
    "STALE_AFTER_S",
    "STALE_SOURCE_S",
    "Classification",
    "blocking_marker",
    "check_reason",
    "classify",
    "pause",
    "resume",
    "skip_line",
    "strike",
]
