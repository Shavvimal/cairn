# Daily check

Read-only health check of the scheduled routines, recall coverage and overdue
decisions. It runs no peer round and changes no file. Every fix it proposes waits for
the user's yes, because `run`, `kickstart`, `install` and `resume` can start a peer
round that spends money.

## Contents

- Steps 1-6
- Acting on the status exit
- Report shape

## Steps

1. List the routines:

   ```bash
   cairn routine list --json
   ```

   Read `name`, `state`, `installed`, `pending` and `failed` of each row. If the array
   is empty, report "no routines configured" and go to step 4.

2. For each routine, read its status:

   ```bash
   cairn routine status NAME --json
   ```

   Read `exit`, `state`, `attention`, `code`, `paused`, `disabled`, `installed`,
   `last_run` (the parsed `last-run.json`: `status`, `failures`, `days`),
   `last_cost_usd` and `log_tail`. Exit 2 with nothing on stdout means the name is not
   in the config (`ROUTINE_UNKNOWN`).

3. Act on the exit with the table below. Run `cairn errors CODE` for the `code` value
   once per check and quote its next step.

4. Read the coverage report:

   ```bash
   cairn recall gaps --json
   ```

   Read `status` and each gap's `severity`, `message` and `fix`.

5. Read the overdue decisions:

   ```bash
   cairn recall decisions --overdue --all-projects --json
   ```

   Count the rows; keep the three oldest `ref` values.

6. On a Codex host, if step 4 shows `export-lag` for `codex-sessions`, run
   `cairn hook status --harness codex` and read `codex_trust` and the last request's
   state. On a Claude Code host, do the same with `--harness claude` for
   `claude-code-sessions`.

## Acting on the status exit

| Exit | State or attention | Meaning | Action (ask first where marked) |
|---|---|---|---|
| 0 | `fresh`, `running` | ran in the last 48 h, or runs now | report the last done day and `last_cost_usd`; nothing to do |
| 1 | `never_run` | no run yet | if `installed` is not `loaded`, propose `cairn routine install NAME` (ask); else propose `cairn routine kickstart NAME` (ask) |
| 1 | `stale` | no finished run in 48 h | same as `never_run`; launchd did not start it |
| 1 | `failed-day`, `attempts-exhausted`, `last-run-exit-<n>` | a day failed | read `last_run.failures` (day and reason, for example `validate-exit-1`) and `log_tail`; name the failing step; after the cause is fixed, propose `cairn routine run NAME --day YYYY-MM-DD` (ask); add `--force` only for `attempts-exhausted` |
| 1 | `stale-source` | a digest source was not exported in 24 h | propose `cairn sync` (ask) or adding the source to `routines.NAME.sources` |
| 1 | `launchd-missing` | the plist is gone | propose `cairn routine install NAME` (ask) |
| 1 | `interrupted-run` | the last run died mid-way | report it; the next run retries the day |
| 1 | `corrupt-stamp` | a stamp file does not parse | report the path from `paths.state_dir`; do not delete it |
| 2 | `paused` | `pause` wrote a marker | report `paused.reason` and `paused.paused_at`; propose `cairn routine resume NAME` (ask) |
| 2 | `disabled` | the breaker stopped it after 3 failed runs | report `disabled.reason`; fix the cause first, then propose `cairn routine resume NAME` (ask) |

Never retry a failed day in a loop. One proposed fix per routine.

## Report shape

    routine daily  fresh (exit 0)  last done 2026-10-29  cost 0.84
    routine notes  paused (exit 2)  since 2026-10-28: moving house
    **Gaps:** <as in the recall skill, Step 4>
    **Overdue decisions:** 2 (claude:3f2a9c1e#4, codex:0f1e2d3c#1)
    **Next action:** <one command, and whether it needs the user's yes>
