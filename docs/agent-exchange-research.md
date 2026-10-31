# Claude Code <-> Codex exchange: research and port plan

Date: 2026-10-16. Branch: `feat/invoke-codex-skill` (worktree, off `origin/main` 48453bb).
Research base: six parallel agents, each reading real code (cloned under the session
scratchpad), plus a `cairn recall` over the owner's own session history.

Goal: port the personal `~/.claude/skills/invoke-codex` skill into cairn, let Claude Code
and Codex send work to each other, read progress midway, keep debating in one thread, and
make every exchange searchable through cairn. Also: does cairn capture subagent activity?

## 1. What the owner does today (from `cairn recall`)

- Orchestrator session fans out 3 to 4 build agents (`Agent`, background) plus one read-only
  Codex review (`run_codex.py`, background), waits for completion notices, verifies each.
  Example: `claude-code-sessions/plantedex/2026-10-15-c4c36add.md`, lines 981 to 1463.
- Instruction pattern: "intermittently, use /invoke-codex astra high as adversarial reviews".
- Resume-and-debate pattern: "This is Claude (<model>) following up. Re: finding X ...
  convince me otherwise or concede." Found in 3 codex-sessions documents since April 2026.
- File-handover pattern: a brief written to disk, Codex told to write `REVIEW.md` and
  `PATCH.md` to a folder (`notes/house/handover-codex-review.md`). This is a markdown mailbox.
- The headless Codex runs ARE already in `codex-sessions` (e.g.
  `plantedex/2026-10-16-01a142be47e4.md`): `session_meta.originator = "codex_exec"`,
  `source = "exec"`, `model: gpt-6-astra`, and the prompt Claude sent is a user message in
  the rollout. On `origin/main` the exported document shows `title: Untitled Session`,
  `messages: 0` because main reads user text only from `event_msg/user_message`, which 0.160
  exec rollouts do not write. `feat/codex-support` fixes this (`content_item_kinds ==
  "user.text"`). There is no link between the Codex document and the parent Claude session.

## 2. Transport facts verified on this machine (codex-cli 0.160.0, claude 2.1.301)

| Fact | Evidence |
|---|---|
| `codex exec --json` streams JSONL events to stdout; first line is `{"type":"thread.started","thread_id":...}` | run in scratchpad `messaging/run2` |
| stdout stream and `~/.codex/sessions/.../rollout-*-<thread_id>.jsonl` both grow line by line during a run | 4, 6, 7, 9 events over 11 s |
| `agent_message` arrives only as a whole `item.completed`; no text deltas in exec mode | same run |
| `codex exec resume --json <thread_id> "<msg>"` continues the same thread and rollout | reply `ok-1-2` |
| `codex exec resume` has no `-C` and no `-s`: run from the original cwd; sandbox is inherited (the Borda bridge found a plain resume can silently widen to `workspace-write`, so re-assert it) | `codex exec resume --help` |
| `codex queue --thread <id> --message <text>` queues for the next turn; it does NOT reach a running `codex exec` turn (ended in `turn_aborted`) | test 5 |
| `codex mcp-server` is gone in 0.160.0 (prints top-level help; deprecated in 0.149.1). Codex-as-MCP-tool from Claude is no longer an option | `codex mcp-server --help` |
| Mid-turn steering exists only via `codex app-server` JSON-RPC: `turn/steer` (needs `expectedTurnId`), `turn/interrupt`, `thread/queue/add`. Needs a long-lived broker process | `generate-json-schema --experimental` |
| `claude -p --output-format stream-json --verbose` emits `system/init` with `session_id`, then `result`; `claude -p --resume <id>` continues; `--session-id <uuid>` lets the caller pick the id up front | run in `messaging/claude1` |
| Inside the Codex macOS sandbox, `claude -p` fails with "Not logged in" (keychain hidden). Codex-to-Claude must go through an MCP server Codex launches outside the sandbox, or an escalated sandbox | Borda/AI-Rig `bridge_cc-codex` docs; untested here |
| `stdin=DEVNULL` is mandatory for `codex exec` children, or the run hangs on "Reading additional input from stdin..." | wrapper comment + reproduced |

Gaps in the current `run_codex.py`:

- No `--json`: the session log holds only the final message, so it is useless midway.
- `session_id` is fabricated (`codex-<epoch>`); resume uses `--last` scoped by cwd, which
  collides when two runs share a directory. `last_run.json` holds one entry for all runs.
- stderr is kept in memory and truncated to 500 chars on failure only.
- State lives in `~/.codex/invoke-codex/`, another app's store.

## 3. Prior art read (code, not blurbs)

| Project | Transport | Background | Midway progress | Persistence | Resume | Direction |
|---|---|---|---|---|---|---|
| openai/codex-plugin-cc (official, Apache-2.0) | `codex app-server` JSON-RPC over stdio, optional Unix-socket broker | detached `task-worker`, job JSON | phase/threadId/turnId in job file + `.log` | JSON state + log under plugin data dir, capped 50 jobs | persistent named thread; `--resume-last` scoped to Claude session id, fallback `thread/list` by name prefix | Claude->Codex, plus `/codex:transfer` (imports Claude JSONL via `externalAgentConfig/import`) |
| FlineDev/TandemKit (MIT) | none of its own; drives `/codex:rescue` | Agent `run_in_background` + `wait-for-state.sh` watcher | file presence only | **one markdown file per turn** (`Claude-NN.md`, `Codex-NN.md`) + `State.json`, in-repo under `TandemKit/` | `/codex:rescue --resume` | Claude->Codex; two Claude sessions coordinate via `State.json` |
| Borda/AI-Rig `bridge_cc-codex` (Apache-2.0) | Claude->Codex: `codex exec --json --output-schema`; Codex->Claude: stdio MCP server running `claude --print --output-format json --json-schema` | detached supervisor (implement only) | job status only | compact JSON result inline, raw transcript `.txt`, `health.jsonl`, incidents | explicit `thread.started` id only; `--last` banned after it picked an unrelated thread (2.46M tokens) | both, with `CC_CODEX_BRIDGE_DEPTH` recursion guard and per-verb timeout budgets |
| scasella/claude-dynamic-workflows-codex | app-server `thread/start` + `turn/start` per agent call | `run_in_background`; JSONL journal in `.workflow-journal/` | `map-run.js --journal` renders progress | journal | n/a | Claude->Codex |
| fujibee/agmsg | shared SQLite (WAL) mailbox, bash + `sqlite3` | n/a | poll | SQLite | n/a | any agent; one base SKILL.md rendered per host (`/` vs `$` prefix) with `agents/openai.yaml` |
| GSD `gsd-review` (local) | `cat prompt.md \| codex exec --skip-git-repo-check -` | foreground, blocking, no timeout, no sandbox, `2>/dev/null` | none | `/tmp/gsd-review-*.md` merged into `{phase}-REVIEWS.md` with frontmatter, committed; raw output deleted | none | Claude->Codex; Codex-side copy calls `claude -p` |

Skill portability survey (CC Switch, Skillshare, ai-config-sync-manager, wshobson/agents,
Omnigent): **no tool translates** `!` inline shell, `$ARGUMENTS`, `${CLAUDE_SKILL_DIR}`,
`allowed-tools` or `disable-model-invocation` for Codex. They copy unchanged, drop fields,
or ship one hand-written file per host. The only Codex-side equivalents are
`agents/openai.yaml` (`policy.allow_implicit_invocation`) and the folder name as skill id.
"Omniant" does not exist; the match is Omnigent (Databricks).

## 4. Does cairn capture subagents today?

Claude Code (origin/main):

| Item | On disk | Exported |
|---|---|---|
| Subagent transcript | `<proj>/<sid>/subagents/agent-<id>.jsonl` (1071 files, 2.6 GB; parent transcripts 519 MB) + `agent-<id>.meta.json` (`agentType`, `description`, `toolUseId`, `spawnDepth`, `requestShape`, `model`) | **No**: `iter_session_files` / `find_session_jsonl` (`jsonl.py:55-79`) look one level deep; `sync_session` returns early on `agent-` prefix (`claude.py:333`); `extract_session_data` would drop every record on `isSidechain` (`claude.py:126`) |
| Agent prompt | parent `tool_use name=Agent input.prompt` | Yes (tool block) |
| Agent final report | parent `user` record, `origin.kind: task-notification`, `<result>` holds the full report | **Yes**, as a User turn |
| Subagent -> parent message (`SendMessage` from child) | `user` record, `isMeta: true`, `origin.kind: peer` | **Dropped** by `if not is_meta` (`claude.py:163-166`) |
| `Workflow` | per-agent transcripts under `subagents/workflows/wf_*/` + `journal.jsonl` | only the script's return value |
| `Bash run_in_background` | task-notification with status/summary | Yes; the /tmp output file is not captured |

Codex:

| Item | Finding |
|---|---|
| Multi-agent is real in 0.160 | `multi_agent` stable flag on; tools `spawn_agent`, `wait_agent`, `send_message`, `followup_task`, `list_agents`, `close_agent`; 108 subagent rollouts locally |
| Child rollout meta | `source.subagent.thread_spawn{parent_thread_id, depth, agent_path, agent_nickname, agent_role}`, `thread_source: subagent` |
| Inter-agent text | encrypted in 0.160 (`gAAAA...` blobs); the child rollout is the only plaintext (reasoning summaries, commentary, `phase: final_answer`) |
| **Bug 1** | forked child writes two `session_meta` lines (own, then parent); `parse_rollout` lets the last win (main `codex.py:106-107`, feat `:122-123`) so the child parses as the parent and dedupe drops it. **100 of 108 subagent rollouts are missing from the export.** Fix: keep the first `session_meta` only |
| **Bug 2** | `custom_tool_call` / `custom_tool_call_output` (code-mode `exec`, 6467 calls locally) have no handler; the 19-31-29 invoke-codex review run has 23 of them, export shows none |
| Gap | exported children have no parent link and title "Untitled Session"; `originator`, `effort`, `sandbox_policy` from `turn_context` are not captured |

## 5. Recommended design

Pattern A (exec `--json` wrapper) for runs, pattern C (markdown record) for the log,
resume by explicit thread id for follow-ups between turns. Defer app-server steering.

1. `cairn invoke codex|claude` top-level CLI command (`src/cairn/invoke.py`, stdlib).
   Harness adapters gain `exec_argv()` / `parse_result()` next to `resume()`.
   - Codex argv: `codex exec --json --skip-git-repo-check -s <sandbox> -m <model>
     -c model_reasoning_effort="<effort>" -C <dir> -o <answer.md> <prompt>`, child with
     `stdin=DEVNULL`, `start_new_session=True`, process-group kill on timeout.
   - Record `thread.started.thread_id` as the real `session_id`. Tee the event stream to
     `data_root/logs/invoke/<thread_id>/events-NN.jsonl` and stderr to `stderr-NN.log`.
   - `--resume <thread_id>` (explicit; never `--last`), run from the recorded cwd, re-assert
     sandbox. Keep `--resume-last` only as sugar over the recorded id.
   - Self-check model/effort against `models_cache.json` as today. Config defaults in an
     optional top-level `"invoke"` block (config as data).
   - After the child exits: targeted export of that rollout (reuse `hooks._process` path),
     `ensure_collection`, `qmd update`; then write link notes both ways
     (`cairn codex note --session-id <child> "[invoke] parent=claude:<id> round=N ..."`,
     `cairn claude note --session-id <parent> "[invoke] child=codex:<id> ..."`). Export
     must precede the note (`mutations.py:59-61`). Do not rely on SessionEnd: no evidence
     Codex fires it for `codex exec`, and the hook path is async anyway.
   - `cairn invoke status <thread_id>`: render progress from `events-NN.jsonl`
     (`item.completed agent_message` commentary, `command_execution`, `todo_list`,
     `turn.completed|failed`). This is the "read it midway" verb.
   - Recursion guard env var (`CAIRN_INVOKE_DEPTH`), per-verb timeout budget told to the peer.
2. Skills `skills/invoke-codex/SKILL.md` and `skills/invoke-claude/SKILL.md`, thin drivers,
   same on both hosts: no `${CLAUDE_SKILL_DIR}`, no `!` block, no `$ARGUMENTS`, no
   `when_to_use`; `agents/openai.yaml` with `allow_implicit_invocation: false`; host
   mechanics (background run + poll, user questions) in `references/harnesses/*.md`.
   `references/peer-review.md` generalises `quality-gates.md` (gate prompts, critical
   evaluation, "identify yourself as Claude <model>", parseable summary line, bounded
   convergence loop with stall detection, accept/dismiss/unresolved reporting).
3. Codex export head gains native fields: `originator`, `effort`, `sandbox`,
   `approval_policy`, `parent_session_id`, `agent_role`, `agent_nickname`, `depth`.
   Non-native links (parent Claude id, round) go in `comments` for v1, or a new preserved
   `links:` key (touches `_TAIL_KEYS`, `preserved_tail`, mutation tests).
4. v2 (optional): an `exchanges` collection with one document per thread, TandemKit style
   (frontmatter: `thread_id`, `parent_session`, `host`, `model`, `effort`, `sandbox`,
   `rounds`; body: per round prompt + final answer). Rationale: the rollout export is tool-call
   heavy; the debate itself is what gets searched later. Cost about 5 to 6 files, 250 lines,
   plus `config set` support for a new collection (`admin.py:189-193` rejects unknown).

Dependencies: items 1 to 3 depend on `feat/codex-support` (PR #28) for `references/harnesses`,
`plugin.json`, `.codex-plugin/`, and the `user.text` prompt fix. Branch after it merges.

## 6. Subagent coverage fixes (independent PRs, off main now)

| PR | Change | Size |
|---|---|---|
| A | Codex: first `session_meta` wins; test with a two-meta fixture; then `cairn codex export --all` recovers 100 sessions | 5 lines + test |
| B | Codex: handle `custom_tool_call` / `custom_tool_call_output` like `function_call` | ~30 lines |
| C | Claude: render `origin.kind == "peer"` records as agent-message turns instead of dropping as meta | ~15 lines |
| D | Claude: export subagent child documents next to the parent (`type: claude-subagent`, `agent_id`, `parent_session_id`, `tool_use_id`, `agent_type`, `description`, `workflow_run`); `## Subagents` index in the parent; `--since` uses newest mtime across parent + children; iterate `subagents/` and `subagents/workflows/*/` | 300 to 400 lines incl. tests |

Child documents, not inlining: one plantedex parent is 2.5 MB with 604 MB of children.

## 7. Open questions for the owner

1. Auto-invocation: explicit only (`allow_implicit_invocation: false`, matches setup), or
   let the model trigger a Codex gate on its own? A run costs 10 to 20 minutes.
2. Link storage: `comments` lines (zero schema change) or a preserved `links:` key?
3. Missing `models_cache.json`: refuse (fail loud) or warn and use the static list?
4. Codex-to-Claude direction: ship `invoke-claude` now and test the sandbox/keychain issue,
   or Claude-to-Codex first?
5. Does the `exchanges` collection (v2) earn its place, or are link notes enough?
