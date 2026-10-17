"""Durable, short lifecycle callbacks and a detached, finite POSIX worker.

Every enqueue launches a waiter. Workers serialize with flock, then drain a
snapshot once. Thus an enqueue during worker shutdown always has a successor.
The short queue lock protects generations; completion never overwrites a newer
request. Failed/running requests survive crashes and `worker`/cron retry them.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .config import CairnConfig, ConfigError, get_config, user_config_path
from .harnesses import SessionEvent, get_harness
from .qmd import ensure_collection, list_collections, locked_index
from .registry import BY_NAME
from .runtime import background_environment
from .storage import atomic_write, locked

# Terminal requests (completed/disabled) are evidence, not work; keep a week of them.
RETENTION_SECONDS = 7 * 24 * 3600
RECENT_COMPLETED = 10  # completed requests shown by `hook status` without --all


def job_dir(config: CairnConfig) -> Path:
    return config.data_root / "sync-jobs"


def allowed(config: CairnConfig, event: SessionEvent) -> bool:
    source = BY_NAME[event.source]
    coll = config.collections.get(source.collection)
    return bool(coll and coll.sync.enabled and coll.sync.on_hook is not False)


def enqueue(config: CairnConfig, event: SessionEvent) -> Path | None:
    if not allowed(config, event):
        return None
    key = hashlib.sha256(f"{event.source}:{event.session_id}".encode()).hexdigest()
    path = job_dir(config) / f"{key}.json"
    with locked(job_dir(config) / ".queue.lock"):
        request = {
            "event": asdict(event),
            "generation": uuid.uuid4().hex,
            "state": "queued",
            "queued_at": time.time(),
            "attempts": 0,
        }
        atomic_write(path, json.dumps(request))
    return path


def launch(config: CairnConfig) -> int:
    logs = config.data_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CAIRN_CONFIG=str(config.config_path.resolve()))
    # sys.executable is the installed venv interpreter. No PATH lookup, inherited
    # terminal, shell interpolation, or host-managed background hook is involved.
    with (logs / "hook-worker.log").open("a") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "cairn", "hook", "worker"],
            env=env,
            cwd=str(config.data_root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            start_new_session=True,
        )
    return process.pid


def handle(config: CairnConfig, harness: str, payload: object) -> dict:
    event = get_harness(harness).decode(payload)
    path = enqueue(config, event)
    if path is None:
        return {"state": "disabled", "source": event.source}
    try:
        pid = launch(config)
    except OSError as exc:
        # Work stays durable and retryable. Never report queued as indexed.
        raise RuntimeError(
            f"request saved at {path}, but worker launch failed: {exc}; run cairn hook worker"
        ) from exc
    return {"state": "queued", "request": str(path), "worker_pid": pid}


def _process(config: CairnConfig, event: SessionEvent) -> None:
    source = BY_NAME[event.source]
    argv = [
        sys.executable,
        "-m",
        "cairn",
        "hook",
        "export",
        "--source",
        event.source,
        "--session-id",
        event.session_id,
    ]
    if event.transcript_path:
        argv.extend(["--transcript", event.transcript_path])
    result = subprocess.run(
        argv,
        env=dict(os.environ, CAIRN_CONFIG=str(config.config_path.resolve())),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"target export failed: {(result.stderr or result.stdout)[-2000:]}")
    with locked_index():
        known = list_collections(config.qmd_binary)
        status = ensure_collection(
            source.collection, config.output_dir(source.collection), config.qmd_binary, known
        )
        if status not in ("added", "exists"):
            raise RuntimeError(f"collection registration {status}: {source.collection}")
        result = subprocess.run(
            [config.qmd_binary, "update"], capture_output=True, text=True, timeout=120
        )
        if result.returncode:
            raise RuntimeError(f"qmd update failed: {(result.stderr or result.stdout)[-2000:]}")


def worker(config: CairnConfig) -> int:
    failures = 0
    with locked(job_dir(config) / ".worker.lock"):
        for path in sorted(job_dir(config).glob("*.json")):
            try:
                with locked(job_dir(config) / ".queue.lock"):
                    request = json.loads(path.read_text(encoding="utf-8"))
                    if request["state"] in ("completed", "disabled"):
                        continue
                    event = SessionEvent(**request["event"])
                    if not allowed(config, event):
                        request.update(state="disabled", disabled_at=time.time())
                        atomic_write(path, json.dumps(request))
                        continue
                    request.update(
                        state="running",
                        started_at=time.time(),
                        worker_pid=os.getpid(),
                        attempts=request.get("attempts", 0) + 1,
                    )
                    atomic_write(path, json.dumps(request))
                try:
                    _process(config, event)
                    request.update(state="completed", completed_at=time.time(), error=None)
                except Exception as exc:
                    failures += 1
                    request.update(state="failed", failed_at=time.time(), error=str(exc)[-2000:])
                with locked(job_dir(config) / ".queue.lock"):
                    current = json.loads(path.read_text(encoding="utf-8"))
                    if current["generation"] == request["generation"]:
                        atomic_write(path, json.dumps(request))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                failures += 1
                print(f"Invalid sync request {path}: {exc}", file=sys.stderr)
        prune(config)
    return int(failures > 0)


def prune(config: CairnConfig, now: float | None = None) -> int:
    """Delete terminal requests older than the retention window; never failed/running.

    Age is measured from the terminal timestamp (``completed_at``/``disabled_at``),
    so a long-queued request that completed today still survives a full week.
    """
    now = time.time() if now is None else now
    removed = 0
    with locked(job_dir(config) / ".queue.lock"):
        for path in job_dir(config).glob("*.json"):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            finished = {
                "completed": request.get("completed_at"),
                "disabled": request.get("disabled_at"),
            }.get(request.get("state"))
            if isinstance(finished, int | float) and now - finished > RETENTION_SECONDS:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def status(config: CairnConfig, harness: str | None = None) -> dict:
    requests = []
    for path in sorted(job_dir(config).glob("*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            if harness is None or request.get("event", {}).get("harness") == harness:
                requests.append(request)
        except (OSError, ValueError) as exc:
            requests.append({"state": "invalid", "error": str(exc), "path": str(path)})
    counts = {
        state: sum(r.get("state") == state for r in requests)
        for state in ("queued", "running", "completed", "failed", "disabled", "invalid")
    }
    return {
        "counts": counts,
        "trust": "unverified: review hooks in the host; file presence does not establish trust",
        "requests": requests,
    }


def launcher_path() -> Path:
    return user_config_path().parent / "hook-launcher.env"


def launcher_paths() -> tuple[Path, ...]:
    """Keep a stable discovery copy for hosts that omit the setup shell's XDG home."""
    return tuple(dict.fromkeys((launcher_path(), Path.home() / ".config/cairn/hook-launcher.env")))


def install_launcher(config: CairnConfig) -> Path:
    """Record the engine and background environment for ``hooks/launch.sh``.

    The file is POSIX ``sh`` assignments (values quoted with :func:`shlex.quote`), so
    the bundled launcher can source it without needing any Python to bootstrap.
    """
    environment = background_environment(config)
    values = {
        "CAIRN_PYTHON": sys.executable,
        "CAIRN_PATH": environment.pop("PATH"),
        **environment,
    }
    content = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in values.items())
    for path in launcher_paths():
        atomic_write(path, content)
    return launcher_path()


def compact_status(report: dict, show_all: bool) -> dict:
    """Keep every actionable request; show only the most recent completed ones."""
    if show_all:
        return report
    requests = report["requests"]
    completed = sorted(
        (r for r in requests if r.get("state") == "completed"),
        key=lambda r: r.get("completed_at") or 0,
        reverse=True,
    )
    kept = [r for r in requests if r.get("state") != "completed"] + completed[:RECENT_COMPLETED]
    hidden = len(completed) - min(len(completed), RECENT_COMPLETED)
    return {**report, "requests": kept, "hidden_completed": hidden}


def iso(timestamp: object) -> str:
    if isinstance(timestamp, int | float):
        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")
    return str(timestamp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairn hook")
    sub = parser.add_subparsers(dest="command", required=True)
    callback = sub.add_parser("handle", help="queue a host SessionEnd event from stdin")
    callback.add_argument("--harness", required=True, choices=("claude", "codex"))
    callback.add_argument("--require-plugin", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("worker", help="drain/retry durable work, without embedding")
    inspect = sub.add_parser("status", help="show observed execution and queue state")
    inspect.add_argument("--harness", choices=("claude", "codex"))
    inspect.add_argument(
        "--all",
        action="store_true",
        help=f"list every completed request, not the last {RECENT_COMPLETED}",
    )
    install = sub.add_parser("install", help="record launcher; inspect/register a host hook")
    install.add_argument("--harness", choices=("claude", "codex"))
    uninstall = sub.add_parser("uninstall", help="remove owned registration; preserve shared data")
    uninstall.add_argument("--harness", choices=("claude", "codex"))
    export = sub.add_parser("export", help=argparse.SUPPRESS)
    export.add_argument(
        "--source", required=True, choices=[s.source for s in BY_NAME.values() if s.targeted]
    )
    export.add_argument("--session-id", required=True)
    export.add_argument("--transcript")
    args = parser.parse_args(argv)
    try:
        if args.command == "uninstall":
            if args.harness == "codex":
                from .harnesses.codex_registration import configure

                print(configure(None, remove=True))
            elif args.harness == "claude":
                print("Disable/remove the Claude plugin in Claude; shared launcher preserved.")
            else:
                from .harnesses.codex_registration import configure

                print(configure(None, remove=True))
                for path in launcher_paths():
                    path.unlink(missing_ok=True)
            return 0
        config = get_config()
        if args.command == "install":
            print(install_launcher(config))
            if args.harness == "codex":
                from .harnesses.codex_registration import configure

                print(configure(config))
        elif args.command == "handle":
            payload = json.load(sys.stdin)
            if args.require_plugin:
                from .harnesses.codex import Codex

                event = get_harness(args.harness).decode(payload)
                if not Codex().plugin_enabled(event.cwd):
                    print(
                        json.dumps(
                            {"state": "disabled", "reason": "Codex plugin disabled or removed"}
                        )
                    )
                    return 0
            print(json.dumps(handle(config, args.harness, payload)))
        elif args.command == "worker":
            return worker(config)
        elif args.command == "status":
            report = compact_status(status(config, args.harness), args.all)
            if args.harness == "codex":
                from .harnesses.codex import Codex

                report["host_hooks"] = Codex().inspect_hooks(os.getcwd())
                report["trust"] = (
                    "See host_hooks trustStatus; completed jobs are separate evidence of execution."
                )
            print(json.dumps(report, indent=2))
        else:
            module = importlib.import_module(f"cairn.sources.{args.source}")
            source = getattr(module, f"{args.source.title()}Source")()
            with locked(source.output_dir / ".operation.lock"):
                try:
                    print(source.export_target(args.session_id, args.transcript))
                finally:
                    source.flush_catalog()
        return 0
    except (OSError, ValueError, RuntimeError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
