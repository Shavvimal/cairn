---
name: invoke-antigravity
description: Run Google Antigravity (the `agy` CLI) headlessly through `cairn invoke antigravity` for an adversarial second opinion, code review, plan review or an independent Gemini pass, with progress readable mid-run, the conversation resumable later, and the exchange recorded in cairn. Use when the user says "invoke antigravity", "ask antigravity", "ask agy", "antigravity review", "gemini review", "second opinion from antigravity", "antigravity resume", or asks for a cross-model quality gate from Google's agent.
argument-hint: [prompt or gate name] [model] [effort]
allowed-tools: Bash(cairn:*), Bash(qmd:*), Bash(agy:*), Read, AskUserQuestion, Monitor
---

Read [the harness contract](../../references/harnesses/contract.md) and its relevant
Claude or Codex reference from this installed package before host-specific operations.
Then read [the peer review doctrine](../../references/peer-review.md).


# Invoke Antigravity

The engine is `cairn invoke antigravity`. This skill only drives it. The CLI is authoritative
for flags: run `cairn invoke antigravity --help`. Every run is recorded under cairn's data
root, the Antigravity conversation is exported as a session document and linked to the
calling session, and an exchange document with the prompt, progress, answer and result per
round lands in the calling session's folder.

## Host caveat

`agy` needs a cached login: the user must have run `agy` interactively once on this machine.
Without it a headless run fails at once with "not logged into Antigravity" and the engine
reports `retryable: false`; ask the user to log in, do not retry. Inside the Codex sandbox
`agy` may not reach its credential store either; run with escalated permissions or from the
Claude host.

## 1. Prompt

Take the prompt from the user's words, or pick a gate prompt from the doctrine. Write
anything longer than a few lines to a file and pass `--prompt-file`. Always ask for the
`VERDICT:` line when the task is a review.

## 2. Model and effort

```bash
agy models
```

Offer only slugs from that list (for example `gemini-3.1-pro-high`); the slug carries the
model's own thinking level. `--effort` is cairn's separate reasoning-effort flag, one of
`low`, `medium`, `high`, `xhigh`, `max`. Both are optional: omit them to let `agy` choose, or
ask with the host's input tool when the user cares. Defaults come from the config
(`cairn config show --json`, key `invoke.antigravity`). An unknown slug fails the run with
exit 1 and the available models in `error`.

## 3. Profile

Default `--profile review`: `agy --mode plan`, which reads the workspace and defers every
file write until a human approves the plan, so a headless run cannot change anything. Ask
before `edit` (`--mode accept-edits`: file writes apply) or `full`
(`--dangerously-skip-permissions`: every tool call approved). In `plan` and `accept-edits` a
shell command runs only when an allow rule covers it (section 3a); `full` needs no rules.
`--mode` passes a native agy mode directly (`plan`, `accept-edits`, `auto`) and overrides the
profile. The record stores the exact flags that ran.

## 3a. Shell permissions (check once per machine)

Headless `agy` cannot prompt, so a tool call that needs approval is auto-denied and the turn
goes on without it; agy still reports success. A review that cannot run `grep`, `rg` or
`git diff` is weak, and an implementation that cannot run its tests is unverified. Check
once, before the first run on a machine:

```bash
cairn doctor                                                # "antigravity permissions": both rule lists
cairn hook install --harness antigravity --grant-commands   # write cairn's allow and deny sets
```

The grant writes `command(...)` allow rules (rg, grep, ls, cat, head, tail, find, echo,
`sed -n`, read-only `git` subcommands, pytest / ruff / mypy / `uv run` and `make` equivalents)
and a deny set (`supabase`, `psql`, `terraform`, `aws`, `gcloud`, `kubectl`, `curl`, `wget`,
`python`, `python3`, `git commit|push|checkout|stash|reset`, `read_file(.env*)`) to
`~/.gemini/antigravity-cli/settings.json`, keeps every existing rule, skips a deny the user
allows explicitly, and prints each rule it added. Tell the user what it wrote. A rule matches
word by word (`command(git log)` covers `git log -3`, not `git` alone), every segment of a
compound command (`a && b`, pipes) needs its own rule, and a deny rule beats an allow rule.

The engine enforces this per run:

- `--profile edit` or `--mode accept-edits` with no command rule is refused before the
  spawn: exit 2, `permissions check failed`, the `--grant-commands` line in the message.
  `--skip-permission-check` starts it anyway for a task that needs no shell.
- `review` (plan) and `auto` start and carry a `permissions:` warning instead.
- A round whose peer was refused calls and ended with an empty answer is `status: denied`
  (exit 2, `retryable: true`); `error` names each refused command and the rule it needs.
  A refusal beside a real answer stays `succeeded` with a `permissions:` warning that
  carries the same verdicts; `denied` in the result lists the tool and target of each.
- The result records `permissions` (the allow and deny rules in force when the round
  started). A resume after a rule change tells the peer what changed (section 7).

When a project needs a command outside the defaults (`poetry run pytest`, `make format`),
add it to `permissions.allow` by hand, never to `full`.

## 3b. Billing route (optional)

`agy` bills either the user's consumer plan quota or, through Application Default
Credentials (ADC), Vertex AI on a GCP project. The route is decided by the environment the
`agy` process starts with, so a headless run from an agent can silently bill the wrong pool.
cairn makes it explicit per run; read the current setting first:

```bash
cairn config show --json        # key invoke.antigravity.billing
cairn doctor                    # "antigravity billing" line: route, and for gcp whether ADC is ready
```

- `inherit` (default): the host environment is passed through unchanged.
- `plan`: `AGY_ADC_AUTH` is stripped; the run bills the consumer plan quota.
- `gcp`: `AGY_ADC_AUTH=true` (plus `GOOGLE_CLOUD_QUOTA_PROJECT` / `GOOGLE_CLOUD_LOCATION`
  when `project` / `location` are set). Before anything is spawned cairn checks that the ADC
  file exists and a quota project is known; otherwise exit 2 with the `gcloud` command to
  run. After the run cairn reads `agy`'s own log and records `billing.observed`
  (`auth_method`, `quota_project`, the projects the model requests went to). A mismatch is a
  `billing:` warning and `billing.problem` in the result; report it to the user.

Set it once for the machine, or override one round with `--billing`:

```bash
cairn config set invoke.antigravity.billing.route gcp
cairn config set invoke.antigravity.billing.project <gcp-project-id>
cairn invoke antigravity --billing plan --prompt "…"     # one round on the plan quota
```

Only change the route when the user asks; it is their money. The summary line
`billing: route=gcp project=<id> auth=adc quota_project=<id> verified` confirms a GCP run.

## 4. Run in the background

Start it the way the host reference describes (Claude: `Bash` with `run_in_background:
true`; Codex: `exec_command` with a long yield) and keep the handle:

```bash
cairn invoke antigravity --json --prompt-file <brief.md> [--model <slug>] [--effort <level>] \
  --profile review [--cd <repo>] [--timeout-s 1800]
```

`agy` chooses the conversation id and reports it in its first event; the engine prints it on
stderr as soon as it is known, with the run directory and the events path on the same line.
On Claude the caller is detected from `CLAUDE_CODE_SESSION_ID`, so `--parent` is redundant;
pass it only from a shell that has no host session. Nested runs are refused at depth 1
unless the user asks for `--allow-nested`.

## 5. Watch

```bash
cairn invoke tail <conversation_id>      # one line per event, until the final event
cairn invoke status <conversation_id>    # health line, progress bullets, answer so far
cairn invoke wait <conversation_id> --json   # block until the round exits; print its result line
```

`tail` replays and follows the round's event stream: `START <id> <model>`, `TOOL <name>
<args>`, `CMD <command>`, `WRITE <path>`, `DENIED <tool> <target>`, `MSG <text>`, and ends
with `RESULT <outcome>` (exit 0 on `succeeded`, else the round's exit code). On Claude it is
the whole `Monitor` command; no filter and no guess at the events file name. `wait` is the
one completion handle: it prints the same JSON line the runner prints and exits with its
code, so a second waiter loop is never needed.

`status` shows execution state, activity with its age, and the last event, then the progress
bullets (`run:`, `wrote:`, `denied:`, `tool:`, `message:`). `silent` beyond the stall
threshold means a probable hang: wait one more interval, then
`cairn invoke cancel <conversation_id>` and resume with a narrower task, or report. Silence
from the host task is never completion.

## 6. Read the result

One JSON line on the task's stdout. Lead with `last_message`. Then check `status`,
`outcome`, `exit_code`, `retryable`, `warnings` and `publication`. Check every claim in
`last_message` against `files_written` (the paths the peer's write tools finished, from the
event stream) and the diff: the peer's report can describe a design it abandoned. `denied`
lists every call agy refused. Evaluate critically per the doctrine. Exit codes: 0 succeeded;
2 self-check, depth, permissions or usage refusal, or a `denied` round; 75 thread busy;
124 wall-clock timeout; 125 idle timeout; 130 cancelled; otherwise the `agy` exit code.

## 7. Debate

```bash
cairn invoke antigravity --json --resume <conversation_id> --prompt "This is <host> (<model>) following up. Re: <finding>. <evidence>. Convince me otherwise or concede. End with one line: VERDICT: accept | reject | unresolved - <reason>."
```

Resume inherits the recorded cwd, profile, model and effort and continues the same `agy`
conversation. When the agy rules changed since the previous round, or that round had
refusals, cairn prepends a note to the prompt: the rules added and removed, and for each
refused command whether it is now allowed, still denied, or still unmatched. Nothing to
restate by hand. `agy` cannot fork a conversation, so `--fork` is refused. At most three
rounds per thread, then escalate to the user with the accepted, dismissed and unresolved
lists.

## 8. Close

Tell the user: the conversation id, the exchange document path and the Antigravity session
document path from the result, and how to continue:
`cairn invoke antigravity --resume <conversation_id> --prompt "…"` for another round, or
`cairn resume antigravity:<conversation_id>` to open it interactively. If `warnings` is empty
and `indexed` is true, `/search` finds the exchange document and `/recall` lists the peer
session now; if `indexed` is false, `rg` finds them at once and `/search` after
`cairn invoke publish <conversation_id>` or the next `cairn sync`.

## Errors

- Exit 2 with `permissions check failed` before any run: an `accept-edits` run with no
  command rule. Run `cairn hook install --harness antigravity --grant-commands`, then start
  again; `--skip-permission-check` only for a task that needs no shell.
- `status: denied` (exit 2, `retryable: true`): agy refused the calls in `denied` and the
  peer answered nothing. Add the rule `error` names to `permissions.allow` (or run the
  grant), then `--resume`; the peer is told what changed.
- `warnings` with `permissions:`: the run answered, but agy refused the calls listed, or no
  command rule exists at all. Read the verdicts; fix the rules before the next round.
- Exit 2 before any run: an unknown effort, `--fast` (Codex only), `--fork`, a nested run
  refused, or `billing check failed` (route `gcp` without ADC or a quota project: run the
  `gcloud` command in the message, or set `invoke.antigravity.billing.project`).
- `warnings` with `billing:`: the run finished but agy did not take the requested route
  (wrong auth method or project). Tell the user which credits were used; do not retry blind.
- Exit 1 with `invalid model selection` in `error`: pick a slug from `agy models`.
- Exit 124 or 125: raise `--timeout-s` or `--idle-timeout-s`, or narrow the task. Never retry
  blind; read `status` first.
- `retryable: false`: check `outcome` first. `cancelled` means someone stopped it; a login or
  model error needs the user. Report the `error` text.
- `warnings`: the answer is saved but a publishing step is pending. `cairn invoke publish
  <conversation_id>` finishes it; the next `cairn sync` also does.
- `status: busy` (exit 75): another round of this conversation is running. Wait or `status` it.
