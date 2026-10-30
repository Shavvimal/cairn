#!/usr/bin/env python3
"""Orchestrate context-collection sync and QMD indexing (``cairn sync``).

Each source runs as its own subprocess via ``python -m cairn <source> export`` -
``sys.executable`` and ``-m`` need no PATH lookup, so this works identically under
cron's minimal PATH, the SessionEnd hook, and a manual run. Per-source isolation
(timeouts, independent ok/FAILED logging) is the whole point: one source failing
never blocks the others or the index refresh.

One sync per ``data_root`` runs at a time. A second ``cairn sync`` finds the sync
lock held, records its mode durably under ``<data_root>/sync-requests/<mode>``,
tries the lock once more (the holder may have just released it), and otherwise
logs one ``skipped`` line and exits 0. Before releasing the lock, the holder
consumes those requests and runs, once, the stages a wider request needs that its
own run did not do (modes widen hook < cron < all). Requests that arrive during
that follow-up pass stay on disk for the next invocation, so the holder never
loops. Lock order, outermost first::

    sync (this module) > hook worker > export operation > qmd index (locked_index)

Only ``cairn sync`` takes the sync lock, always first and never blocking, so it
cannot join a cycle. The hook worker, ``hooks.export_and_index`` and ``cairn
invoke`` never take it; they meet a running sync only at the qmd index lock, which
they take without blocking (bounded retry, then a busy/pending outcome) or, for
the durable hook worker, wait on by design.
"""

import argparse
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from .config import CairnConfig, get_config
from .procs import run_capped
from .qmd import embed_failures, ensure_collection, index_status, list_collections, locked_index
from .registry import SOURCE_COLLECTIONS as SOURCE_COLLECTIONS
from .registry import SOURCES as _BINDINGS
from .storage import atomic_write, lock_path, locked

EMBED_TIMEOUT_S = 3600
MODE_RANK = {"hook": 0, "cron": 1, "all": 2}


def _log_file() -> Path:
    """The sync log path (under the configured ``data_root``)."""
    return get_config().data_root / "logs" / "context-sync.log"


def _export(source: str, *extra: str) -> list[str]:
    """``python -m cairn <source> export …`` - interpreter-relative, PATH-independent."""
    return [sys.executable, "-m", "cairn", source, "export", *extra]


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


def _as_text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


def _log_timeout_output(error: subprocess.TimeoutExpired) -> None:
    """Log the partial stdout and stderr a timed-out command left behind."""
    _log_output(_as_text(error.stdout))
    stderr = _as_text(error.stderr)
    if stderr.strip():
        _log_output(stderr, prefix="      [stderr] ")


def run_step(name, command, timeout=120):
    """Run a command, log its status AND its output, return success.

    A timeout kills the command's whole process group (see :mod:`cairn.procs`).
    """
    start = time.time()
    try:
        result = run_capped(command, timeout=timeout)
        elapsed = time.time() - start
        status = "ok" if result.returncode == 0 else "FAILED"
        log(f"  [{name}] {status} ({elapsed:.1f}s)")
        _log_output(result.stdout)
        if result.stderr and result.stderr.strip():
            _log_output(result.stderr, prefix="      [stderr] ")
        return result.returncode == 0
    except subprocess.TimeoutExpired as e:
        log(f"  [{name}] TIMEOUT after {timeout}s")
        _log_timeout_output(e)
        return False
    except Exception as e:
        log(f"  [{name}] ERROR: {e}")
        return False


def run_embed(qmd: str, timeout: int = EMBED_TIMEOUT_S) -> tuple[bool, bool]:
    """Run ``qmd embed`` once; return ``(succeeded, exited)``.

    QMD exits 0 even when chunks fail, so exit 0 alone is not success: the full
    output is checked for failure reports before it is truncated for the log.
    ``exited`` is True when QMD ran to a normal exit (any code), which is when the
    caller verifies the index with :func:`verify_index` under the same lock. A clean
    exit logs ``exited 0``, never ``ok``: only ``[qmd-verify]`` gives the verdict.
    """
    start = time.time()
    try:
        result = run_capped([qmd, "embed"], timeout=timeout)
    except subprocess.TimeoutExpired as e:
        log(f"  [qmd-embed] TIMEOUT after {timeout}s")
        _log_timeout_output(e)
        return False, False
    except (OSError, RuntimeError) as e:  # RuntimeError: ProcessCleanupError
        log(f"  [qmd-embed] ERROR: {e}")
        return False, False
    elapsed = time.time() - start
    problems = embed_failures(f"{result.stdout or ''}\n{result.stderr or ''}")
    if result.returncode != 0:
        log(f"  [qmd-embed] FAILED ({elapsed:.1f}s) - exit {result.returncode}")
    elif problems:
        log(f"  [qmd-embed] FAILED ({elapsed:.1f}s) - qmd exited 0 but reported: {problems[0]}")
        for extra in problems[1:]:
            log(f"      also: {extra}")
    else:
        # Not "ok": the verdict comes from [qmd-verify], which reads the index next.
        log(f"  [qmd-embed] exited 0 ({elapsed:.1f}s)")
    _log_output(result.stdout)
    if result.stderr and result.stderr.strip():
        _log_output(result.stderr, prefix="      [stderr] ")
    return result.returncode == 0 and not problems, True


def verify_index(qmd: str) -> str | None:
    """Log the post-embed index state; return the failed-step label, or None when complete.

    Pending documents mean the run did not finish the work: ``INCOMPLETE``, a
    failure distinct from a broken index (QMD skips whitespace-only files before
    indexing, so they never show up as pending). An empty index is complete. All
    counts are index-wide.
    """
    try:
        status = index_status(qmd)
    except RuntimeError as e:
        log(f"  [qmd-verify] FAILED - could not verify the index after embedding: {e}")
        return "qmd-verify"
    if status.state == "empty":
        log("  [qmd-verify] ok - empty index: no documents to embed")
    elif status.state == "no-vectors":
        log(f"  [qmd-verify] FAILED - {status.describe()}: no vectors after embedding")
        return "qmd-verify"
    elif status.state == "pending":
        log(
            f"  [qmd-verify] INCOMPLETE - {status.describe()}; "
            "the next 'cairn sync --cron' continues (see 'cairn doctor' for the pending count)"
        )
        return "qmd-verify (incomplete)"
    else:
        log(f"  [qmd-verify] ok - {status.describe()}")
    return None


def run_inproc(name, fn):
    """Run an in-process step, logging status the same way ``run_step`` does.

    Collection registration is a Python call (it loops the config and shells out to
    ``qmd`` per collection), not a single subprocess, but it should appear in the
    sync log with the same ``[name] ok/FAILED (Ns)`` shape as every other step.
    Returns success; never raises - a registration failure must not abort the sync.
    """
    start = time.time()
    try:
        summary = fn()
        log(f"  [{name}] ok ({time.time() - start:.1f}s)")
        if summary:
            _log_output(summary)
        return True
    except Exception as e:
        log(f"  [{name}] FAILED ({time.time() - start:.1f}s)")
        _log_output(str(e))
        return False


def _ensure_collections(config: CairnConfig) -> str:
    """Register every enabled collection that has markdown on disk (idempotent).

    This is the step that makes a fresh index non-empty without any manual
    ``qmd collection add``. It is self-healing: a source whose dir does not exist
    yet (no data) is skipped and picked up automatically on a later sync once it
    produces markdown. One ``collection list`` call is shared across all checks.
    """
    known = list_collections(config.qmd_binary)
    added: list[str] = []
    exists = skipped = 0
    failures: list[str] = []
    for name, coll in sorted(config.collections.items()):
        if not coll.sync.enabled:
            continue
        status = ensure_collection(name, config.output_dir(name), config.qmd_binary, known)
        if status == "added":
            added.append(name)
        elif status == "exists":
            exists += 1
        elif status == "FAILED":
            failures.append(name)
        else:  # skipped
            skipped += 1
    if failures:
        raise RuntimeError(f"qmd collection add failed for: {', '.join(failures)}")
    if added:
        return (
            f"registered {len(added)} new: {', '.join(added)} (exists={exists}, skipped={skipped})"
        )
    return f"all up to date (exists={exists}, skipped={skipped})"


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="cairn sync",
        description="Sync context collections and update the QMD index",
    )
    parser.add_argument(
        "--hook",
        action="store_true",
        help="Legacy fast sync: Claude by default, explicit on_hook overrides; new hooks use cairn hook handle",
    )
    parser.add_argument(
        "--cron", action="store_true", help="Full mode for the cron job (all sources)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Sync all enabled sources and embed incrementally (same as --cron)",
    )
    args = parser.parse_args(argv)

    config = get_config()
    mode = "hook" if args.hook else ("cron" if args.cron else "all")
    sync_lock = lock_path(config.data_root, "sync")
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(sync_lock, blocking=False))
        except BlockingIOError:
            request_sync(config, mode)
            try:
                # The holder may have released the lock between our first try and
                # the request write; then nobody would consume it until later.
                stack.enter_context(locked(sync_lock, blocking=False))
            except BlockingIOError:
                # One line, so it cannot split a running sync's block. Not a failure.
                log(
                    f"=== context-sync ({mode}) {datetime.now().isoformat()} skipped: "
                    f"another cairn sync is running for {config.data_root}; "
                    "it runs this request's stages before it exits ==="
                )
                return 0
        return _run(config, mode)
    raise AssertionError("unreachable")


def _requests_dir(config: CairnConfig) -> Path:
    return config.data_root / "sync-requests"


def request_sync(config: CairnConfig, mode: str) -> None:
    """Record that a ``mode`` sync was skipped, for the lock holder to service."""
    atomic_write(_requests_dir(config) / mode, datetime.now().isoformat() + "\n")


def consume_requests(config: CairnConfig) -> str | None:
    """Remove every recorded request; return the widest mode among them (sync lock held)."""
    widest: str | None = None
    for mode in MODE_RANK:
        path = _requests_dir(config) / mode
        if path.is_file():
            path.unlink(missing_ok=True)
            if widest is None or MODE_RANK[mode] > MODE_RANK[widest]:
                widest = mode
    return widest


def _index_stages(config: CairnConfig, embed: bool, record) -> None:
    """Collections, ``qmd update`` and (if ``embed``) embed + verify, under the index lock."""
    qmd = config.qmd_binary
    with locked_index():
        record(
            "qmd-collections",
            run_inproc("qmd-collections", lambda: _ensure_collections(config)),
        )
        updated = run_step("qmd-update", [qmd, "update"])
        record("qmd-update", updated)
        if not embed:
            return
        if not updated:
            log("  [qmd-embed] skipped - qmd-update failed")
            return
        embedded, exited = run_embed(qmd)
        record("qmd-embed", embedded)
        if exited:
            label = verify_index(qmd)
            record(label or "qmd-verify", label is None)


def _stages(config: CairnConfig, mode: str, record, exported: set[str]) -> None:
    """Run ``mode``'s stages; skip exports already in ``exported`` (and add new ones)."""
    from .hooks import worker

    if mode != "hook":
        record("hook-worker", worker(config) == 0)
    steps = [(name, command) for name, command in _plan(config, mode) if name not in exported]
    if not steps and not exported:
        log("  (no enabled sources for this mode - check 'sync.enabled' in your config)")
    for name, command in steps:
        record(name, run_step(name, command))
        exported.add(name)
    if mode != "hook":
        _exporter_stages(config, record)
    _index_stages(config, embed=mode != "hook", record=record)


def _exporter_stages(config: CairnConfig, record) -> None:
    """After the exports: report the parser-drift findings that are still open."""
    log_drift(config)


def log_drift(config: CairnConfig) -> None:
    """One ``[exporter-drift]`` line while any warn or error finding stays open."""
    from .sources.guards import FindingsStore

    found = FindingsStore(config.data_root).open_findings()
    if not found:
        return
    shown = ", ".join(f"{f.ref} {f.kind}" for f in found[:5])
    more = f", +{len(found) - 5} more" if len(found) > 5 else ""
    log(f"  [exporter-drift] {len(found)} open findings: {shown}{more}")


def _follow_up(config: CairnConfig, mode: str, record, exported: set[str]) -> None:
    """Service skipped requests once. A wider mode runs the stages this run skipped;
    a full run answering a full request re-runs only the hook-worker retry pass (a
    request that failed after the first pass); anything narrower is just consumed."""
    from .hooks import worker

    requested = consume_requests(config)
    if requested is None:
        return
    if MODE_RANK[requested] > MODE_RANK[mode] and mode == "hook":
        log(f"  [coalesced] a skipped '{requested}' sync arrived; running its remaining stages")
        _stages(config, requested, record, exported)
    elif requested != "hook" and mode != "hook":
        log(f"  [coalesced] a skipped '{requested}' sync arrived; retrying hook requests")
        record("hook-worker", worker(config) == 0)
    else:
        log(f"  [coalesced] a skipped '{requested}' sync arrived; this run covered it")


def _run(config: CairnConfig, mode: str) -> int:
    """One sync run under the sync lock; returns the exit code."""
    rotate_log()
    log(f"\n=== context-sync ({mode}) {datetime.now().isoformat()} ===")

    failed: list[str] = []

    def record(name: str, ok: bool) -> None:
        if not ok:
            failed.append(name)

    exported: set[str] = set()
    try:
        _stages(config, mode, record, exported)
        _follow_up(config, mode, record, exported)
    except Exception:
        import traceback

        failed.append("uncaught-error")
        log("UNCAUGHT ERROR:\n" + traceback.format_exc())

    try:
        from .invoke import drain_publications

        for report in drain_publications(config):
            log(f"  [invoke-publish] {report.get('thread_id')}: {report.get('rounds') or 'busy'}")
    except Exception as exc:  # publishing leftovers must never fail the sync
        log(f"  [invoke-publish] WARNING: {exc}")

    if failed:
        log(f"  failed steps: {', '.join(failed)}")
    log("=== FAILED ===" if failed else "=== done ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
