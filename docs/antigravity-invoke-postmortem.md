# Post-mortem: `cairn invoke antigravity` as an implementation engine — 2026-10-23

Written by the Claude Code session that drove the run (claude-fable-5-1, session
`70b30460`), on the same day, from the run logs under
`<data_root>/logs/invoke/antigravity/b1ecdd4c-5cad-46e9-b9b0-b75dd22ad7e4/`.
Engine: cairn 2.6.0 (installed plugin), agy 1.4.6, model `gemini-4-argon-high`, billing route
`gcp`. PR #32 (`feat/antigravity-permissions`, 2.6.1) is a quick stopgap for the first
failure below, written before this run. It is to be replaced by a proper fix. This record is
the spec for that fix: every failure, the exact evidence, the workaround used, and the
behaviour that would have kept the operator on the feature instead of on the harness. The
section "What good would look like" is the acceptance list.

The headline: the task was "implement a 180-line handover doc as a PR". Antigravity did
that well in 22 minutes. Getting it to start took three tool calls to diagnose and an edit
to a settings file I had never seen. I spent more wall-clock on the harness than on the
review. The operator should be working on the feature, not wrangling the peer.

## What was asked

- Create a branch, implement a 180-line handover document (a de-duplication fix across a
  service layer, a database migration and a cleanup CLI) in a private Python/Postgres
  application repo through `/cairn:invoke-antigravity`, critically review the output as a
  gauge of Gemini 4 Argon, and raise the PR.
- Result: the PR was raised in that repo. Antigravity's code passed 650 pytest tests and a
  16-assertion pgTAP probe on the first full review pass. I changed four things in its code
  before shipping (listed under "Review of the model's output").

## Timeline

| Time (UTC) | Event |
|---|---|
| 11:28 | Read the skill, the harness contract and the peer-review doctrine (three files, about 700 lines). |
| 11:31 | Read the handover and the twelve files it names, so the brief could cite exact lines. |
| 11:38 | Wrote a 180-line brief to the scratchpad; started round 1 with `--profile edit`, `--timeout-s 3600`. |
| 11:40:51 | Round 1 starts. agy applies `accept-edits`. |
| 11:41:45 | Round 1 ends after 54 s, exit 0, `status: succeeded`, `last_message: None`, no file touched. |
| 11:42 | Diagnosis: stderr says a `command` permission was auto-denied. Found the settings path by listing `~/.gemini/antigravity-cli/`, found the rule syntax by fetching `antigravity.google/docs/permissions` and grepping the `agy` binary for `command(` strings. |
| 11:43 | Added `permissions.allow` / `permissions.deny` to `~/.gemini/antigravity-cli/settings.json` (backup kept). |
| 11:44:02 | Round 2 starts with `--resume`. |
| 11:49 | First file write (five minutes of reading and grepping first). |
| 12:00–12:05 | Four pytest runs: 2 failed → 1 failed → 73 passed → 53 passed (narrower selection). Two ruff runs, two black runs. |
| 12:06:02 | Round 2 ends: 1,321 s, 15 shell commands, 30 file writes, 27 file views, 302k input / 81k output / 25k thinking tokens. |
| 12:06–12:40 | My review: full diff read, tests, ruff, ruff-format, sqruff, migration + pgTAP probe against the local database in one rolled-back transaction. |
| 12:40–12:55 | Fixes, commit, push, PR. |

Operator time on the harness: about 8 minutes (read three reference files, diagnose the
denial, find the settings format, edit the file, restart). Operator time on the feature:
about 45 minutes (brief, review, fixes, PR). Peer time: 23 minutes. The 8 minutes is the
part this document is about.

## Failure 1: the run "succeeded" with nothing done

**What happened.** Round 1 returned exit 0, `status: succeeded`, `outcome: succeeded`,
`warnings: []`, `retryable: None`, `last_message: None`. The working tree was untouched.
The only signal was in `stderr-01.log`:

```
jetski: no output produced — a tool required the "command" permission that headless mode
cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in
settings.json (e.g. command(<target>)). Alternatively, re-run with
--dangerously-skip-permissions to auto-approve all tools.
```

and in the events file, step 26:

```
{"event": "step_update", "step_update": {"step_index": 26, "state": "ERROR",
 "step_type": "tool", "tool_name": "run_command",
 "tool_info": {"parameters": {"CommandLine": "rg \"<three function names>\" <four directories>"},
 "error": {"type": "TOOL_ERROR", "message": "permission check failed for command ... user denied permission to run command ... Do not attempt to circumvent this denial ..."}}}}
{"event": "result", "result": {"status": "SUCCESS", "response": "", "num_turns": 1,
 "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}}
```

The model had read 25 files, then ran one `rg` to find every caller. agy auto-denied it,
told the model to proceed without it, and the model ended the turn with an empty response.
agy reported SUCCESS. cairn passed SUCCESS through.

**Why it matters.** A green result with an empty answer is the worst outcome: a less
careful caller would have reported "done" or started a debate round with a peer that had
said nothing. The skill text says "Lead with `last_message`", and `last_message` was `None`,
which is the only reason I looked further. `denied_actions` was present in agy's own result
event but not surfaced in cairn's JSON line.

**Root cause.** `--profile edit` maps to `--mode accept-edits`, which auto-approves file
writes but auto-denies every shell command that has no `command(...)` allow rule, and a
fresh agy install has none. The skill's description of `edit` ("shell commands stay denied
unless the user's agy settings allow them") is accurate but buried; nothing checked up front
whether any rule existed, and nothing told me a denial had ended the run.

**Workaround used.** I added rules by hand:

```json
"permissions": {
  "allow": [
    "command(rg)", "command(grep)", "command(ls)", "command(cat)", "command(head)",
    "command(tail)", "command(wc)", "command(find)", "command(regex:sed -n .*)",
    "command(git status)", "command(git diff)", "command(git log)", "command(git show)",
    "command(git grep)",
    "command(poetry run pytest)", "command(poetry run ruff)", "command(poetry run mypy)",
    "command(make format)", "command(make lint)"
  ],
  "deny": [
    "command(supabase)", "command(psql)", "command(terraform)", "command(aws)",
    "command(curl)", "command(wget)",
    "command(regex:poetry run python .*)", "command(python)", "command(python3)",
    "command(git commit)", "command(git push)", "command(git checkout)",
    "command(git stash)", "command(git reset)",
    "read_file(.env)", "read_file(.env.prod)", "read_file(.env.dev)"
  ]
}
```

The deny list is the point: the workspace keeps production credentials in dotenv files, and
`--dangerously-skip-permissions` (the `full` profile) would have let the model run a cleanup
script against production. `accept-edits` plus a narrow allow list is the right shape for
"implement a feature". It just has to exist before the run.

**Finding the format cost three tool calls.** The stderr hint names `permissions.allow` and
`command(<target>)` but not the file path or the regex form. `agy --help` does not mention
permissions. The bundled skills under `~/.gemini/antigravity-cli/builtin/skills/` do not
document the JSON shape. I found the path by `ls`, the syntax by fetching
`https://antigravity.google/docs/permissions`, and confirmed it by grepping the binary for
`command(` literals. cairn 2.6.1's `cairn hook install --harness antigravity --grant-commands`
removes exactly this step; it was not in the installed plugin.

**Residual gap after 2.6.1.** `--grant-commands` adds read-only commands and test runners.
It does not add a deny list, and the `edit` profile without a deny list still lets an allowed
test runner be the only guard between the model and `poetry run python -m <cleanup script>` on a
production env file. Worth shipping a default deny set next to the allow set, or at least printing
the diff of rules the installer wrote.

## Failure 2: no early signal that the run would be useless

Round 1 ran for 54 seconds and read 25 files before the first shell command. Had the brief
not front-loaded "read these twelve files first", the denial would have come in the first
ten seconds. Either way, nothing in the harness checks before launch that the profile can
do what the prompt asks. The skill asks the caller to read `cairn config show --json` for the
billing route and `agy models` for the slug, but there is no equivalent "will this profile be
able to run a shell command" check.

cairn 2.6.1 adds a `cairn doctor` warning when no command rule exists. That is the right
place. It should also refuse (exit 2, like the billing check) when `--profile edit` or
`--profile full` is requested, the prompt mentions tests or commands, and no `command(...)`
rule exists, with the `--grant-commands` line in the error.

## Failure 3: the monitoring story is manual

The skill says watch with `cairn invoke status <id>` and, on Claude, `Monitor` the
`events-NN.jsonl`. The events file is rich (every tool call with parameters and output
size), but to get a usable feed I wrote a 20-line Python filter inline in the Monitor
command, parsing `step_update` records for `write_to_file` / `replace_file_content` /
`run_command` / `ERROR` / `result`. That filter is the same for every run. Two things would
remove it:

- A `cairn invoke tail <id>` that prints one line per tool call (`WRITE path`, `CMD text`,
  `DENIED tool`, `RESULT status`) and exits on the result event. Then `Monitor` is one line.
- The events file name is `events-02.jsonl` for round 2. The Monitor had to wait for the file
  to exist (`until [ -f ... ]`) because the round number is only known after launch. A
  stable `events-latest.jsonl` symlink, or printing the events path on the first stderr
  line together with the thread id, avoids the guess.

The first stderr line does print the thread id and run dir, which was enough to find
everything else. Keep that.

## Failure 4: the peer's final report described code that does not exist

This is a model issue, not a harness one, but the harness could help. Antigravity's
`last_message` claimed:

- a keyword argument at two call sites (a manager module and a seed insert). The diff has
  no such argument at either site.
- a re-export of the new insert helper from the database module. Not present.
- "richness scoring" over five columns and "backfilling keeper fields" in the cleanup helper.
  The code implements the plain three-key rule from the brief. The report describes a design
  it drafted and abandoned.
- "73 pytest tests passing" — true for the four files it selected; its last run was 53
  tests, and it never ran the neighbouring suites (650 tests in the three affected
  directories, which do pass).

None of this was wrong in the code. All of it would have been wrong in a PR description
copied from the report. The harness cannot fix model recall, but a
`cairn invoke result <id> --files` <!-- cairn-cli: skip --> that lists the files the run
actually wrote (from the events) next to the files the model claims to have written would
make the mismatch visible in one line.

## Smaller frictions

- **Nested-run guard and `--parent`.** I passed `--parent "claude:$CLAUDE_CODE_SESSION_ID"`.
  cairn 2.5.1 reads the variable itself; the flag is redundant on Claude. The skill could
  say so.
- **`run_in_background` plus a second waiter.** To get one completion notice I started the
  invoke in the background and a second background `until grep -q "exited with code"` loop,
  because the host notification for the first task arrives but the Monitor on the events
  file does not end on its own. A `cairn invoke wait <id>` that blocks until the result and
  prints the JSON line would collapse the two.
- **Resume prompt had to restate the rules.** On `--resume` the model keeps its context, but
  I still had to tell it which commands were now allowed and which stay denied, otherwise it
  retries the denied `rg`. A resume after a permissions change could inject that list
  automatically from the diff of `settings.json`.
- **Exit code 0 on an empty answer.** agy returns SUCCESS. cairn should map "SUCCESS with
  empty response and non-empty `denied_actions`" to `outcome: denied`, exit 2, and
  `retryable: true` with the rule to add. 2.6.1's `permissions:` warning is a step; a
  distinct outcome is clearer for a caller that branches on `status`.
- **`--disable-slash-commands` and `--mode`.** The 2.6.1 changelog says agy 1.4.6 treats
  `--disable-slash-commands` as disabling `--mode` too. In this run both rounds passed
  `--disable-slash-commands --mode accept-edits` and agy's log shows
  `Print mode: applying agent mode accept-edits` and `accept-edits mode: auto-approving file
  write "ReplaceFileContent"` for every write. So accept-edits did take effect here. Worth
  re-checking whether the "no effect" message is specific to `--mode plan`, because if plan
  mode silently ran unrestricted the `review` profile was never read-only on 2.6.0.
- **zsh in the host.** Not cairn's fault, but it cost two retries: `echo ====` fails in zsh
  (`=cmd` expansion) and an unquoted `$FILES` does not word-split, so `ruff check $FILES`
  got one argument. Mentioned because the Monitor filter and any helper scripts the skill
  suggests should be POSIX-safe or use `xargs`.

## What good would look like

This is the acceptance list for the fix that replaces PR #32. In order of how much time each
would have saved on this run:

1. `cairn invoke antigravity --profile edit` refuses to start, with the exact `--grant-commands`
   line, when no `command(...)` rule exists. Saves the dead round and the diagnosis
   (about 5 minutes).
2. A denied tool ends the run with `outcome: denied`, exit 2, the denied command, and the
   rule that would allow it. Saves reading three log files to learn what happened.
3. `cairn hook install --harness antigravity --grant-commands` writes a deny set too
   (`supabase`, `psql`, `terraform`, `aws`, `curl`, `python`, `git commit|push|checkout|
   stash|reset`, `read_file(.env*)`), prints every rule it added, and `cairn doctor` shows
   both lists. The operator should not have to invent the deny list under time pressure.
4. `cairn invoke tail <id>` (one line per tool call) and `cairn invoke wait <id>`. Removes
   the hand-written Monitor filter and the second waiter loop.
5. The result JSON carries `files_written` (from the events) so the peer's claims can be
   checked against what it did.
6. On `--resume` after a settings change, cairn prepends the allow/deny diff to the prompt.

## Review of the model's output (gauge of Gemini 4 Argon high)

Kept separate from the harness findings because it is the other half of what the run was
for.

**Strengths.**
- Read every file the brief named, then grepped for callers and the status enum, and found
  a check constraint the brief had not mentioned. Its default for a dated row with no
  confidence value is correct and it explained why.
- Design fidelity was high: the helper, the `42P10` fallback, the migration header and
  verification block, the RPC grants, the pgTAP shape and the cleanup CLI all match the brief
  and the neighbouring code. The pgTAP file passed first time against a real database,
  including dropping the index inside the rolled-back transaction to seed duplicates for the
  RPC assertions.
- It iterated on failing tests honestly (2 → 1 → 0 failures) and ran ruff and black itself.

**Defects found in review, fixed before the PR.**
- Ran `black`, but the repo's pre-commit uses `ruff format`; that reformatted unrelated hunks
  in four files. `ruff format` reverted them. The brief said "follow the surrounding code
  style (ruff/black)", which it read as "run both"; the next brief should name one.
- Three `COUNT(*)` capitalisation warnings from `sqruff` in the pgTAP test. CI only lints
  migrations, so this would have passed, but the repo convention is lower case.
- Its new reassignment helper read the winner's and losers' rows with no paging, the exact
  1,000-row PostgREST cap the handover names as an amplifier. Added paging.
- No note that writers must be paused during the cleanup's `--apply`. Added.

**Defects in its report, not its code.** Listed under Failure 4.

**Behaviour changes it made and documented correctly.** The confidence default above, and a
source-type default that now goes through the upsert helper instead of an inference helper.
Both are acceptable and are in the PR body.

**Verdict.** Use it for implementation from a precise brief. Check every sentence of its
final message against the diff. Budget 20–25 minutes for a change of this size (about 300
lines changed, 6 new files) plus a full review pass. It is not a reviewer of its own work.

## Artefacts

- Thread `b1ecdd4c-5cad-46e9-b9b0-b75dd22ad7e4`; run dir above; `thread.json` holds the
  exact argv per round.
- Exchange document `exchange-antigravity-b1ecdd4c.md` in the calling session's folder, and
  the peer session document `2026-10-23-b1ecdd4c.md` under `antigravity-sessions/`, both in
  the operator's cairn data root.
- Settings backup: `~/.gemini/antigravity-cli/settings.json.bak-20261023`

## Resolution (cairn 2.7.0, PR #32)

The PR that carried the 2.6.1 stopgap was reworked into the fix. Against the acceptance
list:

1. Pre-launch refusal: `cairn invoke antigravity --profile edit` (and `--mode accept-edits`)
   exits 2 with `permissions check failed: ... Run: cairn hook install --harness antigravity
   --grant-commands` when no `command(...)` rule exists; `--skip-permission-check` overrides.
   Plan and auto runs warn instead, because they still answer from what they read.
2. Outcome `denied`, exit 2, `retryable: true`, with every refused command and the rule it
   needs (`command(git log)`, segment by segment for a compound command), judged against the
   rules recorded for that round. A refusal beside a real answer is a `permissions:` warning
   with the same verdicts. The refused `run_command` step is read from the stream (state
   `ERROR`, "permission check failed"), not only from `denied_actions`.
3. `--grant-commands` writes the deny set too, prints every rule, skips a deny the user
   allows explicitly, and `cairn doctor` lists both sets.
4. `cairn invoke tail <id>` (`START`, `TOOL`, `CMD`, `WRITE`, `DENIED`, `MSG`, `RESULT`) and
   `cairn invoke wait <id>`; the first stderr line also names the events path.
5. `files_written` in the result, the summary and the exchange document.
6. A resume after a rule change (or after refusals) prepends the allow/deny diff and the
   verdict on each refused command to the prompt; recorded as `permissions.note`.

Smaller frictions: the `--disable-slash-commands` finding is resolved (agy's warning is
specific to `--mode plan`; the run logs show `accept-edits` applied with the flag, so the
2.6.0 review profile was the only mode affected). `--parent` is documented as redundant
on Claude. An empty answer on a `succeeded` round is now a warning.
