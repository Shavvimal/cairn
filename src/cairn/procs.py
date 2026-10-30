"""Bounded child processes whose whole process tree dies with the timeout.

``subprocess.run(timeout=...)`` kills only the direct child. The ``qmd`` on PATH is
usually a Node launcher that starts ``qmd.js`` as a grandchild, and ``python -m
cairn`` exports start their own ``qmd`` children, so a timed-out step could leave
work running outside the lock that was meant to cover it. :func:`run_capped`
starts the child in a new session (its own process group) and signals the whole
group on timeout or interruption.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence

KILL_GRACE_S = 5.0  # SIGTERM -> SIGKILL grace for a timed-out process group


class ProcessCleanupError(RuntimeError):
    """The child's process-group leader was not reaped within the bound after SIGKILL."""


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def kill_group(process: subprocess.Popen[str], grace_s: float = KILL_GRACE_S) -> None:
    """SIGTERM the child's process group, wait ``grace_s``, then SIGKILL the group.

    SIGKILL is sent even when the leader exited on SIGTERM: a grandchild can
    ignore SIGTERM or outlive its parent, and only the group signal reaches it.
    """
    _signal_group(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(grace_s)
    _signal_group(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(grace_s)


def _text(value: str | bytes | None) -> str:
    """Partial output from ``TimeoutExpired`` is bytes even in text mode."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def finish(process: subprocess.Popen[str], grace_s: float = KILL_GRACE_S) -> None:
    """Close the pipes and reap the leader with a bounded wait.

    Raises :class:`ProcessCleanupError` ("cleanup incomplete") when the leader is
    still not reaped after ``grace_s``, instead of waiting without a bound.
    """
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    try:
        process.wait(grace_s)
    except subprocess.TimeoutExpired:
        raise ProcessCleanupError(
            f"cleanup incomplete: pid {process.pid} was not reaped {grace_s:g}s after SIGKILL"
        ) from None


def run_capped(
    command: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with captured text output; kill its process group on timeout.

    ``input`` is written to the child's stdin, which is then closed; without it the
    child's stdin is ``/dev/null``.

    Raises :class:`subprocess.TimeoutExpired` (carrying the partial stdout and
    stderr) after the group is killed. Any other interruption of the wait
    (``KeyboardInterrupt`` included) also kills the group before it propagates,
    because a child in its own session no longer receives the terminal's SIGINT.
    Every wait is bounded; see :func:`finish`.
    """
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=dict(env) if env is not None else None,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as first:
        out, err = _text(first.stdout), _text(first.stderr)
        kill_group(process)
        # A descendant that escaped the group (setsid) can keep the pipes open, so
        # the final read is bounded too. Its exception carries everything read so
        # far, which supersedes the first partial output.
        try:
            out, err = process.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired as second:
            out, err = _text(second.stdout) or out, _text(second.stderr) or err
        except (ValueError, OSError):
            pass
        finish(process)
        raise subprocess.TimeoutExpired(list(command), timeout, output=out, stderr=err) from None
    except BaseException:
        kill_group(process)
        with contextlib.suppress(ProcessCleanupError):
            finish(process)
        raise
    finish(process)
    return subprocess.CompletedProcess(list(command), process.returncode, out, err)


__all__ = ["KILL_GRACE_S", "ProcessCleanupError", "finish", "kill_group", "run_capped"]
