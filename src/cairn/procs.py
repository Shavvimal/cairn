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


def run_capped(
    command: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with captured text output; kill its process group on timeout.

    Raises :class:`subprocess.TimeoutExpired` (carrying the partial output) after
    the group is killed. Any other interruption of the wait (``KeyboardInterrupt``
    included) also kills the group before it propagates, because a child in its
    own session no longer receives the terminal's SIGINT.
    """
    # The context manager closes the pipes and reaps the leader on every exit path.
    with subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        start_new_session=True,
    ) as process:
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(process)
            out = err = ""
            # A descendant that escaped the group can still hold the pipes open; never
            # wait on it indefinitely. Partial output read so far is returned here.
            with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
                out, err = process.communicate(timeout=KILL_GRACE_S)
            raise subprocess.TimeoutExpired(
                list(command), timeout, output=out, stderr=err
            ) from None
        except BaseException:
            kill_group(process)
            raise
    return subprocess.CompletedProcess(list(command), process.returncode, out, err)


__all__ = ["KILL_GRACE_S", "kill_group", "run_capped"]
