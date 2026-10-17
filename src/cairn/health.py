"""Health of peer runs and subagents on three separate axes (plan D2, D20, D21).

* **Execution** - what the processes are doing: ``pending`` (round allocated, peer
  not spawned), ``running`` (peer alive), ``orphaned`` (peer alive, runner dead),
  ``exited`` (round finalised) or ``unknown`` (nothing alive and nothing on disk
  says how the round ended).
* **Activity** - an observation with an age, never terminal: ``active`` (heartbeat
  younger than ``quiet_after_s``), ``quiet`` (younger than ``stall_after_s``),
  ``silent`` (older), or ``none`` when nothing runs.
* **Outcome** - set once when the round ends: ``succeeded``, ``failed``,
  ``timeout``, ``idle_timeout``, ``cancelled``, ``aborted`` or ``unknown``.

Liveness never trusts a bare pid: a process identity is the pid plus its start
time, so a reused pid can never fake a live peer. :func:`classify` is pure; the
callers gather liveness and heartbeat facts and pass them in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

EXECUTION_STATES = ("pending", "running", "orphaned", "exited", "unknown")
ACTIVITY_STATES = ("active", "quiet", "silent", "none")
OUTCOMES = ("succeeded", "failed", "timeout", "idle_timeout", "cancelled", "aborted", "unknown")


class Thresholds(Protocol):
    """The two activity thresholds (a :class:`cairn.config.PeerPolicy` satisfies it)."""

    @property
    def quiet_after_s(self) -> int: ...
    @property
    def stall_after_s(self) -> int: ...


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------


def parse_linux_stat(stat: str, boot_id: str) -> str | None:
    """The start token from a ``/proc/<pid>/stat`` line, or ``None`` for a zombie.

    The token is ``<btime>:<starttime ticks>``: raw clock ticks (no rounding to
    seconds, so a pid reused within the same second still differs) plus the boot
    identity, so a reboot can never make an old record match.
    """
    # Field 2 (comm) may hold spaces and parentheses; split after its closing paren.
    fields = stat[stat.rfind(")") + 2 :].split()
    # After comm the fields start at field 3 (state); field 22 (starttime) is index 19.
    if len(fields) < 20 or fields[0] in ("Z", "X", "x"):
        return None
    if not fields[19].isdigit():
        return None
    return f"{boot_id}:{fields[19]}"


def _linux_start_token(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        boot = next(
            line.split()[1]
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    except (OSError, StopIteration, IndexError):
        return None
    return parse_linux_stat(stat, boot)


def parse_ps_line(line: str) -> str | None:
    """The start token from ``ps -o stat=,lstart=`` output, or ``None`` for a zombie."""
    parts = line.split()
    if len(parts) < 2 or parts[0].startswith("Z"):
        return None
    text = " ".join(parts[1:])
    try:
        return str(int(datetime.strptime(text, "%a %b %d %H:%M:%S %Y").timestamp()))
    except ValueError:
        return text  # an unexpected format is still a stable token for this pid


def _ps_start_token(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    return parse_ps_line(result.stdout.strip())


def process_identity(pid: int | None) -> tuple[int, str] | None:
    """``(pid, start_token)`` for a live process, else ``None``.

    The token is the process start time (``/proc`` ticks plus boot time on Linux,
    ``ps -o lstart=`` seconds elsewhere), stable for the life of the process and
    different for a later process that reuses the pid. A zombie (exited, not yet
    reaped) is not alive: it passes ``kill(pid, 0)`` but will never write again.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by someone else
    token = _linux_start_token(pid) if sys.platform.startswith("linux") else _ps_start_token(pid)
    return (pid, token) if token is not None else None


def identity_alive(pid: int | None, start_token: str | None) -> bool:
    """Whether the process recorded as ``(pid, start_token)`` is still the one running."""
    if pid is None or start_token is None:
        return False
    identity = process_identity(pid)
    return identity is not None and identity[1] == start_token


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heartbeat:
    """The newest sign of life: epoch seconds, where it came from, the last event."""

    latest: float | None
    source: str
    last_event: str | None


def _mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def parse_iso(value: object) -> float | None:
    """Epoch seconds for an ISO-8601 string, else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def heartbeat(
    events_path: Path | None,
    transcript_path: Path | None,
    recorded_at: str | None,
    last_event: str | None = None,
) -> Heartbeat:
    """The newest of the events file mtime, the native transcript mtime and the
    ``heartbeat_at`` the runner recorded."""
    candidates = [
        ("events", _mtime(events_path)),
        ("transcript", _mtime(transcript_path)),
        ("thread.json", parse_iso(recorded_at)),
    ]
    present = [(stamp, source) for source, stamp in candidates if stamp is not None]
    if not present:
        return Heartbeat(None, "none", last_event)
    stamp, source = max(present)
    return Heartbeat(stamp, source, last_event)


def activity_of(age_s: float | None, thresholds: Thresholds) -> str:
    """``active`` / ``quiet`` / ``silent`` for a heartbeat age; ``none`` without one."""
    if age_s is None:
        return "none"
    if age_s < thresholds.quiet_after_s:
        return "active"
    if age_s < thresholds.stall_after_s:
        return "quiet"
    return "silent"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    execution: str
    activity: str
    activity_age_s: float | None
    outcome: str | None


def classify(
    round_: dict,
    now: float,
    policy: Thresholds,
    *,
    runner_alive: bool,
    peer_alive: bool,
    heartbeat_at: float | None = None,
    done_status: str | None = None,
) -> Health:
    """The three axes for one round record (a ``thread.json`` round).

    ``runner_alive`` / ``peer_alive`` come from :func:`identity_alive`;
    ``heartbeat_at`` from :func:`heartbeat`; ``done_status`` is the status of the
    last ``done`` event in the round's events file (``None`` when there is none).
    A round with ``finished_at`` is ``exited`` with its recorded outcome. With no
    live identity, a ``done`` event makes the round ``exited`` with that status
    (a reader still has to adopt it); without one it is ``unknown``.
    """
    if round_.get("finished_at"):
        return Health("exited", "none", None, round_.get("outcome") or "unknown")
    started = parse_iso(round_.get("started_at"))
    beat = heartbeat_at if heartbeat_at is not None else started
    age = max(0.0, now - beat) if beat is not None else None
    if peer_alive:
        execution = "running" if runner_alive else "orphaned"
        return Health(execution, activity_of(age, policy), age, None)
    if runner_alive:
        # Not spawned yet, or the peer exited and the runner is finalising.
        execution = "running" if round_.get("peer_pid") else "pending"
        return Health(execution, activity_of(age, policy), age, None)
    if done_status:
        return Health("exited", "none", None, done_status)
    return Health("unknown", "none", None, "unknown")


__all__ = [
    "ACTIVITY_STATES",
    "EXECUTION_STATES",
    "OUTCOMES",
    "Health",
    "Heartbeat",
    "activity_of",
    "classify",
    "heartbeat",
    "identity_alive",
    "parse_iso",
    "parse_linux_stat",
    "parse_ps_line",
    "process_identity",
]
