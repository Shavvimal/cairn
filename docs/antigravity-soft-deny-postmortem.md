# Post-mortem: two `cairn invoke antigravity` failures, 27 October 2026

Written from the run logs under `<data_root>/logs/invoke/antigravity/` and the cairn 2.7.1
source, on the same day. Engine: cairn 2.7.1, agy 1.4.8, billing route `gcp`. The task was a
plan review: a brief that named a plan file in another repository, run with `--profile
review` and `--cd <the repo under review>`.

## Summary

The first run failed in 2.4 s because the Application Default Credentials (ADC) refresh
token was stale (`invalid_rapt`, a Google Workspace session-length policy), and `cairn
doctor` only checks that the ADC file exists. The second run read a plan file outside the
agy workspace, agy soft-denied that one `read_file`, the whole turn stopped, and cairn
reported `status succeeded`, exit 0, with no answer. The second fault is a cairn engine
bug: the code that turns this case into `status: denied` read only the stream's tool
errors, and a soft deny leaves none.

## Timeline (UTC)

| Time | Command or event | Result |
|---|---|---|
| before 11:18 | `cairn doctor` | `ok   antigravity billing - route gcp via ADC, quota project example-quota-project` |
| 11:18:12 | `cairn invoke antigravity --json --prompt-file <brief> --profile review --cd <repo>` | exit 1 after 2.4 s, `thread_id null`, stderr `Error: authentication required. Run 'agy' to log in.` |
| 11:18:14 | agy log | `adcAuth: failed to get token from token source: oauth2: "invalid_grant" "reauth related error (invalid_rapt)"` |
| 11:33:21 | `gcloud auth application-default login` | new ADC file written |
| 11:33:48 | retry, same command | agy log `adcAuth: authenticated successfully`, `applyAuthResult: ... authMethod=adc` |
| 11:33:50 | the planner issues 9 tool calls in one response (6 `read_file`, 3 `run_command`) | |
| 11:34:10.590 | agy log | `Print mode: soft-denying tool confirmation "ReadFile" at step 2` |
| 11:34:10.610 | agy log | `CLI store manager shutting down` |
| 11:34:10 | result | `status succeeded`, `exit_code 0`, `last_message null`, `denied [{"tool": "read_file", "target": ""}]`, a `permissions:` warning and an `empty answer` warning |
| 11:34:53 | `--resume`, prompt says to read with `cat` | round 2 uses only `run_command` and answers |

## Root causes

### Failure 1: no usable credential

- Skill text said "`agy` needs a cached login". On route `gcp` that is the wrong
  credential: agy had no OAuth login even in the run that worked (`You are not logged into
  Antigravity.` in its log) and authenticated through ADC instead.
- `billing_problems()` checked only that the ADC file exists, parses and names a quota
  project. It never minted a token.
- Google rejected the ADC refresh token with `invalid_rapt`, the Workspace session-length
  policy for Google Cloud. It expires the token on a schedule, so this recurs.

### Failure 2: an empty answer reported as success

- The brief told the peer to read an absolute path outside `--cd`. Plan mode reads the
  workspace only; there was no `read_file(...)` allow rule, and `--grant-commands` does not
  write one.
- agy denied the first read (step 2, the plan file outside the workspace) and ended the turn
  20 ms later. The module docstring said "the turn goes on without it". The other five reads
  and the three git commands gave the model nothing: the transcript has no tool result for
  any of steps 2 to 10.
- `denied_outcome()` read only `replay.denied`, which holds tool steps that ended in state
  `ERROR` with a denial message. A soft-denied read ends `DONE` with no error. The fallback
  that copied the result's `denied_actions` into the round ran after the outcome was fixed.
  `adopt()` had the same gap. The only test fed a stream tool error.

The stderr line agy printed was right all along:

```
jetski: no output produced — a tool required the "read_file" permission that headless mode
cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in
settings.json (e.g. read_file(<target>)).
```

## Why doctor and the pre-spawn check missed the login

| Check | What it tests | Caught it? |
|---|---|---|
| `cairn doctor` billing line | ADC file exists, quota project set | No |
| pre-spawn billing check | same function | No |
| `agy models` | agy's own OAuth login | It failed, but it also fails when ADC is healthy, so it proves nothing on route `gcp` |
| `gcloud auth application-default print-access-token` | the refresh token mints an access token | Yes, with the same `invalid_rapt` |

## Lessons

- On route `gcp`, the login that matters is ADC, and only minting a token proves it.
- A headless agy denial can end the whole turn, so an empty answer with any denial is a
  failure, whether or not the stream shows an error (later runs on agy 1.4.8 showed a
  read denial with a stream error too; both shapes exist).
- Keep every file a peer must read inside its workspace, pass `--extra --add-dir <dir>`,
  or paste the text into the prompt.

## Resolution (cairn 2.8.0)

Reviewed as a plan by Codex (gpt-6-astra, high) before it was built; the review added the
`tail`, `status` and `adopt` paths, the merged refusal sources, the `--add-dir` roots in
the verdict, the retryable split for transport failures, the doctor warning for an
unverified token and the skill's model-selection fix, and cut a prompt scan for paths
outside the workspace.

1. A counted denial decides the outcome: `Replay.refusals()` merges the stream's tool
   errors, the soft denials agy's log names and one entry per action type in
   `denied_actions` the others did not name; the live runner, `adopt`, `tail` and
   `status` all decide `denied` from it, and an adopted denied round exits 2.
2. The refused read gets its path: agy's log says which step it soft-denied, the harness
   keeps the target of every step it saw, and the two are joined. `read_verdict()` judges
   the path against the read rules in force and the roots the round could read with no
   rule (the workspace and every `--extra --add-dir`), so the error and the resume note
   say what would make it readable.
3. The ADC token must mint before a `gcp` spawn: `adc_token_check()` runs `gcloud auth
   application-default print-access-token`; a rejected credential refuses the run (exit 2)
   and is a warn line in `cairn doctor`; gcloud missing, a timeout or a transport failure
   is a warning, never a refusal. `adcAuth: failed` in agy's log becomes the round's error
   text, not retryable for a rejected credential.
4. The skill text says which credential each billing route needs, that `agy models` can
   fail while a `gcp` run works, and that plan mode reads the `--cd` tree with no rule
   (any other path needs a `read_file(<dir>)` rule or `--add-dir`).
