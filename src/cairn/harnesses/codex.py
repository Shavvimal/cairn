import json
import os
import selectors
import subprocess
import time
import tomllib
from pathlib import Path

from . import SessionEvent, decode_event


class Codex:
    name = "codex"
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
