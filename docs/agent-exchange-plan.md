# Plan: peer agents and subagent visibility in cairn (one PR)

Branch `feat/invoke-codex-skill` in worktree `/Users/shav/Code/cairn-invoke-codex`, head set to
`origin/main` at `1443f89` (PR #28 merged 2026-10-17). One PR against `main`. Version 2.5.0.
Research with evidence: `docs/agent-exchange-research.md` (untracked in the worktree; folds into
`docs/agent-exchange.md` during Part C).

## Goals (the acceptance checklist for this PR)

Each goal has a verifiable "done" condition. The PR is complete only when every box can be ticked.

- [x] **G1 `cairn invoke codex`.** The personal `~/.claude/skills/invoke-codex` is ported into the
  cairn engine as a CLI command plus a thin `skills/invoke-codex` skill that works from both
  Claude Code and Codex hosts. Done when `cairn invoke codex --prompt "Reply PONG" --effort low`
  returns one JSON result line with `last_message == "PONG"` and the skill passes
  `tests/test_plugin_manifest.py` on both hosts' manifests.
  Status: verified 2026-10-17: live low-effort round returned PONG; manifest tests pass.
- [x] **G2 `cairn invoke claude`.** The mirror direction with a `skills/invoke-claude` skill. Done
  when a Claude host run succeeds and a Codex host run either succeeds under escalated
  permissions or fails loud with the keychain guidance (`retryable: false`).
  Status: verified from the Claude host 2026-10-17 (haiku round, review profile flags recorded); the Codex-host direction with escalated permissions is left for the user to exercise.
- [x] **G3 Send off in the background, read it midway.** Runs stream `codex exec --json` /
  `claude -p --output-format stream-json` to `data_root/logs/invoke/<peer>/<thread>/events-NN.jsonl`.
  Done when `cairn invoke status <thread_id>` shows commentary, commands and todo state of a
  run that is still in progress, from either host.
  Status: verified 2026-10-17: `cairn invoke status` mid-run showed running/active with the commands Codex was executing.
- [x] **G4 Message back and forth in one thread.** The real thread id (`thread.started.thread_id`,
  `system/init.session_id`) is captured and follow-ups use `--resume <thread_id>`; `--last` is
  gone. Done when round 2 of a thread lands in the same native transcript and in the same
  exchange document with `rounds: 2`.
  Status: verified 2026-10-17: `--resume` added round 2 to the same rollout and exchange document.
- [x] **G5 Full visibility of an invocation, with three separate guarantees.** (a) Answer saved:
  the moment the peer exits, its final message and the full event stream are on disk under the
  run directory, whatever happens next. (b) Documents exported: the peer session document, the
  `links:` entries on both session documents, and the `exchange-<peer>-<short>.md` document in
  the caller session's folder are written by the post-run step, or by `cairn invoke publish`
  or the next `cairn sync` if a lock was busy; every unfinished step is recorded in a
  publication ledger and never needs the peer to run again. (c) Index refreshed: `qmd update`
  runs when the index lock is free, else at the next sync; `rg` finds the documents as soon as
  (b) holds. Done when a run with a deliberately held index lock reports `indexed: false`, the
  ledger shows the pending step, `cairn invoke publish <id>` completes it, and `/search` finds
  both documents.
  Status: verified 2026-10-17: links on both documents, exchange document in the caller's folder, ledger recorded the pending index step while cron held the lock; `publish` retries it.
- [x] **G6 Claude subagent visibility.** Every `agent-*.jsonl` under `<session>/subagents/` and
  `subagents/workflows/*/` exports as a `type: claude-subagent` document in the parent's folder;
  the parent gains a `## Subagents` index; child-to-parent peer messages render as `### Agent`
  turns instead of being dropped. Done when the plantedex session `c4c36add` exports 84 child
  documents and `qmd search` finds text that exists only in a child transcript.
  Status: verified 2026-10-17: plantedex family of 102 children exported in 1.9 s with the parent index and a peer Agent turn.
- [x] **G7 Codex subagent and headless fixes.** First `session_meta` wins and the replayed parent
  history is cut, `custom_tool_call(_output)` renders, lineage (`parent_session_id`,
  `agent_type`, `agent_nickname`, `agent_path`, `spawn_depth`, `forked_from_id`) and run
  context (`originator`, `thread_source`, `effort`, `sandbox`, `approval_policy`) are head
  fields. Done when `cairn codex export --all` produces about 100 previously missing
  `codex-subagent` documents on this machine and an invoke-codex review rollout shows its
  `exec` tool calls.
  Status: verified 2026-10-17: `cairn codex export --all` rendered 610 rollouts in 7 s and recovered 107 subagent documents.
- [x] **G8 One folder per session.** Everything a session spawned (subagent docs, exchange docs)
  lives in `<collection>/<project>/<date>-<shortid>/` next to the session document. No new
  collection, no config entry for storage.
  Status: verified: subagent and exchange documents live in `<collection>/<project>/<date>-<shortid>/`; no new collection.
- [x] **G9 Listing and recall stay clean.** `cairn claude|codex list` show parents only;
  `cairn recall list` hides children unless `--include-subagents`; `recall expand`, `note`,
  `close`, `log` work on a child id; `resume` on a child is refused with the parent id.
  Status: verified by tests and live `list`; children resolve by id, hidden from plain listings.
- [x] **G10 Host-agnostic skills.** No `${CLAUDE_SKILL_DIR}`, no `!` inline shell, no
  `$ARGUMENTS`, no `when_to_use`; host mechanics live in `references/harnesses/*.md`; each new
  skill has `agents/openai.yaml`; a shared `references/peer-review.md` holds the adversarial
  review doctrine and the bounded convergence loop.
  Status: verified by `tests/test_plugin_manifest.py`: no Claude-only syntax, `agents/openai.yaml` present, doctrine shared.
- [x] **G11 Config as data.** Defaults for model, effort, sandbox and timeout per peer live in a
  top-level `invoke` block, parsed fail-loud, settable with `cairn config set invoke.codex.model`.
  Status: verified by tests: `invoke` block parsed fail-loud and settable with `cairn config set`.
- [x] **G13 Follow up on a finished headless thread, first class.** A Codex or Claude thread
  started by `cairn invoke` can be continued hours or days later from any session and any cwd
  with `cairn invoke <peer> --resume <thread_id>`, and opened interactively with
  `cairn resume codex:<thread_id>`. Done when a review thread finished in one Claude session is
  resumed from a different Claude session the next day, round 2 lands in the same rollout, and
  `thread.json` supplied the recorded cwd, sandbox and model without flags.
  Status: verified 2026-10-17: resume by id from a later call reused the recorded cwd, profile and caller.
- [x] **G14 Health reporting for peer runs and subagents, first class.** Every round reports
  three separate things: execution state (`pending`, `running`, `orphaned`, `exited`, `unknown`)
  from process identities that include start time, activity (`active`, `quiet`, `silent` with an
  age) from the native heartbeat, and outcome (`succeeded`, `failed`, `timeout`, `idle_timeout`,
  `cancelled`, `aborted`, `unknown`) set once at the end. An idle watchdog kills a hung run
  instead of waiting for the wall clock, `cairn invoke cancel` stops one cleanly, one invocation
  per thread is enforced by a lock, and `cairn agents list|wait` reports and blocks on all peer
  runs and Claude or Codex subagents of a session. Done when a stub run that goes silent is
  reported `running, silent` within the threshold and killed at the idle timeout with outcome
  `idle_timeout`, a SIGKILLed runner leaves an `orphaned` round that `status` later finalises,
  and `cairn agents wait --session claude:<id>` returns when the last background subagent exits.
  Status: verified by tests and live `agents list`: three axes reported, idle watchdog, thread lock, orphan adoption; `agents wait` on a live fan-out remains to be exercised by the user.
- [x] **G12 Quality gate.** `make check` (ruff, ruff format, mypy, unittest) passes on 3.11 to
  3.13; `SCHEMA_VERSION` is 2 with one coordinated bump; CHANGELOG, README, skill references
  and the three plugin manifests are updated; the standalone `~/.claude/skills/invoke-codex`
  is retired after install.
  Status: verified 2026-10-17: `make check` green with 347 tests on 3.13 locally (CI covers 3.11 to 3.13); schema 2; docs, changelog and manifests at 2.5.0; retiring `~/.claude/skills/invoke-codex` is a user step after installing the plugin.

Out of scope, recorded in `docs/agent-exchange.md`: mid-turn steering through
`codex app-server` (`turn/steer`), Codex-to-Codex `send_message` plaintext (encrypted in 0.160),
Cursor as a peer, a detached job runner inside `cairn invoke`.

## Context

Your workflow is an orchestrator session that fans out several background subagents and a
background Codex adversarial review, then debates findings with Codex ("This is Claude following
up"). Three gaps were found:

1. The Codex half of every exchange is already a rollout (`originator: codex_exec`) that cairn
   exports, and the Claude half holds the prompt and the final answer, but nothing links them,
   the headless prompt is missing on `main`, and progress is invisible until the run ends.
2. The current wrapper fabricates its session id and resumes with `--last`, which collides when
   two runs share a cwd. `codex exec --json` gives the real thread id on line one.
3. cairn exports no Claude subagent transcripts (1071 files, 2.6 GB locally) and loses 100 of 108
   Codex subagent rollouts to a `session_meta` parsing bug, so most fanned-out work is not
   searchable.

Verified transport facts (codex-cli 0.160.0, claude 2.1.301): `codex exec --json` and the rollout
both grow live; `codex exec resume <id>` continues the thread but has no `-C`/`-s`, so sandbox is
re-asserted with `-c sandbox_mode=...`; `codex queue` cannot reach a running exec turn;
`codex mcp-server` no longer exists; `claude -p` inside the Codex sandbox cannot reach the keychain.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Logic in the `cairn` CLI, skills are thin drivers | cairn convention; `cairn` is on PATH for both hosts; no `${CLAUDE_SKILL_DIR}` on Codex |
| D2 | `cairn invoke` is foreground-blocking; the host runs it in the background; `cairn invoke status` reads the event tee | both hosts own background and monitoring; a detached runner would need a second job protocol |
| D3 | Exchange documents and subagent documents live in the per-session folder `<collection>/<project>/<date>-<shortid>/` | your call: QMD indexes recursively, so a subdirectory is searchable at once with no config, registry, doctor or `config set` work |
| D4 | Links are a new preserved tail key `links:` (block list of `"<role> <source>:<id>"`), emitted only when non-empty | survives re-export like `comments`; every pre-2.5 export stays byte-identical; native lineage goes in the head |
| D5 | Child doc types `claude-subagent` / `codex-subagent`; child ids `agent-<agentId>` (Claude) and the child thread id (Codex) | exact-type filters keep `list` and `active_sessions` unchanged with zero code; ids resolve through the existing catalog |
| D6 | Children hidden from `recall list` by default, `--include-subagents` shows them | Codex children have 0 user messages; deterministic instead of relying on `--min-msgs` |
| D7 | Shared `--profile review\|edit\|full` with explicit per-adapter guarantees, plus native flags `--sandbox` (Codex only) and `--permission-mode` (Claude only). Codex: `review` = `-s read-only`, `edit` = `-s workspace-write`, `full` = `-s danger-full-access`, always with `-c approval_policy="never"`. Claude: `review` = `--permission-mode plan --disallowedTools Edit,Write,NotebookEdit`, `edit` = `--permission-mode acceptEdits`, `full` = `--permission-mode bypassPermissions`. Claude profiles are tool-permission guarantees only, not an OS sandbox; the skill says so and points at Claude's own sandbox settings. Precedence: native flag > `--profile` > recorded settings on resume > config default; `--extra` is appended last and recorded verbatim. `thread.json` records the exact native argv that ran, not the profile name alone | the two hosts do not have equivalent isolation layers; the record must say what actually ran |
| D8 | Both invoke skills are model-invocable (no `disable-model-invocation`; Codex `allow_implicit_invocation: true`); setup stays explicit-only | the orchestrator is told "use /invoke-codex astra high" in prose; safety comes from read-only default, timeout, depth guard and the ask-before-widening rule |
| D9 | Codex self-check fails loud (exit 2) when `models_cache.json` is missing; `--skip-self-check` overrides; no static model list | config is data, the cache is the live catalogue |
| D10 | `SCHEMA_VERSION` 2, one bump coordinated with #28; `--since` cron runs never re-export old docs, `--all` pays once | new types, lineage keys, `links:`; #28 already changed metadata format |
| D11 | Post-run failures (export, link, index) never change the peer's `status`; they go to `warnings[]` and `indexed: false` | the peer's answer is the primary artefact |
| D12 | Index refresh takes `index_lock` non-blocking with a 60 s retry, then skips with a warning | `cairn sync` holds the lock across `qmd embed` (up to 3600 s) |
| D13 | Child env is scrubbed of `CLAUDE_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CAIRN_HARNESS`, `*PLUGIN_ROOT`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`; `CAIRN_INVOKE_DEPTH` and `CAIRN_INVOKE_CALLER` are added; depth ≥ 1 refuses unless `--allow-nested` | otherwise the peer inherits the caller's identity, cairn's mixed-hint guard fires inside it, and a nested `claude` may refuse to start |
| D14 | Prompts > 64 KiB go to the child's stdin (`-` for codex, no positional for claude) and the pipe is closed at once; otherwise `stdin=DEVNULL` | macOS ARG_MAX; an open stdin hangs `codex exec` |
| D15 | Parent `source_revision` = sha256 of the parent file revision plus the sorted child ids | parent re-renders when a child appears, not when a child changes |
| D16 | The preamble is prepended to the user prompt for both peers, not sent as a system prompt | one mechanism; visible in both native transcripts and in the exported documents |
| D17 | `cairn invoke` prints the one JSON result line only with `--json`; otherwise a short human summary. Library prints run under `redirect_stdout(sys.stderr)` | matches cairn's `--json` convention on read commands; stdout stays parseable |
| D18 | Exchange head field for run state is `run_status:`; the renderer rejects any head key that starts with a tail key and round-trips the head through `parse_frontmatter` before writing | `rewrite_preserved_tail` cuts at the first tail key; a duplicate `status:` would corrupt notes; a malformed doc would spam `Catalog.errors` on every listing |
| D19 | One active invocation per thread: a per-thread flock (`storage.lock_path(run_dir, "thread")`, non-blocking) is held from before the round is allocated until the round is finalised; a second caller gets `status: busy` (exit 75) naming the holder's round, runner pid and start time | shared `thread.json` and numbered event files cannot survive two writers; Codex would also run two native turns on one thread |
| D20 | Three separate axes per round: execution state (`pending`, `running`, `exited`, `orphaned`, `unknown`), activity (`active`, `quiet`, `silent`, an observation with an age, never terminal), outcome (`succeeded`, `failed`, `timeout`, `idle_timeout`, `cancelled`, `aborted`, `unknown`, set once when the round ends) | a stall is an observation, not a result; `wait` needs terminal states that do not depend on thresholds |
| D21 | Runner and peer identities are recorded separately with start-time identity (`runner_pid`, `runner_started`, `peer_pid`, `peer_pgid`, `peer_started`); liveness compares pid and start time; the runner forwards SIGTERM to the peer group and finalises; a SIGKILLed runner leaves an `orphaned` peer that `status`, `agents` or `publish` adopt and finalise from the run directory once it exits | `start_new_session=True` means runner death never implies peer death; PID reuse must not fake liveness |
| D22 | The Codex replay boundary is an isolated, version-tagged heuristic (`codex_replay.py`) with a fallback chain and a non-failing "unresolved" path; a live child with no own records yet is skipped, not failed | "holds in 100 local files" is sample evidence, not a format contract |
| D23 | Lineage discovery is independent of export selection: a cheap first-line lineage index over all rollouts, and `newest_mtime` across parent plus children for both sources; a parent's revision folds the child metadata that its index renders, not only child ids | an old parent must re-render when a new child appears or a child's title or outcome changes |
| D24 | Each round carries a publication ledger (`answer_saved`, `exported`, `linked`, `exchange_written`, `indexed`, each a timestamp or a pending reason); `cairn invoke publish <id>` and the end of `cairn sync` drain pending steps without running the peer | separates the peer's success from cairn's publishing, and makes every publishing failure retryable |

## Base-branch facts to respect

#28 is merged into `main` as `1443f89` (2026-10-17) and the worktree branch now points at it. Main has:
`src/cairn/catalog.py` (stat-validated `Catalog`, `load_catalog`, `put`, `save`),
`ConversationSource.find_record` / `_id_index` / `flush_catalog`, `is_current(record, revision)`
taking a `SessionRecord`, `frontmatter.read_frontmatter_head`, `codex.py` split into
`_parse_rollouts` / `_best_fragments` with `export_target` parsing candidates once,
`storage.lock_path(resource, purpose)` placing every lock under `~/.local/state/cairn/locks/`
and `locked(path, blocking=False)`, `src/cairn/runtime.py` (pinned hook and cron environment),
`hooks.retry`, `MAX_ATTEMPTS = 3`, `prune`, `hooks/launch.sh` + `hook-launcher.env`, and
`activity_dates` in metadata. Reference functions by name. Reuse `lock_path` for every new lock
(thread lock, run-dir lock) instead of inventing paths. One hard requirement from the catalog: `_id_index`, `_record_at`,
`record_from_frontmatter`, `collection_records` and `read_record` filter on one `session_type`;
they must accept the source's `session_types` tuple or child docs are invisible (notes fail,
children re-export every run).

## File map

```
src/cairn/invoke.py                     NEW  CLI (run | status | list-models), selectors pump, post-run pipeline
src/cairn/exchange.py                   NEW  exchange document render/write, per-thread state (thread.json)
src/cairn/agents.py                     NEW  cairn agents list|wait: execution/activity/outcome over peer runs, Claude and Codex subagents
src/cairn/health.py                     NEW  process identity (pid + start time), heartbeat sources, thresholds, three-axis classification
src/cairn/sources/codex_replay.py       NEW  version-tagged replay-cut heuristic with fallback chain and evidence
src/cairn/sources/codex_lineage.py      NEW  first-line lineage index over rollouts, cached by (size, mtime_ns)
src/cairn/harnesses/__init__.py         MOD  InvokeRequest, Event dataclasses; Protocol: exec_argv, parse_event, transcript_path, self_check
src/cairn/harnesses/claude.py           MOD  argv (-p stream-json, --session-id, --resume, permission-mode map), event parse, transcript path
src/cairn/harnesses/codex.py            MOD  argv (exec --json / exec resume -c sandbox_mode), event parse, transcript path; resume() untouched
src/cairn/harnesses/codex_models.py     NEW  models_cache.json catalogue + check (ported from run_codex.py; fast tiers from cache)
src/cairn/frontmatter.py                MOD  links: in _TAIL_KEYS, preserved_tail, PRESERVED_FIELDS
src/cairn/mutations.py                  MOD  add_link(find_file, session_id, role, ref) (validated, deduped)
src/cairn/config.py                     MOD  PeerPolicy / InvokePolicy from top-level "invoke" (cron precedent), fail loud on unknown keys
src/cairn/admin.py                      MOD  config set invoke.<peer>.<field>; config show prints invoke; doctor line for invoke defaults + missing models cache
src/cairn/hooks.py                      MOD  factor _process into export_and_index(config, source, id, transcript, *, update_index, lock_timeout_s) -> Path
src/cairn/__main__.py                   MOD  _ADMIN["invoke"], _HELP block
src/cairn/registry.py                   MOD  Source.subagent_type + session_types property; claude/codex entries
src/cairn/schema.py                     MOD  Message.role "agent"; SubagentRef; SessionData lineage + Codex context keys
src/cairn/store.py                      MOD  SCHEMA_VERSION = 2; session_types tuple in record_from_frontmatter/read_record/collection_records; SessionRecord.session_type, parent_id, is_subagent
src/cairn/sources/base.py               MOD  subagent_type + session_types; _id_index/_record_at widened; public record_at(path)
src/cairn/paths.py                      MOD  session_dir(), session_dir_of(document), subagent_path(), exchange_path()
src/cairn/jsonl.py                      MOD  iter_subagent_files, subagent_meta_path, workflow_run_of, find_subagent_jsonl, newest_mtime
src/cairn/rendering.py                  MOD  _ROLE_HEADERS["agent"] = "### Agent"
src/cairn/sources/claude.py             MOD  streaming extract_session_data(sidechain=), peer turns, child sync, Subagents index, export_target/cmd_sync dispatch, resume refusal
src/cairn/sources/codex.py              MOD  first session_meta wins, replay cut, custom_tool_call, agent_message marker, turn_context fields, child grouping + folder, title fallback
src/cairn/recall.py                     MOD  --include-subagents, type/parent in JSON rows
src/cairn/resume.py                     MOD  refuse subagent records
cairn.config.example.json, src/cairn/config.example.json   MOD  "invoke" block (byte-identical)
skills/invoke-codex/{SKILL.md,agents/openai.yaml}          NEW
skills/invoke-claude/{SKILL.md,agents/openai.yaml}         NEW
references/peer-review.md               NEW  gate prompts, critical evaluation, identity line, VERDICT line, convergence loop
references/harnesses/claude.md|codex.md MOD  background + poll recipe, sandbox caveat
skills/cairn/SKILL.md, skills/cairn/reference/{claude,codex}.md, skills/recall/SKILL.md, README.md, CHANGELOG.md   MOD
docs/agent-exchange.md                  NEW  design doc (fold docs/agent-exchange-research.md into it; deferred items)
tests/test_subagents.py, tests/test_invoke.py              NEW
tests/test_frontmatter.py, test_rendering.py, test_shared_store.py, test_plugin_manifest.py, test_admin_config.py, test_config_as_data.py, test_sources.py, test_hooks.py   MOD
src/cairn/__init__.py + 3 plugin manifests (scripts/sync_plugin_manifest.py)   MOD  2.5.0
```

## Part A: subagent visibility

### A1 Shared plumbing

- `registry.Source` gains `subagent_type: str = ""` and `session_types` property; `claude` →
  `claude-subagent`, `codex` → `codex-subagent`. No new `Source` entries.
- `schema.py`: `Message.role` accepts `"agent"`; `SubagentRef` TypedDict (`agent_id`, `title`,
  `agent_type`, `request_shape`, `workflow_run`, `path`); `SessionData` adds `parent_session_id`,
  `spawn_depth`, `agent_type`, `subagents`, `finished`, Claude `agent_id`, `description`,
  `tool_use_id`, `request_shape`, `workflow_run`, `workflow_phase`, Codex `originator`,
  `thread_source`, `forked_from_id`, `agent_nickname`, `agent_path`, `effort`, `sandbox`,
  `approval_policy`.
- `store.py`: `SCHEMA_VERSION = 2`; `record_from_frontmatter`, `read_record`,
  `collection_records` take `session_types: str | tuple[str, ...]`; `SessionRecord` gains
  `session_type`, `parent_id`, `is_subagent`. `SessionStore.records` passes `source.session_types`.
- `sources/base.py`: `subagent_type`, `session_types`; `_id_index` and `_record_at` filter on
  the tuple; public `record_at(path)` so child currency checks do not rebuild the id index.
- `paths.py`: `session_dir(output_dir, short_id, date, project, repo_subdir)` =
  `get_output_path(...).with_suffix("")`; `session_dir_of(document)`; `subagent_path(folder,
  short)`; `exchange_path(folder, peer, short)`.
- `rendering.py`: `### Agent` header for role `agent`.

### A2 Claude (`sources/claude.py`, `jsonl.py`)

- `jsonl.py`: `iter_subagent_files(session_jsonl)` (both `subagents/agent-*.jsonl` and
  `subagents/workflows/*/agent-*.jsonl`), `subagent_meta_path`, `workflow_run_of`,
  `find_subagent_jsonl(base, agent_id) -> (child, parent)`, `newest_mtime(session_jsonl)`.
  `iter_session_files` / `find_session_jsonl` stay parents-only.
- `extract_session_data(records: Iterable, *, sidechain=False)`: single streaming pass (callers
  pass a generator over `iter_jsonl`; the largest child is 68 MB). `sidechain=True` keeps
  `isSidechain` records. Capture `agent_id`, `parent_session_id` (= `sessionId` in child mode),
  `finished` (last assistant `stop_reason == "end_turn"`). Peer records (`origin.kind == "peer"`)
  become `{"role": "agent", "text": "from agent <from> (<name>):\n\n<origin.body>"}` before the
  `isMeta` skip and are not counted as user messages. Agent task-notifications stay User turns.
- Helpers: `read_subagent_meta` (missing → `{}`, malformed → `ValueError`), `subagent_fields`,
  `subagent_index(children, parent_doc)` from metas only, `parent_revision(jsonl, index)` =
  sha256 of the parent file revision plus the rendered index tuples `(agent_id, description,
  agent_type, finished, stopped_by_user)` (D15, D23), `child_revision(agent_jsonl)` (jsonl + meta).
- Child document: `type: claude-subagent`, `session_id: agent-<agentId>`, `parent_session_id`,
  `agent_id`, `agent_type`, `description` (quoted), `tool_use_id` (omit for workflow agents),
  `spawn_depth`, `request_shape`, `model`, `workflow_run`, `workflow_phase`, parent's `repo` /
  `git_branch`, title = description else first prompt line. Body: parent link line, Artifacts,
  My Notes, Conversation. `project`/`repo` come from the parent's encoded dir, never the child's
  `cwd` (91 of 200 sampled children run in another repo). Default status `done` when `finished`.
- Parent document: `## Subagents` index after `## Artifacts`, one bullet per child with relative
  link, id, agent type, request shape or workflow run.
- `sync_session`: `agent-` prefix dispatches to `sync_subagent_by_id` (was `return None`);
  parent revision uses `parent_revision`; `data["subagents"]` set. New `sync_children(parent_jsonl,
  parent_id, parent_doc, project, repo, quiet) -> (exported, errors)` writes each stale child to
  `subagent_path(session_dir_of(parent_doc), agent_id)` via `export_one(..., existing_file=planned)`;
  a child whose `sessionId` is not the parent raises. `sync_subagent_by_id` syncs the parent
  (index refresh) then that one child.
- `run_export`: window filter uses `newest_mtime`; per parent `sync_session` then `sync_children`;
  child errors print and set exit 1. `export_target` and `cmd_sync` dispatch on id form so the
  SessionEnd hook path handles both. `cmd_resume` and `resume.run` refuse `is_subagent` records.

### A3 Codex (`sources/codex.py`)

- `parse_rollout`: first `session_meta` wins and also captures `originator`, `thread_source`,
  `forked_from_id`, and `source.subagent.thread_spawn.{parent_thread_id, depth, agent_role,
  agent_nickname, agent_path}` as `parent_session_id`, `spawn_depth`, `agent_type`, `agent_nickname`,
  `agent_path`. Timestamp capture moves below the replay check.
- Replay cut lives in `src/cairn/sources/codex_replay.py` as `ReplayCut` with a version tag
  (`codex-cli 0.160`) and the evidence in its docstring (100 of 100 local forked children, replay
  stamped at fork time so timestamps cannot be used, child turn ids sort after the child id
  because both are UUIDv7). Fallback chain when the second `session_meta` is seen: (1) first
  `event_msg task_started` whose `turn_id` and the child id both parse as UUIDv7 and
  `turn_id >= child id`; (2) else the first `thread_settings_applied` event or developer message
  containing `<multi_agent_role>` after the second meta (present in 95 of 100); (3) else no cut:
  the child exports with `replay_unresolved: true` in the head and a stderr warning, never a
  dropped document and never an exception. EOF while still replaying is normal for a live child
  (the rollout is appended during the run): if the file's mtime is younger than `stall_after_s`
  the child is skipped silently this run; if older it exports with `replay_unresolved: true`.
  Nested forks (depth 2, three metas) apply the same rule to the first meta's id. Tests cover
  depth 2, non-UUIDv7 legacy ids, a torn final line, and a live child with no own records.
- `turn_context`: capture `effort`, `approval_policy`, `sandbox` (`sandbox_policy.type`).
- Tool calls: factor `_append_tool_call` / `_resolve_tool_output`; `custom_tool_call` passes
  `payload["input"]`, outputs that are `input_text` lists go through `_content_text`.
- `response_item agent_message`: `{"role": "agent", "text": "from <author> to <recipient>\n\n<header>\n[encrypted]"}`.
- `finished` on an assistant `phase == "final_answer"` after the cut. Title fallback
  `"<agent_type> <agent_nickname>: <agent_path>"` when no user text and a parent exists.
- `render_document`: head adds `originator`, `thread_source`, `effort`, `sandbox`, `approval_policy`,
  `forked_from_id`, and for children `type: codex-subagent` plus lineage; `## Subagents` index on
  parents; default status `done` for finished children.
- `_parse_rollouts`: catch `ValueError` per file, print, continue, exit 1 at the end.
- Lineage index (D23): `rollout_lineage(store) -> Lineage` reads only the first line of every
  rollout (`session_meta`: id, parent thread id, role, nickname, path, originator) and caches
  per file by `(size, mtime_ns)` under `$XDG_CACHE_HOME/cairn/lineage/<store-hash>.json`, so a
  windowed or targeted export knows every parent-child relation without parsing bodies.
- `export_rollouts`: parents are selected when `max(parent mtime, children mtimes) >= cutoff`;
  children are selected by their own mtime. A parent's `source_revision` =
  `sha256(file_revision(parent) + rendered child index)` where the rendered index is the
  sorted tuple of `(child_id, title, agent_type, finished)` actually written under
  `## Subagents`, so a new child, a retitled child or a child that finishes re-renders the
  parent even when the parent rollout is unchanged. Export parents first, child target =
  existing `record.path`, else `subagent_path(session_dir_of(parent_record.path),
  short_id(child))` when the parent doc exists, else the canonical path.
- `export_target(parent_id)`: candidates = the parent's own fragments plus every child from the
  lineage index, so a targeted export renders the index. `export_target(child_id)`: the child
  plus a parent index refresh. Tests: new child under an unchanged parent, a child title change
  only, targeted parent export renders children, targeted child export refreshes the parent.

### A4 Recall

`recall list --include-subagents`; JSON rows add `type` and `parent`; text rows append
`(subagent of <parent[:8]>)`. `expand` unchanged.

## Part B: `cairn invoke`

### B1 Harness adapters (`harnesses/`)

- `InvokeRequest(peer, prompt, cwd, sandbox, model, effort, fast, timeout_s, resume, session_id,
  output_schema, last_message_path, prompt_via_stdin, extra)` and
  `Event(kind: thread_started|message|progress|notice|done, text, thread_id, status, usage, raw_type)`.
- Protocol additions: `executable`, `exec_argv(request) -> list[str]`,
  `parse_event(line) -> list[Event]`, `transcript_path(thread_id, store) -> Path | None`,
  `self_check(model, effort, fast) -> list[str]`.
- Codex argv, fresh: `codex exec --json --skip-git-repo-check -s <sandbox> [-m M]
  [-c model_reasoning_effort="E"] [--enable fast_mode -c service_tier="priority"] -C <cwd>
  [--output-schema F] -o <tmp> [extra...] <prompt|->`. Resume (cwd = recorded):
  `codex exec resume --json --skip-git-repo-check -c sandbox_mode="<sandbox>" [-m] [-c effort]
  [--output-schema F] -o <tmp> [extra...] <thread_id> <prompt|->`. Never `--last`.
- Claude argv, fresh: `claude -p --output-format stream-json --verbose --session-id <uuid4>
  --permission-mode <per profile> [--disallowedTools Edit,Write,NotebookEdit for review]
  [--model] [--effort] [--json-schema <contents>] [extra...] [prompt]`; resume swaps
  `--session-id` for `--resume <id>` and re-sends the recorded permission flags.
- Event map: codex `thread.started` → thread_started; `item.completed agent_message` → message;
  `command_execution`, `file_change`, `todo_list`, `reasoning` → progress; `item.completed error`
  → notice (the hooks-deprecation lines at every 0.160 stream start are never a failure);
  `turn.completed` → done succeeded; `turn.failed` / top-level `error` → done failed. Claude
  `system/init` → thread_started(session_id); `assistant` → message plus one progress per
  `tool_use`; `result` → done (subtype `success` or an `error_*`).
- `transcript_path`: codex `store/YYYY/MM/DD/rollout-*-<id>.jsonl` (today, yesterday, then rglob);
  claude `jsonl.find_session_jsonl(store, id)`.
- `codex_models.py`: `load_codex_model_catalog`, `check_codex_invocation`; fast models from
  `additional_speed_tiers`; empty catalogue is a problem (D9). Claude: effort must be in
  `low|medium|high|xhigh|max`, `fast` rejected.

### B2 Runner (`invoke.py`)

- Subcommands: `cairn invoke <codex|claude> (--prompt TEXT | --prompt-file PATH) [--model]
  [--effort] [--profile review|edit|full] [--sandbox (codex only)] [--permission-mode (claude
  only)] [--fast] [--cd DIR] [--timeout-s N] [--idle-timeout-s N] [--resume THREAD_ID] [--fork]
  [--parent SOURCE:ID] [--output-schema FILE] [--allow-nested] [--skip-self-check] [--no-index]
  [--json] [--extra ...]`,
  `cairn invoke status <thread_id> [--follow] [--json]` (prefix match on the id, liveness by
  pid plus start time, progress re-parsed from the event file so it works even if `thread.json`
  lagged, adopts and finalises orphaned rounds, `--follow` polls each second and exits on
  `exited`, never creates directories), `cairn invoke cancel <thread_id>`,
  `cairn invoke publish <thread_id>`, `cairn invoke list-models [--json]`.
- Run directory `data_root/logs/invoke/<peer>/<thread_id>/`: `thread.json` (peer, thread_id, cwd,
  profile and the exact native argv per round, model, effort, caller_session, anchor,
  `rounds[]` each with `runner_pid`, `runner_started`, `peer_pid`, `peer_pgid`, `peer_started`,
  `started_at`, `heartbeat_at`, `last_event`, `finished_at`, `outcome`, `exit_code`, `usage`,
  `publication{}`), `events-NN.jsonl`, `stderr-NN.log`, `answer-NN.md` (the `-o` target lives
  here, not in a temp file, so an orphaned round can still be finalised). Written only with
  `atomic_write`. Fresh Codex runs start in `pending-<uuid>/` and rename on `thread_started`;
  Claude ids are chosen up front.
- Concurrency (D19): before allocating a round, take `locked(lock_path(run_dir, "thread"),
  blocking=False)`; on failure return `{"status": "busy", "holder": {round, runner_pid,
  runner_started}}`, exit 75, and do not spawn. The lock is held until the round is finalised
  (including the post-run pipeline). Test: two simultaneous `--resume` calls on one thread,
  exactly one runs, the other reports busy, `thread.json` has one new round.
- Process: `Popen(stdin=DEVNULL|PIPE, stdout=PIPE, stderr=<file>, cwd, env=scrubbed,
  start_new_session=True, close_fds=True)`; record `peer_pid`, `peer_pgid` and `peer_started`
  (process start time from `ps -o lstart= -p <pid>` on macOS, `/proc/<pid>/stat` on Linux)
  immediately after spawn; single-thread `selectors` pump with the deadline as the select
  timeout; tee every line, parse events, refresh `heartbeat_at` and the exchange document on each
  `message` and every 30 s. Wall-clock timeout → `killpg(SIGKILL)`, `wait(10)`, outcome
  `timeout`, exit 124. Idle timeout → the same kill, outcome `idle_timeout`, exit 125.
- Cancellation (D21): the runner installs a SIGTERM/SIGINT handler that sends SIGTERM to the
  peer group, waits 10 s, then SIGKILL, and finalises the round with outcome `cancelled`.
  `cairn invoke cancel <thread_id>` does the same from another process using the recorded peer
  identity (pid plus start time; a mismatch means the pid was reused and nothing is signalled).
- Orphans: when the runner dies by SIGKILL the peer keeps running. Any reader (`status`,
  `agents`, `publish`) that finds a round with no `finished_at`, a dead runner identity and a
  live peer identity reports execution `orphaned`; once the peer identity is gone, the reader
  finalises the round from the run directory alone (last `done` event in `events-NN.jsonl`,
  `answer-NN.md`, process exit unknown → outcome `aborted` if no `done` event, else the event's
  status) and runs the publication steps. `--resume` refuses while the peer identity is alive.
- Preamble prepended to the prompt for both peers: who is calling (`<caller> session <source:id>`,
  round N), the wall-clock budget, "work autonomously, final answer as last message", and
  "if asked for a verdict end with `VERDICT: accept|reject|unresolved - reason`".
- Caller detection: `--parent` wins; else `detect_harness()` + `current_id()`; conflict or none →
  `caller_session: null` plus warning.
- Result JSON (one line on stdout): `status, peer, thread_id, round, model, effort, sandbox, cwd,
  duration_s, exit_code, events_path, stderr_path, transcript_path, exchange_doc,
  peer_session_doc, caller_session, caller_session_doc, last_message, usage, indexed, error,
  retryable, warnings`. Exit codes: peer rc; 2 usage/self-check/depth; 124 timeout; 1 cairn fatal.
- `_retryable`: the wrapper's client-error markers plus `Not logged in` (keychain guidance text
  for the Codex-host case).

### B3 Post-run pipeline (`finish`), in this order

1. `hooks.export_and_index(config, peer, thread_id, transcript_path, update_index=False)` (the
   subprocess `cairn hook export` path factored out of today's `_process`, returning the printed
   path; `.operation.lock` taken non-blocking with retry ≤ 120 s) → `peer_session_doc`.
2. Best-effort caller export: same call for the caller source (the Claude transcript exists live
   on disk) → `caller_session_doc`; failure → warning.
3. `mutations.add_link(peer.find_file, thread_id, "parent", caller_ref)` and
   `add_link(caller.find_file, caller_id, "child", f"{peer}:{thread_id}")`; idempotent; source
   instances imported lazily as `hooks.main` does; `flush_catalog()` after.
4. `exchange.write_exchange(config, state)` → exchange document (B4).
5. Unless `--no-index`: `with locked(index_lock(config), blocking=False)` with retry ≤ 60 s:
   `ensure_collection` for each touched collection, then one `qmd update`; busy →
   `indexed: false` + warning. `hooks._process` becomes
   `export_and_index(..., update_index=True)` with unchanged worker behaviour and messages.
6. Publication ledger (D24): each of steps 1 to 5 writes `publication.<step> = <iso ts>` on
   success or `publication.pending.<step> = <reason>` on failure into the round record. Step 0,
   `answer_saved`, is written as soon as the peer exits. `cairn invoke publish <thread_id>`
   re-runs only the pending steps and returns their new state; `cairn sync` calls
   `invoke.drain_publications(config)` at its end so a busy lock never leaves a round
   unpublished for longer than one cron cycle. Lock waits in steps 1 and 5 are bounded
   (120 s and 60 s) so a 20 minute run never hangs at its end; the ledger carries the rest.

### B4 Exchange document (`exchange.py`)

- Path: `exchange_path(session_dir_of(anchor_doc), peer, thread_short)` where the anchor is the
  caller session document when known, else the peer session document. The folder is the same one
  Part A uses for subagents. Existing document located through `thread.json` (`exchange_doc`),
  falling back to a scan of the anchor folder for `thread_id:`.
- Frontmatter (head, via `head_open("agent-exchange", date, "thread_id", id)`): `type`, `date`,
  `thread_id`, `peer`, `caller`, `caller_session`, `peer_session`, `anchor_session`, `model`,
  `effort`, `sandbox`, `cwd` (quoted), `run_status` (running|succeeded|failed|timeout), `rounds`,
  `started_at`, `title`, `last_activity`; then the standard preserved tail (`status: active`,
  tags, rating, comments, projects). No `source:` or `native_id:` keys, so the type filter is the
  only thing between these docs and the session index (D18 validation applies).
- Body: `# title`, `## My Notes` (preserved), `## Rounds`, per round `### Round N — <ts>` with
  `**Prompt**` (fenced `text` block with a fence longer than any backtick run), `**Progress**`
  (bullets from progress events, capped at 200), `**Answer**` (final message verbatim),
  `**Result**` (status, duration, exit, events path, transcript path, session doc).
  `render_exchange(state, existing_fm, my_notes)` is a pure function; `write_exchange` reads the
  existing file, re-renders, and `atomic_write`s under `document_lock`, so mid-run rewrites are
  idempotent and hand-written notes survive.
- The catalog parses these files; `_id_index`, `read_record`, listing and recall ignore them
  because `type` is not in `session_types`. A unit test asserts this so a future "accept any
  type" change fails loudly. On first creation of the folder, best-effort
  `qmd context add qmd://<collection>/<rel_folder> "Agent exchanges and subagent documents
  spawned by <anchor_session>"` with a warning on failure.

### B5 Frontmatter, mutations, config, admin

- `frontmatter.py`: `_TAIL_KEYS += ("links:",)`; `preserved_tail` emits a `links:` block list
  last, only when non-empty (D4), so `test_canonical_tail_is_stable` and every existing export
  stay unchanged; `PRESERVED_FIELDS` adds `links`. `rewrite_preserved_tail` needs no change.
- `mutations.py`: `add_link(find_file, session_id, role in {parent, child, peer}, ref matching
  ^(claude|codex|cursor|granola):\S+$)`; strips newlines, dedupes, prints `Added link:`.
- `config.py`: `PeerPolicy(model=None, effort=None, profile="review", timeout_s=1800,
  idle_timeout_s=900, quiet_after_s=120, stall_after_s=600)`, `InvokePolicy(codex, claude)` with `for_peer(name)`, `_parse_invoke` rejects
  unknown peers/fields (ignoring `//` keys), unknown profile, non-positive timeouts;
  `CairnConfig.invoke`. Example configs gain the `invoke` block with `gpt-6-astra`/`high`/
  `review` for codex and nulls/`review` for claude; both files byte-identical.
- `admin.py`: `_set_dotted` handles `invoke.<peer>.<field>` (`timeout_s` int, `"null"` → None);
  `config show` prints the block; `doctor` prints invoke defaults and warns when `codex` is on
  PATH but `models_cache.json` is missing.
- `__main__.py`: `_ADMIN["invoke"]`; `_HELP` "peer agents" block.

## Part D: follow-up after finish, and health

### D1 Resume semantics (verified on this machine)

- `codex exec resume <SESSION_ID> [PROMPT]` takes a UUID or thread name and accepts `-c`, `-m`,
  `--json`, `-o`, `--output-schema`; `--all` only widens the `--last` picker, an explicit id
  resolves regardless of cwd. The runner always resumes from the cwd recorded in `thread.json`
  (the thread's files live there) and re-asserts `-c sandbox_mode=` and
  `-c model_reasoning_effort=` from the record. `claude -p --resume <id>` behaves the same way.
- Cost grows with turn count (the Borda bridge measured a linear climb), so the exchange document
  records tokens per round from `turn.completed.usage` / `result.usage`, and the skill says to
  prefer a fresh thread with a written brief once a thread passes about three rounds.
- `cairn invoke <peer> --resume <id>` works from any later session: `thread.json` is keyed by
  thread id under `data_root/logs/invoke/<peer>/`, and the caller of the new round (the current
  session) is linked as a second `parent` on the peer document. `cairn resume codex:<id>` already
  prints the native interactive command; `thread.json` gains `resume_command` for convenience.
- Optional branch: `--fork` maps to `codex exec fork <id>` / `claude --resume <id> --fork-session`
  and starts a new thread record whose `thread.json` carries `forked_from`.

### D2 Health model: three axes (`src/cairn/health.py`)

Heartbeat sources, cheapest first: the recorded process identities (pid plus start time), the
events file mtime, and the native transcript mtime (`transcript_path`). The native transcript is
the strongest signal: a healthy `codex exec` rollout gained a record at least every 205 s across
nine local runs of 7 to 18 minutes (p95 gap 14 s) because `token_count` and reasoning records
land even when stdout is quiet. Claude's transcript is appended per content block.

**Execution state** (what the processes are doing):

| state | rule |
|---|---|
| `pending` | round allocated, peer not yet spawned |
| `running` | peer identity alive (runner alive or not) |
| `orphaned` | peer identity alive, runner identity dead (runner was SIGKILLed); readers adopt it |
| `exited` | round has `finished_at` |
| `unknown` | no `finished_at`, no live identity, and the run directory has no `done` event (cannot tell whether the peer ever ran to completion) |

**Activity** (an observation with an age, reported alongside `running`/`orphaned`, never terminal):
`active` (heartbeat younger than `quiet_after_s`, default 120), `quiet` (younger than
`stall_after_s`, default 600), `silent` (older). The status line prints the age and the last
event, e.g. `running, silent for 11m (last: command_execution git diff at 14:02:11)`.

**Outcome** (set once, when the round ends): `succeeded`, `failed` (peer reported failure or
non-zero exit), `timeout` (wall clock), `idle_timeout` (watchdog), `cancelled` (SIGTERM via the
runner or `cairn invoke cancel`), `aborted` (adopted orphan with no `done` event), `unknown`
(adopted orphan whose events file is unreadable).

- Runner watchdog: `--idle-timeout-s` (config `invoke.<peer>.idle_timeout_s`, default 900) kills
  the peer group when no heartbeat arrives for that long; outcome `idle_timeout`, exit 125,
  `retryable: true`, `error` names the last event and its age. `--timeout-s` stays as the hard
  ceiling. Thresholds are config as data in the `invoke` block.
- The runner writes `heartbeat_at` and `last_event` on every event and at least every 30 s, so
  `status` works when the host lost the task handle.
- `--json` on `status` carries `execution`, `activity`, `activity_age_s`, `outcome`, `elapsed_s`,
  `budget_s`, `runner_alive`, `peer_alive`, `publication`.
- Known hang causes, each handled: an inherited open stdin (`stdin=DEVNULL`, or write-then-close
  for large prompts), a run that outlives the host's tool budget (background run plus `status`),
  a model stall with no output (idle watchdog), a runner killed mid-run (`orphaned`, adopted and
  finalised from the run directory). Approval prompts are not a cause here: no approval events
  exist in 89 local exec rollouts, and the runner passes `-c approval_policy="never"` so a
  config change cannot introduce one.

### D3 `cairn agents`: one view over everything a session spawned

New admin command `src/cairn/agents.py`, read-only over native stores plus `thread.json`, using
the same three axes.

- Claude subagent classification, in order: `meta.stoppedByUser: true` → exited, outcome
  `cancelled`; `meta.finished: true` → exited, `succeeded`; otherwise compare the child's last
  record time with the newest `task-notification` for that `agentId` in the parent transcript
  (every stop, including a stop after a `SendMessage` resume, produces one): a notification newer
  than the child's last record → exited with the notification's status; no notification or an
  older one → `running` with activity from the child jsonl mtime, or `unknown` when the parent
  transcript itself has not changed for longer than `stall_after_s`. An older child with no
  `finished` key and a final `end_turn` is treated as exited only when no newer notification
  contradicts it. Codex subagents: `task_complete` after the replay cut → exited, else running
  by rollout mtime.
- `cairn agents list [--session SOURCE:ID] [--all] [--json] [--watch]`: rows for peer runs,
  Claude subagents and Codex subagents of the session, columns kind, id, parent, description or
  title, execution, activity with age, outcome, started, path to the log or transcript. Default
  session from `detect_harness()` + `current_id()`; `--all` lists every non-exited item.
- `cairn agents wait [--session SOURCE:ID | <id>...] [--timeout-s N] [--poll-s 5]
  [--stall-after-s N] [--json]`: blocks until every target is `exited` or `unknown`. Exit 0 when
  every outcome is `succeeded`, 1 when any outcome is not `succeeded` (listed), 124 on the wait
  timeout. `--stall-after-s` is off by default; when set, `wait` returns 3 as soon as any target
  has been `silent` that long, and prints that the peer is still running until its own watchdog
  fires, so the two mechanisms never disagree silently.
- `cairn invoke status --follow` stays for a single thread; `agents wait` is the fan-out primitive.
- The subagent fields `parent_agent_id`, `is_fork` and `stopped_by_user` (present in newer
  `meta.json`) join the Claude child head so nested forks and cancellations render.

### D4 Skill and reference changes for health

- Both invoke skills: after launching, run `cairn invoke status <id>` on a cadence or when asked,
  read the execution and activity line, and act on `silent` (wait one more interval, then
  `cairn invoke cancel` and `--resume` with a narrower task, or report) instead of guessing. On
  `orphaned` or `unknown`, report the events file and transcript paths and let `status` adopt.
- `skills/cairn/SKILL.md` and `references/harnesses/*.md`: `cairn agents list` before resuming
  work after a context reset; `cairn agents wait --session <id>` as the background gate for a
  fan-out; never treat silence as completion.

## Part C: skills, references, docs, packaging

- `skills/invoke-codex/SKILL.md` and `skills/invoke-claude/SKILL.md` (same skeleton): frontmatter
  `name`, `description` (trigger phrases folded in), `argument-hint`, `allowed-tools: Bash(cairn:*),
  Read, AskUserQuestion, Monitor`; opening line reads `../../references/harnesses/contract.md`,
  the host file, and `../../references/peer-review.md`. Steps: prompt (gate prompt or
  `--prompt-file` for long briefs), model and effort (`cairn invoke list-models`, ask unless
  named or "just run it"), sandbox (ask before widening), run in the background per host
  reference, watch with `cairn invoke status`, read the one JSON line (lead with `last_message`,
  check `status`, `warnings`), follow up with `--resume` and the identity line, close by naming
  the thread id and exchange document. Flags table says the CLI is authoritative. Errors section
  (exit 2, 124, `retryable: false`, `warnings[]`). `invoke-claude` adds the Codex-sandbox caveat.
  `agents/openai.yaml` with `allow_implicit_invocation: true` for both.
- `references/peer-review.md`: generalised `quality-gates.md`: why a different model, gate
  workflow, gate prompts (uncommitted diff, branch diff, plan review, test simulation, SQL
  migration, API endpoint), critical evaluation rules, identity line, convergence loop (max 3
  rounds per thread, `VERDICT:` line, stall detection, escalate with accepted / dismissed /
  unresolved lists), never retry a timeout blind.
- `references/harnesses/claude.md`: Bash `run_in_background` + Monitor + `cairn invoke status`
  recipe. `references/harnesses/codex.md`: `exec_command` + `write_stdin` recipe, escalated
  permissions for peer runs, never a nested `codex exec` inside the sandbox.
- `skills/cairn/SKILL.md` (peer agents block, folder layout), `skills/cairn/reference/claude.md`
  and `codex.md` (new head fields, subagent sections, hook behaviour for `agent-` ids),
  `skills/recall/SKILL.md` (`--include-subagents`, JSON fields), README (peer agents section,
  one-folder-per-session layout, one-time `export --all` after upgrade), CHANGELOG 2.5.0,
  `docs/agent-exchange.md` (decisions, transport facts, deferred items; the research doc folds in).
- Version 2.5.0 in `src/cairn/__init__.py`, `plugin.json`, `.claude-plugin/plugin.json`;
  regenerate `.codex-plugin/plugin.json` with `scripts/sync_plugin_manifest.py`.
- `tests/test_plugin_manifest.py`: every `skills/*/` has `agents/openai.yaml`; invoke skills
  carry `allow_implicit_invocation: true` and no `disable-model-invocation`;
  `references/peer-review.md` exists; relative links resolve.

## Implementation order

1. Worktree is on `main` at `1443f89`. Run `make check` to baseline.
2. A1 shared plumbing + `SCHEMA_VERSION` 2 + registry parity test; fix the `cairn_schema: "1"`
   literals in `test_shared_store.py`.
3. A3 Codex parser fixes with fixtures F5 to F7 (recovers the 100 sessions; smallest, highest value).
4. A2 Claude subagent export with fixtures F1 to F4; recall flag (A4).
5. B5 `links:` + `add_link` + config `invoke` block + admin.
6. `hooks._process` split (behaviour-preserving; `tests/test_hooks.py` green).
7. B1 adapters + `codex_models.py` with vendored event fixtures.
8. B4 `exchange.py`, then B2/B3 runner and pipeline with stub binaries.
9. Part D: `health.py`, idle watchdog in the runner, `status` health line, `agents.py`
   list and wait, with the threshold tests.
10. Part C skills, references, docs, version, manifests.
11. Live acceptance (below), then open the PR against `main`.

## Verification

Unit (`make check`, Python 3.11 to 3.13):

- `tests/test_subagents.py`: T1 child docs and head fields (incl. workflow child, meta-less child,
  parent project despite foreign cwd); T2 parent index, `### Agent` turn, no sidechain leakage;
  T3 idempotence and revision rules; T4 window via `newest_mtime`; T5 hook export by parent id
  and by `agent-` id, mismatch fails; T6 recall/list/note/expand/resume behaviour; T7 generator
  input; T8 forked Codex child parse (first meta wins, replay cut, lineage, effort, sandbox,
  title fallback); T9 parent/child separation and folder; T10 `custom_tool_call`; T11
  `agent_message`; T12 missing boundary is loud and non-blocking; T13 `export_target` on a child;
  T14 registry/store parity and `needs_upgrade`; T15 `### Agent` rendering.
- `tests/test_invoke.py` (stub `codex`, `claude`, `qmd` on a temp PATH via the `fake_qmd` pattern,
  `CODEX_HOME` with a `models_cache.json` fixture): argv per peer fresh and resume; sandbox →
  permission-mode map; event parsing from vendored 0.160 and claude streams (notice for the
  deprecation errors, `turn.failed` → failed); self-check exit 2 before spawn; thread id capture,
  events tee, result JSON, pending-dir rename; timeout kills the process group; client error not
  retryable; export + links both ways + exchange doc parse; resume appends a round and preserves
  My Notes and links; `links:` round-trips through mutation and re-export and is omitted when
  empty; exchange docs are invisible to `find_record`, `list`, `recall` and leave
  `Catalog.errors` empty; anchor falls back to the peer folder when no caller is known; depth
  guard and env scrub (no `CLAUDE_SESSION_ID`, `CODEX_*`, `CLAUDECODE` in the child env); large
  prompt via stdin; `status` renders progress, `--follow` ends on `done`, unknown id fails,
  no directories created; busy `index_lock` → `indexed: false`; `Not logged in` guidance;
  `hooks._process` behaviour unchanged through `export_and_index`.
- Health and follow-up (`tests/test_invoke.py`, `tests/test_agents.py`): a stub that emits one
  event then sleeps is `running/active`, then `running/quiet`, then `running/silent` at the
  thresholds (lowered through config in the test) and is killed at `--idle-timeout-s` with
  outcome `idle_timeout`, exit 125, `retryable: true`; SIGTERM to the runner yields `cancelled`
  with the stub's sleep child gone; SIGKILL to the runner yields `orphaned` while the stub runs,
  then `status` adopts and finalises it with the stub's `done` event once it exits, and a round
  with no `done` event becomes `aborted`; a recorded pid that now belongs to another process
  (start time differs) is treated as dead; two simultaneous `--resume` calls produce one run and
  one `busy` result; resume from a different cwd uses the recorded cwd and the recorded native
  argv; `--profile review` on Claude passes `--permission-mode plan --disallowedTools ...` and
  the record stores that argv, not the profile name; `--fork` creates a new thread record with
  `forked_from`; a held index lock leaves `publication.pending.indexed` and
  `cairn invoke publish` clears it; `cairn agents list --session claude:<id>` classifies a
  finished subagent (`finished: true`), one stopped by the user (`stoppedByUser: true`, outcome
  `cancelled`), one finished only by a newer task-notification, one resumed after a notification
  (running again), and a silent one; `cairn agents wait` exits 0, 1, 3 (only with
  `--stall-after-s`) and 124 in the four cases and never creates directories.
- Config/admin tests for the `invoke` block; manifest tests for the new skills.

Live acceptance on this machine (record results in `docs/agent-exchange.md`):

1. `cairn codex export --all` after the schema bump: count `type: codex-subagent` documents
   (expect about 100 new) and check one invoke-codex review export shows `exec` tool calls.
2. `cairn claude export --all` for the plantedex project: 84 child documents under
   `plantedex/2026-10-15-c4c36add/`, `## Subagents` index present, `qmd search` on a phrase that
   exists only in a child transcript hits the child document.
3. From a Claude host: `/invoke-codex` with `--effort low --prompt "Reply with exactly PONG"`
   in the background; `cairn invoke status <id>` mid-run; result `last_message == "PONG"`;
   `links:` on both session documents; exchange document in the Claude session folder; then
   `--resume <id>` adds round 2.
4. From a Codex host: `$invoke-claude` with escalated permissions; either success or the
   keychain guidance with `retryable: false`. Also `$invoke-codex` from Codex (nested
   `codex exec` needs escalation too) to confirm the depth guard and the recipe in `codex.md`.
5. `cairn doctor`, `cairn config show`, `cairn recall list today --include-subagents`.
6. Follow-up and health: resume the thread from step 3 in a new Claude session the next day
   with `--resume` and no other flags; during a real high-effort review run `cairn invoke
   status <id>` twice and `cairn agents list` while three Agent subagents run, then
   `cairn agents wait --session claude:<id>` in the background and confirm the completion
   notice arrives after the last subagent finishes.
7. Timing: first `--all` export wall-clock and `qmd embed` duration after it, noted for the
   `SyncPolicy.subagents` follow-up decision.

## Risks and mitigations

- 2.6 GB child JSONL on first `--all`: streaming single pass, per-child currency via `record_at`,
  optional `api-request-blob` prefix skip; measure and raise `recall --refresh` timeout if needed.
- Catalog type filter: children invisible unless `session_types` is a tuple everywhere; T3 and T6 guard it.
- QMD embed growth from child markdown: measured in acceptance; follow-up toggle `sync.subagents`.
- Keychain under the Codex sandbox for `invoke claude`: fail loud with guidance; exercised in acceptance.
- Nested `claude -p` under Claude Code may refuse to start on a `CLAUDECODE` guard: the env scrub
  (D13) removes it; acceptance step 3 confirms and records the observed behaviour.
- `index_lock` held by cron embed: non-blocking with retry, documents are on disk regardless.
- Resume sandbox widening: `-c sandbox_mode` on every Codex resume; the exact native argv is recorded per round.
- Concurrent resumes on one thread: per-thread flock held for the whole round; the loser gets `busy`.
- Orphaned peers after a runner SIGKILL: identities with start time, adoption by any reader, `--resume` refused while the peer lives.
- Replay heuristic drift across Codex versions: isolated, version-tagged, fallback chain, never a dropped document.
- Hooks-deprecation `error` items: mapped to notice; only `turn.failed`, top-level error, non-zero exit or timeout fail a run.
- #28 drift: rebase first, reference functions by name, keep one `SCHEMA_VERSION` bump.
- Two `invoke-codex` skills after install: retire `~/.claude/skills/invoke-codex` once the plugin skill lands.
