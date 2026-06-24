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


def find_session_file(output_dir: Path, short_id: str) -> Path | None:
    """Find an existing markdown file for a session (searches all subfolders).

    Returns ``None`` if nothing matches. Raises :class:`FileExistsError` if more
    than one file matches the short id - picking an arbitrary one could mutate the
    wrong session, so an ambiguous id must be resolved explicitly by the caller.
    """
    matches = sorted(output_dir.rglob(f"*-{short_id}.md"))
    if not matches:
        return None
    if len(matches) > 1:
        listed = ", ".join(str(m) for m in matches)
        raise FileExistsError(
            f"Ambiguous short id {short_id!r} matches {len(matches)} files: {listed}"
        )
    return matches[0]


__all__ = ["find_session_file", "get_output_path"]
