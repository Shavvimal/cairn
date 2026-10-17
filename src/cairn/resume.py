"""Select a shared-store session, delegate native argv, launch only on request."""

import argparse
import json
import os
import shlex
import sys

from .config import ConfigError, get_config
from .harnesses import get_harness
from .store import SessionStore


def run(args: argparse.Namespace) -> int:
    try:
        reference = args.session_id
        if getattr(args, "source", None) and ":" not in reference:
            reference = f"{args.source}:{reference}"
        record = SessionStore(get_config()).resolve(reference)
        if record.is_subagent:
            raise ValueError(
                "cannot resume a subagent transcript; "
                f"resume its parent {record.source}:{record.parent_id}"
            )
        argv = get_harness(record.source).resume(record.session_id, args.fork)
        if args.launch:
            if not sys.stdin.isatty():
                raise ValueError(
                    "--launch requires an interactive terminal; run the printed command there"
                )
            if record.cwd and os.path.isdir(record.cwd):
                os.chdir(record.cwd)
            os.execvp(argv[0], argv)
        print(json.dumps({"argv": argv, "cwd": record.cwd}) if args.json else shlex.join(argv))
        return 0
    except (ValueError, ConfigError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id", help="source-qualified ID or unambiguous prefix")
    parser.add_argument("--fork", action="store_true")
    parser.add_argument("--launch", action="store_true", help="launch in this interactive terminal")
    parser.set_defaults(func=run)


def add_source_parser(
    subparsers: argparse._SubParsersAction, common: argparse.ArgumentParser, source: str
) -> None:
    parser = subparsers.add_parser("resume", parents=[common], help="print a native resume command")
    _arguments(parser)
    parser.set_defaults(source=source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairn resume")
    _arguments(parser)
    parser.add_argument("--json", action="store_true")
    return run(parser.parse_args(argv))
