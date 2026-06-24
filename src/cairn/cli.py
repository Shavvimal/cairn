"""Shared argparse CLI - turns a ConversationSource into a full command set.

One canonical vocabulary, applied identically to every source. Shared flags live
in two parent parsers so they are declared once and attached everywhere:

* ``common`` - ``-q/--quiet`` and ``--json`` (every subcommand accepts them).
* ``window`` - ``--since`` / ``--all`` (the time filter for ``export``).

Verbs are stable across sources: ``export``, ``list``, ``context``, ``note``,
``close``, ``log``, plus any source-specific extras (Claude adds ``sync`` / ``resume``).
The source supplies only its native ``run_export`` driver and, optionally, extra
positionals on ``export`` (via :meth:`ConversationSource.extend_export_parser`).
"""

from __future__ import annotations

import argparse
import sys

from .sources.base import ConversationSource
from .timeutil import parse_since

# Default export window when neither --since nor --all is given: the last 24h.
_DEFAULT_SINCE = "1d"
_SINCE_METAVAR = "today|yesterday|Nd|Nw|YYYY-MM-DD"


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Show argument defaults *and* keep multi-line example epilogs verbatim."""


def _common_parent() -> argparse.ArgumentParser:
    """Flags every subcommand shares (attached via ``parents=``)."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "-q", "--quiet", action="store_true", help="suppress human-facing output on stderr"
    )
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON on stdout")
    return p


def _window_parent() -> argparse.ArgumentParser:
    """The ``export`` time filter: ``--since`` xor ``--all`` (mutually exclusive)."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--since",
        type=parse_since,
        default=parse_since(_DEFAULT_SINCE),
        metavar=_SINCE_METAVAR,
        help="export items newer than this window",
    )
    g.add_argument("--all", action="store_true", help="export everything, ignoring --since")
    return p


def build_parser(source: ConversationSource) -> argparse.ArgumentParser:
    common = _common_parent()
    window = _window_parent()

    # prog is left to argparse: invoked via the dispatcher, sys.argv[0] is already
    # "cairn <source>", so usage/help read naturally without hardcoding a name.
    parser = argparse.ArgumentParser(
        description=f"Export and annotate {source.name} to markdown.",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {source.version}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    # export - shared window flags + the source's native driver
    p_export = sub.add_parser(
        "export",
        parents=[common, window],
        help="render items to markdown",
        formatter_class=_HelpFormatter,
        epilog=(
            "examples:\n"
            "  export                 # last 24h (the default window)\n"
            "  export --since 7d      # today|yesterday|Nd|Nw|YYYY-MM-DD\n"
            "  export --all --quiet   # everything, no per-item output"
        ),
    )
    source.extend_export_parser(p_export)
    p_export.set_defaults(func=source.run_export)

    # list
    p_list = sub.add_parser("list", parents=[common], help="list exported items")
    p_list.add_argument(
        "--all", action="store_true", help="include done/closed items, not just active"
    )
    p_list.set_defaults(func=source.cmd_list)

    # context
    p_ctx = sub.add_parser("context", parents=[common], help="register QMD context descriptions")
    p_ctx.set_defaults(func=source.cmd_context)

    # note / close / log - shared frontmatter mutations
    p_note = sub.add_parser("note", parents=[common], help="add a timestamped note")
    p_note.add_argument("text", nargs="+", help="note text")
    p_note.add_argument("--session-id", help="target item id (defaults to the current one)")
    p_note.set_defaults(func=source.cmd_note)

    p_close = sub.add_parser("close", parents=[common], help="mark an item done")
    p_close.add_argument("text", nargs="*", help="optional closing note")
    p_close.add_argument("--session-id", help="target item id (defaults to the current one)")
    p_close.set_defaults(func=source.cmd_close)

    p_log = sub.add_parser("log", parents=[common], help="annotate with status/tags/rating")
    p_log.add_argument("text", nargs="*", help="optional comment")
    p_log.add_argument("--status", "-s", help="set status (active|done|blocked|handoff)")
    p_log.add_argument("--tags", "-t", help="set tags (comma-separated)")
    p_log.add_argument("--rating", "-r", type=int, metavar="N", help="set rating (1-10)")
    p_log.add_argument("--session-id", help="target item id (defaults to the current one)")
    p_log.set_defaults(func=source.cmd_log)

    # source-specific extras (e.g. Claude sync / resume) share the common flags too
    source.add_extra_parsers(sub, common)

    return parser


def run(source: ConversationSource, argv: list[str] | None = None) -> None:
    """Build the source's parser, dispatch, and exit with the handler's code.

    ``argv`` defaults to ``sys.argv[1:]``; the ``cairn`` dispatcher passes an
    explicit slice so ``cairn <source> <args…>`` routes here cleanly.
    """
    parser = build_parser(source)
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


__all__ = ["build_parser", "run"]
