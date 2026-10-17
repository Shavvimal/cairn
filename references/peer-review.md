# Peer review doctrine

Shared by `invoke-codex` and `invoke-claude`. The peer is the other agent: Codex when Claude
calls, Claude when Codex calls. The same rules apply when a host calls its own kind with a
fresh context.

## Why a different model

A peer from a different provider has different training and different blind spots. It
catches what the author's own re-read cannot. Rule of thumb: the host builds, the peer audits
what was built. A second opinion is worth paying for when a change is significant, when a
plan has a step that could fail silently, or when the host and the user disagree.

## Gate workflow

1. Stage the work: all changes saved, not yet committed, or a plan file written to disk.
2. Run the peer with profile `review` (read-only) and a targeted gate prompt from the table.
3. Read the result critically. Fix genuine issues. Dismiss false positives with written reasons.
4. On an ambiguous finding, resume the same thread and debate it. Identify yourself.
5. Report to the user in three lists: accepted (with the fix), dismissed (with the reason),
   unresolved (both positions).

## Gate prompts

| Gate | Prompt |
|---|---|
| Uncommitted diff | `Review the uncommitted changes in this repository (run git diff). Identify: (1) logic errors or edge cases, (2) security issues, (3) violations of existing patterns in the codebase, (4) missing test coverage. Be adversarial: assume the author missed something. Give file:line for every finding.` |
| Branch diff | `Run git diff against main. For each hunk: is it correct, is it safe, does it match the surrounding code's conventions? List concrete defects with file:line.` |
| Plan review | `Read <plan file>. Find the step most likely to fail in practice and explain why. Then find the step that is missing. Then find the assumption that, if wrong, invalidates the most work.` |
| Test simulation | `Run the test suite for the changed modules. Report failures verbatim, then propose the minimal fix for each.` Needs profile `edit` when tests write files. |
| Migration | `Review the migration at <file>. Check idempotency guards, row-level security, tenant scoping, rollback safety.` |
| API surface | `Review the router or CLI changes. Check auth on every route, scoping on every data access, schema validation, error handling, and that every new flag is documented.` |

Add this line to every gate prompt so the reply is machine readable:
`End with one line: VERDICT: accept | reject | unresolved - <reason>.`

Write long briefs to a file and pass `--prompt-file`. A prompt is a brief, not a chat line:
state the repository, the files to read first, the exact question, what the peer may and may
not touch, and the output shape.

## Critical evaluation of the peer's output

Treat the peer as a colleague, not an authority.

- Trust your own knowledge when you are confident. If the peer claims something you know is
  wrong, push back with evidence.
- Research disagreements with documentation or a web search before accepting a claim.
- Remember knowledge cutoffs. The peer may not know recent releases or API changes, and it
  may "correct" a true recent fact from memory. Do not defer on model names, library versions
  or evolved best practice.
- A finding without a file and line, a reproduction, or a concrete failure scenario is a
  hypothesis. Verify it before acting on it.

## Follow-ups and identity

Continue the same thread with `--resume <thread_id>`. Open every follow-up with
`This is <host> (<your model name>) following up.` so the peer knows it is a peer discussion,
then restate the finding, give your evidence, and ask it to convince you or concede. Frame it
as a discussion. Either side can be wrong.

## Convergence loop

- At most three rounds per thread. Each round's prompt ends with the `VERDICT:` line request.
- Parse the last `VERDICT:` line of the reply. Missing line: ask once more for it, then treat
  the round as `unresolved`.
- Stall: the same verdict with no new evidence two rounds running. Stop and escalate.
- Escalate to the user with the accepted, dismissed and unresolved lists and both positions on
  each unresolved item. The user decides.
- Resume cost grows with the thread length. After three rounds, or when the remaining
  disagreement is narrow, start a fresh thread with a written brief instead of a fourth round.

## Budgets and failure

- Reviews of plans or architecture need `--timeout-s` of at least 900. High effort runs take
  10 to 20 minutes.
- Never retry a timeout blind. Read `cairn invoke status <thread_id>` first: a `silent` run may
  be hung, a `quiet` run may be thinking. Raise the timeout, narrow the task, or cancel.
- `retryable: false` with outcome `cancelled` means someone stopped the run. Any other
  `retryable: false` is a provider or login error. Report the `error` text; do not retry.
- `warnings` in the result mean the answer is saved but a publishing step is pending. Run
  `cairn invoke publish <thread_id>` when the lock is free, or let the next `cairn sync` do it.
