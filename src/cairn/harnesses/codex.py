import json
import os
import selectors
import subprocess
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from . import Event, InvokeRequest, SessionEvent, decode_event
from .codex_models import check_codex_invocation, load_codex_model_catalog, models_cache_path

# Shared profile -> Codex sandbox mode. Every run also gets approval_policy="never",
# so no approval prompt can stall a headless run.
SANDBOX_BY_PROFILE: dict[str, str] = {
    "review": "read-only",
    "edit": "workspace-write",
    "full": "danger-full-access",
}
_APPROVAL_NEVER = ("-c", 'approval_policy="never"')
_PROGRESS_ITEMS = ("mcp_tool_call", "web_search", "collab_tool_call")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _sandbox_mode(profile: str, native: tuple[str, ...]) -> str:
    """The sandbox mode a request runs under: an explicit ``-s``/``--sandbox`` wins."""
    for index, arg in enumerate(native):
        if arg in ("-s", "--sandbox") and index + 1 < len(native):
            return native[index + 1]
        if arg.startswith("--sandbox="):
            return arg.split("=", 1)[1]
    try:
        return SANDBOX_BY_PROFILE[profile]
    except KeyError:
        known = ", ".join(SANDBOX_BY_PROFILE)
        raise ValueError(f"unknown profile {profile!r}; choose {known}") from None


def _codex_item_events(event_type: str, item: dict) -> list[Event]:
    item_type = str(item.get("type") or "")
    raw = f"{event_type}:{item_type}"
    completed = event_type == "item.completed"
    if item_type == "agent_message":
        if completed:
            return [Event("message", text=str(item.get("text") or ""), raw_type=raw)]
        return []
    if item_type == "error":
        if completed:
            # Not a failure: codex 0.160 emits hooks-deprecation errors at the start
            # of every stream. Only turn.failed or a top-level error fail a run.
            return [Event("notice", text=str(item.get("message") or ""), raw_type=raw)]
        return []
    if item_type == "command_execution":
        command = _clip(str(item.get("command") or ""), 200)
        if event_type == "item.started":
            return [Event("progress", text=f"run: {command}", raw_type=raw)]
        if completed:
            return [
                Event("progress", text=f"exit {item.get('exit_code')}: {command}", raw_type=raw)
            ]
        return []
    if item_type == "file_change":
        if not completed:
            return []
        changes = item.get("changes") or []
        parts = [
            f"{change.get('kind', 'update')} {change.get('path', '?')}"
            for change in changes
            if isinstance(change, dict)
        ]
        return [Event("progress", text=f"edit: {_clip(', '.join(parts), 200)}", raw_type=raw)]
    if item_type == "todo_list":
        if event_type not in ("item.updated", "item.completed"):
            return []
        steps = [
            f"[{'x' if step.get('completed') else ' '}] {step.get('text', '')}"
            for step in item.get("items") or []
            if isinstance(step, dict)
        ]
        return [Event("progress", text=f"plan: {'; '.join(steps)}", raw_type=raw)]
    if item_type == "reasoning":
        if not completed:
            return []
        return [
            Event(
                "progress",
                text=f"thinking: {_clip(str(item.get('text') or ''), 200)}",
                raw_type=raw,
            )
        ]
    if item_type in _PROGRESS_ITEMS:
        if not completed:
            return []
        detail = item.get("query") or item.get("tool") or item.get("prompt") or ""
        server = item.get("server")
        if server:
            detail = f"{server}.{detail}"
        return [Event("progress", text=f"{item_type}: {_clip(str(detail), 200)}", raw_type=raw)]
    return []


class Codex:
    name = "codex"
    executable = "codex"
    manifest_paths: tuple[str, ...] = ("plugin.json", ".codex-plugin/plugin.json")
    update_command = (
        "codex plugin marketplace upgrade cairn; codex plugin add cairn@cairn; restart Codex"
    )

    def decode(self, payload: object) -> SessionEvent:
        # Event identity is authoritative. Nested client processes can inherit
        # stale CODEX_* IDs; never substitute those for the payload's session_id.
        return decode_event(self.name, payload)

    def current_id(self) -> str | None:
        thread = os.environ.get("CODEX_THREAD_ID")
        session = os.environ.get("CODEX_SESSION_ID")
        if thread and session and thread != session:
            raise ValueError("conflicting Codex session hints; pass --session-id")
        return thread or session or None

    def resume(self, session_id: str, fork: bool = False) -> list[str]:
        return ["codex", "fork" if fork else "resume", session_id]

    def policy_argv(self, profile: str, native: tuple[str, ...] = ()) -> tuple[str, ...]:
        """The exact native sandbox flags: ``native`` when given, else the profile's."""
        if native:
            return native
        return ("-s", _sandbox_mode(profile, ()))

    def exec_argv(self, request: InvokeRequest) -> list[str]:
        """The ``codex exec`` argv for a fresh run, or ``codex exec resume`` for a resume.

        Resume has no ``-C`` and no ``-s``: the runner starts it in the recorded cwd,
        and the sandbox is re-asserted with ``-c sandbox_mode=...`` so a resume can
        never silently widen it.
        """
        options: list[str] = []
        if request.model:
            options += ["-m", request.model]
        if request.effort:
            options += ["-c", f'model_reasoning_effort="{request.effort}"']
        if request.fast:
            options += ["--enable", "fast_mode", "-c", 'service_tier="priority"']
        outputs: list[str] = []
        if request.output_schema is not None:
            outputs += ["--output-schema", str(request.output_schema)]
        if request.last_message_path is not None:
            outputs += ["-o", str(request.last_message_path)]
        prompt = "-" if request.prompt_via_stdin else request.prompt
        if request.resume:
            mode = _sandbox_mode(request.profile, request.native_policy)
            return [
                self.executable,
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-c",
                f'sandbox_mode="{mode}"',
                *_APPROVAL_NEVER,
                *options,
                *outputs,
                *request.extra,
                request.resume,
                prompt,
            ]
        return [
            self.executable,
            "exec",
            "--json",
            "--skip-git-repo-check",
            *self.policy_argv(request.profile, request.native_policy),
            *_APPROVAL_NEVER,
            *options,
            "-C",
            str(request.cwd),
            *outputs,
            *request.extra,
            prompt,
        ]

    def stdin_payload(self, prompt: str) -> bytes:
        """A ``-`` prompt makes ``codex exec`` read the raw prompt from stdin."""
        return prompt.encode("utf-8")

    def parse_event(self, line: str) -> list[Event]:
        """Map one ``codex exec --json`` stdout line to zero or more events."""
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
        if event_type == "thread.started":
            return [Event("thread_started", thread_id=data.get("thread_id"), raw_type=event_type)]
        if event_type in ("item.started", "item.updated", "item.completed"):
            item = data.get("item")
            return _codex_item_events(event_type, item) if isinstance(item, dict) else []
        if event_type == "turn.completed":
            usage = data.get("usage")
            return [
                Event(
                    "done",
                    status="succeeded",
                    usage=usage if isinstance(usage, dict) else None,
                    raw_type=event_type,
                )
            ]
        if event_type == "turn.failed":
            error = data.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            return [Event("done", status="failed", error=str(message or ""), raw_type=event_type)]
        if event_type == "error":
            message = data.get("message") or data.get("error") or ""
            return [Event("done", status="failed", error=str(message), raw_type=event_type)]
        return []

    def transcript_path(self, store: Path, thread_id: str, started: datetime | None) -> Path | None:
        """The rollout file for ``thread_id``: the start day's folder, the day before, then a scan."""
        pattern = f"rollout-*-{thread_id}.jsonl"
        day = (started.astimezone() if started else datetime.now().astimezone()).date()
        for candidate_day in (day, day - timedelta(days=1)):
            folder = store / f"{candidate_day:%Y}" / f"{candidate_day:%m}" / f"{candidate_day:%d}"
            matches = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
            if matches:
                return matches[-1]
        if not store.is_dir():
            return None
        matches = sorted(store.rglob(pattern), key=lambda p: p.stat().st_mtime)
        return matches[-1] if matches else None

    def self_check(self, model: str | None, effort: str | None, fast: bool) -> list[str]:
        """Problems with the model/effort/fast choice against the live Codex model cache."""
        catalog = load_codex_model_catalog()
        if not catalog:
            return [
                f"no Codex model cache at {models_cache_path()}; run `codex` once "
                "or pass --skip-self-check"
            ]
        return check_codex_invocation(model, effort, fast, catalog)

    def plugin_enabled(self, cwd: str) -> bool:
        """Conservative guard for our user-level fallback after disable/removal."""
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        enabled = False
        layers = [
            home / "config.toml",
            *[p / ".codex/config.toml" for p in reversed((Path(cwd), *Path(cwd).parents))],
        ]
        for path in layers:
            if path.is_file():
                data = tomllib.loads(path.read_text())
                value = data.get("plugins", {}).get("cairn@cairn", {}).get("enabled")
                if value is not None:
                    enabled = value is True
        cache = home / "plugins/cache/cairn/cairn"
        return enabled and any(cache.glob("*/.codex-plugin/plugin.json"))

    def inspect_hooks(self, cwd: str) -> list[dict]:
        """Read the client's actual loaded hooks and trust state via its local API."""
        process = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
        )
        assert process.stdin is not None and process.stdout is not None
        stdin, stdout = process.stdin, process.stdout
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b""

        def call(identifier: int, method: str, params: dict) -> dict:
            nonlocal buffer
            stdin.write(
                (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            )
            stdin.flush()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    response = json.loads(line)
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise RuntimeError(str(response["error"]))
                        return response["result"]
                if selector.select(max(0, deadline - time.monotonic())):
                    chunk = os.read(stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("Codex app-server closed before answering")
                    buffer += chunk
            raise RuntimeError("Codex hook inspection timed out; no registration changed")

        try:
            call(1, "initialize", {"clientInfo": {"name": "cairn", "version": "1"}})
            result = call(2, "hooks/list", {"cwds": [cwd]})
            items = result.get("data", [])
            if not items or any(item.get("errors") for item in items):
                raise RuntimeError(f"Cannot inspect Codex hooks: {items}")
            return [hook for item in items for hook in item.get("hooks", [])]
        finally:
            selector.close()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
