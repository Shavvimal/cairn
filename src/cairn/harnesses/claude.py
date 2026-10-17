import json
import os
from datetime import datetime
from pathlib import Path

from ..jsonl import find_session_jsonl
from . import Event, InvokeRequest, SessionEvent, decode_event

# Shared profile -> Claude permission flags. These are tool-permission guarantees
# only, not an OS sandbox: Claude's own sandbox settings govern process isolation.
PERMISSIONS_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "review": ("--permission-mode", "plan", "--disallowedTools", "Edit,Write,NotebookEdit"),
    "edit": ("--permission-mode", "acceptEdits"),
    "full": ("--permission-mode", "bypassPermissions"),
}
CLAUDE_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Inside the Codex macOS sandbox ``claude -p`` cannot read its login keychain and
# reports one of these. The runner matches them in stderr or the result text.
KEYCHAIN_MARKERS: tuple[str, ...] = ("Not logged in", "not authenticated")
KEYCHAIN_GUIDANCE = (
    "claude cannot reach its login keychain inside the Codex sandbox; "
    "rerun with escalated permissions or from the Claude host"
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _assistant_events(message: object) -> list[Event]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    tools: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)
            tools.append(
                Event(
                    "progress",
                    text=f"tool: {block.get('name', '?')} {_clip(arguments, 120)}",
                    raw_type="assistant:tool_use",
                )
            )
    events = [Event("message", text="\n".join(texts), raw_type="assistant")] if texts else []
    return events + tools


class Claude:
    name = "claude"
    executable = "claude"
    manifest_paths: tuple[str, ...] = (".claude-plugin/plugin.json",)
    update_command = "/plugin update cairn@cairn, then /reload-plugins"

    def decode(self, payload: object) -> SessionEvent:
        return decode_event(self.name, payload)

    def current_id(self) -> str | None:
        # Claude Code exports CLAUDE_CODE_SESSION_ID to every child process (2.1.x);
        # CLAUDE_SESSION_ID is only a template variable substituted into skill text,
        # so it is accepted for callers that export it by hand.
        native = os.environ.get("CLAUDE_CODE_SESSION_ID")
        manual = os.environ.get("CLAUDE_SESSION_ID")
        if native and manual and native != manual:
            raise ValueError("conflicting Claude session hints; pass --session-id")
        return native or manual or None

    def resume(self, session_id: str, fork: bool = False) -> list[str]:
        return ["claude", "--resume", session_id, *(["--fork-session"] if fork else [])]

    def policy_argv(self, profile: str, native: tuple[str, ...] = ()) -> tuple[str, ...]:
        """The exact native permission flags: ``native`` when given, else the profile's."""
        if native:
            return native
        try:
            return PERMISSIONS_BY_PROFILE[profile]
        except KeyError:
            known = ", ".join(PERMISSIONS_BY_PROFILE)
            raise ValueError(f"unknown profile {profile!r}; choose {known}") from None

    def exec_argv(self, request: InvokeRequest) -> list[str]:
        """The ``claude -p`` argv: ``--session-id`` for a fresh run, ``--resume`` otherwise.

        A resume re-sends the permission flags, because ``claude -p`` does not keep
        them with the session.
        """
        if request.resume:
            identity = ["--resume", request.resume]
        elif request.session_id:
            identity = ["--session-id", request.session_id]
        else:
            raise ValueError("a fresh claude run needs a session_id chosen up front")
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            *identity,
            *self.policy_argv(request.profile, request.native_policy),
        ]
        if request.model:
            argv += ["--model", request.model]
        if request.effort:
            argv += ["--effort", request.effort]
        if request.output_schema is not None:
            argv += ["--json-schema", request.output_schema.read_text(encoding="utf-8")]
        argv += request.extra
        if not request.prompt_via_stdin:
            # ``--disallowedTools`` (and any variadic flag in ``extra``) would swallow a
            # bare trailing prompt; ``--`` ends option parsing. Verified live on 2.1.301.
            argv += ["--", request.prompt]
        return argv

    def parse_event(self, line: str) -> list[Event]:
        """Map one ``claude -p --output-format stream-json`` line to zero or more events."""
        line = line.strip()
        if not line:
            return []
        try:
            data = json.loads(line)
        except ValueError:
            return [Event("notice", text=f"non-JSON stdout: {_clip(line, 500)}")]
        if not isinstance(data, dict):
            return [Event("notice", text=f"non-JSON stdout: {_clip(line, 500)}")]
        event_type = str(data.get("type") or "")
        if event_type == "system":
            if data.get("subtype") == "init":
                return [
                    Event(
                        "thread_started",
                        thread_id=data.get("session_id"),
                        text=data.get("model"),
                        raw_type="system:init",
                    )
                ]
            return []
        if event_type == "assistant":
            return _assistant_events(data.get("message"))
        if event_type == "result":
            subtype = str(data.get("subtype") or "")
            result = data.get("result")
            text = result if isinstance(result, str) else None
            succeeded = subtype == "success" and not data.get("is_error")
            usage = {key: data.get(key) for key in ("duration_ms", "total_cost_usd", "num_turns")}
            return [
                Event(
                    "done",
                    text=text,
                    status="succeeded" if succeeded else "failed",
                    error=None if succeeded else f"{subtype}: {(text or '')[:500]}",
                    usage=usage,
                    raw_type=f"result:{subtype}",
                )
            ]
        return []

    def transcript_path(self, store: Path, thread_id: str, started: datetime | None) -> Path | None:
        """The session's JSONL under the Claude projects store (``started`` is unused)."""
        return find_session_jsonl(store, thread_id)

    def self_check(self, model: str | None, effort: str | None, fast: bool) -> list[str]:
        """Problems with an effort/fast choice. Any model name is passed through."""
        problems: list[str] = []
        if effort is not None and effort not in CLAUDE_EFFORTS:
            problems.append(
                f"effort {effort!r} is not supported by claude; supported: "
                f"{', '.join(CLAUDE_EFFORTS)}"
            )
        if fast:
            problems.append("fast mode is Codex-only")
        return problems


def keychain_failure(text: str) -> bool:
    """Whether ``text`` (stderr or a result) shows the keychain-unreachable login failure."""
    low = text.lower()
    return any(marker.lower() in low for marker in KEYCHAIN_MARKERS)
