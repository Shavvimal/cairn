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


__all__ = ["get_output_path"]
