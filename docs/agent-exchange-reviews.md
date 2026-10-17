# Adversarial review log for the peer-agent and subagent visibility PR

Each phase of this PR was reviewed by Codex (`gpt-6-astra`, effort high, read-only) through
the standalone `invoke-codex` wrapper, with `docs/agent-exchange-plan.md` and
`docs/agent-exchange-research.md` as the brief. This file records every finding and what
was done about it, so a later reviewer can see the trail instead of re-deriving it.

## Phase 1: shared plumbing, subagent exports, invoke foundations

Reviewed range: `111881e..c5d974c`. Duration 403 s. Verdict: not merge-ready.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | blocker | `claude -p` argv ended with `--disallowedTools Edit,Write,NotebookEdit "<prompt>"`; the flag is variadic and swallowed the prompt. Reproduced live: "Input must be provided either through stdin or as a prompt argument". | Fixed. `Claude.exec_argv` emits `--` before the positional prompt (verified live with haiku; both `--` and prompt-after-`-p` work, `--` also guards variadic `--extra` flags). Regression test added. |
| 2 | blocker | A multiline Claude subagent `description` became a multiline `title:` and the exported file failed `parse_frontmatter`; `export_one` wrote before validating. | Fixed in two places. `frontmatter.title_line` collapses any whitespace run or line break to one space. `ConversationSource.export_one` parses the rendered frontmatter and checks the id before `atomic_write`; a document the catalog could not read is never written (error on stderr, `None` returned). Tests added. |
| 3 | should-fix | Targeted Codex export of a child or parent rewrote nested documents with incomplete `## Subagents` indexes (P → {A, B}, B → G: targeting A rewrote B without G). | Fixed by the Codex stream: candidate discovery includes ancestors and the full transitive descendants of every document that will be rewritten. Regression test for nested targeted exports. |
| 4 | should-fix | Two Codex children with colliding short ids got separate files through the collision suffix, but the parent index linked both to the planned path, and the revision excluded link paths so later exports could not repair it. | Fixed by the Codex stream: collision-safe destinations are resolved before the index is rendered and the destinations are part of the parent revision. Regression test with two colliding ids. |
| 5 | should-fix | One malformed Claude child `meta.json` blocked the parent and every healthy sibling because `subagent_index` read all metas before the per-child handling. | Fixed by the Claude stream: index discovery errors are collected per child, healthy documents export, and the aggregate failure is reported afterwards. Regression test. |
| 6 | should-fix | Codex `export_target` discarded the per-file failure flag: an unreadable child vanished from the parent index while the call succeeded, and the hook worker never saw it. | Fixed by the Codex stream: readable documents export, then an aggregate `ValueError` names the failed family members so the durable hook job retries. |
| 7 | should-fix | The replay cut locked into UUIDv7 mode after seeing an ancestor's turn id and ignored an exact `thread_settings_applied(thread_id=child)` marker followed by a legacy-format child turn. | Fixed by the Codex stream: an exact child marker is always authoritative; the first signal that resolves wins. Regression test with mixed ids. |
| 8 | should-fix | The Claude parent revision hashed only some index fields; changing `requestShape` or `workflow_run` changed the rendered row without re-rendering the parent. | Fixed by the Claude stream: the revision hashes one normalised representation of every rendered index row, including the path. Regression test. |
| 9 | should-fix | A hand-quoted `links:` entry (`- "peer claude:x"`) parsed with its quotes, so `add_link` appended a second, bare copy of the same relationship. | Fixed. `add_link` normalises existing entries to the bare form before comparing and re-emits them bare. Regression test. |
| 10 | should-fix | `_parse_invoke(data.get("invoke") or {})` let `false`, `[]`, `""` and `0` silently become defaults. | Fixed. `None` and absent mean defaults; any other non-object raises `ConfigError`. Regression test. |
| 11 | nit | `ruff format --check` failed on `registry.py` (the Makefile's `check` target runs lint, not format). | Fixed. Tree formatted; `ruff format --check` is part of the acceptance run. |

Found sound by the reviewer: first-meta identity keeps forked children out of their parent;
registry, store and adapter id filters include children while plain listings and the
active-session fallback keep the exact parent type; both resume entry points refuse children;
export, document and index lock ordering; Codex resume flags against the installed help;
item-level Codex errors as notices; lineage cache invalidation; custom tool output; unquoted
link round trips; schema 1 records go stale under schema 2.

Coverage the reviewer asked for and this PR adds: nested targeted exports, short-id
collisions, multiline descriptions, partial family failures, mixed replay ids, quoted links,
and the pre-write frontmatter validation.

## Phase 2: runner, exchange documents, health, agents, skills

Reviewed range: `b8b0e05..a2ae4d8`. Duration 360 s. Verdict: not merge-ready.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | blocker | `finished_at` was saved before publishing with an empty pending list, so a runner killed between steps left unattempted steps with neither a timestamp nor a pending entry; `publish` reported nothing pending. | Fixed: every applicable step is recorded as pending before the round is marked finished; `publish` and `sync` derive work from the ledger. Test kills between steps. |
| 2 | blocker | A failed export with a successful exchange write and index left the new document unindexed and the exchange stale on retry; a failed caller export permanently anchored the exchange in the peer folder. | Fixed: steps carry dependencies (export → links → exchange → index); completing an earlier step on retry invalidates and redoes the later ones; the anchor is not fixed to the peer folder while a known caller's export is pending. |
| 3 | blocker | Two threads sharing a short id mapped to the same exchange file under one caller and the writer never checked the existing `thread_id`, so one thread's rounds overwrote the other's. | Fixed: collision-safe destinations (sha256 suffix when the file holds another thread) and a refusal to overwrite a foreign document. |
| 4 | should-fix | Between the pending-directory rename and the thread lock, a concurrent `publish` could take the lock; the runner's acquire then failed and its cleanup killed the peer. | Fixed: the destination lock is held before the directory is exposed, both locks span the transition. |
| 5 | should-fix | The peer wrote stdout to a pipe the runner owned, so a dead runner cost the rest of the stream (EPIPE) and an adopted orphan could only be `aborted`; cancellation stopped pumping before draining. | Fixed: the peer writes stdout straight to the durable events file and the runner tails it; a successful orphan is adopted with its answer, and cancellation keeps emitted output. |
| 6 | should-fix | The transcript heartbeat was polled every 10 s regardless of the idle timeout, so a run whose only heartbeat was the rollout could be killed as idle. | Fixed: the transcript is refreshed before idle is declared and polled relative to the timeout. |
| 7 | should-fix | A peer that closed stdout but stayed alive dropped the loop into a plain wait that ignored cancellation and idle. | Fixed: the watchdog loop keeps running after EOF until the process exits. |
| 8 | should-fix | Zombies passed `kill(pid, 0)` with their start token; the Linux token truncated start ticks to seconds. | Fixed: zombie and dead states count as not alive; the Linux token keeps raw ticks plus the boot identity. |
| 9 | should-fix | Codex subagent classification relied on an assistant `final_answer`, so a rollout ending in `task_complete` or `turn_aborted` stayed running forever in `agents wait`. | Fixed: the latest turn's terminal event after the replay boundary decides the outcome. |
| 10 | should-fix | The skills promised the thread id on the first stderr line; the committed runner never printed it. | Fixed: the runner announces `cairn invoke: <peer> thread <id> round N` when the id is known and on adoption; tested as an early-output contract. |
| 11 | should-fix | The environment scrub removed two exact `PLUGIN_ROOT` names, not the promised wildcard. | Fixed: every key ending in `PLUGIN_ROOT` is removed. |
| 12 | should-fix | `status --follow` tracked the capped progress list length, so output stopped after 200 entries. | Fixed: an absolute cursor that counts dropped entries. |
| 13 | nit | The busy and orphan tests relied on fixed sleeps. | Fixed: explicit readiness and release handshakes. |
| 14 | (live) | A `--resume` without `--parent` and without a host hint warned "caller unknown" although round 1 recorded the caller. | Fixed: resume defaults to the thread's recorded caller. |

Found sound: three phase 1 fixes confirmed landed (`--` before the prompt, flattened titles with
pre-write validation, quoted-link normalisation); pump buffering across split UTF-8 and a final
line without a newline; large-prompt stdin writes and close; resumed-thread locking, recorded cwd
and sandbox re-assertion; exchange head validation (`run_status`, no tail-key or identity keys,
growing fences); link targets and dedupe; the Claude subagent classification order; the Claude
argv; the exit-code semantics of `status` versus `agents wait`; the skills' host-agnostic form.

## Phase 3: verification pass over the whole branch

Reviewed range: `1443f89..81d8799`. Duration 421 s. Verdict: not merge-ready.

Of the 24 earlier findings the reviewer confirmed 19 as landed with tests and marked five
partial: phase 1 #2 (flattening and validation landed, no child-export regression), phase 2
#2 (moved exchange kept the old anchor in its head), #4 (the rename test did not exercise the
transition itself), #5 (no tailer split-write or cancellation-output regression), #9 (no
fixture with a replayed parent). New findings:

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | should-fix | For a Codex child with an unresolved replay, `agents` fell back to `finished`, which can come from the replayed parent's final answer, so `wait` could report success without evidence. | Fixed: unresolved replay classifies as unknown or running, never succeeded. Regression with a parent final answer and no child boundary. |
| 2 | should-fix | Resumed Codex fragments produced one agent row per file, so one child appeared twice and `agents wait <id>` called the full id ambiguous. | Fixed: fragments are grouped by thread id and the newest fragment's terminal state decides. |
| 3 | should-fix | `agents list --all` kept the implicitly detected host session as a filter and kept exited rows. | Fixed: `--all` bypasses implicit scoping (an explicit `--session` still applies) and shows non-exited rows only. |
| 4 | should-fix | Between the pending-directory rename and the symlink, the `-o` answer path Codex was given did not exist. | Fixed: the answer file lives at a stable path outside the renamed directory and is moved into the round after the peer exits. |
| 5 | should-fix | The exchange document was rendered before `anchor_session` was updated, so a moved document recorded the previous anchor and `publish` never repaired it. | Fixed: the anchor is resolved on the state before rendering; the move test asserts the document head and the ledger. |
| 6 | nit | A reader that stat-ed a provisional exchange document before it moved recorded a spurious catalog error. | Fixed: `Catalog._parse` treats `FileNotFoundError` as a vanished entry, silently. Regression test. |

User-facing corrections applied: the README's one-line-JSON example now passes `--json`; the
skills and changelog say `/search` and `rg` find exchange documents while `/recall` lists the
peer session (exchange documents stay out of the session timeline by design); the status
description names the identity line before the health line; continuation commands include
`--prompt`; `retryable: false` is read after `outcome` because cancellation also sets it;
`--prompt -` already read stdin as the help says and now has a regression test; the G10 wording says "no required Claude-only
runtime syntax" since `allowed-tools` metadata is Claude-only; the plan copy marks G2, G13 and
G14 as code-complete with live acceptance steps left to the user, qualifies G8 with the
documented Codex exception, and no longer points at a `docs/agent-exchange.md` that was never
written. `make check` now includes `ruff format --check`.

The partial dispositions gained their regressions: a multiline child description exports a
valid document; `export_one` refuses an unparseable or mis-identified render and leaves the
existing file untouched; the rename transition is exercised with a barrier; the tailer is
tested across a split multi-byte write, a final line without a newline, temporary EOF with a
live peer, and output emitted during cancellation; the Codex terminal-outcome fixtures include
a replayed parent whose final answer is ignored once the boundary resolves.

## Phase 4: sync and embedding reliability

Reviewed commit `ae28d78`, the validated parts of `docs/qmd-reliability-pr.md` and
`docs/sync-robustness-plan.md`. Duration 275 s. Verdict: not merge-ready. The reviewer
confirmed the log evidence (the 83,072 of 131,291 false success, the interleaved 17:04 and
17:07 runs), the rejections of the pre-flight probe and the 2.4.1 bump, the lock order, the
exit codes and output capture of `run_capped`, the failure wordings, and the fixture changes.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | should-fix | A `--cron` sync skipped because a `--hook` sync held the lock exited 0 although hook mode retries no failed requests, exports no other sources and embeds nothing; a failed request arriving after a full sync's first worker pass waited for another invocation. | Fixed: a skipped sync writes a durable request marker for its mode; before releasing the lock the holder services the widest request once: a wider one gets the stages it has not run (worker retry, remaining exports, update, embed, verify), a same-mode or narrower one gets one more worker retry pass so a request that failed after the first pass does not wait an hour. One follow-up pass per run, so it cannot loop. Mixed-mode and late-request regressions. |
| 2 | should-fix | A clean embed followed by positive pending returned 0 and `=== done ===`. The whitespace-only justification did not hold: QMD 2.5.3 skips such files before indexing. | Fixed: `INCOMPLETE` exits nonzero with its distinct message; the Validation row, changelog and README corrected. |
| 3 | should-fix | When a descendant escaped the group and kept the pipes, the second `communicate` timed out and `run_capped` raised with empty output; sync's timeout handlers omitted stderr. | Fixed: the first timeout's partial output is kept and replaced by the second's when available; both stream tails are logged. |
| 4 | should-fix | `Pending: unknown …` and `Pending: 8 documents need embedding` both parsed as zero pending, so an unfamiliar status format passed strict verification. | Fixed: an absent `Pending:` line is zero, a present but unparseable one is unknown, which fails strict doctor and sync verification. |
| 5 | should-fix | Recovery text promised that each run keeps its vectors; QMD 2.5.3 removes the vectors of a partially embedded document after a batch, so a document that never fits the 30-minute session can lose progress repeatedly. | Fixed: the docs say completed documents keep their vectors, partial ones may restart, and repeated runs without a falling pending count need investigation. |
| 6 | should-fix | `kill_group` bounded its own waits but leaving the `Popen` context waited without a bound, so a child stuck in kernel I/O could hold the sync locks past the advertised cleanup. | Fixed: pipes are closed and the child reaped with a bounded wait; an unreaped child is surfaced as incomplete cleanup. |
| 7 | nit | `[qmd-embed] ok` was logged before verification, so an unreadable status produced `ok` then `[qmd-verify] FAILED`. | Fixed: the embed line reports the subprocess exit and the verdict lives in `[qmd-verify]`. |
| 8 | nit | The process-tree timeout test started its clock before the grandchild existed and treated a zombie as alive. | Fixed: readiness handshake and a state check. |

## Live acceptance on the author's machine (2026-10-17)

- **G7** `cairn codex export --all` on the real store: 610 rollouts in 7 s, 107 `codex-subagent`
  documents with lineage heads, parents with `## Subagents`.
- **G6** The plantedex session `c4c36add` family: 102 children (775 MB of JSONL) exported in
  1.9 s into `plantedex/2026-10-15-c4c36add/`, index and one peer `### Agent` turn on the parent.
- **G1, G3, G4, G5, G13** From this Claude session as caller: a low-effort Codex round of 42 s
  (thread `01a14aa2-…`), `cairn invoke status` mid-run showed `running, active for 7s` with the
  commands Codex was executing, `cairn agents list` showed the live peer run beside fourteen
  subagents with outcomes, both session documents gained `links:`, the exchange document landed in
  `claude-code-sessions/cairn/2026-10-16-1572cb71/`, the ledger recorded `indexed` as pending
  because the hourly cron sync held the QMD lock, and `--resume` added round 2 to the same
  rollout and the same exchange document (`rounds: 2`).
- **G2** From this Claude host: a haiku `cairn invoke claude` round of 8 s succeeded with the
  review profile's native flags recorded (`--permission-mode plan --disallowedTools …`), the peer
  session exported and linked, and the exchange document in the caller's folder. The Codex-host
  direction with escalated permissions remains to be exercised by the user.
- **G9** `cairn claude list` and `codex list` show parents only; children resolve by id.
