"""Output-path and existing-file resolution.

Sources differ only in how they shorten a session id for the filename suffix
(Claude/Cursor use the first 8 chars; Codex uses a collision-resistant
head8+tail4). The caller passes the already-computed ``short_id``, so these
helpers are identical for every source.
"""

from __future__ import annotations

from pathlib import Path


def get_output_path(
    output_dir: Path,
    short_id: str,
    date: str,
    project: str | None = None,
    repo_subdir: str | None = None,
) -> Path:
    """Output file path for a session, organized by project/repo subfolder."""
    if project and repo_subdir:
        return output_dir / project / repo_subdir / f"{date}-{short_id}.md"
    elif project:
        return output_dir / project / f"{date}-{short_id}.md"
    return output_dir / f"{date}-{short_id}.md"


def session_dir(
    output_dir: Path,
    short_id: str,
    date: str,
    project: str | None = None,
    repo_subdir: str | None = None,
) -> Path:
    """Folder holding everything one session spawned (subagent transcripts, peer
    exchanges): the session document's path without its ``.md`` suffix."""
    return get_output_path(output_dir, short_id, date, project, repo_subdir).with_suffix("")


def session_dir_of(document: Path) -> Path:
    """The same folder derived from an already-resolved session document, so a
    collision-suffixed or legacy-located parent keeps its children beside it."""
    return document.with_suffix("")


def subagent_path(session_folder: Path, agent_short: str) -> Path:
    """Child document for one subagent transcript inside a session folder."""
    return session_folder / f"agent-{agent_short}.md"


def exchange_path(session_folder: Path, peer: str, thread_short: str) -> Path:
    """Exchange document for one peer thread inside a session folder."""
    return session_folder / f"exchange-{peer}-{thread_short}.md"


__all__ = ["exchange_path", "get_output_path", "session_dir", "session_dir_of", "subagent_path"]
