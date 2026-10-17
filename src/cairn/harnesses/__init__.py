"""Small host adapters: lifecycle input, identity, manifests and resume argv."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SessionEvent:
    harness: str
    source: str
    session_id: str
    transcript_path: str | None
    cwd: str
    event: str


class Harness(Protocol):
    name: str
    manifest_paths: tuple[str, ...]
    update_command: str

    def decode(self, payload: object) -> SessionEvent: ...
    def current_id(self) -> str | None: ...
    def resume(self, session_id: str, fork: bool = False) -> list[str]: ...


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
    from .claude import Claude
    from .codex import Codex

    adapters: dict[str, Harness] = {"claude": Claude(), "codex": Codex()}
    if name not in adapters:
        raise ValueError(f"unsupported harness {name!r}; choose claude or codex")
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
