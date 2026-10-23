"""Top-level ``cairn`` command - a lazy dispatcher over the per-source CLIs.

``cairn <source> <args…>`` routes to that source's entry point, importing ONLY
the chosen module so a single invocation never pulls in all five sources (and
``cryptography``, and five config reads). ``cairn help`` prints an overview an
LLM agent can read to drive the tool, importing nothing.

Each target reads ``sys.argv`` for its own argparse, so the dispatcher rewrites
``sys.argv`` to drop the ``cairn`` and ``<command>`` tokens before handing off:
``cairn claude export --since 7d`` becomes ``export --since 7d`` for the source.
"""

from __future__ import annotations

import importlib
import sys

# name -> "module:function". Values are strings so nothing imports until dispatch.
from .registry import SOURCES

_SOURCES = {source.source: source.entrypoint for source in SOURCES}

# Top-level orchestration / admin commands.
_ADMIN: dict[str, str] = {
    "hook": "cairn.hooks:main",
    "resume": "cairn.resume:main",
    "sync": "cairn.sync:main",
    "recall": "cairn.recall:main",
    "doctor": "cairn.admin:doctor_main",
    "config": "cairn.admin:config_main",
    "cron": "cairn.admin:cron_main",
    "invoke": "cairn.invoke:main",
    "agents": "cairn.agents:main",
}

_HELP = """\
cairn - local session & docs sync engine

usage:
  cairn <command> [args…]

sources (each forwards its remaining args to that source's CLI):
  cairn claude export [--since 7d | --all]   export Claude Code sessions
  cairn claude resume --pick                 resume a Claude session (interactive)
  cairn codex export                         export Codex CLI sessions (last 24h)
  cairn cursor export --all                  export Cursor IDE sessions
  cairn granola export --since 2w            export Granola meetings + transcripts
  cairn service-docs export [--clean]        mirror service docs into one collection

shared per-source commands: export, list, context, note, close, log
  cairn claude list --json
  cairn codex note "got it working"
  cairn cursor log --status done --tags "x,y" --rating 8

recall (temporal view over shared exported sessions; topic search is qmd's job):
  cairn recall list yesterday                sessions from a date window
  cairn recall list "last week" --json       machine-readable session list
  cairn recall expand <source:session-id>           condensed transcript for one session

peer agents (one foreground round per call; run it in the background):
  cairn invoke codex --prompt TEXT [--effort E] [--profile review|edit|full] [--json]
  cairn invoke claude --prompt-file FILE [--model M] [--permission-mode MODE]
  cairn invoke antigravity --prompt TEXT [--model M] [--mode plan|accept-edits|auto]
  cairn invoke codex --resume THREAD_ID --prompt TEXT   follow up in the same thread
  cairn invoke status THREAD_ID [--follow] [--json]     health, progress and answer
  cairn invoke cancel THREAD_ID                         stop a running round
  cairn invoke publish THREAD_ID                        finish pending exports/index
  cairn invoke list-models [--json]                     the Codex model catalogue

agents (peer runs and subagents of a session):
  cairn agents list [--session SOURCE:ID] [--all] [--json] [--watch]
  cairn agents wait [--session SOURCE:ID | ID...] [--timeout-s N] [--stall-after-s N]

orchestration / admin:
  cairn sync [--hook | --cron | --all]       sync every source, then refresh QMD
  cairn hook install | status | worker       configure hooks / inspect / retry sync
  cairn resume <source:session-id>            print the correct native resume command
  cairn doctor [--strict-index]              check install, config, qmd + index, rg, cron, PATH
  cairn config init | path                   create / locate the per-user config
  cairn config show [--json]                 print the resolved config + integrations
  cairn config set KEY VALUE                 set one config value (e.g. claude.store …)
  cairn config add-service-doc NAME PATH     mirror a docs folder into service-docs
  cairn cron install | uninstall             manage the hourly sync crontab entry

other:
  cairn help                                 show this overview
  cairn --version                            print the cairn version

config resolution: $CAIRN_CONFIG, else $XDG_CONFIG_HOME/cairn/config.json
(else ~/.config/cairn/config.json), else the repo root (dev checkouts).
"""


def _resolve(spec: str):
    """Import ``"module:function"`` lazily and return the callable."""
    module_name, func_name = spec.split(":", 1)
    return getattr(importlib.import_module(module_name), func_name)


def _version() -> str:
    from . import __version__

    return __version__


def main() -> None:
    argv = sys.argv[1:]

    if not argv or argv[0] in ("help", "-h", "--help"):
        print(_HELP)
        sys.exit(0)
    if argv[0] in ("--version", "-V"):
        print(f"cairn {_version()}")
        sys.exit(0)

    command, rest = argv[0], argv[1:]
    spec = _SOURCES.get(command) or _ADMIN.get(command)
    if spec is None:
        known = ", ".join(sorted([*_SOURCES, *_ADMIN, "help"]))
        print(
            f"cairn: unknown command {command!r}.\nknown commands: {known}\n"
            f"run 'cairn help' for usage.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Re-point argv at the target's own parser, then hand off. Sources call
    # sys.exit() themselves (via cli.run); admin/sync return an int - propagate it.
    sys.argv = [f"cairn {command}", *rest]
    sys.exit(_resolve(spec)())


if __name__ == "__main__":
    main()
