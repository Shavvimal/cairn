"""Lazy source identities and capabilities; source is independent of harness."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    source: str
    collection: str
    session_type: str
    id_field: str = "session_id"
    windowed: bool = True
    targeted: bool = False
    on_hook_default: bool = False  # legacy `sync --hook` policy only

    @property
    def entrypoint(self) -> str:
        return f"cairn.sources.{self.source.replace('-', '_')}:main"


SOURCES = (
    Source("claude", "claude-code-sessions", "claude-session", targeted=True, on_hook_default=True),
    Source("codex", "codex-sessions", "codex-session", targeted=True),
    Source("cursor", "cursor-sessions", "cursor-session", "composer_id"),
    Source("granola", "granola-sessions", "granola-meeting", "meeting_id"),
    Source("service-docs", "service-docs", "", windowed=False),
)
BY_NAME = {source.source: source for source in SOURCES}
SOURCE_COLLECTIONS = {source.source: source.collection for source in SOURCES}
