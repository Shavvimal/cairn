---
name: invoke-claude
description: Run Claude Code headlessly through `cairn invoke claude` for an adversarial second opinion, code review, plan review or an independent fresh-context pass, with progress readable mid-run, the session resumable later, and the whole exchange recorded in cairn. Use when the user says "invoke claude", "ask claude", "claude review", "second opinion from claude", "claude resume", or asks for a cross-model quality gate from Codex.
argument-hint: [prompt or gate name] [model] [effort]
allowed-tools: Bash(cairn:*), Bash(qmd:*), Read, AskUserQuestion, Monitor
---

Read [the harness contract](../../references/harnesses/contract.md) and its relevant
Claude or Codex reference from this installed package before host-specific operations.
Then read [the peer review doctrine](../../references/peer-review.md).


# Invoke Claude

The engine is `cairn invoke claude`. This skill only drives it. The CLI is authoritative for
flags: run `cairn invoke claude --help`. Every run is recorded under cairn's data root, the
Claude session is exported and linked to the calling session, and an exchange document with
the prompt, progress, answer and result per round lands in the calling session's folder.

## Host caveat

Inside the Codex sandbox, `claude` cannot reach its login keychain and reports "Not logged
in". Run this skill from Codex with escalated permissions for the `cairn invoke claude`
command, or from the Claude host. The engine reports that failure with `retryable: false`
and the guidance text; do not retry it unchanged.

## 1. Prompt

Take the prompt from the user's words, or pick a gate prompt from the doctrine. Write
anything longer than a few lines to a file and pass `--prompt-file`. Always ask for the
`VERDICT:` line when the task is a review.

## 2. Model and effort

Claude has no local model catalogue. `--model` and `--effort` are optional; effort is one of
`low`, `medium`, `high`, `xhigh`, `max`. Omit both to let the Claude CLI choose, or ask with
the host's input tool when the user cares. Defaults come from the config
(`cairn config show --json`, key `invoke.claude`).

## 3. Profile

Default `--profile review`: permission mode `plan` with the edit tools disallowed. Ask before
`edit` (`acceptEdits`) or `full` (`bypassPermissions`). These are tool-permission guarantees,
not an operating-system sandbox; Claude's own sandbox settings are separate.
`--permission-mode` passes a native mode directly and overrides the profile. The record
stores the exact flags that ran.

`--profile` also accepts a name from `invoke.claude.profiles` in the config: a built-in
`base` plus `extra_args` such as `--allowedTools` and `--disallowedTools`, each one
comma-joined string. Such a profile cannot drop its base's deny rules or switch to a looser
permission mode, and deny beats allow. Bash rules use the prefix form `Bash(cmd:*)`. A
profile holding `{{NAME}}` tokens runs only through `cairn routine run`. `--owner LABEL`
records who started the round (a routine passes `routine:<name>`); a resume keeps it.

## 4. Run in the background

Start it the way the host reference describes (Claude: `Bash` with `run_in_background:
true`; Codex: `exec_command` with a long yield, escalated as the caveat says) and keep the
handle:

```bash
cairn invoke claude --json --prompt-file <brief.md> [--model <slug>] [--effort <level>] \
  --profile review [--cd <repo>] [--timeout-s 1800]
```

The session id is chosen up front and printed on the first stderr line. Nested runs are
refused at depth 1 unless the user asks for `--allow-nested`.

## 5. Watch

```bash
cairn invoke status <session_id>              # health line, progress, answer so far
cairn invoke tail <session_id>                # one line per event, until RESULT <outcome>
cairn invoke wait <session_id> --json         # block until the round exits; its result line
```

After the identity line, the health line shows execution state, activity with its age, and the last event.
Then the progress bullets (tool calls and messages). `silent` beyond the stall threshold
means a probable hang: wait one more interval, then `cairn invoke cancel <session_id>` and
resume with a narrower task, or report. Silence from the host task is never completion.

## 6. Read the result

One JSON line on the task's stdout. Lead with `last_message`. Then check `status`,
`outcome`, `exit_code`, `retryable`, `warnings` and `publication`. Evaluate critically per
the doctrine. Exit codes: 0 succeeded; 2 self-check, depth or usage refusal; 75 thread busy;
124 wall-clock timeout; 125 idle timeout; 130 cancelled; otherwise the Claude exit code.

## 7. Debate

```bash
cairn invoke claude --json --resume <session_id> --prompt "This is <host> (<model>) following up. Re: <finding>. <evidence>. Convince me otherwise or concede. End with one line: VERDICT: accept | reject | unresolved - <reason>."
```

Resume inherits the recorded cwd, profile, model and effort. At most three rounds per thread,
then escalate to the user with the accepted, dismissed and unresolved lists.

## 8. Close

Tell the user: the session id, the exchange document path and the Claude session document path
from the result, and how to continue: `cairn invoke claude --resume <session_id> --prompt "…"` for another
round, or `cairn resume claude:<session_id>` to open it interactively. If `warnings` is empty
and `indexed` is true, `/search` finds the exchange document and `/recall` lists the peer
session now; if `indexed` is false, `rg` finds them at once and `/search` after `cairn invoke publish <session_id>` or the next
`cairn sync`.

## Errors

- Exit 2 before any run: an unknown effort, `--fast` (Codex only), or a nested run refused.
- Exit 124 or 125: raise `--timeout-s` or `--idle-timeout-s`, or narrow the task. Never retry
  blind; read `status` first.
- `retryable: false` with the keychain guidance: rerun with escalated permissions or from the
  Claude host. Otherwise check `outcome` first: `cancelled` means someone stopped it; any other
  `retryable: false` is a provider error. Report the `error` text.
- `warnings`: the answer is saved but a publishing step is pending. `cairn invoke publish
  <session_id>` finishes it; the next `cairn sync` also does.
- `status: busy` (exit 75): another round of this session is running. Wait or `status` it.
