"""The error registry: one stable code per common cairn failure, and ``cairn errors``.

Each entry names a failure (``code``), says what it means and why cairn stops, gives
the next step and a read-only command that checks the state, and records the exit
status the command already returns. The registry describes exits; it never drives
them: every site keeps its own return value, and the site tests check the two agree.

A converted site keeps its first stderr line byte-identical and adds one line::

    code: CONFIG_UNKNOWN_KEY (run 'cairn errors CONFIG_UNKNOWN_KEY')

With ``--json`` a failure prints ``{"ok": false, "code": ..., "error": ..., "exit": ...,
"retryable": ..., "fix": {"next_step": ..., "verify": [...]}}`` on stdout, and a
result object that already exists gains ``code`` and ``fix``.

``cairn errors [CODE] [--json]`` reads only this module: no config, no state.
``python -m cairn.errors --render`` prints ``docs/error-codes.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

# The only placeholders a ``verify`` argv may hold. A site fills what it knows; an
# argv with an unfilled placeholder is left out of ``fix``, never sent as a template.
PLACEHOLDERS = ("name", "thread_id", "peer", "harness")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

CONTRACT = "On a failure, read `code`, run `cairn errors CODE`, run its verify command, then act."
RENDER_COMMAND = "python -m cairn.errors --render > docs/error-codes.md"


@dataclass(frozen=True)
class ErrorCode:
    """One registry row. ``exit`` is the usual exit status; ``exit_by_command`` holds
    the commands that return another one; ``exit_note``, when set, replaces the number in
    the rendered text. ``shown_in`` says where the code is printed today (empty:
    described only), and ``raised_at`` names the sites as ``path:function``."""

    code: str
    meaning: str
    why: str
    next_step: str
    verify: tuple[str, ...] | None
    exit: int
    retryable: bool
    raised_at: tuple[str, ...]
    shown_in: str = ""
    exit_by_command: Mapping[str, int] = field(default_factory=dict)
    exit_note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "meaning": self.meaning,
            "why": self.why,
            "next_step": self.next_step,
            "verify": list(self.verify) if self.verify is not None else None,
            "exit": self.exit,
            "exit_by_command": dict(self.exit_by_command),
            "exit_note": self.exit_note or None,
            "retryable": self.retryable,
            "shown_in": self.shown_in or None,
            "raised_at": list(self.raised_at),
        }


_CONFIG_EXITS = MappingProxyType({"routine": 2})

_ENTRIES = (
    ErrorCode(
        "CLI_UNKNOWN_COMMAND",
        "The first argument is not a cairn command.",
        "The dispatcher knows a fixed set of commands and guesses none.",
        "Use a command from the list the error prints.",
        ("cairn", "help"),
        2,
        False,
        ("src/cairn/__main__.py:main",),
        "stderr code line",
    ),
    ErrorCode(
        "CONFIG_NOT_FOUND",
        "No config file at $CAIRN_CONFIG, the user config path or the repo root.",
        "Every command that touches a store needs the config; cairn has no built-in default.",
        "Run 'cairn config init', then edit the file it names.",
        ("cairn", "config", "path"),
        1,
        False,
        ("src/cairn/config.py:_config_path",),
        "stderr code line; --json failure object; doctor [CODE] suffix",
        _CONFIG_EXITS,
    ),
    ErrorCode(
        "CONFIG_INVALID",
        "The config is not valid JSON, cannot be read, or a value fails its check.",
        "A config error is never swallowed: a wrong value would act on the wrong store.",
        "Fix the field the message names.",
        ("cairn", "config", "show", "--json"),
        1,
        False,
        ("src/cairn/config.py:load_config_from",),
        "stderr code line; --json failure object; doctor [CODE] suffix",
        _CONFIG_EXITS,
    ),
    ErrorCode(
        "CONFIG_UNKNOWN_KEY",
        "A config object holds a key cairn does not know.",
        "An unknown key is a typo or a stale knob; cairn refuses it so the config "
        "cannot claim a setting the code never reads.",
        "Remove or rename the key the message names; the message lists the allowed keys.",
        ("cairn", "config", "show", "--json"),
        1,
        False,
        (
            "src/cairn/config.py:_parse_collection",
            "src/cairn/config.py:_parse_billing",
            "src/cairn/config.py:_parse_peer",
            "src/cairn/config.py:_parse_profile",
            "src/cairn/config.py:_parse_routine",
        ),
        "stderr code line; --json failure object; doctor [CODE] suffix",
        _CONFIG_EXITS,
    ),
    ErrorCode(
        "QMD_MISSING",
        "The configured qmd_binary is not on PATH and is not a file.",
        "Search and indexing run through QMD; without it no collection is searchable.",
        "Install QMD, or set qmd_binary to its path.",
        ("cairn", "doctor"),
        1,
        False,
        ("src/cairn/admin.py:doctor_main",),
        "doctor [CODE] suffix",
    ),
    ErrorCode(
        "QMD_RUNTIME_FAILED",
        "'qmd status' failed, so QMD cannot open its index.",
        "A QMD that cannot open its index answers no search.",
        "Read the QMD error in the doctor line, then check the QMD install.",
        ("cairn", "doctor"),
        1,
        False,
        ("src/cairn/admin.py:_check_qmd_runtime",),
        "doctor [CODE] suffix",
    ),
    ErrorCode(
        "QMD_INDEX_INCOMPLETE",
        "The QMD index is empty, has no vectors, has pending documents, or its status "
        "could not be read.",
        "Keyword search can still work, but semantic search misses what is not embedded.",
        "Run 'cairn sync --cron'; investigate if the pending count does not fall across runs.",
        ("cairn", "doctor", "--strict-index"),
        0,
        True,
        ("src/cairn/admin.py:_check_embeddings",),
        "doctor [CODE] suffix",
        exit_note="0 as a warning; 1 with --strict-index (an empty index stays a warning)",
    ),
    ErrorCode(
        "HOOK_NOT_TRUSTED",
        "Hook files exist, but the host has not confirmed that it trusts them.",
        "File presence does not establish trust; only the host can say a hook runs.",
        "Ask the user to review and trust cairn's hook in the host.",
        ("cairn", "hook", "status"),
        0,
        False,
        ("src/cairn/admin.py:doctor_main", "src/cairn/admin.py:_check_codex_trust"),
        "doctor [CODE] suffix",
    ),
    ErrorCode(
        "CODEX_HOOK_TRUST_BROKEN",
        "cairn's Codex SessionEnd group is stale, has no trust table, or is disabled.",
        "Codex skips a hook whose trusted hash does not match, so no session is exported "
        "at session end.",
        "Run 'cairn hook install --harness codex', or enable and trust the hook in /hooks.",
        ("cairn", "doctor", "--harness", "codex"),
        0,
        False,
        ("src/cairn/admin.py:_check_codex_trust",),
        "doctor [CODE] suffix",
    ),
    ErrorCode(
        "EXPORT_PARSER_DRIFT",
        "A source file holds records, but the parser rendered no message, read no line, "
        "or found no session id.",
        "The native format probably changed; writing the document would replace the last "
        "good one with an empty one, so the export keeps the old document.",
        "Read the finding in 'cairn doctor', then update cairn or report the format change.",
        ("cairn", "doctor"),
        1,
        False,
        ("src/cairn/sources/guards.py:check_parse", "src/cairn/admin.py:_check_drift"),
        "doctor [CODE] suffix; the export's own 'Error: parser drift' line has no code yet",
    ),
    ErrorCode(
        "EXPORT_DOCUMENT_UNPARSEABLE",
        "An exported document has front matter cairn cannot parse, so its re-export is refused.",
        "Re-export rewrites the document and keeps its annotations; with unparseable front "
        "matter it cannot tell what to keep.",
        "Fix or move the document the message names, then export again.",
        ("cairn", "doctor"),
        1,
        False,
        ("src/cairn/sources/guards.py:document_finding", "src/cairn/admin.py:_check_drift"),
        "doctor [CODE] suffix; the export's own 'Error: refusing to re-export' line has no "
        "code yet",
    ),
    ErrorCode(
        "SYNC_SOURCE_GAP",
        "Sessions changed in the last 30 days have no exported document.",
        "A gap that one re-export cannot close points to a parser or store problem, not to timing.",
        "Run 'cairn sync'; if the gap stays, read the parser drift lines in 'cairn doctor'.",
        ("cairn", "recall", "gaps"),
        1,
        True,
        ("src/cairn/sync.py:gap_check", "src/cairn/admin.py:_check_gaps"),
        "doctor [CODE] suffix; 'cairn sync' fails the gap-check step without a code",
        exit_note="1 from 'cairn sync --cron' or '--all' (step gap-check:COLLECTION); "
        "0 in 'cairn doctor', which only warns",
    ),
    ErrorCode(
        "RECALL_FAILED",
        "recall could not answer: a bad argument, an unreadable file, a config error or "
        "a refresh timeout.",
        "recall reads the exported store and never guesses a date or a session.",
        "Act on the argument or file the message names.",
        ("cairn", "recall", "list", "today"),
        1,
        False,
        ("src/cairn/recall.py:main",),
        "stderr code line; --json failure object",
    ),
    ErrorCode(
        "RECALL_DEGRADED",
        "Coverage is degraded: export lag, index lag, unindexed files, a disabled "
        "source, a missing store or a missing rg.",
        "A miss on a degraded store is not evidence that the work never happened.",
        "Run the fix each gap names, or say the result is degraded.",
        ("cairn", "recall", "gaps", "--json"),
        0,
        True,
        ("src/cairn/recall_gaps.py:status_of", "src/cairn/recall_gaps.py:coverage"),
        "",
        exit_note="0: the status is data ('status: degraded', verdict 'degraded-miss')",
    ),
    ErrorCode(
        "RECALL_VERIFY_FAILED",
        "A journal citation does not resolve to an unchanged digest frame, or a quote is "
        "not in a cited frame.",
        "An unverified citation or quote would put words in the record that the sessions "
        "do not hold.",
        "Correct the lines the failures name, or re-run with --fix to move them under "
        "'## Unverified'.",
        None,
        4,
        False,
        ("src/cairn/verify.py:run_verify",),
        "stderr code line; 'code' in --json",
    ),
    ErrorCode(
        "DIGEST_EMPTY_DAY",
        "No session was written for the day.",
        "An empty day is not an error, but a routine must not write a journal from nothing.",
        "Nothing to digest; treat the day as empty.",
        ("cairn", "recall", "list", "yesterday", "--all-projects"),
        3,
        False,
        ("src/cairn/digest.py:run_digest",),
        "stderr code line; 'code' and 'fix' in --json",
    ),
    ErrorCode(
        "DECISION_CLAIM_WITHDRAWN",
        "decide or supersede was refused because the claim was withdrawn with 'forget'.",
        "A withdrawn claim must not come back through a later annotation by accident.",
        "Do not add it again. Pass --force only if the user confirms the claim is true now.",
        ("cairn", "recall", "decisions", "--include-struck", "--json"),
        3,
        False,
        ("src/cairn/withdrawals.py:guard", "src/cairn/decisions.py:cmd_decide"),
        "stderr code line",
    ),
    ErrorCode(
        "INSTRUCTIONS_HOME_MISSING",
        "The harness home (Claude Code config directory or Codex home) does not exist.",
        "cairn edits an instruction file only inside an existing harness home.",
        "Install the harness, or pick the other --harness.",
        ("cairn", "instructions", "status", "--json"),
        1,
        False,
        ("src/cairn/instructions.py:_require_home",),
        "stderr code line; 'code' and 'fix' in --json",
    ),
    ErrorCode(
        "INSTRUCTIONS_BLOCK_EDITED",
        "The recall-first block was edited by hand since cairn wrote it.",
        "install replaces the whole block, so it would drop the hand edit.",
        "Move the text outside the markers, or ask the user before passing --force.",
        ("cairn", "instructions", "status", "--harness", "{harness}"),
        1,
        False,
        ("src/cairn/instructions.py:install", "src/cairn/admin.py:_check_recall_first"),
        "stderr code line; 'code' and 'fix' in --json; doctor [CODE] suffix",
    ),
    ErrorCode(
        "INSTRUCTIONS_MARKERS_DAMAGED",
        "The recall-first markers are not one begin and one end, in that order.",
        "With damaged markers cairn cannot tell its block from the user's text.",
        "Fix or delete the markers by hand, then re-run.",
        ("cairn", "instructions", "status", "--harness", "{harness}"),
        1,
        False,
        ("src/cairn/instructions.py:_damaged_error", "src/cairn/admin.py:_check_recall_first"),
        "stderr code line; 'code' and 'fix' in --json; doctor [CODE] suffix",
    ),
    ErrorCode(
        "INSTRUCTIONS_FAILED",
        "The instruction file or the opt-out ledger could not be read or written: not "
        "UTF-8, changed during the write, invalid JSON, or an OS error.",
        "cairn writes nothing it cannot read back exactly.",
        "Act on the path the message names, then re-run.",
        ("cairn", "instructions", "status", "--harness", "{harness}"),
        1,
        False,
        ("src/cairn/instructions.py:InstructionsError",),
        "stderr code line; 'code' and 'fix' in --json",
    ),
    ErrorCode(
        "ROUTINE_UNKNOWN",
        "The routine name is not in the routines config.",
        "A routine runs only from its config entry.",
        "Use a name from 'cairn routine list', or add the routine to the config.",
        ("cairn", "routine", "list"),
        2,
        False,
        ("src/cairn/routine.py:_routine",),
        "stderr code line",
    ),
    ErrorCode(
        "ROUTINE_USAGE",
        "A routine argument is wrong: a bad name, a --day that has not ended, a bad "
        "--reason, or a launchd action off macOS.",
        "The run refuses before it spends anything and writes no stamp.",
        "Correct the argument the message names.",
        ("cairn", "routine", "list"),
        2,
        False,
        ("src/cairn/routine.py:main", "src/cairn/routine_run.py:run_routine"),
        "stderr code line; 'code' in last-run.json and --json",
    ),
    ErrorCode(
        "ROUTINE_TEMPLATE_UNKNOWN_VAR",
        "A {{NAME}} token in the prompt file, prepare, validate or profile extra_args is "
        "not a known template variable.",
        "Rendering happens before any spend; an unknown token is a prompt or config "
        "defect, so no stamp is written.",
        "Fix the token the message names, or add an outputs key that defines it, then dry-run.",
        ("cairn", "routine", "run", "{name}", "--dry-run"),
        2,
        False,
        ("src/cairn/routine_plan.py:_unknown",),
        "stderr code line; 'code' in last-run.json and --json",
    ),
    ErrorCode(
        "ROUTINE_INPUT_INVALID",
        "The prompt_file is missing, unreadable or renders empty, or cd is not a directory.",
        "A run with no prompt or no working directory cannot do the task.",
        "Fix the path in the routine config, then dry-run.",
        ("cairn", "routine", "run", "{name}", "--dry-run"),
        2,
        False,
        ("src/cairn/routine_plan.py:check_inputs", "src/cairn/routine_plan.py:render_day"),
        "stderr code line; 'code' in last-run.json and --json",
    ),
    ErrorCode(
        "ROUTINE_NESTED",
        "'cairn routine run' started inside a peer run.",
        "A routine started by a peer would spend and record under the wrong owner.",
        "Start it from a shell, or with 'cairn routine kickstart NAME'.",
        ("cairn", "routine", "status", "{name}"),
        2,
        False,
        ("src/cairn/routine_run.py:run_routine",),
        "stderr code line",
    ),
    ErrorCode(
        "ROUTINE_DAY_FAILED",
        "A day failed or was deferred; the reasons are in 'failures'.",
        "Each failed day keeps its attempt dir and stamp, so a later run can retry it.",
        "Read the reason and the attempt dir, then re-run with --day.",
        ("cairn", "routine", "status", "{name}", "--json"),
        1,
        True,
        ("src/cairn/routine_run.py:body",),
        "'code' in last-run.json and --json",
    ),
    ErrorCode(
        "ROUTINE_NEEDS_ATTENTION",
        "'cairn routine status' found a state that needs attention: never run, stale, a "
        "failed day, a non-zero last run, an interrupted run, a missing plist or a "
        "stale source.",
        "The routine is not paused, but its output is missing or old.",
        "Read the attention codes in the status output and act on each.",
        ("cairn", "routine", "status", "{name}", "--json"),
        1,
        True,
        ("src/cairn/routine_state.py:classify", "src/cairn/routine.py:status"),
        "stderr code line; 'code' in --json",
    ),
    ErrorCode(
        "ROUTINE_HELD",
        "The routine is paused, or the breaker disabled it after repeated failures.",
        "A held routine skips every run until 'cairn routine resume'; --force does not bypass it.",
        "Read the reason; ask the user before 'cairn routine resume NAME'.",
        ("cairn", "routine", "status", "{name}"),
        2,
        False,
        ("src/cairn/routine_state.py:classify", "src/cairn/routine.py:status"),
        "stderr code line; 'code' in --json",
    ),
    ErrorCode(
        "INVOKE_USAGE",
        "The request was refused before spawning: a bad flag, profile, prompt, cwd, owner "
        "or resume target, or a failed self-check or permissions check.",
        "Nothing ran and nothing was spent.",
        "Fix the flag or value the message names.",
        ("cairn", "invoke", "{peer}", "--help"),
        2,
        False,
        ("src/cairn/invoke.py:main", "src/cairn/invoke.py:run"),
        "stderr code line; --json failure object",
    ),
    ErrorCode(
        "INVOKE_NESTED",
        "invoke started inside a peer run without --allow-nested.",
        "A peer that starts peers can loop and spend without a person in it.",
        "Do not nest; ask the user before passing --allow-nested.",
        None,
        2,
        False,
        ("src/cairn/invoke.py:run",),
        "stderr code line; --json failure object",
    ),
    ErrorCode(
        "INVOKE_BILLING_CHECK_FAILED",
        "The billing route is not ready, or Google refused to mint an ADC token.",
        "A run on the wrong credits cannot be undone, so the check is never skipped.",
        "Run the command the message names, or ask the user to sign in again.",
        ("cairn", "doctor", "--harness", "antigravity"),
        2,
        False,
        ("src/cairn/invoke.py:run",),
        "stderr code line; --json failure object",
    ),
    ErrorCode(
        "INVOKE_BUSY",
        "Another round of the thread holds the lock.",
        "One thread runs one round at a time.",
        "Wait for the running round, then retry.",
        ("cairn", "invoke", "status", "{thread_id}"),
        75,
        True,
        ("src/cairn/invoke.py:run", "src/cairn/invoke.py:cmd_publish"),
        "'code' and 'fix' in the result line and in 'invoke publish --json'",
    ),
    ErrorCode(
        "INVOKE_DENIED",
        "The peer's tool calls were refused and nothing it did stands.",
        "The round answered without the reads or writes it needed.",
        "Add the allow rule the error names, then resume the thread.",
        ("cairn", "invoke", "tail", "{thread_id}", "--no-follow"),
        2,
        True,
        ("src/cairn/invoke.py:round_result",),
        "'code' and 'fix' in the result line",
    ),
    ErrorCode(
        "INVOKE_TIMEOUT",
        "The round hit its wall-clock timeout.",
        "cairn stops a round at --timeout-s so a stuck peer cannot run without end.",
        "Raise --timeout-s or narrow the task, then resume.",
        ("cairn", "invoke", "status", "{thread_id}"),
        124,
        True,
        ("src/cairn/invoke.py:round_result",),
        "'code' and 'fix' in the result line",
    ),
    ErrorCode(
        "INVOKE_IDLE_TIMEOUT",
        "The round showed no activity for idle_timeout_s.",
        "A silent peer is usually waiting on something that will not come.",
        "Raise --idle-timeout-s or narrow the task, then resume.",
        ("cairn", "invoke", "status", "{thread_id}"),
        125,
        True,
        ("src/cairn/invoke.py:round_result",),
        "'code' and 'fix' in the result line",
    ),
    ErrorCode(
        "INVOKE_CANCELLED",
        "A signal stopped the round.",
        "Someone chose to stop it.",
        "Resume only if the user still wants the task.",
        ("cairn", "invoke", "status", "{thread_id}"),
        130,
        False,
        ("src/cairn/invoke.py:round_result",),
        "'code' and 'fix' in the result line",
    ),
    ErrorCode(
        "INVOKE_FAILED",
        "The peer exited non-zero or without a final event.",
        "The peer's own error decides whether a retry can help.",
        "Read 'error' and 'stderr_path'; retry when 'retryable' is true.",
        ("cairn", "invoke", "status", "{thread_id}"),
        1,
        False,
        ("src/cairn/invoke.py:round_result",),
        "'code' and 'fix' in the result line; 'retryable' is set per round",
        exit_note="the peer's exit code when it is positive, else 1",
    ),
)

REGISTRY: Mapping[str, ErrorCode] = MappingProxyType({e.code: e for e in _ENTRIES})

_OUTCOMES = MappingProxyType(
    {
        "busy": "INVOKE_BUSY",
        "denied": "INVOKE_DENIED",
        "timeout": "INVOKE_TIMEOUT",
        "idle_timeout": "INVOKE_IDLE_TIMEOUT",
        "cancelled": "INVOKE_CANCELLED",
        "failed": "INVOKE_FAILED",
    }
)


# ---------------------------------------------------------------------------
# Helpers for the raise sites
# ---------------------------------------------------------------------------


def entry(code: str) -> ErrorCode:
    """The row for ``code``; ``KeyError`` for an unknown one, so a typo fails a test."""
    return REGISTRY[code]


def code_of(exc: BaseException, default: str) -> str:
    """The ``code`` an exception carries, else ``default``."""
    return getattr(exc, "code", None) or default


def exit_for(code: str, command: str | None = None) -> int:
    """The exit the registry records for ``code`` (from ``command``, when it differs)."""
    row = entry(code)
    return row.exit_by_command.get(command, row.exit) if command else row.exit


def fix(code: str, **fill: str | None) -> dict[str, Any]:
    """``{"next_step": ..., "verify": [...]}``; ``verify`` is left out when absent or
    when a placeholder in it stays unfilled."""
    row = entry(code)
    out: dict[str, Any] = {"next_step": row.next_step}
    if row.verify is None:
        return out
    values = {k: str(v) for k, v in fill.items() if v is not None}
    argv: list[str] = []
    for part in row.verify:
        names = _PLACEHOLDER_RE.findall(part)
        if any(name not in values for name in names):
            return out
        argv.append(_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], part))
    out["verify"] = argv
    return out


def tag(payload: Mapping[str, Any], code: str, **fill: str | None) -> dict[str, Any]:
    """``payload`` plus ``code`` and ``fix``; keys already in ``payload`` win."""
    out = dict(payload)
    out.setdefault("code", code)
    out.setdefault("fix", fix(code, **fill))
    return out


def code_line(code: str) -> str:
    """The second stderr line of a converted site."""
    entry(code)
    return f"code: {code} (run 'cairn errors {code}')"


def emit(code: str) -> None:
    """Print the ``code:`` line on stderr."""
    print(code_line(code), file=sys.stderr)


def fail(
    code: str,
    message: str,
    *,
    exit: int,
    prefix: str = "",
    as_json: bool = False,
    **fill: str | None,
) -> int:
    """Print ``prefix + message`` and the ``code:`` line on stderr, and with ``as_json``
    the failure object on stdout. Returns ``exit``: the site decides its exit."""
    entry(code)
    print(f"{prefix}{message}", file=sys.stderr)
    emit(code)
    if as_json:
        print(json.dumps(failure(code, message, exit=exit, **fill), ensure_ascii=False))
    return exit


def failure(code: str, message: str, *, exit: int, **fill: str | None) -> dict[str, Any]:
    """The ``--json`` failure object; ``error`` is the message without a prefix."""
    return {
        "ok": False,
        "code": code,
        "error": message,
        "exit": exit,
        "retryable": entry(code).retryable,
        "fix": fix(code, **fill),
    }


def outcome_code(status: str | None) -> str | None:
    """The code of an invoke round status; ``None`` for ``succeeded`` and the rest."""
    return _OUTCOMES.get(status or "")


def finding(code: str | None) -> str:
    """The `` [CODE]`` suffix a doctor line carries."""
    if code is None:
        return ""
    entry(code)
    return f" [{code}]"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _exit_text(row: ErrorCode) -> str:
    if row.exit_note:
        return row.exit_note
    text = str(row.exit)
    if row.exit_by_command:
        text += " (" + ", ".join(f"cairn {c}: {n}" for c, n in row.exit_by_command.items()) + ")"
    return text


def _verify_text(row: ErrorCode) -> str:
    return " ".join(row.verify) if row.verify is not None else "none"


def describe(row: ErrorCode) -> str:
    """The labelled lines ``cairn errors CODE`` prints."""
    lines = [
        row.code,
        f"Meaning:    {row.meaning}",
        f"Why:        {row.why}",
        f"Next step:  {row.next_step}",
        f"Verify:     {_verify_text(row)}",
        f"Exit:       {_exit_text(row)}",
        f"Retryable:  {'yes' if row.retryable else 'no'}",
        f"Shown in:   {row.shown_in or 'described only; no output carries the code yet'}",
        f"Raised at:  {', '.join(row.raised_at)}",
    ]
    return "\n".join(lines)


def render_markdown() -> str:
    """``docs/error-codes.md``: the contract, a summary table and one section per code."""
    rows = sorted(REGISTRY.values(), key=lambda r: r.code)
    out = [
        "<!-- GENERATED by python -m cairn.errors --render; do not edit -->",
        "",
        "# cairn error codes",
        "",
        "Each common cairn failure has a stable upper-snake code. A converted command keeps "
        "its first",
        "stderr line and adds `code: CODE (run 'cairn errors CODE')` as the second; with "
        "`--json` the",
        'failure object, or the existing result object, carries `"code"` and `"fix"`. Doctor lines',
        "end with ` [CODE]`. `cairn errors CODE` prints one row; it reads no config.",
        "",
        "The contract for an agent:",
        "",
        "1. Read `code` (the second stderr line, the JSON key, or the doctor suffix).",
        "2. Run `cairn errors CODE`.",
        "3. Run its verify command. Every verify command is read-only (`cairn doctor` creates a",
        "   missing `data_root` and nothing else).",
        "4. Act on the next step, or ask the user when the step says so.",
        "",
        "An exit status alone is ambiguous: 2 is a usage error and a denied invoke round, 3 is an",
        "empty digest day, a stalled `agents wait` and a withdrawn claim. The registry "
        "records each",
        "exit; the command decides it.",
        "",
        "## Summary",
        "",
        "| Code | Exit | Retryable | Meaning |",
        "|---|---|---|---|",
    ]
    for row in rows:
        exit_text = _exit_text(row).replace("|", "\\|")
        out.append(
            f"| `{row.code}` | {exit_text} | {'yes' if row.retryable else 'no'} | {row.meaning} |"
        )
    for row in rows:
        out += [
            "",
            f"## {row.code}",
            "",
            f"- Meaning: {row.meaning}",
            f"- Why: {row.why}",
            f"- Next step: {row.next_step}",
            f"- Verify: `{_verify_text(row)}`" if row.verify is not None else "- Verify: none",
            f"- Exit: {_exit_text(row)}",
            f"- Retryable: {'yes' if row.retryable else 'no'}",
            f"- Shown in: {row.shown_in or 'described only; no output carries the code yet'}",
            "- Raised at: " + ", ".join(f"`{site}`" for site in row.raised_at),
        ]
    out += [
        "",
        "## Regenerate",
        "",
        "```",
        RENDER_COMMAND,
        "```",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn errors",
        description="Look up a cairn error code: meaning, next step, read-only verify command.",
    )
    parser.add_argument("code", nargs="?", metavar="CODE", help="a code such as CONFIG_INVALID")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--render", action="store_true", help="print docs/error-codes.md (for maintainers)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.render:
        sys.stdout.write(render_markdown())
        return 0
    if args.code is None:
        rows = sorted(REGISTRY.values(), key=lambda r: r.code)
        if args.json:
            print(json.dumps({"codes": [r.as_json() for r in rows]}, indent=2))
            return 0
        width = max(len(r.code) for r in rows)
        for row in rows:
            print(f"{row.code:<{width}}  exit {row.exit:<3}  {row.meaning}")
        return 0
    found = REGISTRY.get(args.code.upper())
    if found is None:
        print(
            f"cairn errors: unknown code {args.code!r}; run 'cairn errors' for the list",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(found.as_json(), indent=2) if args.json else describe(found))
    return 0


__all__ = [
    "CONTRACT",
    "PLACEHOLDERS",
    "REGISTRY",
    "ErrorCode",
    "code_line",
    "code_of",
    "emit",
    "entry",
    "exit_for",
    "fail",
    "failure",
    "finding",
    "fix",
    "main",
    "outcome_code",
    "render_markdown",
    "tag",
]


if __name__ == "__main__":
    sys.exit(main())
