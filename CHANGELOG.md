# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.13.0]

### Added

- `cairn routine pause NAME --reason TEXT` and `cairn routine resume NAME`. A paused
  routine skips every run (exit 0, no stamp, no `last-run.json`) until resumed; `--force`
  does not bypass it.
- A routine-level breaker: after 3 consecutive failed runs with a counted failure the
  routine writes `disabled.json` and stops spending peer rounds; `resume` clears it.
- `routines.<name>.terminal_hook`: an argv list (absolute first element) run once per day
  stamp with a 10 s timeout, the day's report row on stdin and `CAIRN_ROUTINE_*` in its
  environment. Its failure is logged and changes nothing else.
- `routines.<name>.stale_sources` (`warn`, `refuse`, `ignore`) and
  `cairn routine run --stale-sources`: warn about, or refuse on, a digest source not
  exported in the last 24 h. A collection-wide `export` (no FILE argument) now writes a
  receipt to `<state_home>/cairn/exports/<source>-<key>.json`, keyed by its output
  directory. An export of one FILE and `sync` write none.
- `cairn routine run --json`: one run report with `schema_version` and `status`; the same
  object is `last-run.json` (the old keys are kept).
- `cairn routine status` names a state (`fresh`, `running`, `never_run`, `stale`,
  `paused`, `disabled`) and attention codes from files only; `list` shows the state;
  `doctor` reports a paused or disabled routine.
- `procs.run_capped` takes an `input` string for the child's stdin.

### Changed

- `cairn routine status NAME` exit codes: 0 fresh or running, 1 needs attention (never
  run, stale, a failed day, a non-zero last run, an interrupted run, a missing launchd
  plist, a stale source), 2 paused or disabled. It always exited 0 before, so a script
  that runs `status` under `set -e` now stops on a failed day.
- `stale-source` joins the reasons that cost no attempt.

## [2.12.0]

### Added
- `cairn hook install --harness codex` trusts the SessionEnd fallback it registers: on Codex
  0.165.x it writes the `[hooks.state."<key>"]` `trusted_hash` table in
  `$CODEX_HOME/config.toml` (backup in `config.toml.cairn-bak`), then asks Codex once more
  and removes the table if Codex does not report the hook as trusted. Other Codex versions
  keep the manual `/hooks` step.
- `--dry-run` for `cairn hook install|uninstall --harness codex` prints every planned write
  with its file, key or index and sha256, and writes no cairn file; install still asks
  Codex for its loaded hooks, and the Codex app-server can write its own state in
  `$CODEX_HOME` while it answers. `--no-trust` registers without trust.
- `$CODEX_HOME/cairn-hook-receipt.json` records a sha256 per entry cairn wrote.
- `cairn hook status --harness codex` reports `codex_trust`; `cairn doctor --harness codex`
  prints a `Codex hook trust` line (trusted, stale, missing, disabled or manual).

### Changed
- Reinstalling the Codex fallback replaces cairn's group in place, so later SessionEnd
  groups keep their index and their trust. If a person edited cairn's group since cairn
  wrote it, reinstall exits 1 and changes nothing.
- `cairn hook uninstall --harness codex` removes a group or trust table only while its
  sha256 equals the receipt, warns about changed entries and about index shifts.
- The XDG `codex-hook-registration.json` ownership file is migrated into the receipt and deleted.

## [2.11.0]

### Added
- Parser-drift guard. An export refuses to write a session whose source file holds
  records but renders no user or assistant message (`zero-messages`), whose lines do not
  parse (`unreadable-lines`) or, for a Codex rollout, has no session id
  (`no-session-id`). It prints `Error: parser drift: SOURCE:ID KIND: ...`, keeps the last
  good document and exits 1. A top-level session with no user turn is still written,
  with a `no-user-turns` warning. Findings persist in `<data_root>/exporter/findings.json`
  until a later parse succeeds. The hook worker records the same message as the request
  error.
- `cairn <source> export --json` prints one JSON object on stdout: documents written,
  findings and the spec target. Human lines go to stderr.
- Trailing-window gap check in `cairn sync --cron` / `--all` for Claude Code, Codex and
  Antigravity. Sessions changed in the last 30 days with no document are re-exported once
  and checked again. A gap that stays open fails `gap-check:<collection>`. A
  `[exporter-drift]` log line repeats while any finding is open.
- `cairn doctor` prints warn-only `exporter [SOURCE]` (spec target), `parser drift` and
  `gap check [COLLECTION]` lines.
- Every exported document carries `exporter_version` and `exporter_spec_target`.
- Invented per-source parser fixtures with an exact message-recall test.

### Changed
- An existing document with unparseable frontmatter now blocks its re-export with
  `Error: refusing to re-export SOURCE:ID: ...; fix or move it`, recorded as a finding.
  The export no longer aborts with a traceback.
- A document deleted by hand comes back on the next full sync while its source file is
  under 30 days old.

## [2.10.0]

### Added
- Every exported session document and every peer exchange document carries a
  `provenance` key in its front matter: `tool` (a session a person drove), `agent` (a
  peer run, a subagent or an exchange), `imported` (a Granola meeting) or `user`
  (hand-written). A session document carries it directly before `status:`, an exchange
  document directly before `title:`. Documents exported earlier get a value derived from
  their front matter when they are read; nothing is rewritten and no backfill is needed.
- `cairn recall list --provenance VALUE` and `cairn recall digest --provenance VALUE`
  (repeatable) keep only documents with that value. `--provenance tool` leaves out peer
  and routine runs.

### Changed
- `cairn recall list --json` and `cairn recall digest --json` rows carry `provenance` and
  `owner`. Text rows of both commands show the provenance value, and `cairn recall expand`
  prints a `Provenance:` line. Digest files carry `provenance` in their front matter.
- A present but empty `provenance` value warns and is derived again, as an invalid one is.
- The digest reads the `cairn invoke` preamble (role, parent, owner, provenance) only at
  the start of the first user frame with text. A preamble pasted later in a session no
  longer marks it as a peer or routine run.

## [2.9.0]

### Added
- `cairn recall digest DATE`: one UTC day of sessions condensed for a model to read, with
  provenance. Each session gives `<source>-<shortid>.md` (user text, assistant prose,
  one line per tool call, the head and tail of each tool output, no reasoning) and
  `<source>-<shortid>.frames.jsonl` (ordinal, UTC time, role, offset from the
  `## Conversation` heading, length, `sha1_8`, `in_window`, `kept` for every frame of
  the session). A `<!-- fN TS ROLE -->` marker cites frame ordinal `N`, the stable id;
  the `cairn:turn` markers stay length frames. The window must be one day.
  `--min-msgs` defaults to 1, so short sessions appear. `--max-chars` (default 40000)
  caps each body by whole frames, keeping user frames, then the tail, then the head.
  Output goes to `$XDG_CACHE_HOME/cairn/digests/DATE/` by default; `--out` refuses a
  non-empty directory without the `.cairn-digest` marker. A peer session from
  `cairn invoke` keeps the start of its prompt and one answer per round, and names its
  caller as `parent`. A legacy or corrupt export is skipped with a warning, never
  guessed, and `--json` lists both under `skipped`; exit 3 means an empty day (nothing written).
- `cairn recall frame REF ORDINAL [--frames FILE]`: prints one frame exactly as exported.
  With the sidecar it checks offset, length and hash, and on a change prints a warning
  on stderr and the current frame, exit 0.
- `cairn routine install | uninstall | run | list | status | kickstart`: a named job under
  `routines` in config that runs a prompt file through `claude` or `codex` once a day.
  launchd starts `cairn routine run NAME` at `at` (local time); each run handles every UTC
  day in the last `catch_up_days` (default 3) without a done stamp, oldest first: refresh
  the listed `sources` once (`--since` the ISO date before the oldest day), render the
  prompt, run `prepare`, run `cairn invoke <peer> --owner routine:NAME`, run `validate`.
  Template variables `{{DAY}}`, `{{DATA_ROOT}}`, `{{ATTEMPT_DIR}}`, `{{DIGEST_DIR}}` and
  one `{{KEY}}` per `outputs` key; an unknown one exits 2 with no stamp. Exit 0 from
  `validate` stamps the day `done`, 3 stamps it `empty`, anything else fails it; `prepare`
  exit 3 stamps `empty` and skips the peer. A day is done only after `validate` passes. A
  failed day is retried up to 3 attempts, and a run stops at its first failed day.
  `budget_usd_per_day` (Claude only) caps a day's spend through `--max-budget-usd`.
  `run --dry-run` prints every argv and the rendered prompt; `run --day D --force` re-runs
  one day. Stamps live under `$XDG_STATE_HOME/cairn/routines/NAME/`, each attempt under
  `<data_root>/routines/NAME/DAY/attempt-N/`, the log at
  `<data_root>/logs/routine-NAME.log`. `kickstart` leaves a running instance alone unless
  `--force-restart`. launchd only (macOS): on another OS `install` prints the crontab line
  and exits 2. The config template ships `example-daily`, which runs nothing until
  installed. The workflow is `skills/cairn/workflows/routine.md`.
- Config profiles: `invoke.claude.profiles` and `invoke.codex.profiles` hold named profiles,
  each a built-in `base` (`review`, `edit`, `full`) plus `extra_args` emitted after the
  base's flags. A profile cannot drop its base's deny rules or switch to a looser
  permission mode; flags cairn sets itself are refused. Claude: one `--permission-mode`
  (the profile's replaces the base's), `--disallowedTools` appends to `review`'s list,
  `--allowedTools` and `--disallowedTools` take one comma-joined string, and `--settings`,
  `--mcp-config`, `--strict-mcp-config` and `--permission-prompt-tool` need base `full`. Codex:
  only `--add-dir DIR` and `-c KEY=VALUE` pairs; a resume drops `--add-dir` with a warning.
  A resumed thread reuses the recorded profile args, not today's config. An explicit
  `--permission-mode` or `--sandbox` replaces only the mode of a config profile.
- `cairn invoke --profile-args-file PATH`: plumbing for `cairn routine run`, a JSON array of
  rendered args that replaces a config profile's `extra_args` for one round. Plain `cairn
  invoke` refuses a profile whose args hold a `{{NAME}}` token.
- `cairn invoke --owner LABEL`: an owner label on the thread, each round, the result line,
  the exchange document and the peer session's front matter (`owner:`, kept across
  re-exports). A resume keeps the recorded owner and refuses a different one.
- The `journal` collection in the config template: a store-less collection that `cairn
  sync` registers as `<data_root>/journal` once a routine has written markdown there.
  `config show` labels a store-less collection other than `service-docs` as `notes`.
- `cairn config unset KEY` for `routines.<name>`, an optional routine field, a routine
  output and `invoke.<peer>.profiles.<name>`. It re-validates the file and restores it on
  failure. Unsetting a routine reminds you that its launchd job stays until `cairn routine
  uninstall`.

### Changed
- `--profile` and `invoke.<peer>.profile` accept a config profile name next to the
  built-ins.
- `cairn config set` takes JSON objects for `routines.<name>` and
  `invoke.<peer>.profiles.<name>`, JSON arrays for argv fields (`prepare`, `validate`,
  `extra_args`), and a JSON array or comma list for `routines.<name>.sources`.

### Fixed
- The recorded background environment (cron line, hook launcher, Codex and Antigravity hook
  registrations, a routine's launchd job) no longer pins `CLAUDE_CONFIG_DIR` and
  `CODEX_HOME` unless the host set them. With `CLAUDE_CONFIG_DIR` exported, headless
  `claude -p` read `<dir>/.claude.json` instead of `~/.claude.json` and reported `Not
  logged in`. An existing install picks up the change on the next `cairn hook install` and
  `cairn cron install`.

## [2.8.1]

### Changed
- The Claude Code update instructions (README, the Claude harness reference) are one
  command, `/plugin marketplace update cairn`: in a session it refreshes the marketplace
  and updates the plugins installed from it, and closing the panel reloads them.
  `/plugin update cairn@cairn` on its own reinstalls the version the marketplace last saw.

## [2.8.0]

The fix for the two dead rounds in `docs/antigravity-soft-deny-postmortem.md`: a headless
`agy` run on billing route `gcp` whose Application Default Credentials file existed but whose
refresh token Google had expired, and a plan-mode run that was soft-denied one `read_file`
outside the workspace, ended with no answer and still reported success.

### Added
- `cairn invoke antigravity` on route `gcp` probes the ADC token before the spawn
  (`gcloud auth application-default print-access-token`, token discarded): a rejected
  credential (`invalid_rapt`, the Workspace session-length policy, which gcloud with no
  terminal reports as "Reauthentication failed. cannot prompt during non-interactive
  execution") refuses the run with exit 2 and the login command; gcloud missing, a timeout or a transport failure is a
  `billing:` warning, never a refusal. `cairn doctor` runs the same probe (up to 20 s): `token ok`, a
  warn line for a rejected credential, or `token not verified` with the reason. A run agy
  itself could not authenticate names the cause from agy's log (`ADC token refresh
  failed: ...`), not retryable for a rejected credential.
- A refused `read_file` names its path: agy's log says which step it soft-denied and the
  harness joins that to the path the stream showed. The `denied` outcome's error, the
  `permissions:` warning and the resume note judge it against the read rules in force
  (deny beats ask beats allow, and an `ask` rule denies headlessly; `read_file(<dir>)`
  covers its subtree; a relative target is workspace-relative; `write_file` grants read; a
  bare target matches the file name) and the roots the round could read with no rule (the
  workspace and every `--extra --add-dir`), which the round records as `permissions.roots`:
  `now readable (inside <dir>)`, `still denied by read_file(.env*)`, or `still outside the
  workspace with no read_file rule`.

### Fixed
- A soft-denied tool call ended the turn with an empty answer and the round reported
  `status succeeded`, exit 0, `last_message null`. agy may show no tool error for a soft
  deny; the result's `denied_actions` always counts it, and cairn read that count after
  the outcome was fixed. The outcome now counts it, in the live runner, in `adopt`, in
  `cairn invoke tail` (which follows a live runner to its record, adopts the round when
  both processes are gone, and holds for another reader's adoption before classifying the
  stream itself) and in `status` (a finished round's record is authoritative, so no progress
  message poses as the answer of a denied round). An adopted `denied` round exits 2 like
  a live one. Refusals merge the stream's tool errors, the log's soft denials and one
  entry per counted action type the others did not name.
- `cairn invoke wait` on a round that another reader is adopting (both processes gone,
  the thread lock held, no `finished_at`) holds for that reader's record up to the peer's
  `quiet_after_s`, then reports `status: busy` (exit 75) with the reason; it never builds a
  result from an unfinished round.
- The docstrings and the skill said the turn goes on after an auto-denied call. Live runs
  show both shapes (a refusal with a tool error in the stream, a soft deny with none) and
  both can end the turn; the text says so.

### Changed
- The `invoke-antigravity` skill says which credential each billing route needs (ADC on
  `gcp`, agy's own login on `plan`), that `agy models` can fail while a `gcp` run works,
  and that plan mode reads the `--cd` tree with no rule: a file outside it goes inside, through
  a `read_file(<dir>)` allow rule,
  `--extra --add-dir <dir>` (last on the line, repeated on every resume) or pasted into
  the prompt.

## [2.7.1]

### Fixed
- `cairn invoke`: when a peer reports its thread id, the run directory is renamed from
  `pending-<uuid>/` to the id. `thread.json` was rewritten with the id after the rename, so
  for an instant the directory existed under its final name with a null `thread_id` beside a
  running round; a reader without the thread lock (one CI run was that reader) could see it.
  The file is now rewritten while the directory is still pending, so it is coherent the
  moment it appears.

## [2.7.0]

The fix for the dead round in `docs/antigravity-invoke-postmortem.md`: a headless `agy`
run that was refused one shell command, gave up with an empty answer and still reported
success, after an 8-minute detour through settings files and hand-written log filters.

### Added
- `cairn invoke antigravity` refuses an `accept-edits` run (`--profile edit`, `--mode
  accept-edits`) before anything is spawned when agy has no `command(...)` allow rule
  (exit 2, `permissions check failed`, the `--grant-commands` line in the message), like
  the billing check. Plan and auto runs still start and carry a `permissions:` warning.
  `--skip-permission-check` starts an edit run that needs no shell.
- Outcome `denied` (exit 2, `retryable: true`): a round whose peer refused tool calls for
  want of a permission and ended with an empty answer. The `error` names each refused
  command and the exact rule it needs (`command(git log)`, judged against the rules
  recorded for the round; a compound command lists its unmatched segments). A refusal
  beside a real answer stays `succeeded` with a `permissions:` warning that carries the
  same verdicts. The result and `thread.json` carry `denied` (tool and target per
  refusal) and `permissions` (the allow and deny rules in force when the round started).
- `cairn hook install --harness antigravity --grant-commands` now also writes a deny set
  (`supabase`, `psql`, `terraform`, `aws`, `gcloud`, `kubectl`, `curl`, `wget`, `python`,
  `python3`, `git commit|push|checkout|stash|reset`, `read_file(.env*)` by name), prints
  every rule it added, and skips a default deny whose target the user allows explicitly
  (deny beats allow in agy). `cairn doctor` lists both rule sets.
- `cairn invoke tail <id>`: one line per event of the last round (`START`, `TOOL`, `CMD`,
  `WRITE`, `DENIED`, `MSG`, `RESULT <outcome>`), replayed from `events-NN.jsonl` and
  followed until the final event; `--round N`, `--no-follow`, `--json`. Exit 0 on
  `succeeded`, else the round's exit code. Works for every peer.
- `cairn invoke wait <id>`: block until the last round exits, print the same result line
  the runner printed (from `result-NN.json`; an abandoned round is adopted first) and exit
  with its code. One completion handle instead of a second waiter loop.
- The result carries `files_written`: every path the peer's write tools finished, from the
  event stream (antigravity; `null` for peers that do not report writes), so a peer's
  claims can be checked against what it did. The human summary and the exchange document
  list them; progress bullets read `run: <cmd>`, `wrote: <path>`, `denied: <tool> <target>`.
- On `--resume`, cairn prepends a permissions note to the prompt when the agy rules changed
  since the previous round or that round had refusals: the rules added and removed, and
  for each refused command whether it is now allowed, still denied by a rule, or still
  unmatched (so the peer does not retry it). The note is recorded as `permissions.note`.
- The first stderr line now carries the events path next to the run directory.
- A `succeeded` round with no final message carries an `empty answer` warning.

### Fixed
- `cairn invoke antigravity` passed `--disable-slash-commands`, which agy 1.4.6 refuses to
  combine with `--mode plan` ("--mode plan has no effect while slash command expansion is
  disabled"), so the review profile ran without plan mode. `accept-edits` was unaffected
  (the run logs show it applied). The flag is no longer passed; the prompt starts with
  cairn's preamble and cannot expand as a command.
- A tool step agy ends in state `ERROR` (how a refusal is reported) was ignored; it now
  yields the `denied` event, or a `tool failed` bullet for any other error.

## [2.6.0]

### Added
- `cairn invoke antigravity`: run Google Antigravity's `agy` CLI headlessly as a peer, the
  way `codex` and `claude` already run. Verified against agy 1.4.6: `-p=<prompt>` with
  `--output-format stream-json`, the conversation id captured from the `init` event,
  `--resume` continuing the same conversation with `--conversation`, large prompts through
  `--input-format stream-json`, and `--json-schema` for structured answers. Profiles map
  to `--mode plan` (review: writes and shell commands deferred), `--mode accept-edits`
  (edit) and `--dangerously-skip-permissions` (full); `--mode` passes a native mode
  directly. An unknown model or a missing login is reported as not retryable. Defaults
  live under `invoke.antigravity` in the config.
- Skill `invoke-antigravity` for both hosts.
- Source adapter `antigravity` (`cairn antigravity export|list|context|note|close|log|resume`,
  collection `antigravity-sessions`): every agy conversation exports from the JSONL
  transcript agy keeps under `<app data>/brain/<id>/.system_generated/logs/`, with the
  title, workspace, run status and lineage from `conversation_summaries.db`. User prompts
  lose agy's wrapper blocks, thinking is folded, tool results are paired with their calls,
  system messages render as `### System`, subagent conversations become
  `antigravity-subagent` documents in the parent's folder, and the agent's artifacts are
  listed. Peer runs (`cairn invoke antigravity`) now export and link the conversation like
  Codex and Claude peers.
- `cairn hook install|uninstall|status --harness antigravity`: a cairn-owned `Stop` hook in
  the global `~/.gemini/config/hooks.json` (agy has no SessionEnd; `Stop` fires per turn).
  `cairn hook handle --harness antigravity` queues the export and always answers `{}` so it
  never blocks agy. Link references accept every registered session source.
- `invoke.antigravity.billing` (`route`, `project`, `location`): which credits a headless
  `agy` run bills. `inherit` (default) passes the host environment through; `plan` strips
  `AGY_ADC_AUTH` so the consumer plan quota pays; `gcp` sets `AGY_ADC_AUTH=true` and the
  `GOOGLE_CLOUD_QUOTA_PROJECT` / `GOOGLE_CLOUD_LOCATION` exports, refuses to spawn (exit 2)
  unless the Application Default Credentials file and a quota project exist, and after the
  run reads agy's own log (`--log-file`, kept as `peer-NN.log`) to record the auth method,
  quota project and request projects as `billing.observed`; a mismatch is a `billing:`
  warning. `cairn invoke antigravity --billing ROUTE` overrides one round,
  `cairn config set invoke.antigravity.billing.route gcp` sets the machine default, and
  `cairn doctor` reports whether the `gcp` route is ready.

### Changed
- Publication steps are decided per peer: a peer whose collection is not configured has no
  session document to export or link, so those steps are not applicable to its rounds
  (one warning in the result) instead of pending forever. With a known caller the exchange
  document still lands in the caller's folder and is indexed; without one, only the answer
  is kept under the run directory.
- Harness adapters own the bytes a large prompt takes on the peer's stdin
  (`stdin_payload`), and the recorded argv masks a prompt glued to a flag.

## [2.5.1]

### Fixed

- Claude Code exports `CLAUDE_CODE_SESSION_ID` to every tool process, not
  `CLAUDE_SESSION_ID` (that name is only a template variable substituted into skill
  text). cairn read the wrong one, so under Claude Code `cairn invoke` never detected
  its caller without `--parent` (the exchange document then landed in the Codex session
  folder with no `links:`), and `cairn claude note|close|log` could not infer the
  current session. The Claude adapter now reads `CLAUDE_CODE_SESSION_ID` first and
  still accepts a hand-set `CLAUDE_SESSION_ID`; conflicting values are refused like
  the Codex pair.
- The test suite scrubs host identity variables, so running it from inside a Claude
  Code session no longer leaks that session into subprocess tests.

## [2.5.0]

### Upgrading from 2.4.0

Upgrade the engine (`uv tool upgrade cairn`) and the plugin, then run
`cairn claude export --all` and `cairn codex export --all` once. The export schema is now 2:
the full run re-renders every session document with the new head fields and, for the first
time, exports subagent transcripts as child documents. On a machine with a year of history
this is a one-time cost of a few minutes plus the embedding of the new child documents on
the next `cairn sync --cron`. Windowed cron runs never re-export old documents.

### Added
- `cairn invoke codex|claude`: run the other agent headlessly from either host, stream its
  events to a per-thread log, capture the real thread id, continue the same thread later
  with `--resume`, read progress mid-run with `cairn invoke status`, cancel with
  `cairn invoke cancel`, and finish pending publishing with `cairn invoke publish`. Defaults
  per peer (model, effort, profile, timeouts) live in the config `invoke` block.
- Every peer run exports the peer session at once, writes `links:` on both session
  documents, and records an exchange document (prompt, progress, answer and result per
  round) in the calling session's folder, so `/search` and `rg` find it (session recall
  lists the peer session; exchange documents stay out of the session timeline by design).
- Health for long-running work: execution state, activity with age, and outcome are
  reported separately; an idle watchdog kills a hung run; one invocation per thread is
  enforced; a runner killed mid-run leaves an orphaned round that `status` adopts.
- `cairn agents list|wait`: one view over the peer runs and the Claude and Codex subagents
  a session spawned, and a blocking wait for a whole fan-out.
- Claude Code subagent transcripts (`<session>/subagents/agent-*.jsonl`, including
  workflow agents) export as `claude-subagent` documents in the parent's folder with the
  agent's type, description, tool use id, spawn depth, request shape, model and lineage;
  parents gain a `## Subagents` index; child-to-parent messages render as Agent turns.
- Codex subagent rollouts export as `codex-subagent` documents with parent thread, role,
  nickname, path and depth; rollouts carry `originator`, `thread_source`, `effort`,
  `sandbox` and `approval_policy`; `custom_tool_call` records render like function calls;
  inter-agent messages render as Agent turns marked encrypted.
- `cairn recall list --include-subagents`; JSON rows carry `type` and `parent`.
- Skills `invoke-codex` and `invoke-claude` for both hosts, `references/peer-review.md`
  (gate prompts, critical evaluation, the bounded convergence loop), and peer-run recipes
  in the harness references.
- `links` is a preserved frontmatter field.
- `cairn doctor` probes the configured QMD: `qmd --version` (warns below the tested 2.5.3
  or on a prerelease) and `qmd status` as a runtime check (a binary that crashes opening
  its index is now a critical failure). The embeddings line reports documents, vectors and
  pending documents. `cairn doctor --strict-index` fails unless the index is readable and
  fully embedded; an empty index passes but says nothing can be verified.
- Full sync logs a `[qmd-verify]` line with the index-wide counts after `qmd embed`, and
  a `failed steps:` line naming every failed step before `=== FAILED ===`.

### Fixed
- Sync reported `[qmd-embed] ok` when QMD exited 0 with failed chunks (seen: 48,219 of
  131,291 chunks after QMD's 30-minute embed session expired). The whole embed output is
  now checked for failed-chunk reports and chunk errors, and the run fails. A clean exit
  logs `[qmd-embed] exited 0`; `[qmd-verify]` gives the verdict. Pending documents after
  the embed fail the run as `INCOMPLETE` (`failed steps: qmd-verify (incomplete)`), and a
  non-empty index with no vectors, an unreadable `qmd status`, or a `Pending` line in an
  unknown shape also fails it.
- Two `cairn sync` runs could overlap (an hourly cron run during a manual one) and
  interleave their log blocks. A per-data-root sync lock now lets one run at a time. The
  other records its mode under `<data_root>/sync-requests/`, logs one `skipped` line and
  exits 0; the running sync then runs, once, the stages the request needs that it did not
  run (a `--cron` request after a legacy `--hook` run gets the worker retry, the other
  exports, update, embed and verify; a full request after a full run gets a hook-worker
  retry). Requests that arrive during that pass wait for the next invocation.
- A timed-out sync step, hook export or `qmd update` killed only its direct child, so the
  Node grandchild behind the `qmd` launcher could keep running outside the index lock. The
  whole process group is now killed (also on Ctrl-C). A timeout keeps the partial stdout
  and stderr even when a descendant escaped the group, and process cleanup is bounded
  (`cleanup incomplete` is reported instead of an unbounded wait).
- Full sync no longer runs `qmd embed` after `qmd update` failed.
- The test suite wrote catalogs and lock files into the real home (`~/.cache/cairn`,
  `~/.local/state/cairn`); CI now fails if it does.
- Codex: a forked subagent rollout carries two `session_meta` records; the parser let the
  last one win, so the child was parsed as its parent and dropped by the fragment dedupe.
  The first record now wins and the replayed parent history is cut. On the author's
  machine this recovers 100 of 108 subagent rollouts.
- Codex: `custom_tool_call` and `custom_tool_call_output` records were silently skipped.
- Claude: messages a subagent sent to its parent were dropped as meta records.

### Changed
- Export schema 2; see Upgrading.
- `cairn doctor` no longer says "All critical checks passed." without a qualifier when the
  semantic index is empty, unreadable or has pending documents.
- Setup verifies keyword, vector and hybrid search separately and documents QMD
  compatibility and recovery without touching QMD's database.
- `cairn resume` refuses a subagent transcript and names the parent to resume instead.

## [2.4.0]

### Upgrading from 2.3.0

Run the [setup skill](skills/setup/SKILL.md) again to follow its
[update workflow](skills/setup/references/updating.md). It upgrades the engine first,
then the plugin, using the relevant host adapter. Afterwards it runs
`cairn hook install --harness claude` (or `codex`) so
the SessionEnd hook records the engine's interpreter and config. Until then the
bundled launcher falls back to `cairn` on PATH. If the plugin updates before the
engine, the launcher detects the 2.3.0 engine, runs the legacy `cairn sync --hook`
detached, and prints the upgrade command to the hook log.

Keep the existing Markdown store and QMD database; no full rebuild is required.
Legacy metadata remains readable and upgrades on export when native data is available.
For older history, optionally export each enabled source with `--all`, then run
`cairn sync --cron`. Rewritten documents need their embeddings refreshed; unchanged
documents do not. See the setup skill's update workflow for preservation and
unavailable-history behavior.
Reinstall hooks and the existing cron entry to record the complete QMD environment.

### Added
- Hooks and cron retain the selected QMD configuration, cache and database paths,
  including effective defaults, instead of inheriting a different host index.
- Index writers coordinate on canonical configuration and database resources, even
  when environment aliases or different cache directories refer to shared state.
- Collection discovery and registration time out after 30 seconds; failed hook jobs
  release their locks and can be retried.
- Legacy Claude `agent-*` sidechains are intentional skips, not export failures.
- Legacy date-only records retain their recorded day label across machine timezones.
- Structurally invalid catalog caches rebuild from Markdown instead of crashing.

- Shared Claude/Codex harness adapters, portable packaging, explicit Codex onboarding,
  and discovery-based lifecycle registration with trust diagnostics.
- Durable session exports queued to a detached worker; failures get at most three attempts,
  index updates are serialised, and embedding stays out of the callback. Completed and
  disabled requests and exhausted failures are pruned seven days after becoming terminal.
  `cairn hook retry SOURCE:FULL_SESSION_ID` explicitly resets a failed request's attempt
  budget; `cairn hook status`
  shows every actionable request plus the ten most recent completed ones (`--all`).
- Recall of exported conversations across sources with source-qualified IDs, UTC activity
  filtering, schema migration, bounded expansion, and safe concurrent annotations.
- Native resume command selection, host-aware diagnostics, and shared skill references.
- A stat-validated session catalog (`$XDG_CACHE_HOME/cairn/catalogs/`) so session lookup,
  `list` and `recall` cost one directory walk instead of parsing every exported file.
  The catalog is a derived cache: entries are trusted only while the file's
  mtime/ctime/size/inode match, and a missing or corrupt catalog is rebuilt.
- A POSIX `sh` hook launcher (`hooks/launch.sh`) that never depends on the macOS
  `/usr/bin/python3` stub.
- Resource locks under `~/.local/state/cairn/locks/` keep lock files out of Markdown
  and QMD directories while coordinating shared canonical paths across profiles.
- Community health files: `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue and pull
  request templates, and Dependabot configuration.
- CI: a `version-bump` gate that requires `__version__` to change when
  `src/cairn/` is modified.
- Live verification and outstanding desktop/skill-evaluation acceptance recorded in
  `docs/codex-acceptance.md`.

### Fixed
- Hook and cron index updates create the QMD database and config directories they
  pin, so the first job on a machine where QMD has never run no longer fails.
- A code fence left open in `## My Notes` no longer swallows the conversation that
  follows it, which made every export append another copy of the conversation.
- Granola notes and summaries containing `## Conversation` no longer cause
  handwritten `## My Notes` content to be discarded on re-export.
- Recall expansion preserves embedded CRLF line endings so message limits remain
  accurate for existing exports. Annotation edits preserve transcript bytes and
  their turn lengths as well.

### Changed
- The setup skill handles existing installations through a dedicated update workflow;
  the README points users there for upgrades and optional historical metadata backfill.
- Exported frontmatter records `activity_dates` (unique UTC days) instead of one line
  per message timestamp; the full timestamps remain in each turn header. Files written
  by the pre-release branch with `activity_timestamps` are still read.

## [2.3.0]

### Added
- First-class ripgrep search in the skills. `/search` gets an rg engine (`--exact`,
  or chosen automatically for identifiers, error strings, paths, session IDs and
  regex), and `/recall` topic mode runs an exact-match rg pass next to the BM25
  searches. rg reads the markdown directly, so it also finds files that QMD has not
  indexed yet.
- The skills resolve each collection's directory from `qmd collection show`, so rg
  also covers collections outside `data_root`. No path is hardcoded.
- Output guardrails for rg in both skills: list files first (`-l`, newest first via
  `--sortr modified`, capped with `head`), then print lines for chosen files only with
  `-M 300 -m 5`. A session transcript can hold a single line of several megabytes.
- `cairn doctor` checks for `rg` on PATH. A missing rg is a warning, never a failure.
- `/cairn:setup` preflight reports `rg` and offers to install it (optional).

## [2.2.0]

### Added
- The plugin manifest version is now locked to the Python package version:
  `.claude-plugin/plugin.json` declares `version` equal to `cairn.__version__`,
  enforced by a test (and `release.yml`) so the plugin and engine can never drift
  apart in the repo. One release is one version across both artifacts.
- `cairn doctor` detects plugin/engine drift: given the plugin manifest
  (`--plugin-manifest`, or `$CLAUDE_PLUGIN_ROOT` when run from the plugin) it compares
  the installed engine version to the plugin version and tells you to
  `uv tool upgrade cairn` (or `/plugin update`) when they differ.

### Changed
- The CI `version-bump` gate now fires for any user-facing change - the engine
  (`src/cairn/`) **or** the plugin (`skills/`, `hooks/`, `.claude-plugin/`,
  `commands/`, `agents/`) - so plugin-only changes also bump the shared version (and
  the plugin cache actually refreshes for users).
- `service-docs --version` now reports the cairn package version instead of a stale
  hardcoded `1.0.0`.

## [2.1.0]

### Added
- Self-healing QMD collection registration: `cairn sync` now idempotently runs
  `qmd collection add` for every enabled collection that has markdown on disk
  (new `qmd-collections` step, before `qmd update`, in all modes). A fresh install
  no longer needs the manual `qmd collection add` step, and sources that produce
  data later (Cursor/Codex/Granola) register automatically on a subsequent sync.
- `cairn doctor` now checks that enabled collections are registered in qmd and that
  the index has embedded vectors - so a green doctor reflects a searchable index,
  not just wired-up plumbing. Both are warnings (a fresh install is legitimately
  empty until the first embed).
- `cairn.qmd.list_collections()` and `cairn.qmd.ensure_collection()` helpers.
- `cairn config add-service-doc NAME PATH [-d DESC]` and `cairn config
  remove-service-doc NAME` to configure the `service-docs` collection's folders from
  the CLI (previously `service_sources` could only be set by hand-editing JSON). The
  add command validates the path exists and clears the bundled placeholder on first
  use. `cairn config show` now lists each service-docs folder.
- `/cairn:setup` now gives service docs their own question (separate from the session
  sources) and prompts for the folders to mirror, instead of bundling service-docs on
  the same checkbox as granola.

### Changed
- The first `qmd embed` during `cairn sync --cron`/`--all` may download ~2GB of
  models; its timeout is raised from 600s to 3600s so an unattended first run can
  finish.
- `/cairn:setup` now bootstraps collections and runs the first embed (watched via
  the Monitor tool) and verifies a real query returns results before declaring
  success - it no longer stops at a green `cairn doctor`.

## [2.0.0]

- Repackaged as an installable CLI (`src/` layout, hatchling) and a Claude Code
  marketplace plugin (skills + SessionEnd hook).
