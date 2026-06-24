"""The ConversationSource adapter - base class for the per-agent skills.

This is the seam the three skills plug into. A subclass supplies the
source-specific parts:

* discovery + parse of its native store (``run_export``), and
* its frontmatter *head* + body-extra sections (``render_document``),

while the base class provides everything that is identical across sources -
``find_file``, listing, the note/close/log mutations, and the QMD ``context``
command - built on the shared helper modules. :mod:`cairn.cli` turns a
source instance into a full argparse CLI.

(The data-side seams - renderer and schema - stay composition/Protocol style per
the conversation_provenance_service design; this base class only consolidates the
CLI/orchestration layer, which is where inheritance genuinely removes boilerplate.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from .. import frontmatter as fm_mod
from .. import listing, mutations, qmd
from ..paths import find_session_file, get_output_path
from ..rendering import render_conversation, strip_lone_surrogates
from ..schema import Message, SessionData


class ConversationSource(ABC):
    # --- subclasses MUST override these class attributes ---
    name: str = "sessions"  # CLI prog name
    version: str = "2.0.0"
    output_dir: Path = Path(".")  # collection output directory
    session_type: str = "session"  # frontmatter `type` value
    collection: str = "sessions"  # QMD collection name

    # `list` default: "active" -> status == "active"; "not-done" -> status != "done"
    list_default: str = "active"
    # env var consulted to resolve the "current" session for mutations
    session_env_var: str = "CLAUDE_SESSION_ID"
    # frontmatter key holding the id (claude/codex: session_id; cursor: composer_id)
    id_field: str = "session_id"
    # default status written when no existing file (cursor overrides per-composer)
    default_status: str = "active"
    # preserved-section wording in the My Notes placeholder ("syncs" vs "exports")
    preserved_note: str = "syncs"

    # ====================================================================
    # Overridable per source
    # ====================================================================

    def short_id(self, session_id: str) -> str:
        """Filename suffix id. Default: first 8 chars."""
        return session_id[:8]

    def list_mid_col(self, fm: dict) -> str:
        """Pre-width-formatted middle column for `list` (e.g. model/mode). Empty by default."""
        return ""

    def extend_export_parser(self, parser: argparse.ArgumentParser) -> None:
        """Add source-specific positionals/flags to the shared ``export`` parser.

        The ``--since`` / ``--all`` window and the common flags are attached by
        :mod:`cairn.cli`; override only to add extras (Claude adds an optional FILE).
        """
        return None

    @abstractmethod
    def run_export(self, args: argparse.Namespace) -> int:
        """Discover and export the source's native store. Returns a process exit code.

        ``args.all`` is True when the user asked for everything; otherwise
        ``args.since`` is a POSIX timestamp cutoff (items must be newer than it).
        """

    @abstractmethod
    def render_document(
        self, data: SessionData, existing_fm: dict | None, my_notes: str | None
    ) -> str:
        """Render one parsed session into a full markdown document.

        ``data`` is the source's parsed session (see :class:`cairn.schema.SessionData`);
        ``existing_fm`` and ``my_notes`` are the preserved frontmatter and My Notes
        from a prior export (both ``None`` on first export). Implementations build the
        source-specific frontmatter head plus any body-extra sections and finish via
        :meth:`assemble` (chat bodies) or :meth:`assemble_document` (arbitrary bodies).

        This is the one canonical signature every source shares; per-source values
        like the session id or pre-ordered messages travel inside ``data`` rather than
        as extra parameters, so :meth:`export_one` can call it uniformly.
        """

    def add_extra_parsers(
        self, subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser
    ) -> None:
        """Hook for source-specific subcommands (Claude adds sync / resume).

        ``common`` is the shared parent (``-q/--quiet``, ``--json``); attach it via
        ``parents=[common]`` so extras share the same flags.
        """
        return None

    # ====================================================================
    # Shared concrete helpers
    # ====================================================================

    def find_file(self, session_id: str) -> Path | None:
        return find_session_file(self.output_dir, self.short_id(session_id))

    def output_path(
        self,
        session_id: str,
        date: str,
        project: str | None = None,
        repo_subdir: str | None = None,
    ) -> Path:
        return get_output_path(
            self.output_dir, self.short_id(session_id), date, project, repo_subdir
        )

    def load_existing(self, output_file: Path) -> tuple[dict | None, str | None, bool]:
        """Load preserved frontmatter + My Notes from a prior export of this session.

        Returns ``(existing_fm, my_notes, ok)``. ``ok`` is False only when the file
        exists but its frontmatter cannot be parsed - the caller must then skip the
        session rather than overwrite (and silently reset) a file we can't round-trip.
        A missing file is the normal first-export case and yields ``(None, None, True)``.
        """
        if not output_file.exists():
            return None, None, True
        content = output_file.read_text(encoding="utf-8")
        try:
            existing_fm = fm_mod.parse_frontmatter(content)
        except fm_mod.FrontmatterParseError as e:
            print(f"Warning: skipping {output_file}: {e}", file=sys.stderr)
            return None, None, False
        return existing_fm, fm_mod.extract_my_notes_section(content), True

    def export_one(
        self, source_id: str, data: SessionData, existing_file: Path | None
    ) -> Path | None:
        """Render one parsed session and write it; return the output path (or ``None``).

        The shared tail of every source's export loop: resolve the output path
        (reusing ``existing_file`` when a prior export exists), preserve that export's
        frontmatter + My Notes, render via :meth:`render_document`, and write. Returns
        ``None`` when a prior file exists but can't be round-tripped - the caller skips
        it rather than overwriting an unparseable file (mirrors :meth:`load_existing`).

        Discovery, the per-source up-to-date skip-check, and any enrichment stay in the
        source's own loop; ``data`` must already carry ``date`` (and ``project`` /
        ``repo_subdir`` where the source partitions output by repo).
        """
        output_file = existing_file or self.output_path(
            source_id, data["date"], data.get("project"), data.get("repo_subdir")
        )
        existing_fm, my_notes, ok = self.load_existing(output_file)
        if not ok:
            return None
        markdown = strip_lone_surrogates(self.render_document(data, existing_fm, my_notes))
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(markdown, encoding="utf-8")
        return output_file

    def all_sessions(self) -> list[tuple[Path, dict]]:
        return listing.get_session_files(self.output_dir, self.session_type)

    def active_sessions(self) -> list[tuple[Path, dict]]:
        return listing.get_active_sessions(self.output_dir, self.session_type)

    def current_session_id(self, explicit: str | None) -> str | None:
        """Resolve the session to act on: explicit id, then env var, then the single
        active session. Refuses to guess when more than one session is active so a
        mutation can never silently land on the wrong one."""
        sid = explicit or os.environ.get(self.session_env_var)
        if sid:
            return sid

        active = self.active_sessions()
        if not active:
            return None
        if len(active) > 1:
            print(
                f"Error: {len(active)} active sessions found; pass --session-id or set "
                f"{self.session_env_var} to choose one.",
                file=sys.stderr,
            )
            return None

        sid = active[0][1].get(self.id_field)
        if sid:
            print(f"Using current active session {sid[:8]}", file=sys.stderr)
        return sid

    def _require_session_id(self, explicit: str | None) -> str | None:
        """Resolve the session id or print the canonical hint to stderr (returns None)."""
        sid = self.current_session_id(explicit)
        if not sid:
            print(
                f"Error: No session_id. Use --session-id or set {self.session_env_var}",
                file=sys.stderr,
            )
        return sid

    def assemble_document(
        self,
        head: list[str],
        body_extra: list[str],
        title: str,
        existing_fm: dict | None,
        my_notes: str | None,
        body_main: list[str],
        default_status: str | None = None,
    ) -> str:
        """Assemble a full document from already-rendered parts.

        ``head`` is the source-specific frontmatter (type … last_activity); the
        preserved tail, title heading, and My Notes block are shared; ``body_main``
        is the rendered body (a conversation for chat agents, a transcript for
        meetings - the base does not care which). ``default_status`` overrides the
        class default when the source derives status per-session (Cursor).
        """
        lines = list(head)
        lines.extend(fm_mod.preserved_tail(existing_fm, default_status or self.default_status))
        lines.append("---")
        lines.append("")
        lines.append(f"# {title}")
        lines.append("")
        lines.extend(body_extra)
        lines.extend(fm_mod.notes_block(my_notes, self.preserved_note))
        lines.extend(body_main)
        return "\n".join(lines)

    def assemble(
        self,
        head: list[str],
        body_extra: list[str],
        title: str,
        existing_fm: dict | None,
        my_notes: str | None,
        messages: list[Message],
        default_status: str | None = None,
    ) -> str:
        """Convenience for chat agents: body is a rendered ``## Conversation``."""
        return self.assemble_document(
            head,
            body_extra,
            title,
            existing_fm,
            my_notes,
            render_conversation(messages),
            default_status,
        )

    # ====================================================================
    # Shared commands
    # ====================================================================

    def list_sessions(self, show_all: bool) -> tuple[list[tuple[Path, dict]], str]:
        """The ``(sessions, heading)`` for ``list``. Override to customise selection,
        ordering, or wording (Granola sorts by date and renames to "Meetings")."""
        if show_all:
            return self.all_sessions(), "All Sessions"
        if self.list_default == "active":
            return self.active_sessions(), "Active Sessions"
        not_done = [(f, fm) for f, fm in self.all_sessions() if fm.get("status") != "done"]
        return not_done, "Sessions (not done)"

    def print_listing(self, sessions: list[tuple[Path, dict]], title: str) -> None:
        """Render the human-readable ``list`` table. Override for a custom format."""
        listing.print_sessions(sessions, self.output_dir, title, self.list_mid_col)

    def cmd_list(self, args: argparse.Namespace) -> int:
        sessions, title = self.list_sessions(getattr(args, "all", False))
        if getattr(args, "json", False):
            data = [{"path": str(p), **fm} for p, fm in sessions]
            print(json.dumps(data, indent=2))
        else:
            self.print_listing(sessions, title)
        return 0

    def cmd_context(self, args: argparse.Namespace) -> int:
        return qmd.register_context(self.collection)

    def cmd_note(self, args: argparse.Namespace) -> int:
        session_id = self._require_session_id(getattr(args, "session_id", None))
        if not session_id:
            return 1
        text = " ".join(args.text)
        if not text:
            print("Error: No note text provided", file=sys.stderr)
            return 2
        return 0 if mutations.add_comment(self.find_file, session_id, text) else 1

    def cmd_close(self, args: argparse.Namespace) -> int:
        session_id = self._require_session_id(getattr(args, "session_id", None))
        if not session_id:
            return 1
        note = f"[CLOSED] {' '.join(args.text)}" if args.text else "[CLOSED]"
        # Only mark done if the comment landed - never report success on a no-op.
        ok = mutations.add_comment(self.find_file, session_id, note)
        if ok:
            ok = mutations.set_session_status(self.find_file, session_id, "done")
        if not ok:
            return 1
        print(f"Session {session_id[:8]} marked as done")
        return 0

    def cmd_log(self, args: argparse.Namespace) -> int:
        session_id = self._require_session_id(getattr(args, "session_id", None))
        if not session_id:
            return 1
        if not (args.status or args.tags or args.rating is not None or args.text):
            print(
                "Error: nothing to log. Provide a comment, --status, --tags, or --rating.",
                file=sys.stderr,
            )
            return 2
        # Validate up front (before mutating any field) via the mutation-boundary
        # authority, so the vocabulary and wording live in exactly one place.
        if args.status and not mutations.is_valid_status(args.status):
            print(f"Error: {mutations.invalid_status_message(args.status)}", file=sys.stderr)
            return 2

        ok = True
        if args.status:
            if mutations.set_session_status(self.find_file, session_id, args.status):
                print(f"Status: {args.status}")
            else:
                ok = False
        if args.tags:
            tags = [t.strip() for t in args.tags.split(",")]
            if mutations.set_session_tags(self.find_file, session_id, tags):
                print(f"Tags: {', '.join(tags)}")
            else:
                ok = False
        if args.rating is not None:
            if mutations.set_session_rating(self.find_file, session_id, args.rating):
                print(f"Rating: {args.rating}/10")
            else:
                ok = False
        if args.text and not mutations.add_comment(self.find_file, session_id, " ".join(args.text)):
            ok = False

        if not ok:
            return 1
        print(f"Session {session_id[:8]} updated")
        return 0


__all__ = ["ConversationSource"]
