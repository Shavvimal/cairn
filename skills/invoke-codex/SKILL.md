---
name: invoke-codex
description: Run OpenAI Codex headlessly through `cairn invoke codex` for an adversarial second opinion, code review, plan review or test simulation, with progress readable mid-run, the thread resumable later, and the whole exchange recorded in cairn. Use when the user says "invoke codex", "run codex", "ask codex", "codex review", "codex audit", "second opinion from codex", "codex resume", names a Codex model and effort such as "astra high", or asks for a cross-model quality gate before committing a significant change.
argument-hint: [prompt or gate name] [model] [effort]
allowed-tools: Bash(cairn:*), Bash(qmd:*), Read, AskUserQuestion, Monitor
---

Read [the harness contract](../../references/harnesses/contract.md) and its relevant
Claude or Codex reference from this installed package before host-specific operations.
Then read [the peer review doctrine](../../references/peer-review.md).


# Invoke Codex

The engine is `cairn invoke codex`. This skill only drives it. The CLI is authoritative for
flags: run `cairn invoke codex --help`. Every run is recorded under cairn's data root, the
Codex session is exported and linked to the calling session, and an exchange document with
the prompt, progress, answer and result per round lands in the calling session's folder.

## 1. Prompt

Take the prompt from the user's words, or pick a gate prompt from the doctrine. Write
anything longer than a few lines to a file and pass `--prompt-file`. Always ask for the
`VERDICT:` line when the task is a review.

## 2. Model and effort

```bash
cairn invoke list-models
```

Offer only slugs and efforts from that list. Ask with the host's input tool unless the user
named them ("astra high" means `gpt-6-astra` at `high`) or said "just run it". The defaults
come from the config (`cairn config show --json`, key `invoke.codex`). If the list says there
is no model cache, ask the user to run `codex` once, then retry.

## 3. Profile

Default `--profile review` (Codex sandbox `read-only`). Ask before `edit` (`workspace-write`)
or `full` (`danger-full-access`). `--sandbox` passes a native Codex sandbox mode directly and
overrides the profile. The record stores the exact flags that ran.

## 4. Run in the background

A high effort run takes 10 to 20 minutes, past any foreground tool budget. Start it the way
the host reference describes (Claude: `Bash` with `run_in_background: true`; Codex:
`exec_command` with a long yield) and keep the handle:

```bash
cairn invoke codex --json --prompt-file <brief.md> --model <slug> --effort <level> \
  --profile review [--cd <repo>] [--timeout-s 1800]
```

The first stderr line names the thread id. Nested runs are refused at depth 1 unless the user
asks for `--allow-nested`.

## 5. Watch

```bash
cairn invoke status <thread_id>
```

After the identity line, the health line shows execution state (`running`, `orphaned`, `exited`), activity
(`active`, `quiet`, `silent` with its age) and the last event. Then the progress bullets.
Check every few minutes or when the user asks. `silent` for more than the stall threshold
means a probable hang: wait one more interval, then `cairn invoke cancel <thread_id>` and
resume with a narrower task, or report. Silence from the host task is never completion.

## 6. Read the result

The result is one JSON line on the task's stdout. Lead with `last_message`. Then check
`status`, `outcome`, `exit_code`, `retryable`, `warnings` and `publication`. Evaluate the
findings critically per the doctrine. Exit codes: 0 succeeded; 2 self-check, depth or usage
refusal; 75 thread busy; 124 wall-clock timeout; 125 idle timeout; 130 cancelled; otherwise
the Codex exit code.

## 7. Debate

```bash
cairn invoke codex --json --resume <thread_id> --prompt "This is <host> (<model>) following up. Re: <finding>. <evidence>. Convince me otherwise or concede. End with one line: VERDICT: accept | reject | unresolved - <reason>."
```

Resume inherits the recorded cwd, profile, model and effort. At most three rounds per thread,
then escalate to the user with the accepted, dismissed and unresolved lists.

## 8. Close

Tell the user: the thread id, the exchange document path and the Codex session document path
from the result, and how to continue: `cairn invoke codex --resume <thread_id> --prompt "…"` for another
round, or `cairn resume codex:<thread_id>` to open it interactively. If `warnings` is empty
and `indexed` is true, `/search` finds the exchange document and `/recall` lists the peer
session now; if `indexed` is false, `rg` finds them at once and `/search` after `cairn invoke publish <thread_id>` or the next
`cairn sync`.

## Errors

- Exit 2 before any run: the model and effort pair is not in the catalogue, or a nested run
  was refused. Re-check the list or ask the user.
- Exit 124 or 125: raise `--timeout-s` or `--idle-timeout-s`, or narrow the task. Never retry
  blind; read `status` first.
- `retryable: false`: check `outcome` first. `cancelled` means someone stopped it; otherwise it
  is a provider or login error. Report the `error` text.
- `warnings`: the answer is saved but a publishing step is pending (export, link or index).
  `cairn invoke publish <thread_id>` finishes it; the next `cairn sync` also does.
- `status: busy` (exit 75): another round of this thread is running. Wait or `status` it.
