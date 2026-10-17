"""``cairn invoke``: run Codex or Claude headlessly as a peer agent (plan Part B, Part D).

One invocation runs one round of one thread in the foreground; the host runs it
in the background and reads progress with ``cairn invoke status`` (D2). The run
directory ``data_root/logs/invoke/<peer>/<thread_id>/`` holds ``thread.json``
(:class:`cairn.exchange.ThreadState`), and per round ``events-NN.jsonl`` (the raw
stdout tee), ``stderr-NN.log``, ``answer-NN.md`` and ``result-NN.json``.

Round lifecycle:

1. Resolve settings (CLI > recorded thread settings on resume > config policy),
   the caller session, the depth guard and the model self-check.
2. Take the per-thread lock (D19) without blocking; a second caller gets
   ``busy`` (exit 75) and nothing is spawned. The lock is held until the round,
   including the post-run pipeline, is finalised. A thread whose id is not known
   up front (a fresh Codex run, a fork) starts in ``pending-<uuid>/``, locked by
   that path; on ``thread_started`` the thread-id lock is taken, the directory
   is renamed to the thread id and the pending lock released. Codex's ``-o``
   file lives outside the run directory (``.answers/<uuid>.md``), so the rename
   never invalidates a path the peer holds; it moves to ``answer-NN.md`` once
   the peer has exited.
3. Spawn the peer in its own session with a scrubbed environment (D13) and its
   stdout written straight to ``events-NN.jsonl``, record runner and peer
   identities (pid plus start time, D21), and tail that file on one thread:
   parse, heartbeat, exchange refresh, the wall-clock and idle watchdogs, and
   SIGTERM/SIGINT forwarding. A runner that dies loses none of the stream.
4. Finalise the outcome, save the answer, then run the publication pipeline
   (peer export, caller export, links, exchange document, index). Pipeline
   failures never change the outcome (D11); each step lands in the round's
   publication ledger (D24) so ``cairn invoke publish`` or the next
   ``cairn sync`` can finish it without running the peer again.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from .config import INVOKE_PEERS, PROFILES, CairnConfig, ConfigError, PeerPolicy, get_config
from .exchange import ThreadState, now_iso, write_exchange
from .harnesses import Event, Harness, InvokeRequest, detect_harness, get_harness, is_client_error
from .harnesses.claude import KEYCHAIN_GUIDANCE, keychain_failure
from .health import Health, classify, heartbeat, identity_alive, parse_iso, process_identity
from .registry import BY_NAME
from .storage import atomic_write, lock_path, locked

# Exit codes (the contract the skills read).
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_BUSY = 75
EXIT_TIMEOUT = 124
EXIT_IDLE = 125
EXIT_CANCELLED = 130

# A prompt larger than this goes through the child's stdin (macOS ARG_MAX, D14).
STDIN_PROMPT_BYTES = 64_000
PROGRESS_CAP = 200
PROGRESS_CHARS = 200
STATE_WRITE_INTERVAL_S = 2.0
EXCHANGE_REFRESH_S = 30.0
TRANSCRIPT_POLL_S = 10.0
PUMP_POLL_S = 0.1
KILL_GRACE_S = 10.0
INDEX_LOCK_TIMEOUT_S = 60.0
INDEX_RETRY_INTERVAL_S = 1.0
QMD_UPDATE_TIMEOUT_S = 120
STDERR_TAIL = 2000

# Environment a peer must not inherit: the caller's identity hints and plugin roots
# (D13), plus the Claude Code session/messaging variables that would make a nested
# ``claude`` act as a child of the caller.
SCRUBBED_ENV = (
    "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CAIRN_HARNESS",
    "CLAUDE_PLUGIN_ROOT",
    "PLUGIN_ROOT",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
)
DEPTH_ENV = "CAIRN_INVOKE_DEPTH"
CALLER_ENV = "CAIRN_INVOKE_CALLER"

PUBLICATION_STEPS = ("exported", "caller_exported", "linked", "exchange_written", "indexed")
_CALLER_REF = re.compile(r"^(claude|codex):\S+$")


class UsageError(Exception):
    """A request cairn refuses before spawning anything (exit 2)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def invoke_root(config: CairnConfig) -> Path:
    return config.data_root / "logs" / "invoke"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _nn(number: int) -> str:
    return f"{number:02d}"


def _round_files(run_dir: Path, number: int) -> dict[str, Path]:
    nn = _nn(number)
    return {
        "events_path": run_dir / f"events-{nn}.jsonl",
        "stderr_path": run_dir / f"stderr-{nn}.log",
        "answer_path": run_dir / f"answer-{nn}.md",
        "result_path": run_dir / f"result-{nn}.json",
    }


def _tail(path: Path | None, limit: int = STDERR_TAIL) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _store(config: CairnConfig, peer: str) -> Path | None:
    try:
        return config.collection(BY_NAME[peer].collection).require_store()
    except (ConfigError, KeyError):
        return None


def _transcript(config: CairnConfig, harness: Harness, state: ThreadState, round_: dict):
    store = _store(config, state.peer)
    if store is None or not state.thread_id:
        return None
    started = parse_iso(round_.get("started_at"))
    from datetime import datetime

    when = datetime.fromtimestamp(started).astimezone() if started else None
    try:
        return harness.transcript_path(store, state.thread_id, when)
    except OSError:
        return None


def _load_source(name: str) -> Any:
    """A fresh source adapter, imported lazily (the modules bind config at import)."""
    module = importlib.import_module(f"cairn.sources.{name}")
    return getattr(module, f"{name.title()}Source")()


def _split_ref(ref: str) -> tuple[str, str]:
    source, native = ref.split(":", 1)
    return source, native


# ---------------------------------------------------------------------------
# Event replay (shared by the live pump, status and adoption)
# ---------------------------------------------------------------------------


class LineTail:
    """Complete lines from a growing byte stream.

    A partial line, including one that ends inside a multi-byte UTF-8 character,
    waits in the buffer for the rest; lines are only decoded once complete.
    """

    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        return [line + b"\n" for line in lines]

    def flush(self) -> list[bytes]:
        """The final line when the stream ended without a trailing newline."""
        rest, self.buffer = self.buffer, b""
        return [rest + b"\n"] if rest.strip() else []


@dataclass
class Replay:
    """What a stream of normalised events says about a round so far."""

    thread_id: str | None = None
    progress: list[str] = field(default_factory=list)
    dropped: int = 0
    last_message: str | None = None
    done: Event | None = None
    last_event: str | None = None
    notices: list[str] = field(default_factory=list)

    def add_progress(self, text: str) -> None:
        self.progress.append(_clip(text, PROGRESS_CHARS))
        if len(self.progress) > PROGRESS_CAP:
            excess = len(self.progress) - PROGRESS_CAP
            del self.progress[:excess]
            self.dropped += excess

    def feed(self, events: list[Event]) -> None:
        for event in events:
            label = event.raw_type or event.kind
            detail = event.text or event.error or event.thread_id or ""
            self.last_event = _clip(f"{label} {detail}".strip(), 160)
            if event.kind == "thread_started" and event.thread_id:
                self.thread_id = event.thread_id
            elif event.kind == "message" and event.text:
                self.last_message = event.text
                self.add_progress(f"message: {event.text}")
            elif event.kind == "progress" and event.text:
                self.add_progress(event.text)
            elif event.kind == "notice" and event.text:
                self.notices.append(_clip(event.text, 500))
            elif event.kind == "done":
                self.done = event


def replay_file(harness: Harness, path: Path) -> Replay:
    """Re-parse an ``events-NN.jsonl`` tee with the peer's adapter."""
    replay = Replay()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            replay.feed(harness.parse_event(line))
    return replay


# ---------------------------------------------------------------------------
# Locating threads
# ---------------------------------------------------------------------------


def iter_run_dirs(config: CairnConfig) -> Iterator[Path]:
    """Every run directory with a ``thread.json`` (never creates anything)."""
    root = invoke_root(config)
    for peer in INVOKE_PEERS:
        folder = root / peer
        if not folder.is_dir():
            continue
        for run_dir in sorted(folder.iterdir()):
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            if (run_dir / "thread.json").is_file():
                yield run_dir


def locate(config: CairnConfig, thread_id: str, peer: str | None = None) -> Path:
    """The run directory for a thread id or unique prefix; ``LookupError`` otherwise."""
    if not thread_id or "/" in thread_id:
        raise LookupError(f"invalid thread id {thread_id!r}")
    matches = [
        run_dir
        for run_dir in iter_run_dirs(config)
        if not run_dir.name.startswith("pending-")
        and run_dir.name.startswith(thread_id)
        and (peer is None or run_dir.parent.name == peer)
    ]
    exact = [run_dir for run_dir in matches if run_dir.name == thread_id]
    if len(exact) == 1:
        return exact[0]
    if not matches:
        raise LookupError(f"no invoke thread {thread_id!r} under {invoke_root(config)}")
    if len(matches) > 1:
        names = ", ".join(f"{d.parent.name}:{d.name}" for d in matches)
        raise LookupError(f"ambiguous thread id {thread_id!r}; choose {names}")
    return matches[0]


# ---------------------------------------------------------------------------
# Thread lock
# ---------------------------------------------------------------------------


class ThreadLock:
    """A non-blocking per-thread flock that can be moved to the renamed directory."""

    def __init__(self) -> None:
        self._stack = contextlib.ExitStack()

    def acquire(self, run_dir: Path) -> None:
        """Take the lock for ``run_dir``; ``BlockingIOError`` when another holder has it."""
        stack = contextlib.ExitStack()
        stack.enter_context(locked(lock_path(run_dir, "thread"), blocking=False))
        previous, self._stack = self._stack, stack
        previous.close()

    def release(self) -> None:
        self._stack.close()


def _holder(state: ThreadState | None) -> dict:
    last = state.last_round if state else None
    if not last:
        return {}
    return {
        "round": last.get("round"),
        "runner_pid": last.get("runner_pid"),
        "runner_started": last.get("runner_started"),
        "started_at": last.get("started_at"),
    }


# ---------------------------------------------------------------------------
# Publication pipeline (B3, D24)
# ---------------------------------------------------------------------------


def _mark(round_: dict, step: str, error: str | None) -> None:
    publication = round_.setdefault("publication", {})
    pending = publication.setdefault("pending", {})
    if error is None:
        publication[step] = now_iso()
        pending.pop(step, None)
    else:
        publication.pop(step, None)
        pending[step] = _clip(error, 1000)


def _collection_of(config: CairnConfig, path: Path) -> str | None:
    for name in config.collections:
        try:
            path.resolve().relative_to(config.output_dir(name).resolve())
        except ValueError:
            continue
        return name
    return None


def _touched_collections(config: CairnConfig, state: ThreadState) -> list[str]:
    names = {BY_NAME[state.peer].collection}
    for round_ in state.rounds:
        caller = round_.get("caller_session")
        if caller:
            names.add(BY_NAME[_split_ref(caller)[0]].collection)
    if state.exchange_doc:
        found = _collection_of(config, Path(state.exchange_doc))
        if found:
            names.add(found)
    return sorted(name for name in names if name in config.collections)


def update_index(config: CairnConfig, collections: list[str], timeout_s: float) -> None:
    """``ensure_collection`` for each collection, then one ``qmd update``, under the
    QMD index lock taken without blocking and retried for up to ``timeout_s``."""
    from .qmd import ensure_collection, list_collections, locked_index

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with locked_index(blocking=False):
                known = list_collections(config.qmd_binary)
                for name in collections:
                    status = ensure_collection(
                        name, config.output_dir(name), config.qmd_binary, known
                    )
                    if status == "FAILED":
                        raise RuntimeError(f"qmd collection add failed for {name}")
                result = subprocess.run(
                    [config.qmd_binary, "update"],
                    capture_output=True,
                    text=True,
                    timeout=QMD_UPDATE_TIMEOUT_S,
                )
                if result.returncode:
                    raise RuntimeError(
                        f"qmd update failed: {(result.stderr or result.stdout)[-STDERR_TAIL:]}"
                    )
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"index busy for {timeout_s:g}s (another cairn sync holds the QMD lock); "
                    "run 'cairn invoke publish' or the next 'cairn sync' indexes it"
                ) from None
            time.sleep(INDEX_RETRY_INTERVAL_S)


def _step_exported(config: CairnConfig, state: ThreadState, round_: dict) -> None:
    from .hooks import export_and_index

    if not state.thread_id:
        raise RuntimeError("no thread id was captured; nothing to export")
    harness = get_harness(state.peer)
    transcript = _transcript(config, harness, state, round_)
    if transcript is None:
        raise RuntimeError(f"no native transcript found for {state.peer}:{state.thread_id}")
    round_["transcript_path"] = str(transcript)
    document = export_and_index(
        config, state.peer, state.thread_id, str(transcript), update_index=False
    )
    state.peer_session_doc = str(document)
    round_["peer_session_doc"] = str(document)


def _step_caller_exported(config: CairnConfig, state: ThreadState, round_: dict) -> None:
    from .hooks import export_and_index

    source, native = _split_ref(round_["caller_session"])
    document = export_and_index(config, source, native, None, update_index=False)
    round_["caller_session_doc"] = str(document)
    if state.caller_session == round_["caller_session"]:
        state.caller_session_doc = str(document)


def _step_linked(config: CairnConfig, state: ThreadState, round_: dict) -> None:
    from .mutations import add_link

    if not state.thread_id:
        raise RuntimeError("no thread id was captured; nothing to link")
    caller = round_["caller_session"]
    source, native = _split_ref(caller)
    peer_source = _load_source(state.peer)
    caller_source = peer_source if source == state.peer else _load_source(source)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            up = add_link(peer_source.find_file, state.thread_id, "parent", caller)
            down = add_link(
                caller_source.find_file, native, "child", f"{state.peer}:{state.thread_id}"
            )
    finally:
        peer_source.flush_catalog()
        if caller_source is not peer_source:
            caller_source.flush_catalog()
    if not (up and down):
        missing = [name for name, ok in (("peer", up), ("caller", down)) if not ok]
        raise RuntimeError(f"link not written on the {' and '.join(missing)} document")


def _step_exchange(config: CairnConfig, state: ThreadState, round_: dict) -> None:
    path = write_exchange(config, state, describe_folder=not round_.get("no_index", False))
    if path is None:
        raise RuntimeError("no anchor yet: neither the caller nor the peer document exists")
    round_["exchange_doc"] = str(path)


_STEP_FUNCTIONS = {
    "exported": _step_exported,
    "caller_exported": _step_caller_exported,
    "linked": _step_linked,
    "exchange_written": _step_exchange,
}


# The steps each step feeds. When a step completes (first run or a retry), the
# steps downstream of it are redone so they never describe stale upstream state:
# a late peer or caller export must be linked, re-anchor the exchange document
# and be indexed.
DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "exported": ("linked", "exchange_written", "indexed"),
    "caller_exported": ("linked", "exchange_written", "indexed"),
    "linked": ("indexed",),
    "exchange_written": ("indexed",),
    "indexed": (),
}
NOT_ATTEMPTED = "not attempted yet"


def _applicable(step: str, round_: dict) -> bool:
    if step in ("caller_exported", "linked"):
        return bool(round_.get("caller_session"))
    return True


def applicable_steps(round_: dict) -> tuple[str, ...]:
    return tuple(step for step in PUBLICATION_STEPS if _applicable(step, round_))


def init_ledger(round_: dict) -> None:
    """Record every applicable step without a completion timestamp as pending, so a
    runner killed between steps leaves an explicit to-do list behind."""
    publication = round_.setdefault("publication", {})
    pending = publication.setdefault("pending", {})
    for step in applicable_steps(round_):
        if step not in publication:
            pending.setdefault(step, NOT_ATTEMPTED)


def pending_steps(round_: dict) -> tuple[str, ...]:
    """Applicable steps of a finished round that have no completion timestamp."""
    if not round_.get("finished_at"):
        return ()
    publication = round_.get("publication") or {}
    return tuple(step for step in applicable_steps(round_) if step not in publication)


def publish_round(
    config: CairnConfig,
    state: ThreadState,
    round_: dict,
    *,
    steps: tuple[str, ...] | None = None,
    index: bool = True,
    warnings: list[str] | None = None,
) -> list[str]:
    """Run publication ``steps`` (default: every applicable step) for one finalised
    round, in dependency order, saving the ledger after each.

    A step that completes invalidates and redoes the steps downstream of it
    (:data:`DOWNSTREAM`). Failures are recorded as ``publication.pending.<step>`` and
    returned as warnings; they never raise and never touch the outcome (D11).
    """
    warnings = warnings if warnings is not None else []
    todo = set(applicable_steps(round_) if steps is None else steps)
    init_ledger(round_)
    for step in PUBLICATION_STEPS:
        if step not in todo or not _applicable(step, round_):
            continue
        try:
            if step == "indexed":
                if not index:
                    raise RuntimeError(
                        "skipped (--no-index); run 'cairn invoke publish' or 'cairn sync'"
                    )
                update_index(
                    config, _touched_collections(config, state), timeout_s=INDEX_LOCK_TIMEOUT_S
                )
            else:
                _STEP_FUNCTIONS[step](config, state, round_)
        except Exception as exc:  # a publishing failure is data, never a crash
            _mark(round_, step, str(exc) or type(exc).__name__)
            warnings.append(f"{step}: {_clip(str(exc), 500)}")
        else:
            _mark(round_, step, None)
            for later in DOWNSTREAM[step]:
                if _applicable(later, round_) and later not in todo:
                    _mark(round_, later, f"redo after {step}")
                    todo.add(later)
        state.save()
    return warnings


# ---------------------------------------------------------------------------
# Finalising rounds
# ---------------------------------------------------------------------------


def _retryable(text: str) -> bool:
    return not is_client_error(text) and not keychain_failure(text)


def _finish(
    state: ThreadState,
    round_: dict,
    *,
    outcome: str,
    exit_code: int | None,
    replay: Replay,
    duration_s: float,
    error: str | None,
    retryable: bool | None,
) -> None:
    files = _round_files(state.path.parent if state.path else Path("."), int(round_["round"]))
    answer_path = files["answer_path"]
    target = Path(round_["answer_target"]) if round_.get("answer_target") else None
    if target is not None and target.is_file():
        os.replace(target, answer_path)  # the peer has exited: its -o file is final
    answer = _read_text(answer_path).strip()
    done = replay.done
    if state.peer == "claude" or not answer:
        answer = (done.text if done and done.text else None) or replay.last_message or ""
        if answer:
            atomic_write(answer_path, answer + "\n")
    round_.update(
        finished_at=now_iso(),
        outcome=outcome,
        exit_code=exit_code,
        duration_s=round(duration_s, 3),
        last_message=answer or None,
        progress=list(replay.progress),
        progress_dropped=replay.dropped,
        last_event=replay.last_event,
        usage=done.usage if done else None,
        error=error,
        retryable=retryable,
        events_path=str(files["events_path"]),
        stderr_path=str(files["stderr_path"]),
        answer_path=str(answer_path) if answer_path.exists() else None,
    )
    _mark(round_, "answer_saved", None)
    init_ledger(round_)


def adopt(config: CairnConfig, state: ThreadState, round_: dict, *, outcome: str | None = None):
    """Finalise a round whose runner and peer are both gone, from the run directory.

    The last ``done`` event decides the outcome (``aborted`` without one, ``unknown``
    when the events file cannot be read) unless ``outcome`` overrides it (cancel).
    Then the publication steps run. Returns the round.
    """
    harness = get_harness(state.peer)
    files = _round_files(state.path.parent if state.path else Path("."), int(round_["round"]))
    try:
        replay = replay_file(harness, files["events_path"])
        derived = (replay.done.status if replay.done else None) or "aborted"
    except OSError:
        replay, derived = Replay(), "unknown"
    final = outcome or derived
    started = parse_iso(round_.get("started_at")) or time.time()
    try:
        ended = files["events_path"].stat().st_mtime
    except OSError:
        ended = time.time()
    stderr = _tail(files["stderr_path"])
    error = None
    if final != "succeeded":
        error = (replay.done.error if replay.done else None) or stderr.strip() or None
        if final == "aborted":
            error = error or "the runner died and the stream has no final event"
    text = f"{stderr}\n{error or ''}"
    retryable = final != "cancelled" and _retryable(text)
    if keychain_failure(text):
        error = KEYCHAIN_GUIDANCE
    round_["adopted_at"] = now_iso()
    _finish(
        state,
        round_,
        outcome=final,
        exit_code=None,
        replay=replay,
        duration_s=max(0.0, ended - started),
        error=error,
        retryable=retryable,
    )
    state.save()
    publish_round(config, state, round_, index=not round_.get("no_index", False))
    return round_


# ---------------------------------------------------------------------------
# Observation (status, agents)
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    state: ThreadState
    round_: dict | None
    health: Health
    runner_alive: bool
    peer_alive: bool
    replay: Replay
    heartbeat_age_s: float | None
    elapsed_s: float | None
    budget_s: int | None
    last_event: str | None


def observe(config: CairnConfig, run_dir: Path, *, settle: bool = True) -> Observation:
    """The three health axes of a thread's last round.

    With ``settle``, a round whose runner and peer are both gone is adopted and
    finalised first (taking the thread lock without blocking; a busy lock means
    another reader is already doing it).
    """
    state = ThreadState.load(run_dir / "thread.json")
    harness = get_harness(state.peer)
    policy = config.invoke.for_peer(state.peer)
    round_ = state.last_round
    if round_ is None:
        health = Health("pending", "none", None, None)
        return Observation(state, None, health, False, False, Replay(), None, None, None, None)
    runner_alive = identity_alive(round_.get("runner_pid"), round_.get("runner_started"))
    peer_alive = identity_alive(round_.get("peer_pid"), round_.get("peer_started"))
    if settle and not round_.get("finished_at") and not runner_alive and not peer_alive:
        lock = ThreadLock()
        try:
            lock.acquire(run_dir)
        except BlockingIOError:
            pass
        else:
            try:
                state = ThreadState.load(run_dir / "thread.json")
                round_ = state.last_round or round_
                if not round_.get("finished_at"):
                    adopt(config, state, round_)
            finally:
                lock.release()
    files = _round_files(run_dir, int(round_["round"]))
    try:
        replay = replay_file(harness, files["events_path"])
    except OSError:
        replay = Replay()
    transcript = _transcript(config, harness, state, round_)
    beat = heartbeat(
        files["events_path"],
        transcript,
        round_.get("heartbeat_at"),
        replay.last_event or round_.get("last_event"),
    )
    now = time.time()
    health = classify(
        round_,
        now,
        policy,
        runner_alive=runner_alive,
        peer_alive=peer_alive,
        heartbeat_at=beat.latest,
        done_status=replay.done.status if replay.done else None,
    )
    started = parse_iso(round_.get("started_at"))
    finished = parse_iso(round_.get("finished_at"))
    elapsed = ((finished or now) - started) if started else None
    return Observation(
        state=state,
        round_=round_,
        health=health,
        runner_alive=runner_alive,
        peer_alive=peer_alive,
        replay=replay,
        heartbeat_age_s=(now - beat.latest) if beat.latest else None,
        elapsed_s=elapsed,
        budget_s=round_.get("timeout_s"),
        last_event=beat.last_event,
    )


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def health_line(obs: Observation) -> str:
    health = obs.health
    execution = health.execution
    if health.outcome and execution in ("exited", "unknown"):
        execution = f"{execution} ({health.outcome})"
    activity = health.activity
    age = obs.heartbeat_age_s if activity == "none" else health.activity_age_s
    elapsed = int((obs.elapsed_s or 0) // 60)
    budget = int((obs.budget_s or 0) // 60)
    return (
        f"{execution}, {activity} for {_age(age)} (last: {obs.last_event or 'none'}), "
        f"runner {'alive' if obs.runner_alive else 'gone'}, "
        f"peer {'alive' if obs.peer_alive else 'gone'}, elapsed {elapsed}m of {budget}m"
    )


def observation_json(obs: Observation) -> dict:
    round_ = obs.round_ or {}
    progress = obs.replay.progress or round_.get("progress") or []
    return {
        "peer": obs.state.peer,
        "thread_id": obs.state.thread_id,
        "round": round_.get("round"),
        "execution": obs.health.execution,
        "activity": obs.health.activity,
        "activity_age_s": obs.health.activity_age_s,
        "outcome": obs.health.outcome,
        "elapsed_s": obs.elapsed_s,
        "budget_s": obs.budget_s,
        "runner_alive": obs.runner_alive,
        "peer_alive": obs.peer_alive,
        "last_event": obs.last_event,
        "progress": progress,
        "last_message": round_.get("last_message") or obs.replay.last_message,
        "publication": round_.get("publication") or {},
        "events_path": round_.get("events_path"),
        "transcript_path": round_.get("transcript_path"),
        "exchange_doc": obs.state.exchange_doc,
        "run_dir": str(obs.state.path.parent) if obs.state.path else None,
    }


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    model: str | None
    effort: str | None
    profile: str
    native_policy: tuple[str, ...]
    cwd: Path
    timeout_s: int
    idle_timeout_s: int
    fast: bool
    output_schema: Path | None
    extra: tuple[str, ...]


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        text = sys.stdin.read() if args.prompt == "-" else args.prompt
    elif args.prompt_file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --prompt-file: {exc}") from exc
    if not text.strip():
        raise UsageError("the prompt is empty")
    return text


def preamble(peer: str, caller: str | None, number: int, timeout_s: int) -> str:
    """The identity and budget preamble prepended to every prompt (D16)."""
    who = f"{_split_ref(caller)[0]} session {caller}" if caller else "an operator"
    return (
        f"[cairn invoke] You are {peer} being invoked by {who} via cairn (round {number}). "
        f"Reply budget: {timeout_s // 60} minutes of wall-clock time; the run is killed after "
        "that. Work autonomously; nobody can answer questions mid-run. Write your complete "
        "final answer as your last message. If asked for a verdict, end with one line: "
        "VERDICT: <accept|reject|unresolved> - <reason>."
    )


def resolve_caller(parent: str | None) -> tuple[str | None, list[str]]:
    """The calling session as ``source:id``: ``--parent``, else the detected host."""
    if parent:
        if not _CALLER_REF.match(parent):
            raise UsageError(f"--parent must look like claude:<id> or codex:<id>, got {parent!r}")
        return parent, []
    try:
        name = detect_harness()
        native = get_harness(name).current_id() if name else None
    except ValueError as exc:
        return None, [f"caller unknown: {exc}"]
    if name and native:
        return f"{name}:{native}", []
    return None, ["caller unknown: no single host session detected; pass --parent SOURCE:ID"]


def scrubbed_env(depth: int, caller: str | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in SCRUBBED_ENV and not key.endswith("PLUGIN_ROOT")
    }
    env[DEPTH_ENV] = str(depth + 1)
    env[CALLER_ENV] = caller or "unknown"
    return env


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


class Runner:
    """One round of one thread, from lock to result."""

    def __init__(self, config: CairnConfig, peer: str, args: argparse.Namespace) -> None:
        self.config = config
        self.peer = peer
        self.args = args
        self.harness = get_harness(peer)
        self.policy: PeerPolicy = config.invoke.for_peer(peer)
        self.warnings: list[str] = []
        self.lock = ThreadLock()
        self.cancel_requested = False
        self.state: ThreadState
        self.round: dict
        self.run_dir: Path
        self.thread_id: str | None = None

    # ---- settings -------------------------------------------------------

    def _native(self) -> tuple[str, ...]:
        sandbox = getattr(self.args, "sandbox", None)
        mode = getattr(self.args, "permission_mode", None)
        if self.peer == "codex" and sandbox:
            return ("-s", str(sandbox))
        if self.peer == "claude" and mode:
            return ("--permission-mode", str(mode))
        return ()

    def settings(self, recorded: ThreadState | None) -> Settings:
        args = self.args
        native = self._native()
        if recorded is None:
            profile = args.profile or self.policy.profile
            model = args.model or self.policy.model
            effort = args.effort or self.policy.effort
            cwd = Path(args.cd).expanduser().resolve() if args.cd else Path.cwd().resolve()
        else:
            profile = args.profile or recorded.profile
            if not native and not args.profile:
                native = tuple(recorded.native_policy)
            model = args.model or recorded.model
            effort = args.effort or recorded.effort
            cwd = Path(recorded.cwd)
            if args.cd and Path(args.cd).expanduser().resolve() != cwd.resolve():
                raise UsageError(
                    f"--cd {args.cd} differs from the thread's recorded cwd {cwd}; a resumed "
                    "thread runs where it started (omit --cd)"
                )
        if profile not in PROFILES:
            raise UsageError(f"unknown profile {profile!r}; choose {', '.join(PROFILES)}")
        if not cwd.is_dir():
            raise UsageError(f"working directory does not exist: {cwd}")
        schema = None
        if args.output_schema:
            schema = Path(args.output_schema).expanduser().resolve()
            if not schema.is_file():
                raise UsageError(f"--output-schema file not found: {schema}")
        return Settings(
            model=model,
            effort=effort,
            profile=profile,
            native_policy=native,
            cwd=cwd,
            timeout_s=args.timeout_s or self.policy.timeout_s,
            idle_timeout_s=args.idle_timeout_s or self.policy.idle_timeout_s,
            fast=bool(args.fast),
            output_schema=schema,
            extra=tuple(args.extra or ()),
        )

    # ---- result ---------------------------------------------------------

    def result(self, status: str, **extra: Any) -> dict:
        round_ = getattr(self, "round", None) or {}
        state = getattr(self, "state", None)
        files = (
            _round_files(self.run_dir, int(round_["round"]))
            if round_ and hasattr(self, "run_dir")
            else {}
        )
        exit_code = round_.get("exit_code")
        publication = round_.get("publication") or {}
        result = {
            "status": status,
            "outcome": round_.get("outcome"),
            "peer": self.peer,
            "thread_id": state.thread_id if state else None,
            "round": round_.get("round"),
            "model": round_.get("model"),
            "effort": round_.get("effort"),
            "profile": round_.get("profile"),
            "native_policy": round_.get("native_policy"),
            "cwd": state.cwd if state else None,
            "caller": round_.get("caller_session"),
            "duration_s": round_.get("duration_s"),
            "exit_code": exit_code,
            "events_path": str(files["events_path"]) if files else None,
            "stderr_path": str(files["stderr_path"]) if files else None,
            "answer_path": round_.get("answer_path"),
            "transcript_path": round_.get("transcript_path"),
            "exchange_doc": state.exchange_doc if state else None,
            "peer_session_doc": round_.get("peer_session_doc"),
            "caller_session_doc": round_.get("caller_session_doc"),
            "last_message": round_.get("last_message"),
            "usage": round_.get("usage"),
            "indexed": "indexed" in publication,
            "publication": publication,
            "error": round_.get("error"),
            "retryable": round_.get("retryable"),
            "warnings": self.warnings,
        }
        result.update(extra)
        return result

    # ---- the round ------------------------------------------------------

    def run(self) -> tuple[dict, int]:
        args = self.args
        depth = int(os.environ.get(DEPTH_ENV) or 0)
        if depth >= 1 and not args.allow_nested:
            raise UsageError(
                f"refusing a nested invoke ({DEPTH_ENV}={depth}); a peer must not invoke "
                "another peer unless --allow-nested is passed"
            )
        if self.peer == "codex" and getattr(args, "permission_mode", None):
            raise UsageError("--permission-mode is Claude-only; use --sandbox or --profile")
        if self.peer == "claude" and getattr(args, "sandbox", None):
            raise UsageError("--sandbox is Codex-only; use --permission-mode or --profile")
        if args.fork and not args.resume:
            raise UsageError("--fork needs --resume THREAD_ID (the thread to fork)")
        caller, caller_warnings = resolve_caller(args.parent)
        prompt = _read_prompt(args)

        recorded: ThreadState | None = None
        if args.resume:
            try:
                source_dir = locate(self.config, args.resume, self.peer)
            except LookupError as exc:
                raise UsageError(str(exc)) from exc
            recorded = ThreadState.load(source_dir / "thread.json")
            if caller is None:
                # A follow-up from a context with no host hint continues for the
                # session that started the thread; --parent still wins.
                last = recorded.last_round or {}
                caller = last.get("caller_session") or recorded.caller_session
                if caller:
                    caller_warnings = []
        self.warnings += caller_warnings
        settings = self.settings(recorded)
        if recorded is None and not args.skip_self_check:
            problems = self.harness.self_check(settings.model, settings.effort, settings.fast)
            if problems:
                raise UsageError("self-check failed: " + "; ".join(problems))

        root = invoke_root(self.config) / self.peer
        session_id: str | None = None
        if recorded is not None and not args.fork:
            assert recorded.path is not None
            self.run_dir = recorded.path.parent
            self.thread_id = recorded.thread_id
        elif self.peer == "claude" and recorded is None:
            session_id = str(uuid.uuid4())
            self.thread_id = session_id
            self.run_dir = root / session_id
        else:
            self.run_dir = root / f"pending-{uuid.uuid4()}"

        try:
            self.lock.acquire(self.run_dir)
        except BlockingIOError:
            holder = ThreadState.load(self.run_dir / "thread.json") if recorded else None
            self.state = holder or ThreadState(self.peer, self.thread_id, "", "", [])
            return self.result("busy", holder=_holder(holder)), EXIT_BUSY
        try:
            return self._run_locked(recorded, settings, prompt, caller, session_id, depth)
        finally:
            self.lock.release()

    def _new_state(self, recorded: ThreadState | None, settings: Settings, caller: str | None):
        if recorded is not None and not self.args.fork:
            state = ThreadState.load(self.run_dir / "thread.json")  # re-read under the lock
            last = state.last_round
            if last and not last.get("finished_at"):
                if identity_alive(last.get("peer_pid"), last.get("peer_started")):
                    return state, "orphaned"
                adopt(self.config, state, last)
            return state, None
        state = ThreadState(
            peer=self.peer,
            thread_id=self.thread_id,
            cwd=str(settings.cwd),
            profile=settings.profile,
            native_policy=list(settings.native_policy),
            model=settings.model,
            effort=settings.effort,
            caller_session=caller,
            forked_from=recorded.thread_id if recorded is not None else None,
        )
        state.path = self.run_dir / "thread.json"
        return state, None

    def _run_locked(
        self,
        recorded: ThreadState | None,
        settings: Settings,
        prompt: str,
        caller: str | None,
        session_id: str | None,
        depth: int,
    ) -> tuple[dict, int]:
        state, blocked = self._new_state(recorded, settings, caller)
        self.state = state
        if blocked:
            return self.result(
                "busy",
                holder=_holder(state),
                error="an orphaned peer from an earlier round is still running; "
                "wait for it or run 'cairn invoke cancel'",
            ), EXIT_BUSY
        # Thread-level settings follow the latest round (the next resume reuses them).
        state.profile, state.native_policy = settings.profile, list(settings.native_policy)
        state.model, state.effort = settings.model, settings.effort
        if state.caller_session is None and caller:
            state.caller_session = caller
        number = len(state.rounds) + 1
        files = _round_files(self.run_dir, number)
        full_prompt = (
            f"{preamble(self.peer, caller, number, settings.timeout_s)}\n\n---\n\n{prompt}"
        )
        via_stdin = len(full_prompt.encode("utf-8")) > STDIN_PROMPT_BYTES
        resume_id = recorded.thread_id if recorded is not None else None
        extra = settings.extra
        if self.args.fork and self.peer == "claude":
            extra = ("--fork-session", *extra)
        # Codex writes ``-o`` at the end of the run. The target lives outside the run
        # directory, which may be renamed mid-run, and moves to answer-NN.md once the
        # peer has exited (by the runner, or by whoever adopts the round).
        answer_target: Path | None = None
        if self.peer == "codex":
            answer_target = invoke_root(self.config) / self.peer / ".answers" / f"{uuid.uuid4()}.md"
            answer_target.parent.mkdir(parents=True, exist_ok=True)
        request = InvokeRequest(
            peer=self.peer,
            prompt=full_prompt,
            cwd=settings.cwd,
            profile=settings.profile,
            native_policy=settings.native_policy,
            model=settings.model,
            effort=settings.effort,
            fast=settings.fast,
            timeout_s=settings.timeout_s,
            resume=resume_id,
            session_id=session_id,
            output_schema=settings.output_schema,
            last_message_path=answer_target,
            prompt_via_stdin=via_stdin,
            extra=extra,
        )
        argv = self.harness.exec_argv(request)
        if self.args.fork and self.peer == "codex":
            # ``codex exec fork`` takes exactly the options of ``codex exec resume``.
            if argv[1:3] != ["exec", "resume"]:
                raise RuntimeError(f"unexpected codex resume argv: {argv[:3]}")
            argv[2] = "fork"
        recorded_argv = list(argv)
        if not via_stdin and recorded_argv and recorded_argv[-1] == full_prompt:
            recorded_argv[-1] = "<prompt>"
        runner = process_identity(os.getpid())
        self.round = {
            "round": number,
            "prompt": prompt,
            "argv": recorded_argv,
            "model": settings.model,
            "effort": settings.effort,
            "profile": settings.profile,
            "native_policy": list(
                self.harness.policy_argv(settings.profile, settings.native_policy)
            ),
            "caller_session": caller,
            "timeout_s": settings.timeout_s,
            "idle_timeout_s": settings.idle_timeout_s,
            "prompt_via_stdin": via_stdin,
            "no_index": bool(self.args.no_index),
            "started_at": now_iso(),
            "heartbeat_at": None,
            "last_event": None,
            "finished_at": None,
            "runner_pid": os.getpid(),
            "runner_started": runner[1] if runner else None,
            "peer_pid": None,
            "peer_pgid": None,
            "peer_started": None,
            **{key: str(path) for key, path in files.items() if key != "result_path"},
            "answer_target": str(answer_target) if answer_target else None,
            "publication": {"pending": {}},
        }
        state.rounds.append(self.round)
        if state.thread_id:
            state.resume_command = " ".join(self.harness.resume(state.thread_id))
        self._lookup_caller_doc(state, caller)
        state.save(self.run_dir / "thread.json")
        return self._spawn_and_pump(argv, request, depth, caller)

    def _lookup_caller_doc(self, state: ThreadState, caller: str | None) -> None:
        """Find an already exported caller document so the exchange document can be
        written mid-run (best effort; the post-run export is authoritative)."""
        if not caller or state.caller_session_doc or state.anchor:
            return
        source, native = _split_ref(caller)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                document = _load_source(source).find_file(native)
        except Exception:  # a lookup is an optimisation, never a failure
            return
        if document is not None and state.caller_session == caller:
            state.caller_session_doc = str(document)

    # ---- process ----------------------------------------------------------

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        self.cancel_requested = True

    def _spawn_and_pump(
        self, argv: list[str], request: InvokeRequest, depth: int, caller: str | None
    ) -> tuple[dict, int]:
        files = _round_files(self.run_dir, int(self.round["round"]))
        started = time.monotonic()
        previous = {
            sig: signal.signal(sig, self._on_signal) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        process: subprocess.Popen[bytes] | None = None
        try:
            with (
                files["events_path"].open("ab") as events,
                files["stderr_path"].open("ab") as stderr,
            ):
                try:
                    if self.thread_id:
                        print(
                            f"cairn invoke: {self.peer} thread {self.thread_id} "
                            f"round {int(self.round['round'])} (run dir {self.run_dir})",
                            file=sys.stderr,
                            flush=True,
                        )
                    process = subprocess.Popen(
                        argv,
                        cwd=str(request.cwd),
                        env=scrubbed_env(depth, caller),
                        stdin=subprocess.PIPE if request.prompt_via_stdin else subprocess.DEVNULL,
                        stdout=events,
                        stderr=stderr,
                        start_new_session=True,
                        close_fds=True,
                    )
                except OSError as exc:
                    self.round["error"] = f"cannot start {argv[0]}: {exc}"
                    return self._finalise(None, Replay(), "failed", 127, time.monotonic() - started)
                identity = process_identity(process.pid)
                self.round.update(
                    peer_pid=process.pid,
                    peer_pgid=process.pid,
                    peer_started=identity[1] if identity else None,
                )
                self.state.save()
                outcome, replay = self._pump(process, files["events_path"], request, started)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if process is not None and process.poll() is None:
                self._kill(process, graceful=False)
        assert process is not None
        rc = process.returncode
        return self._finalise(process, replay, outcome, rc, time.monotonic() - started)

    def _kill(self, process: subprocess.Popen[bytes], *, graceful: bool) -> None:
        pgid = process.pid
        if graceful:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(KILL_GRACE_S)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(KILL_GRACE_S)

    def _adopt_thread_id(self, thread_id: str) -> None:
        """Rename ``pending-<uuid>/`` to the thread id, holding both locks across it.

        The destination lock is taken before the directory appears under its final
        name, so no reader (``publish``, ``status``) can lock it first; ``thread.json``
        is rewritten with the real id before the pending lock is released.
        """
        target = self.run_dir.parent / thread_id
        if target.exists():
            raise RuntimeError(f"run directory already exists for thread {thread_id}: {target}")
        destination = ThreadLock()
        destination.acquire(target)
        pending = self.run_dir
        try:
            os.rename(pending, target)
        except OSError:
            destination.release()
            raise
        self.run_dir = target
        self.thread_id = thread_id
        self.state.thread_id = thread_id
        self.state.path = target / "thread.json"
        self.state.resume_command = " ".join(self.harness.resume(thread_id))
        number = int(self.round["round"])
        for key, path in _round_files(target, number).items():
            if key != "result_path":
                self.round[key] = str(path)
        self.state.save()
        self.lock.release()
        self.lock = destination
        # The skills read this line to learn the id while the run is still going.
        print(
            f"cairn invoke: {self.peer} thread {thread_id} round {number} (run dir {target})",
            file=sys.stderr,
            flush=True,
        )

    def _pump(
        self,
        process: subprocess.Popen[bytes],
        events_path: Path,
        request: InvokeRequest,
        started: float,
    ) -> tuple[str | None, Replay]:
        """Tail the events file the peer writes its stdout to, with the watchdogs.

        The peer writes straight into ``events-NN.jsonl``, so a runner that dies
        loses nothing: the stream keeps landing on disk and an adopting reader sees
        the final event. The loop ends only when the process has exited and the
        file is drained; the wall clock, idle watchdog and cancellation keep
        running whatever the peer does with its stdout.
        """
        replay = Replay()
        pending_input = request.prompt.encode("utf-8") if request.prompt_via_stdin else b""
        stdin_fd: int | None = None
        if process.stdin is not None:
            stdin_fd = process.stdin.fileno()
            os.set_blocking(stdin_fd, False)
        deadline = started + int(self.round["timeout_s"])
        idle_timeout = int(self.round["idle_timeout_s"])
        beat_interval = max(0.05, min(TRANSCRIPT_POLL_S, idle_timeout / 4))
        last_beat = time.time()
        last_state_write = 0.0
        last_exchange = time.monotonic()
        last_transcript_check = 0.0
        transcript_mtime: float | None = None
        tail = LineTail()
        outcome: str | None = None
        dirty = False

        def handle(line: bytes) -> bool:
            parsed = self.harness.parse_event(line.decode("utf-8", errors="replace"))
            replay.feed(parsed)
            message = False
            for event in parsed:
                if event.kind == "thread_started" and event.thread_id and not self.thread_id:
                    self._adopt_thread_id(event.thread_id)
                message = message or event.kind == "message"
            return message

        def drain(reader: Any, *, final: bool) -> tuple[bool, bool]:
            """Parse every complete line now on disk: ``(read_any, saw_message)``."""
            read_any = message = False
            while chunk := reader.read(65536):
                read_any = True
                for line in tail.feed(chunk):
                    message = handle(line) or message
            if final:
                for line in tail.flush():
                    message = handle(line) or message
            return read_any, message

        def transcript_beat() -> None:
            nonlocal transcript_mtime, last_beat, dirty
            if not self.thread_id:
                return
            path = _transcript(self.config, self.harness, self.state, self.round)
            try:
                mtime = path.stat().st_mtime if path is not None else None
            except OSError:
                mtime = None
            if mtime is not None and mtime != transcript_mtime:
                transcript_mtime = mtime
                last_beat = max(last_beat, mtime)
                dirty = True

        def feed_stdin() -> None:
            nonlocal pending_input, stdin_fd
            if stdin_fd is None:
                return
            try:
                written = os.write(stdin_fd, pending_input[:65536])
                pending_input = pending_input[written:]
            except BlockingIOError:
                return
            except BrokenPipeError:
                pending_input = b""
            if not pending_input:
                assert process.stdin is not None
                with contextlib.suppress(BrokenPipeError):
                    process.stdin.close()
                stdin_fd = None

        with events_path.open("rb") as reader:
            while True:
                feed_stdin()
                exited = process.poll() is not None
                read_any, message = drain(reader, final=exited)
                if read_any:
                    last_beat = time.time()
                    dirty = True
                if exited:
                    break
                now_mono = time.monotonic()
                if self.cancel_requested:
                    self._kill(process, graceful=True)
                    outcome = "cancelled"
                    break
                if now_mono >= deadline:
                    self._kill(process, graceful=False)
                    outcome = "timeout"
                    break
                if now_mono - last_transcript_check >= beat_interval:
                    last_transcript_check = now_mono
                    transcript_beat()
                if time.time() - last_beat >= idle_timeout:
                    transcript_beat()  # never declare idle on a stale reading
                    if time.time() - last_beat >= idle_timeout:
                        self._kill(process, graceful=False)
                        outcome = "idle_timeout"
                        break
                now_wall = time.time()
                if dirty and (message or now_wall - last_state_write >= STATE_WRITE_INTERVAL_S):
                    self._write_progress(replay, last_beat)
                    last_state_write, dirty = now_wall, False
                if message or time.monotonic() - last_exchange >= EXCHANGE_REFRESH_S:
                    last_exchange = time.monotonic()
                    self._refresh_exchange(replay, last_beat)
                if not read_any:
                    time.sleep(max(0.0, min(PUMP_POLL_S, deadline - now_mono)))
            # Keep whatever the peer wrote while it was being stopped.
            drain(reader, final=True)
        return outcome, replay

    def _write_progress(self, replay: Replay, last_beat: float) -> None:
        from datetime import UTC, datetime

        self.round.update(
            heartbeat_at=datetime.fromtimestamp(last_beat, UTC).isoformat(timespec="seconds"),
            last_event=replay.last_event,
            progress=list(replay.progress),
            last_message=replay.last_message,
        )
        self.state.save()

    def _refresh_exchange(self, replay: Replay, last_beat: float) -> None:
        if not self.state.thread_id:
            return
        self._write_progress(replay, last_beat)
        try:
            write_exchange(self.config, self.state, describe_folder=not self.args.no_index)
        except Exception as exc:  # mid-run refresh is best effort
            note = f"exchange refresh: {_clip(str(exc), 300)}"
            if note not in self.warnings:
                self.warnings.append(note)
            return
        self.state.save()

    # ---- finalise -----------------------------------------------------------

    def _finalise(
        self,
        process: subprocess.Popen[bytes] | None,
        replay: Replay,
        outcome: str | None,
        rc: int | None,
        duration: float,
    ) -> tuple[dict, int]:
        files = _round_files(self.run_dir, int(self.round["round"]))
        stderr = _tail(files["stderr_path"])
        done = replay.done
        error: str | None = self.round.get("error")
        if outcome is None:
            succeeded = done is not None and done.status == "succeeded" and rc == 0
            outcome = "succeeded" if succeeded else "failed"
        if outcome == "failed":
            error = error or (done.error if done and done.error else None) or stderr.strip()
            if not error:
                error = f"peer exited with code {rc} and no final event"
        elif outcome == "timeout":
            error = f"wall-clock timeout after {self.round['timeout_s']}s"
        elif outcome == "idle_timeout":
            error = (
                f"no activity for {self.round['idle_timeout_s']}s "
                f"(last: {replay.last_event or 'none'})"
            )
        elif outcome == "cancelled":
            error = "cancelled by signal"
        text = f"{stderr}\n{error or ''}\n{(done.text if done else '') or ''}"
        retryable: bool | None
        if outcome in ("timeout", "idle_timeout"):
            retryable = True
        elif outcome == "cancelled":
            retryable = False
        else:
            retryable = _retryable(text)
        if outcome == "failed" and keychain_failure(text):
            error = KEYCHAIN_GUIDANCE
        if outcome == "succeeded":
            error, retryable = None, None
        exit_code = {
            "succeeded": EXIT_OK,
            "timeout": EXIT_TIMEOUT,
            "idle_timeout": EXIT_IDLE,
            "cancelled": EXIT_CANCELLED,
        }.get(outcome)
        if exit_code is None:
            exit_code = rc if isinstance(rc, int) and rc > 0 else EXIT_FATAL
        _finish(
            self.state,
            self.round,
            outcome=outcome,
            exit_code=exit_code,
            replay=replay,
            duration_s=duration,
            error=error,
            retryable=retryable,
        )
        self.round["peer_returncode"] = rc
        self.state.save()
        if self.state.thread_id:
            publish_round(
                self.config,
                self.state,
                self.round,
                index=not self.args.no_index,
                warnings=self.warnings,
            )
        else:
            self.warnings.append("no thread id was captured; nothing was published")
        result = self.result(outcome)
        atomic_write(files["result_path"], json.dumps(result, ensure_ascii=False) + "\n")
        return result, exit_code


# ---------------------------------------------------------------------------
# Publication drain (``cairn invoke publish``, end of ``cairn sync``)
# ---------------------------------------------------------------------------


def publish_thread(config: CairnConfig, run_dir: Path, *, index: bool = True) -> dict:
    """Adopt an abandoned round if needed, then re-run every pending step of every
    finished round. Returns ``{"thread_id", "rounds": {n: pending-after}, "busy"}``."""
    lock = ThreadLock()
    try:
        lock.acquire(run_dir)
    except BlockingIOError:
        return {"thread_id": run_dir.name, "busy": True, "rounds": {}}
    try:
        state = ThreadState.load(run_dir / "thread.json")
        last = state.last_round
        if last and not last.get("finished_at"):
            runner = identity_alive(last.get("runner_pid"), last.get("runner_started"))
            peer = identity_alive(last.get("peer_pid"), last.get("peer_started"))
            if not runner and not peer:
                adopt(config, state, last)
        report: dict[str, Any] = {}
        warnings: list[str] = []
        for round_ in state.rounds:
            if not round_.get("finished_at"):
                continue
            steps = pending_steps(round_)
            if not steps:
                continue
            publish_round(config, state, round_, steps=steps, index=index, warnings=warnings)
            report[str(round_["round"])] = dict(
                (round_.get("publication") or {}).get("pending") or {}
            )
        return {
            "thread_id": state.thread_id,
            "busy": False,
            "rounds": report,
            "warnings": warnings,
        }
    finally:
        lock.release()


def drain_publications(config: CairnConfig) -> list[dict]:
    """Finish pending publication steps for every thread (called by ``cairn sync``)."""
    reports = []
    for run_dir in iter_run_dirs(config):
        try:
            state = ThreadState.load(run_dir / "thread.json")
        except (OSError, ValueError) as exc:
            print(f"Warning: skipping {run_dir}: {exc}", file=sys.stderr)
            continue
        last = state.last_round
        open_round = bool(last and not last.get("finished_at"))
        if not open_round and not any(pending_steps(r) for r in state.rounds):
            continue
        reports.append(publish_thread(config, run_dir))
    return reports


# ---------------------------------------------------------------------------
# status / cancel / list-models
# ---------------------------------------------------------------------------


def _print_status(obs: Observation, as_json: bool) -> None:
    if as_json:
        print(json.dumps(observation_json(obs), ensure_ascii=False))
        return
    data = observation_json(obs)
    print(f"{obs.state.peer} thread {obs.state.thread_id} round {data['round']}")
    print(health_line(obs))
    progress = data["progress"]
    if progress:
        print("progress:")
        for item in progress:
            print(f"  - {item}")
    if obs.health.execution == "exited":
        print("answer:")
        print(data["last_message"] or "(no answer)")
        if obs.state.exchange_doc:
            print(f"exchange: {obs.state.exchange_doc}")


def unseen_progress(progress: list[str], dropped: int, seen: int) -> tuple[list[str], int]:
    """Bullets past absolute position ``seen`` and the new cursor. The replay keeps the
    newest :data:`PROGRESS_CAP` bullets and counts the ``dropped`` older ones, so the
    cursor is absolute and keeps advancing past the cap."""
    fresh = [item for offset, item in enumerate(progress) if dropped + offset >= seen]
    return fresh, max(seen, dropped + len(progress))


def cmd_status(config: CairnConfig, thread_id: str, follow: bool, as_json: bool) -> int:
    run_dir = locate(config, thread_id)
    if not follow:
        _print_status(observe(config, run_dir), as_json)
        return EXIT_OK
    seen = 0
    last_line = ""
    while True:
        obs = observe(config, run_dir)
        if as_json:
            print(json.dumps(observation_json(obs), ensure_ascii=False), flush=True)
        else:
            line = health_line(obs)
            if line != last_line:
                print(line, flush=True)
                last_line = line
            dropped = obs.replay.dropped if obs.replay.progress else 0
            fresh, seen = unseen_progress(observation_json(obs)["progress"], dropped, seen)
            for item in fresh:
                print(f"  - {item}", flush=True)
        if obs.health.execution in ("exited", "unknown"):
            if not as_json:
                print("answer:")
                print(observation_json(obs)["last_message"] or "(no answer)")
            return EXIT_OK
        time.sleep(1)


def cmd_cancel(config: CairnConfig, thread_id: str, as_json: bool) -> int:
    run_dir = locate(config, thread_id)
    state = ThreadState.load(run_dir / "thread.json")
    round_ = state.last_round
    message: str
    if round_ is None or round_.get("finished_at"):
        message = "nothing to cancel: the last round already exited"
    else:
        runner_alive = identity_alive(round_.get("runner_pid"), round_.get("runner_started"))
        peer_pid, peer_started = round_.get("peer_pid"), round_.get("peer_started")
        peer_alive = identity_alive(peer_pid, peer_started)
        if runner_alive:
            # The runner forwards SIGTERM to the peer group and finalises the round.
            os.kill(int(round_["runner_pid"]), signal.SIGTERM)
            deadline = time.monotonic() + KILL_GRACE_S * 2 + 5
            while time.monotonic() < deadline:
                current = ThreadState.load(run_dir / "thread.json").last_round or {}
                if current.get("finished_at"):
                    break
                time.sleep(0.2)
            message = "signalled the runner; it cancels the peer and finalises the round"
        elif peer_alive:
            pgid = int(round_.get("peer_pgid") or peer_pid or 0)
            if pgid <= 1:
                raise RuntimeError(f"refusing to signal process group {pgid}")
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + KILL_GRACE_S
            while time.monotonic() < deadline and identity_alive(peer_pid, peer_started):
                time.sleep(0.2)
            if identity_alive(peer_pid, peer_started):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
            publish_lock = ThreadLock()
            try:
                publish_lock.acquire(run_dir)
                try:
                    state = ThreadState.load(run_dir / "thread.json")
                    adopt(config, state, state.last_round or round_, outcome="cancelled")
                finally:
                    publish_lock.release()
            except BlockingIOError:
                pass
            message = "cancelled the orphaned peer and finalised the round"
        else:
            if isinstance(peer_pid, int) and process_identity(peer_pid) is not None:
                message = "pid reused; nothing signalled"
            else:
                message = "the peer is not running; nothing signalled"
            publish_thread(config, run_dir)
    obs = observe(config, run_dir)
    if as_json:
        print(json.dumps({"message": message, **observation_json(obs)}, ensure_ascii=False))
    else:
        print(message)
        print(health_line(obs))
    return EXIT_OK


def cmd_publish(config: CairnConfig, thread_id: str, as_json: bool) -> int:
    run_dir = locate(config, thread_id)
    report = publish_thread(config, run_dir)
    if report.get("busy"):
        message = "thread is busy (a round is running); try again when it ends"
        print(json.dumps({**report, "error": message}) if as_json else message)
        return EXIT_BUSY
    remaining = {n: p for n, p in report["rounds"].items() if p}
    if as_json:
        print(json.dumps({**report, "pending": remaining}, ensure_ascii=False))
    elif not report["rounds"]:
        print("nothing pending")
    else:
        for number, pending in report["rounds"].items():
            if pending:
                for step, reason in pending.items():
                    print(f"round {number}: {step} still pending: {reason}")
            else:
                print(f"round {number}: published")
    return EXIT_FATAL if remaining else EXIT_OK


def cmd_list_models(config: CairnConfig, as_json: bool) -> int:
    from .harnesses.codex_models import format_models, load_codex_model_catalog, models_cache_path

    catalog = load_codex_model_catalog()
    if not catalog:
        print(
            f"Error: no Codex model cache at {models_cache_path()}; run codex once", file=sys.stderr
        )
        return EXIT_FATAL
    print(format_models(catalog, config.invoke.codex.model, as_json))
    print(
        "note: claude has no model catalogue; pass any model name it accepts "
        "(efforts: low, medium, high, xhigh, max)",
        file=sys.stderr,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn invoke",
        description="Run Codex or Claude headlessly as a peer agent, one round per call.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = argparse.ArgumentParser(add_help=False)
    prompt = run.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="the prompt text ('-' reads stdin)")
    prompt.add_argument("--prompt-file", help="read the prompt from a file ('-' reads stdin)")
    run.add_argument("--model")
    run.add_argument("--effort")
    run.add_argument("--profile", choices=PROFILES, help="shared permission profile")
    run.add_argument("--fast", action="store_true", help="Codex fast tier")
    run.add_argument("--cd", help="working directory for a fresh thread")
    run.add_argument("--timeout-s", type=_positive, help="wall-clock budget in seconds")
    run.add_argument("--idle-timeout-s", type=_positive, help="kill after this long silent")
    run.add_argument("--resume", metavar="THREAD_ID", help="continue a recorded thread")
    run.add_argument("--fork", action="store_true", help="with --resume: fork into a new thread")
    run.add_argument("--parent", metavar="SOURCE:ID", help="the calling session")
    run.add_argument("--output-schema", metavar="FILE", help="JSON schema for the final answer")
    run.add_argument("--allow-nested", action="store_true", help="permit invoke inside a peer")
    run.add_argument("--skip-self-check", action="store_true", help="skip the model check")
    run.add_argument("--no-index", action="store_true", help="do not refresh the QMD index")
    run.add_argument("--json", action="store_true", help="print one JSON result line")
    codex = sub.add_parser("codex", parents=[run], help="run codex exec as a peer")
    codex.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    codex.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="native args")
    claude = sub.add_parser("claude", parents=[run], help="run claude -p as a peer")
    claude.add_argument(
        "--permission-mode",
        choices=("default", "plan", "acceptEdits", "bypassPermissions", "dontAsk", "auto"),
    )
    claude.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="native args")
    for name, text in (
        ("status", "health, progress and answer of a thread"),
        ("cancel", "stop the running round of a thread"),
        ("publish", "finish pending publication steps"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("thread_id")
        command.add_argument("--json", action="store_true")
        if name == "status":
            command.add_argument("--follow", action="store_true", help="poll until it exits")
    models = sub.add_parser("list-models", help="the Codex model catalogue")
    models.add_argument("--json", action="store_true")
    return parser


def _summary(result: dict, run_dir: Path | None) -> str:
    lines = [
        f"{result['peer']} thread {result.get('thread_id')} round {result.get('round')}: "
        f"{result['status']}"
        + (f" in {result['duration_s']:.1f}s" if result.get("duration_s") is not None else "")
        + (f" (exit {result['exit_code']})" if result.get("exit_code") is not None else "")
    ]
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    if result.get("last_message"):
        lines += ["", str(result["last_message"]), ""]
    for key in ("exchange_doc", "peer_session_doc", "events_path"):
        if result.get(key):
            lines.append(f"{key}: {result[key]}")
    for warning in result.get("warnings") or []:
        lines.append(f"warning: {warning}")
    if run_dir is not None and result.get("round"):
        lines.append(f"result: {_round_files(run_dir, int(result['round']))['result_path']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = get_config()
        if args.command in ("codex", "claude"):
            runner = Runner(config, args.command, args)
            result, code = runner.run()
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                run_dir = getattr(runner, "run_dir", None)
                print(_summary(result, run_dir if result["status"] != "busy" else None))
            return code
        if args.command == "status":
            return cmd_status(config, args.thread_id, args.follow, args.json)
        if args.command == "cancel":
            return cmd_cancel(config, args.thread_id, args.json)
        if args.command == "publish":
            return cmd_publish(config, args.thread_id, args.json)
        return cmd_list_models(config, args.json)
    except UsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except LookupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except (OSError, ValueError, RuntimeError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FATAL


__all__ = [
    "EXIT_BUSY",
    "EXIT_CANCELLED",
    "EXIT_IDLE",
    "EXIT_TIMEOUT",
    "PUBLICATION_STEPS",
    "Observation",
    "Replay",
    "adopt",
    "drain_publications",
    "invoke_root",
    "iter_run_dirs",
    "locate",
    "main",
    "observe",
    "preamble",
    "publish_round",
    "publish_thread",
    "replay_file",
]
