#!/usr/bin/env python3
"""Orchestrate context-collection sync and QMD indexing (``cairn sync``).

Each source runs as its own subprocess via ``python -m cairn <source> export`` -
``sys.executable`` and ``-m`` need no PATH lookup, so this works identically under
cron's minimal PATH, the SessionEnd hook, and a manual run. Per-source isolation
(timeouts, independent ok/FAILED logging) is the whole point: one source failing
never blocks the others or the index refresh.
"""

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import CairnConfig, get_config


def _log_file() -> Path:
    """The sync log path (under the configured ``data_root``)."""
    return get_config().data_root / "logs" / "context-sync.log"


def _export(source: str, *extra: str) -> list[str]:
    """``python -m cairn <source> export …`` - interpreter-relative, PATH-independent."""
    return [sys.executable, "-m", "cairn", source, "export", *extra]


# Static binding from a source's stable CLI identity to the config collection that
# carries its data (store, enable/window/hook policy) and to whether it accepts a
# ``--since`` window. This is *code* - which adapter implements a source - never
# policy: WHETHER and HOW OFTEN each runs is read from the config (see _plan).
#
# ``on_hook_default`` is the engine default for the fast SessionEnd hook, used only
# when the config leaves ``sync.on_hook`` unset; the hook is a Claude Code hook, so
# Claude is the natural default. Any source can opt in via ``"sync": {"on_hook": true}``.
@dataclass(frozen=True)
class _SourceBinding:
    source: str  # cairn CLI verb, e.g. "claude"
    collection: str  # config.collections key, e.g. "claude-code-sessions"
    windowed: bool  # accepts --since (mirrors like service-docs do not)
    on_hook_default: bool


_BINDINGS: tuple[_SourceBinding, ...] = (
    _SourceBinding("claude", "claude-code-sessions", windowed=True, on_hook_default=True),
    _SourceBinding("codex", "codex-sessions", windowed=True, on_hook_default=False),
    _SourceBinding("cursor", "cursor-sessions", windowed=True, on_hook_default=False),
    _SourceBinding("granola", "granola-sessions", windowed=True, on_hook_default=False),
    _SourceBinding("service-docs", "service-docs", windowed=False, on_hook_default=False),
)

# source CLI verb -> config collection key. Consumed by ``cairn config init --enable``
# to flip ``sync.enabled`` on the right collections. Importing this never reads config.
SOURCE_COLLECTIONS: dict[str, str] = {b.source: b.collection for b in _BINDINGS}


def _plan(config: CairnConfig, mode: str) -> list[tuple[str, list[str]]]:
    """Resolve which sources to export for ``mode`` from the *config* (data-driven).

    A source runs only when its collection is present in the config and
    ``sync.enabled`` is true; in ``hook`` mode it additionally needs ``on_hook``
    (config value, else the binding default). Returns ``(collection_name, command)``
    pairs. A source absent from the config is simply not configured → skipped, so
    disabling an integration can never crash sync on a missing store.
    """
    steps: list[tuple[str, list[str]]] = []
    for b in _BINDINGS:
        coll = config.collections.get(b.collection)
        if coll is None or not coll.sync.enabled:
            continue
        if mode == "hook":
            on_hook = b.on_hook_default if coll.sync.on_hook is None else coll.sync.on_hook
            if not on_hook:
                continue
        extra = ("--since", coll.sync.since) if b.windowed else ("-q",)
        steps.append((b.collection, _export(b.source, *extra)))
    return steps


MAX_LOG_BYTES = 5 * 1024 * 1024  # rotate once the log passes ~5 MB


def rotate_log():
    """Keep the log bounded: move the current file aside once it gets large."""
    log_file = _log_file()
    try:
        if log_file.exists() and log_file.stat().st_size > MAX_LOG_BYTES:
            log_file.replace(log_file.with_suffix(".log.1"))
    except OSError:
        pass


def log(msg):
    """Append to the log file; also echo to the terminal when interactive.

    The script owns the log file directly so it is always populated (cron, the
    SessionEnd hook, or a manual run). Under cron stdout is not a TTY, so we do
    NOT print - that avoids the crontab's `2>&1` redirect duplicating every line.
    """
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as f:
        f.write(msg + "\n")
    if sys.stdout.isatty():
        print(msg)


def _log_output(text, prefix="      "):
    """Log a captured command's output, capped head+tail so the log stays sane."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return
    head, tail = 40, 15
    if len(lines) > head + tail + 1:
        lines = [*lines[:head], f"... ({len(lines) - head - tail} more lines)", *lines[-tail:]]
    for line in lines:
        log(prefix + line)


def run_step(name, command, timeout=120):
    """Run a command, log its status AND its output, return success."""
    start = time.time()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - start
        status = "ok" if result.returncode == 0 else "FAILED"
        log(f"  [{name}] {status} ({elapsed:.1f}s)")
        _log_output(result.stdout)
        if result.stderr and result.stderr.strip():
            _log_output(result.stderr, prefix="      [stderr] ")
        return result.returncode == 0
    except subprocess.TimeoutExpired as e:
        log(f"  [{name}] TIMEOUT after {timeout}s")
        partial = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout
        _log_output(partial)
        return False
    except Exception as e:
        log(f"  [{name}] ERROR: {e}")
        return False


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="cairn sync",
        description="Sync context collections and update the QMD index",
    )
    parser.add_argument(
        "--hook", action="store_true", help="Fast mode for SessionEnd hook (Claude only)"
    )
    parser.add_argument(
        "--cron", action="store_true", help="Full mode for the cron job (all sources)"
    )
    parser.add_argument("--all", action="store_true", help="Everything including a full re-embed")
    args = parser.parse_args(argv)

    config = get_config()
    qmd = config.qmd_binary
    mode = "hook" if args.hook else ("cron" if args.cron else "all")
    rotate_log()
    log(f"\n=== context-sync ({mode}) {datetime.now().isoformat()} ===")

    try:
        # Run exports for the sources this mode enables (resolved from config).
        steps = _plan(config, mode)
        if not steps:
            log("  (no enabled sources for this mode - check 'sync.enabled' in your config)")
        for name, command in steps:
            run_step(name, command)

        # Always update index
        run_step("qmd-update", [qmd, "update"])

        # Embed only in cron/all modes (slow, not needed for hook)
        if mode in ("cron", "all"):
            run_step("qmd-embed", [qmd, "embed"], timeout=600)
    except Exception:
        import traceback

        log("UNCAUGHT ERROR:\n" + traceback.format_exc())

    log("=== done ===")


if __name__ == "__main__":
    main()
