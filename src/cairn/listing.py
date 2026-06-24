"""Listing and interactive selection of already-exported sessions.

Reads the exported markdown frontmatter (never the raw source store), so this is
shared across every source - parameterized only by the output directory and the
frontmatter ``type`` value that identifies the collection.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .frontmatter import FrontmatterParseError, parse_frontmatter
from .schema import Frontmatter

MAX_LISTED = 20  # rows shown by print_sessions before truncating


def get_session_files(output_dir: Path, session_type: str) -> list[tuple[Path, Frontmatter]]:
    """All exported session files of ``session_type``, newest activity first."""
    sessions = []
    for f in output_dir.rglob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
        except (OSError, FrontmatterParseError) as e:
            # An unreadable or unparseable file is surfaced, not silently dropped
            # from every listing - that would hide data loss (CORE_PRINCIPLES §8).
            print(f"Warning: could not read {f}: {e}", file=sys.stderr)
            continue
        if fm.get("type") == session_type:
            sessions.append((f, fm))

    return sorted(
        sessions,
        key=lambda x: x[1].get("last_activity", x[1].get("date", "")),
        reverse=True,
    )


def get_active_sessions(output_dir: Path, session_type: str) -> list[tuple[Path, Frontmatter]]:
    """Sessions with status 'active' (used for 'current session' resolution)."""
    return [
        (f, fm)
        for f, fm in get_session_files(output_dir, session_type)
        if fm.get("status") == "active"
    ]


def print_sessions(
    sessions: list[tuple[Path, Frontmatter]],
    output_dir: Path,
    title: str = "Sessions",
    mid_col: Callable[[Frontmatter], str] | None = None,
) -> None:
    """Print a formatted session list.

    ``mid_col`` is an optional ``fn(fm) -> str`` injecting a pre-width-formatted
    middle column (e.g. model/mode) between the message count and the project.
    """
    print(f"\n{title}:")
    print("-" * 80)
    if not sessions:
        print("  No sessions found.")
        return
    for i, (path, fm) in enumerate(sessions[:MAX_LISTED], 1):
        status = fm.get("status", "?")
        date = fm.get("date", "?")
        messages = fm.get("messages", "?")
        title_text = fm.get("title", "Untitled")[:40]
        proj = path.parent.name if path.parent != output_dir else ""
        proj_str = f" {proj}" if proj else ""
        mid = mid_col(fm) if mid_col else ""
        print(f"  {i:2}. [{status:8}] {date} ({messages:>3} msgs){mid}{proj_str:>20} {title_text}")
    if len(sessions) > MAX_LISTED:
        print(f"  ... and {len(sessions) - MAX_LISTED} more")


def interactive_pick(sessions: list[tuple[Path, Frontmatter]]) -> Path | None:
    """Interactive session picker (fzf if available, else a numbered prompt)."""
    if not sessions:
        print("No sessions to pick from.")
        return None

    lines = []
    for path, fm in sessions:
        t = fm.get("title", "Untitled")[:60]
        s = fm.get("status", "?")
        d = fm.get("date", "?")
        m = fm.get("messages", "?")
        lines.append(f"{path}\t[{s}] {d} ({m} msgs) {t}")

    try:
        result = subprocess.run(
            ["fzf", "--delimiter=\t", "--with-nth=2", "--preview=head -50 {1}"],
            input="\n".join(lines),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip().split("\t")[0])
    except FileNotFoundError:
        # fzf isn't installed - fall back to a numbered prompt.
        print("\nPick a session:")
        for i, line in enumerate(lines, 1):
            print(f"  {i:2}. {line.split(chr(9), 1)[1]}")  # drop the leading path\t
        try:
            choice = input("\nEnter number (or q to quit): ").strip()
            if choice.lower() == "q":
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx][0]
        except (ValueError, KeyboardInterrupt):
            return None
    return None


__all__ = ["get_active_sessions", "get_session_files", "interactive_pick", "print_sessions"]
