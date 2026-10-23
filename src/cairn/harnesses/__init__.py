"""Small host adapters: lifecycle input, identity, manifests, resume and invoke argv.

Each adapter also knows how to run its CLI headlessly as a peer agent for
``cairn invoke``: the exact native argv for a request, the native flags behind a
shared permission profile, the bytes a prompt takes on the child's stdin, how to
read one line of its JSON event stream, where its native transcript lands, and how
to check a model/effort choice up front. Claude and Codex are hosts and peers;
Antigravity (``agy``) is a peer whose conversations reach cairn through its
``Stop`` hook (:mod:`cairn.harnesses.antigravity_registration`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

# Substrings in a peer's stderr or error text that mark a request the service
# rejected (bad model, bad auth). Retrying the same request cannot succeed.
CLIENT_ERROR_MARKERS: tuple[str, ...] = (
    '"status":400',
    '"status":401',
    '"status":403',
    '"status":422',
    "invalid_request_error",
    "model is not support",
    "authentication",
    # agy: an unknown --model, or no cached login (verified live on 1.4.6)
    "invalid model selection",
    "not logged into antigravity",
)


def is_client_error(text: str) -> bool:
    """Whether ``text`` carries one of :data:`CLIENT_ERROR_MARKERS` (case-insensitive)."""
    low = text.lower()
    return any(marker.lower() in low for marker in CLIENT_ERROR_MARKERS)


@dataclass(frozen=True)
class SessionEvent:
    harness: str
    source: str
    session_id: str
    transcript_path: str | None
    cwd: str
    event: str


@dataclass(frozen=True)
class InvokeRequest:
    """One headless peer run, fully resolved (no config lookups left to do).

    ``native_policy`` holds explicit native permission flags (for example
    ``("-s", "read-only")`` or ``("--permission-mode", "plan")``); empty means
    derive them from ``profile``. ``resume`` is the thread id to continue, or
    ``None`` for a fresh run. ``session_id`` is the id chosen up front (Claude
    only). With ``prompt_via_stdin`` the prompt is not put on the command line.
    ``log_path`` is where the peer writes its own diagnostic log when its CLI can
    be told to (agy ``--log-file``); ``None`` leaves the peer's default.
    """

    peer: str
    prompt: str
    cwd: Path
    profile: str
    native_policy: tuple[str, ...]
    model: str | None
    effort: str | None
    fast: bool
    timeout_s: int
    resume: str | None
    session_id: str | None
    output_schema: Path | None
    last_message_path: Path | None
    prompt_via_stdin: bool
    extra: tuple[str, ...]
    log_path: Path | None = None


@dataclass(frozen=True)
class Event:
    """One normalised event from a peer's JSON stream.

    ``kind`` is ``thread_started`` | ``message`` | ``progress`` | ``notice`` |
    ``done``. ``status`` (``succeeded`` | ``failed``) is set on ``done`` only.
    ``raw_type`` keeps the native event type for logs and status lines.
    """

    kind: str
    text: str | None = None
    thread_id: str | None = None
    status: str | None = None
    error: str | None = None
    usage: dict | None = None
    raw_type: str = ""


class Harness(Protocol):
    name: str
    manifest_paths: tuple[str, ...]
    update_command: str
    executable: str

    def decode(self, payload: object) -> SessionEvent: ...
    def current_id(self) -> str | None: ...
    def resume(self, session_id: str, fork: bool = False) -> list[str]: ...
    def exec_argv(self, request: InvokeRequest) -> list[str]: ...
    def stdin_payload(self, prompt: str) -> bytes: ...
    def policy_argv(self, profile: str, native: tuple[str, ...] = ()) -> tuple[str, ...]: ...
    def parse_event(self, line: str) -> list[Event]: ...
    def transcript_path(
        self, store: Path, thread_id: str, started: datetime | None
    ) -> Path | None: ...
    def self_check(self, model: str | None, effort: str | None, fast: bool) -> list[str]: ...


def decode_event(name: str, payload: object) -> SessionEvent:
    if not isinstance(payload, dict):
        raise ValueError("hook input must be an object")
    if payload.get("hook_event_name") != "SessionEnd":
        raise ValueError("expected a SessionEnd event")
    for field in ("session_id", "cwd"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"hook requires a nonempty {field}")
        if any(ord(c) < 32 for c in payload[field]):
            raise ValueError(f"invalid {field}")
    transcript = payload.get("transcript_path")
    if transcript is not None and (
        not isinstance(transcript, str) or not Path(transcript).is_absolute()
    ):
        raise ValueError("transcript_path must be an absolute path or null")
    if not Path(payload["cwd"]).is_absolute():
        raise ValueError("cwd must be absolute")
    return SessionEvent(name, name, payload["session_id"], transcript, payload["cwd"], "SessionEnd")


def get_harness(name: str) -> Harness:
    from .antigravity import Antigravity
    from .claude import Claude
    from .codex import Codex

    adapters: dict[str, Harness] = {
        "claude": Claude(),
        "codex": Codex(),
        "antigravity": Antigravity(),
    }
    if name not in adapters:
        raise ValueError(f"unsupported harness {name!r}; choose claude, codex or antigravity")
    return adapters[name]


def detect_harness() -> str | None:
    # Compatibility PLUGIN_ROOT variables say where a plugin lives, not which
    # harness is executing it. Mixed session hints are deliberately ambiguous.
    explicit = os.environ.get("CAIRN_HARNESS")
    if explicit in ("claude", "codex"):
        return explicit
    try:
        present = [name for name in ("claude", "codex") if get_harness(name).current_id()]
    except ValueError:
        return None
    return present[0] if len(present) == 1 else None
