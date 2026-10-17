import os

from . import SessionEvent, decode_event


class Claude:
    name = "claude"
    manifest_paths: tuple[str, ...] = (".claude-plugin/plugin.json",)
    update_command = "/plugin update cairn@cairn, then /reload-plugins"

    def decode(self, payload: object) -> SessionEvent:
        return decode_event(self.name, payload)

    def current_id(self) -> str | None:
        return os.environ.get("CLAUDE_SESSION_ID") or None

    def resume(self, session_id: str, fork: bool = False) -> list[str]:
        return ["claude", "--resume", session_id, *(["--fork-session"] if fork else [])]
