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
    # Frontmatter ``type`` of child documents (subagent transcripts) that share this
    # source's collection and CLI but are hidden from plain listings.
    subagent_type: str = ""

    @property
    def entrypoint(self) -> str:
        return f"cairn.sources.{self.source.replace('-', '_')}:main"

    @property
    def session_types(self) -> tuple[str, ...]:
        """Every frontmatter ``type`` this source owns: the session, then its children."""
        return tuple(t for t in (self.session_type, self.subagent_type) if t)


SOURCES = (
    Source(
        "claude",
        "claude-code-sessions",
        "claude-session",
        targeted=True,
        on_hook_default=True,
        subagent_type="claude-subagent",
    ),
    Source("codex", "codex-sessions", "codex-session", targeted=True, subagent_type="codex-subagent"),
    Source("cursor", "cursor-sessions", "cursor-session", "composer_id"),
    Source("granola", "granola-sessions", "granola-meeting", "meeting_id"),
    Source("service-docs", "service-docs", "", windowed=False),
)
BY_NAME = {source.source: source for source in SOURCES}
SOURCE_COLLECTIONS = {source.source: source.collection for source in SOURCES}
