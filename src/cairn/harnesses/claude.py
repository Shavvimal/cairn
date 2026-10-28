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

# Config-profile ``extra_args`` rules (docs/routine-spec.md section 4.3). Flags checked
# against ``claude --help`` 2.1.312.
# The --permission-mode values each base may switch to: never a looser mode.
PROFILE_MODES: dict[str, tuple[str, ...] | None] = {
    "review": ("plan",),
    "edit": ("plan", "dontAsk", "acceptEdits"),
    "full": None,  # any value the CLI accepts
}
# Flags cairn invoke sets itself; a profile must not repeat or contradict them.
PROFILE_RESERVED_FLAGS = frozenset(
    {
        "-p",
        "--print",
        "--output-format",
        "--input-format",
        "--verbose",
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--session-id",
        "--fork-session",
        "--model",
        "--effort",
        "--json-schema",
        "--",
    }
)
# Flags that skip every permission prompt: only a ``full`` base may carry them.
PROFILE_FULL_ONLY_FLAGS = frozenset(
    {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
)
_TOOL_LIST_FLAGS = ("--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools")
_DISALLOWED_FLAGS = ("--disallowedTools", "--disallowed-tools")
_PAIRED_FLAGS = ("--permission-mode", *_TOOL_LIST_FLAGS)

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


def split_rules(value: str) -> list[str]:
    """Split a comma-joined permission rule list, keeping commas inside ``(...)``."""
    rules: list[str] = []
    depth = 0
    current = ""
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            rules.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        rules.append(current.strip())
    return [rule for rule in rules if rule]


def _space_form_warning(rule: str) -> str | None:
    """The warning for a ``Bash(x *)`` rule: Claude only matches ``Bash(x:*)``."""
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return None
    inner = rule[len("Bash(") : -1]
    if " " not in inner or ":*" in inner:
        return None
    prefix = inner[:-2] if inner.endswith(" *") else inner
    return (
        f"rule {rule!r} uses the space form; Claude matches only the prefix form "
        f"'Bash({prefix.strip()}:*)'"
    )


def check_profile_args(base: str, args: tuple[str, ...]) -> list[str]:
    """Validate a config profile's ``extra_args`` for base ``base``.

    Raises ``ValueError`` with the reason; returns warnings that do not fail the load.
    """
    if base not in PROFILE_MODES:
        raise ValueError(f"unknown base {base!r}; choose {', '.join(PROFILE_MODES)}")
    warnings: list[str] = []
    seen_mode = False
    index = 0
    while index < len(args):
        token = args[index]
        name = token.split("=", 1)[0] if token.startswith("--") else token
        if name in PROFILE_RESERVED_FLAGS:
            raise ValueError(f"{name} is set by cairn invoke; remove it")
        if name == "--max-budget-usd":
            raise ValueError(
                "--max-budget-usd is set from the routine's budget_usd_per_day; remove it"
            )
        if name in PROFILE_FULL_ONLY_FLAGS and base != "full":
            raise ValueError(f"{name} needs base 'full'")
        if name in _PAIRED_FLAGS and name != token:
            raise ValueError(f"write {name} and its value as two argv elements")
        if token in _PAIRED_FLAGS:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise ValueError(f"{token} needs a value")
            value = args[index + 1]
            if token == "--permission-mode":
                if seen_mode:
                    raise ValueError("--permission-mode appears twice; keep one")
                seen_mode = True
                allowed = PROFILE_MODES[base]
                if allowed is not None and value not in allowed:
                    raise ValueError(
                        f"--permission-mode {value!r} would loosen base {base!r}; "
                        f"allowed: {', '.join(allowed)}"
                    )
                index += 2
                continue
            following = []
            cursor = index + 2
            while cursor < len(args) and not args[cursor].startswith("-"):
                following.append(args[cursor])
                cursor += 1
            if following:
                parts = ", ".join(repr(part) for part in (value, *following))
                raise ValueError(f"{token} takes one comma-joined string; join {parts} with ','")
            for rule in split_rules(value):
                warning = _space_form_warning(rule)
                if warning:
                    warnings.append(warning)
            index += 2
            continue
        index += 1
    return warnings


def merge_profile_args(base_argv: tuple[str, ...], args: tuple[str, ...]) -> tuple[str, ...]:
    """The base's native flags merged with a profile's ``extra_args``.

    Rule A: a ``--permission-mode X`` in ``args`` replaces the base's mode value in
    place. Rule B: a ``--disallowedTools V`` is appended to the base's own
    ``--disallowedTools`` list when it has one. Everything else follows in order.
    """
    merged = list(base_argv)
    index = 0
    while index < len(args):
        token = args[index]
        value = args[index + 1] if index + 1 < len(args) else None
        if token == "--permission-mode" and value is not None and token in merged:
            merged[merged.index(token) + 1] = value
            index += 2
            continue
        if token in _DISALLOWED_FLAGS and value is not None:
            present = next((flag for flag in _DISALLOWED_FLAGS if flag in merged), None)
            if present is not None:
                at = merged.index(present) + 1
                merged[at] = f"{merged[at]},{value}"
                index += 2
                continue
        merged.append(token)
        index += 1
    return tuple(merged)


def _with_mode(argv: tuple[str, ...], native: tuple[str, ...]) -> tuple[str, ...]:
    """``argv`` with its ``--permission-mode`` value replaced by ``native``'s."""
    if "--permission-mode" not in native:
        return argv
    mode = native[native.index("--permission-mode") + 1]
    merged = list(argv)
    if "--permission-mode" in merged:
        merged[merged.index("--permission-mode") + 1] = mode
    else:
        merged[:0] = ["--permission-mode", mode]
    return tuple(merged)


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

    def policy_argv(
        self,
        profile: str,
        native: tuple[str, ...] = (),
        profile_args: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        """The exact native permission flags.

        ``profile`` is a built-in base. ``profile_args`` is ``None`` for a built-in
        profile, else a config profile's ``extra_args``. For a built-in, ``native``
        replaces the whole tuple. For a config profile, the base is merged with the
        args and ``native`` replaces only the ``--permission-mode`` value, so the
        profile's allow and deny rules survive an explicit mode.
        """
        if native and profile_args is None:
            return native
        try:
            base = PERMISSIONS_BY_PROFILE[profile]
        except KeyError:
            known = ", ".join(PERMISSIONS_BY_PROFILE)
            raise ValueError(f"unknown profile {profile!r}; choose {known}") from None
        if profile_args is None:
            return base
        return _with_mode(merge_profile_args(base, profile_args), native)

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
            *self.policy_argv(request.profile, request.native_policy, request.profile_args),
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

    def stdin_payload(self, prompt: str) -> bytes:
        """``claude -p`` with no prompt argument reads the raw prompt from stdin."""
        return prompt.encode("utf-8")

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
