# `cairn routine`: implementation spec

Status: implemented on branch `feat/routine-and-digest` (PR 37), version 2.9.0. Section 14
holds the maintainer answers; where an answer differs from an earlier section (6.2.2 strips
the depth variable, 6.6 names `--kill`), the answer wins.

This document specifies a generic feature: run a prompt file through a peer agent on a
schedule, gate the result with a consumer-supplied validation command, and record
per-day state. It also specifies the four supporting changes the feature needs:
config-defined permission profiles, `cairn invoke --owner`, a `journal` collection and
nested `config set` / `config show` support.

Every claim about current behaviour cites `path:line` on `main` at 2.8.1. A claim this
author could not check is marked UNVERIFIED. A place where this spec departs from the
original design brief says so under "Refinement" with the reason.

Notation: `<data_root>` is the resolved `data_root` (`config.py:458`). `<state_home>`
is `$XDG_STATE_HOME`, else `~/.local/state`. Fixture paths (`~/projects/site`,
`/tmp/...`, `~/...`) are invented.

---

## 1. Purpose and non-goals

### 1.1 Purpose

A routine is a named, config-defined job:

1. At a local wall-clock time, launchd starts `cairn routine run NAME`.
2. The run works out which UTC days still need work (catch-up).
3. It refreshes the configured session exports once, for the whole window.
4. For each day, oldest first, it renders a prompt file, runs an optional `prepare`
   command, runs `cairn invoke <peer> --prompt-file ...`, then runs the `validate`
   command.
5. Only a passing `validate` writes the day's done stamp.

The first consumer is a daily journal: `prepare` calls `cairn recall digest` (specified
elsewhere, assumed to exist and print JSON rows), the peer writes a journal entry, and
the consumer's own validator checks it. cairn code holds no journal, inbox or content
concept. Every path comes from config.

cairn already owns the parts this composes: the cron installer (`admin.py:497-520`), the
PATH-independent invocation (`admin.py:51-58`), the background environment
(`runtime.py:30-54`), flock locks (`storage.py:28-36`), `cairn invoke` with its result
line (`invoke.py:1175-1220`) and the source exporters (`sync.py:52-54`).

### 1.2 Non-goals for v1

- No cron fallback. `routine install` is macOS launchd only. On another OS it prints the
  crontab line it would need and exits 2 (section 6.1).
- No templating inside `cairn invoke`. Invoke never expands `{{...}}`. `routine run`
  renders everything and hands invoke finished values (section 4.6 explains how the
  rendered profile arguments reach invoke).
- No new exchange-document location. A routine's invoke round writes its exchange
  document where any caller-less round writes it today: the peer session's folder
  (`exchange.py:171-174`).
- No content knowledge. cairn does not know what a journal, an inbox or a run folder
  means. It creates the configured directories and passes their paths through.
- No Antigravity routines. `peer` is `claude` or `codex` (open question 3).
- No `cairn sync` call from a routine. The routine refreshes its own sources with the
  per-source exporter and never takes the sync lock.

---

## 2. Config schema

### 2.1 Where it lives today

- `_build` checks only the required keys and ignores any other top-level key
  (`config.py:425-428`). A `routines` block written today is silently ignored. This
  change adds explicit parsing, so a malformed block fails loudly.
- `invoke.<peer>` rejects unknown keys (`config.py:388-393`). A `profiles` key is
  rejected today.
- `invoke.<peer>.profile` must be one of the closed `PROFILES` tuple (`config.py:54`,
  checked at `config.py:403-407`).
- Collection specs reject unknown keys (`config.py:311-323`).

### 2.2 New top-level block: `routines`

```json
"routines": {
  "daily": {
    "peer": "claude",
    "prompt_file": "~/projects/site/prompts/daily.md",
    "cd": "~/projects/site",
    "profile": "scheduled",
    "model": "opus",
    "at": "05:20",
    "day": "yesterday-utc",
    "sources": ["claude", "codex", "antigravity"],
    "catch_up_days": 3,
    "timeout_s": 3600,
    "budget_usd_per_day": null,
    "outputs": {
      "journal_dir": "{{DATA_ROOT}}/journal",
      "inbox_dir": "~/projects/site/inbox",
      "runs_dir": "~/projects/site/inbox/runs"
    },
    "prepare": ["cairn", "recall", "digest", "{{DAY}}", "--out", "{{DIGEST_DIR}}", "--json"],
    "validate": ["pnpm", "--dir", "~/projects/site", "validate", "--day", "{{DAY}}", "--attempt", "{{ATTEMPT_DIR}}"]
  }
}
```

The block is optional. Omitted means no routines. Keys starting with `//` are ignored,
as everywhere else (`config.py:318`, `config.py:378`).

| Field | Type | Required | Default | Rule |
|---|---|---|---|---|
| (name) | object key | yes | | `^[a-z0-9][a-z0-9-]{0,63}$` |
| `peer` | string | yes | | `claude` or `codex` |
| `prompt_file` | string path | yes | | absolute after expansion (2.4) |
| `cd` | string path | yes | | absolute after expansion |
| `profile` | string or null | no | `invoke.<peer>.profile` | built-in or a profile in `invoke.<peer>.profiles` |
| `model` | string or null | no | `invoke.<peer>.model` | non-empty string or null |
| `at` | string | yes | | `^([01][0-9]|2[0-3]):[0-5][0-9]$`, local time |
| `day` | string | no | `yesterday-utc` | only `yesterday-utc` in v1 |
| `sources` | array of strings | no | `[]` | each a windowed session source (2.3) |
| `catch_up_days` | integer | no | `3` | 1 to 30 inclusive |
| `timeout_s` | integer or null | no | `invoke.<peer>.timeout_s` | positive integer |
| `budget_usd_per_day` | number or null | no | `null` | positive; `claude` only |
| `outputs` | object of string paths | no | `{"journal_dir": "{{DATA_ROOT}}/journal"}` | keys `^[a-z][a-z0-9_]*$`, values absolute after expansion |
| `prepare` | array of strings or null | no | `null` | non-empty argv list |
| `validate` | array of strings | yes | | non-empty argv list |

Refinement: `prepare` is new. The brief has the peer read a digest, but nothing in the
brief's run steps produces it. Running `cairn recall digest` before invoke keeps the
peer free of shell access to cairn, keeps the digest out of the peer's token budget when
the day is empty, and stays generic (it is just an argv). Section 6.2 step 5 defines
it. Open question 1 asks whether to keep it.

Refinement: `validate` is required. The whole safety story ("only a validated day is
done") rests on it. A routine with no check would stamp a day done on any invoke exit 0.

`outputs.journal_dir` is filled in when `outputs` omits it, so `{{JOURNAL_DIR}}` always
exists. An explicit `outputs` object without `journal_dir` still gets the default key.

### 2.3 `sources`

Each entry must name a registry source (`registry.py:29-51`) that has a `session_type`
and is `windowed` (`registry.py:12`): `claude`, `codex`, `antigravity`, `cursor`,
`granola`. `service-docs` is rejected because it has no window (`registry.py:50`).
The source's collection must be present in `collections` and enabled.

### 2.4 Path fields

`prompt_file`, `cd` and every `outputs` value accept exactly two substitutions:

1. A leading `~` or `~/` expands to the home directory.
2. `{{DATA_ROOT}}` anywhere expands to `<data_root>`.

No `$VAR` expansion. Refinement: `config.expand` also expands `$VARS`
(`config.py:66-68`), but a routine runs under launchd with a different environment from
the shell that wrote the config. A path that means one thing in the shell and another
under launchd is a silent wrong-directory write. So routine paths use a separate helper
that does only the two substitutions above. A `$` stays literal, and the result is then
almost always relative, which the absolute check rejects.

After substitution the path must be absolute. Spaces are allowed. Any other `{{...}}`
token in a path field is an error: paths cannot depend on `{{DAY}}`.

### 2.5 New key in `invoke.<peer>`: `profiles`

```json
"invoke": {
  "claude": {
    "profile": "review",
    "profiles": {
      "scheduled": {"base": "edit", "extra_args": ["--permission-mode", "dontAsk", "..."]}
    }
  }
}
```

| Field | Type | Rule |
|---|---|---|
| (profile name) | object key | `^[a-z0-9][a-z0-9-]{0,63}$`, not `review`, `edit` or `full` |
| `base` | string | `review`, `edit` or `full` |
| `extra_args` | array of strings | may be empty; never a string; per-peer rules in section 4.3 |

`profiles` is accepted for `claude` and `codex`. For `antigravity` it is an error in v1.
`invoke.<peer>.profile` may now name a built-in or a key of that peer's `profiles`.

### 2.6 New collection convention: `journal`

```json
"collections": {
  "journal": { "sync": { "enabled": true } }
}
```

No new collection keys. Section 8 explains why an ordinary store-less collection entry
is enough.

### 2.7 Typed model (config.py)

```text
@dataclass(frozen=True) ProfileSpec: name: str, base: str, extra_args: tuple[str, ...]
PeerPolicy (config.py:174-193) gains: profiles: dict[str, ProfileSpec] = field(default_factory=dict)
@dataclass(frozen=True) RoutineSpec:
    name, peer, prompt_file: Path, cd: Path, profile: str, model: str | None, at: str,
    day: str, sources: tuple[str, ...], catch_up_days: int, timeout_s: int | None,
    budget_usd_per_day: float | None, outputs: dict[str, Path], prepare: tuple[str, ...] | None,
    validate: tuple[str, ...]
CairnConfig (config.py:215-232) gains: routines: dict[str, RoutineSpec] = field(default_factory=dict)
```

`RoutineSpec.profile` holds the resolved name (the routine's own value, else the peer's
default) so later code never repeats the fallback. `outputs` holds expanded paths.
`prepare` and `validate` hold the raw template strings: they are rendered per day.

Knock-on edits:

- `_PEER_FIELDS` (`config.py:346`) derives from `fields(PeerPolicy)`, so `profiles`
  joins the allowed keys automatically. `_parse_peer` must parse it before the
  `PeerPolicy(**values)` call (`config.py:408`).
- `_INVOKE_FIELDS` in admin (`admin.py:202`) also derives from `fields(PeerPolicy)`. It
  must subtract `profiles` as it subtracts `billing`, or `config set
  invoke.claude.profiles VALUE` would write a string into the object.
- `_invoke_view` uses `asdict` (`admin.py:136`). That now includes `profiles`, with
  `extra_args` as a list. That is the wanted JSON shape.
- Parse order inside `_build`: `invoke` first (`config.py:445-453`), then `routines`,
  because a routine's `profile` is checked against the parsed profiles, and `sources`
  against the parsed collections.

### 2.8 Validation errors (exact messages)

`ConfigError` messages, in the style of `config.py:355-407`. `<n>` is the routine name,
`<p>` the peer, `<q>` the profile name.

Routines:

```text
'routines' must be an object of named routines, got <type>
routine name <n!r> is invalid: use 1 to 64 characters from a-z, 0-9 and '-', starting with a letter or digit
routines.<n> must be an object, got <type>
routines.<n> has unknown keys: <k1, k2>. Allowed keys: at, budget_usd_per_day, catch_up_days, cd, day, model, outputs, peer, prepare, profile, prompt_file, sources, timeout_s, validate.
routines.<n> is missing required keys: <k1, k2>
routines.<n>.peer must be one of claude, codex, got <v!r>
routines.<n>.<field> must be a non-empty string, got <v!r>
routines.<n>.<field> must be absolute after expanding ~ and {{DATA_ROOT}}, got <expanded!r>
routines.<n>.<field> may only use {{DATA_ROOT}}, found {{<NAME>}}
routines.<n>.at must be HH:MM in 24-hour local time, got <v!r>
routines.<n>.day must be "yesterday-utc", got <v!r>
routines.<n>.catch_up_days must be an integer from 1 to 30, got <v!r>
routines.<n>.timeout_s must be a positive integer or null, got <v!r>
routines.<n>.budget_usd_per_day must be a positive number or null, got <v!r>
routines.<n>.budget_usd_per_day is Claude-only (codex reports no cost); remove it or set it to null
routines.<n>.profile <q!r> is not a profile of <p>; choose <review, edit, full, and the names in invoke.<p>.profiles>
routines.<n>.sources must be an array of source names, got <type>
routines.<n>.sources names <s!r>, which is not a windowed session source; choose antigravity, claude, codex, cursor, granola
routines.<n>.sources names <s!r>, but collections has no <collection!r>
routines.<n>.sources names <s!r>, whose collection <collection!r> is disabled (sync.enabled=false)
routines.<n>.outputs must be an object of name -> directory, got <type>
routines.<n>.outputs key <k!r> is invalid: use a-z, 0-9 and '_', starting with a letter
routines.<n>.outputs key <k!r> renders as {{<K>}}, which is a built-in variable
routines.<n>.<prepare|validate> must be a non-empty array of strings (an argv list, never a shell string), got <type>
```

The `{{<K>}}` collision covers `DAY`, `DATA_ROOT`, `ATTEMPT_DIR` and `DIGEST_DIR`, so the
keys `day`, `data_root`, `attempt_dir` and `digest_dir` are rejected.

Note: `docs/routine-hardening-spec.md` section 3 adds the `terminal_hook` and
`stale_sources` keys and their messages, and the `--reason` message of `routine pause`.

Profiles:

```text
invoke.<p>.profiles must be an object of named profiles, got <type>
invoke.antigravity.profiles is not supported; only claude, codex take config profiles
invoke.<p>.profiles name <q!r> is invalid: use 1 to 64 characters from a-z, 0-9 and '-', starting with a letter or digit
invoke.<p>.profiles.<q> shadows the built-in profile <q!r>; pick another name
invoke.<p>.profiles.<q> has unknown keys: <k>. Allowed keys: base, extra_args.
invoke.<p>.profiles.<q>.base must be one of review, edit, full, got <v!r>
invoke.<p>.profiles.<q>.extra_args must be an array of strings (an argv list, never a string), got <type>
invoke.<p>.profiles.<q>.extra_args: <reason>
invoke.<p>.profile must be one of review, edit, full<, and the names in invoke.<p>.profiles>, got <v!r>
```

The last line replaces the message at `config.py:405-407`. The `<reason>` texts are in
section 4.3.

### 2.9 The two example-file changes

`src/cairn/config.example.json` (packaged, read by `config init`, `config.py:279-281`)
and `cairn.config.example.json` (repo root) must stay byte-identical
(`tests/test_config_examples.py:20-29`). Make each edit in one file, then copy the file
over the other.

1. Add a `journal` collection after `service-docs`:

   ```json
   "journal": { "sync": { "enabled": true } }
   ```

   and a comment key at top level, next to `//collections` and `//sync`:

   ```json
   "//journal": "Optional. Markdown a routine writes under <data_root>/journal (see 'routines'). A store-less collection: nothing is exported into it. 'cairn sync' only registers and indexes it once it holds markdown."
   ```

   The comment must not go inside `collections`: `_build` parses every key of
   `data["collections"]` as a collection (`config.py:440-442`), with no `//` skip at
   that level, so `"//journal"` there would fail as a collection spec.

2. Add an `invoke.claude.profiles` example and a top-level `routines` example. A
   routine in the template must not run for a user who never asked for one. `config
   init` writes the template verbatim (`admin.py:77-78`), but nothing runs until
   `routine install`. So the example routine is safe to ship, but its paths are
   placeholders that fail `prompt_file` existence checks at `install` time, not at
   load time. Load-time validation checks shape only (absolute paths), not existence,
   so the template still loads. Use:

   ```json
   "//routines": "Optional. Scheduled peer runs: 'cairn routine install NAME' adds a launchd job (macOS) that runs 'cairn routine run NAME' daily at 'at' (local time). Each run handles every UTC day in the last catch_up_days without a done stamp: refresh sources, render prompt_file, run prepare, invoke the peer, run validate. Exit 0 from validate marks the day done, 3 marks it done and empty, anything else failed. Template variables: {{DAY}}, {{DATA_ROOT}}, {{ATTEMPT_DIR}}, {{DIGEST_DIR}}, and one {{UPPER_KEY}} per outputs key.",
   "routines": {
     "example": {
       "peer": "claude",
       "prompt_file": "~/projects/site/prompts/daily.md",
       "cd": "~/projects/site",
       "profile": "scheduled",
       "at": "05:20",
       "sources": ["claude", "codex"],
       "outputs": { "journal_dir": "{{DATA_ROOT}}/journal" },
       "validate": ["true"]
     }
   }
   ```

   and in `invoke.claude`:

   ```json
   "profiles": { "scheduled": { "base": "edit", "extra_args": ["--permission-mode", "dontAsk", "--allowedTools", "Read,Glob,Grep,Edit,Write", "--add-dir", "{{JOURNAL_DIR}}"] } }
   ```

   Open question 6 asks whether the template should carry a routine at all.

The `invoke.claude` line in both files is one long line today (`cairn.config.example.json`,
the `"claude": {...}` line). Keep the profiles inside it or break the object over lines.
Either way, keep the two files identical.

---

## 3. Template contract

### 3.1 Variables

`routine run` renders exactly these variables, per day and per attempt:

| Variable | Value |
|---|---|
| `{{DAY}}` | the UTC day, `YYYY-MM-DD` |
| `{{DATA_ROOT}}` | `<data_root>`, absolute, no trailing slash |
| `{{ATTEMPT_DIR}}` | `<data_root>/routines/<name>/<DAY>/attempt-<n>` |
| `{{DIGEST_DIR}}` | `<ATTEMPT_DIR>/digest` |
| `{{<KEY>}}` | one per `outputs` key, upper-cased: `journal_dir` gives `{{JOURNAL_DIR}}` |

### 3.2 Where rendering happens

1. The prompt file. Its text is rendered and written to `<ATTEMPT_DIR>/prompt.md`.
   Invoke reads that copy (`--prompt-file`, `invoke.py:1245-1251`).
2. Each element of `prepare` and `validate`.
3. Each element of the routine profile's `extra_args`, when `profile` names a config
   profile. The rendered list is written to `<ATTEMPT_DIR>/profile-args.json`
   (section 4.6).

Nowhere else. Invoke itself never renders (non-goal 1.2).

### 3.3 Syntax

- A token is `{{NAME}}` with `NAME` matching `[A-Za-z_][A-Za-z0-9_]*`, no spaces inside
  the braces. Regex: `\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}`.
- Rendering is one pass. A substituted value is never scanned again.
- Text that does not match the regex is literal: `{{ DAY }}` (spaces), `{{#each}}`,
  a lone `{{`. There is no escape in v1 (open question 7).
- In `prepare` and `validate` elements only, an element that is exactly `~` or starts
  with `~/` has the `~` expanded after rendering. This matches what a shell does to a
  bare word, so `["pnpm", "--dir", "~/projects/site", ...]` works. A `~` anywhere else
  is literal. Profile `extra_args` follow the same rule.

### 3.4 Directories

Before the prompt is rendered, `routine run` creates (`mkdir -p`) every `outputs`
directory, `<ATTEMPT_DIR>` and `<DIGEST_DIR>`. A creation failure is a day failure with
reason `render-error` (6.2).

### 3.5 Error cases

| Case | When detected | Result |
|---|---|---|
| unknown `{{NAME}}` in the prompt file | render, before any invoke | exit 2, no stamp |
| unknown `{{NAME}}` in `prepare`, `validate` or profile `extra_args` | render | exit 2, no stamp |
| path field not absolute after expansion | config load | `ConfigError` (2.8) |
| path field uses a variable other than `{{DATA_ROOT}}` | config load | `ConfigError` |
| `prompt_file` missing or unreadable | render | exit 2, no stamp |
| `prompt_file` renders to whitespace only | render | exit 2, no stamp (invoke refuses an empty prompt anyway, `invoke.py:1252-1253`) |
| `cd` not a directory | render | exit 2, no stamp |
| an `outputs` directory cannot be created | per day | failed stamp, reason `render-error`, attempt not counted |

Error text for the unknown-variable case:

```text
routine daily: unknown template variable {{JORNAL_DIR}} in ~/projects/site/prompts/daily.md line 14; known: ATTEMPT_DIR, DATA_ROOT, DAY, DIGEST_DIR, INBOX_DIR, JOURNAL_DIR, RUNS_DIR
```

For an argv element, the location reads `validate[5]` or `invoke.claude.profiles.scheduled.extra_args[3]`.

Why exit 2 with no stamp: an unknown variable is a config or prompt defect. It fails
every day the same way, so stamping each day failed would burn the attempt cap on a
typo. The `last-run.json` record (6.2) keeps the error visible to `status` and `doctor`.

---

## 4. Profiles

### 4.1 Current behaviour

- `PROFILES = ("review", "edit", "full")` is closed (`config.py:54`).
- Claude maps a name to a native tuple (`harnesses/claude.py:11-15`): `review` gives
  `--permission-mode plan --disallowedTools Edit,Write,NotebookEdit`, `edit` gives
  `--permission-mode acceptEdits`, `full` gives `--permission-mode bypassPermissions`.
- Codex maps a name to a sandbox mode (`harnesses/codex.py:15-19`) and emits `-s
  <mode>` on a fresh run (`codex.py:129-133`, used at `codex.py:177`). On resume it
  emits `-c sandbox_mode="<mode>"` instead (`codex.py:156-164`), because `codex exec
  resume` takes no `-s`.
- An explicit native flag (`--permission-mode`, `--sandbox`, `--mode`) replaces the
  profile's tuple entirely (`invoke.py:1323-1333`, `claude.py:80-81`,
  `codex.py:131-132`).
- argparse restricts `--profile` to `PROFILES` (`invoke.py:2516`). `Runner.settings`
  checks it again (`invoke.py:1363-1364`).
- On resume, `args.profile or recorded.profile` (`invoke.py:1352`). An explicit
  `--profile` beats the recorded one. With no `--profile` and no native flag, the
  recorded explicit native flags come back (`invoke.py:1353-1354`). The module
  docstring states the order as "CLI > recorded thread settings on resume > config
  policy" (`invoke.py:13`).
- The round records the full emitted policy (`invoke.py:1619-1622`). The thread records
  the profile name and only the explicit native flags (`invoke.py:1539`).

### 4.2 Resolution order (new)

For a fresh thread:

1. `--profile NAME` on the command line, else `invoke.<peer>.profile` from config.
2. If `NAME` is `review`, `edit` or `full`: base `NAME`, `extra_args` empty.
3. Else if `NAME` is a key of `invoke.<peer>.profiles`: base and `extra_args` from it.
4. Else `UsageError`: `unknown profile 'NAME' for claude; choose review, edit, full, scheduled`.

A helper in `config.py` does steps 2 to 4:
`resolve_profile(policy: PeerPolicy, name: str) -> tuple[str, tuple[str, ...]]`
(raises `ConfigError`; the runner converts it to `UsageError`).

argparse: drop `choices=PROFILES` at `invoke.py:2516`, keep `help`, and validate in
`Runner.settings` (`invoke.py:1363`) through `resolve_profile`. The config check at
`config.py:403-407` also goes through `resolve_profile`.

Explicit native flag plus a config profile: the flag wins for the mode, as today.
`cairn invoke claude --permission-mode MODE` already overrides the profile's
permission-mode tuple for Claude only (`invoke.py:1323-1333`, applied by
`claude.py:80-81`). For a built-in profile that behaviour stays exactly as it is: the
flag replaces the whole tuple, so `--permission-mode acceptEdits --profile review`
also drops review's `--disallowedTools` (today's behaviour, unchanged).

For a config profile, replacing the whole tuple would silently drop the profile's
`extra_args`, including its deny rules. So the flag wins only for the mode:

1. Emit the base tuple merged with `extra_args` (4.3).
2. Then replace the single `--permission-mode` value with the flag's value (merge
   rule A, applied last). The flag beats both the base's mode and any
   `--permission-mode` inside `extra_args`.
3. No looseness check on the flag's value: an explicit command-line choice is the
   operator's decision, as it is today. The emitted argv, recorded in the round's
   `native_policy` (`invoke.py:1619-1622`), shows the result.
4. `ThreadState.native_policy` records `("--permission-mode", MODE)` as today
   (`invoke.py:1539`), so a resume without flags re-applies it on top of the recorded
   `profile_args` (4.5).

Codex `--sandbox MODE` with a config profile follows the same rule: `-s MODE` replaces
the base's sandbox mode, and the profile's `--add-dir` and `-c` pairs are still
emitted. `routine run` never passes either flag.

### 4.3 Emission and the `extra_args` rules

The base's native tuple comes first, then `extra_args`, with two merge rules for
Claude that keep a single copy of a flag instead of relying on how the CLI treats a
repeated flag (repeat behaviour is UNVERIFIED for `--permission-mode` and for the
variadic `--disallowedTools`).

Claude (`claude` 2.1.312, flags checked against `claude --help` on this machine):

- Merge rule A. If `extra_args` holds `--permission-mode X`, the emitter replaces the
  base's mode value with `X` in place. The argv holds one `--permission-mode`.
- Merge rule B. If `extra_args` holds `--disallowedTools V` (or `--disallowed-tools V`)
  and the base already has `--disallowedTools` (only `review` does), `V` is appended
  to the base's comma list. Otherwise the pair passes through.
- Everything else passes through in order.
- Shape: `extra_args` is an argv list, never a shell string. `--allowedTools` and
  `--disallowedTools` each take one following element that is a single comma-joined
  string, for example `["--allowedTools", "Read,Grep,Bash(pnpm:*)"]`. Several
  following elements (`["--allowedTools", "Read", "Grep"]`) are rejected:
  `--allowedTools takes one comma-joined string; join 'Read', 'Grep' with ','`. The
  variadic CLI form would accept them, but a single element keeps merge rule B and
  the recorded argv unambiguous.
- Rule syntax: Bash rules use the prefix form `Bash(<command prefix>:*)`, for example
  `Bash(cairn:*)` or `Bash(git push:*)`. The space form `Bash(cairn *)` does not match
  and a call under it is denied (verified live by the maintainer's session: a
  subagent's call was refused under the space form). Config validation warns, but
  does not fail, on a `Bash(` rule that contains a space and no `:*`:
  `rule 'Bash(cairn *)' uses the space form; Claude matches only the prefix form 'Bash(cairn:*)'`.
  The warning goes to stderr at `config show`, `doctor` and `routine install`.
  [Superseded in review: the Claude Code permissions reference states that `Bash(cmd *)`
  and `Bash(cmd:*)` are the same trailing wildcard and that the permission dialog writes
  the space form, so the warning was removed.]
- Allowed `--permission-mode` values per base: `review` allows `plan`. `edit` allows
  `plan`, `dontAsk`, `acceptEdits`. `full` allows any value the CLI accepts.
  Reason text: `--permission-mode 'auto' would loosen base 'edit'; allowed: plan, dontAsk, acceptEdits`.
- Forbidden unless base is `full`: `--dangerously-skip-permissions`,
  `--allow-dangerously-skip-permissions`. Reason: `<flag> needs base 'full'`.
- Always forbidden, because cairn sets them itself (`claude.py:100-119`): `-p`,
  `--print`, `--output-format`, `--input-format`, `--verbose`, `--resume`, `-r`,
  `--continue`, `-c`, `--session-id`, `--fork-session`, `--model`, `--effort`,
  `--json-schema`, `--`. Reason: `<flag> is set by cairn invoke; remove it`.
- `--max-budget-usd` is forbidden in a profile. The routine owns the budget (6.2).

Codex (`codex-cli` 0.164.0, flags checked against `codex exec --help` and `codex exec
resume --help` on this machine):

- Allowed: `--add-dir DIR` pairs and `-c KEY=VALUE` / `--config KEY=VALUE` pairs.
  Everything else is rejected: `codex extra_args allow only --add-dir DIR and -c KEY=VALUE; got '<token>'`.
- Forbidden `-c` keys (they would loosen or override what cairn sets at
  `codex.py:20` and `codex.py:142-148`): `sandbox_mode`, `approval_policy`, `model`,
  `model_provider`, `model_reasoning_effort`, `service_tier`, `profile`. Reason:
  `-c <key>=... is set by cairn invoke or would loosen the sandbox; remove it`.
- Fresh run: emitted right after `-s <mode>` (inside `policy_argv`), so the argv reads
  `codex exec --json --skip-git-repo-check -s workspace-write <extra_args> -c approval_policy="never" ...`.
- Resume: `codex exec resume` has no `--add-dir` (checked on 0.164.0). The emitter keeps
  the `-c` pairs after `-c sandbox_mode=...`, drops each `--add-dir DIR` pair and adds
  a warning to the round: `codex resume cannot take --add-dir; DIR is not writable this round`.

A note on "a profile cannot loosen its base". Claude deny rules beat allow rules, so a
profile cannot remove a `review` base's `--disallowedTools` entries, and merge rule B
only appends to that list. But `--allowedTools` can widen what an `edit` base
auto-approves without a prompt (for example `Bash(pnpm:*)`). That widening is the point
of a scheduled profile, so it is allowed. The protections are: the mode check above,
the forbidden flags, and deny rules. Deny beats allow was verified live from a
LaunchAgent by the maintainer's session: with `--allowedTools 'Bash(pnpm:*),...'` and a
`--disallowedTools` entry naming one pnpm script by prefix, that script was blocked
while other pnpm commands ran. So a profile can allow a tool family and deny the one
dangerous member of it. Refinement: the brief's sentence was too strong.
The docs must say "a profile cannot drop its base's deny rules or switch to a looser
permission mode", not "cannot loosen".

Claude permission rules with absolute paths: in a Claude Code rule, a single leading `/`
is relative to the settings file and `//` marks an absolute path (UNVERIFIED for
2.1.312; check the Claude Code permissions docs). A rendered `{{JOURNAL_DIR}}` is
absolute, so a rule must be written `Edit(/{{JOURNAL_DIR}}/**)`, which renders to
`Edit(//...)`. The workflow page must show this.

### 4.4 Data flow through invoke

- `Settings` (`invoke.py:1228-1239`) gains `profile_base: str` and
  `profile_args: tuple[str, ...]`. `Settings.profile` keeps the name as given
  (`scheduled`), for display and records.
- `InvokeRequest` (`harnesses/__init__.py:53-80`) gains
  `profile_args: tuple[str, ...] = ()`. `InvokeRequest.profile` carries the base
  (`edit`), so the harness lookup tables stay keyed by base.
- `policy_argv(profile, native, profile_args=())` in each harness: native when given,
  else the base tuple merged with `profile_args` (4.3). `exec_argv` passes
  `request.profile_args`.
- `ThreadState` (`exchange.py:66-93`) gains `profile_base: str | None = None` and
  `profile_args: list[str] = field(default_factory=list)`. `from_dict` already drops
  unknown keys and keeps defaults (`exchange.py:104-110`), so old `thread.json` files
  load. `_run_locked` sets them next to `state.profile` (`invoke.py:1539`).
- The round's `native_policy` (`invoke.py:1619-1622`) already records the full emitted
  policy. It now includes the profile args, so the exchange document's
  `native_policy:` head line (`exchange.py:325`) shows exactly what ran.

### 4.5 Resume precedence

Refinement. The brief says "the recorded profile wins". Today an explicit `--profile`
on resume wins (`invoke.py:1352`), and the docstring promises CLI over recorded
(`invoke.py:13`). This spec keeps that order and makes "recorded wins" true for the
config lookup:

1. `--profile NAME` given: resolve `NAME` against today's config (4.2).
2. No `--profile`, no native flag: reuse `recorded.profile_base` and
   `recorded.profile_args` verbatim from `thread.json`. Do not re-read config. An
   edited or deleted config profile cannot change a resumed thread.
3. Old `thread.json` with no `profile_base`: base is `recorded.profile` (it can only be
   a built-in), args empty.
4. Native flag given: today's behaviour (`invoke.py:1353-1354`), plus the 4.2 refusal
   when the recorded profile is a config profile.

A thread resumed from rendered routine args (`profile_args` with absolute paths) stays
correct because the recorded args are already rendered.

### 4.6 How rendered profile args reach invoke

Invoke never renders (1.2), but a routine profile's `extra_args` may hold
`{{JOURNAL_DIR}}`. So:

- New invoke flag `--profile-args-file PATH`, on the shared `run` parser
  (`invoke.py:2510-2528`). The file holds a JSON array of strings.
- It requires `--profile` naming a config profile:
  `--profile-args-file needs --profile naming a config profile of claude`.
- Its array replaces that profile's `extra_args` for this round. It is validated with
  the same 4.3 rules against the profile's base.
- It must contain no `{{` token: `--profile-args-file holds an unrendered token {{X}}`.
- Plain `cairn invoke` of a config profile whose `extra_args` hold a `{{...}}` token,
  with no `--profile-args-file`, is refused:
  `profile 'scheduled' has template variables ({{JOURNAL_DIR}}); run it through 'cairn routine run' or pass --profile-args-file`.

The flag is documented as plumbing for `routine run`, not as a user flag.

### 4.7 Worked example: Claude `scheduled`

Config:

```json
"scheduled": {
  "base": "edit",
  "extra_args": [
    "--permission-mode", "dontAsk",
    "--allowedTools", "Read,Glob,Grep,Edit(/{{JOURNAL_DIR}}/**),Write(/{{JOURNAL_DIR}}/**),Bash(pnpm:*)",
    "--disallowedTools", "WebFetch,WebSearch,NotebookEdit,Bash(pnpm publish:*),Bash(git push:*)",
    "--add-dir", "{{JOURNAL_DIR}}"
  ]
}
```

With `data_root` `~/.context`, day `2026-10-27`, attempt 1, the rendered
`profile-args.json` holds the same list with `{{JOURNAL_DIR}}` replaced by
`~/.context/journal`. The emitted argv (merge rule A replaces `acceptEdits`):

```text
claude -p --output-format stream-json --verbose --session-id <uuid>
  --permission-mode dontAsk
  --allowedTools Read,Glob,Grep,Edit(/~/.context/journal/**),Write(/~/.context/journal/**),Bash(pnpm:*)
  --disallowedTools WebFetch,WebSearch,NotebookEdit,Bash(pnpm publish:*),Bash(git push:*)
  --add-dir ~/.context/journal
  --model opus
  --max-budget-usd 4.00
  -- <prompt>
```

Each of `--allowedTools` and `--disallowedTools` is one argv element after its flag.
`Bash(pnpm:*)` lets the peer run the consumer's pnpm scripts. The two deny entries
block publishing and pushing even though `Bash(pnpm:*)` would allow the first, because
deny beats allow.

`--max-budget-usd` comes from the routine through `--extra` (6.2), which lands after
the model flags (`claude.py:115`). The trailing `--` already protects the prompt from
the variadic flags (`claude.py:116-119`).

### 4.8 Worked example: Codex `scheduled`

```json
"scheduled": {
  "base": "edit",
  "extra_args": ["--add-dir", "{{JOURNAL_DIR}}", "-c", "sandbox_workspace_write.network_access=false"]
}
```

Fresh run argv:

```text
codex exec --json --skip-git-repo-check -s workspace-write
  --add-dir ~/.context/journal -c sandbox_workspace_write.network_access=false
  -c approval_policy="never" -m <model> -C ~/projects/site -o <answer> <prompt>
```

Resume argv (no `--add-dir`, one warning in the round):

```text
codex exec resume --json --skip-git-repo-check -c sandbox_mode="workspace-write"
  -c approval_policy="never" -c sandbox_workspace_write.network_access=false ... <thread> <prompt>
```

Whether `sandbox_workspace_write.network_access` is honoured by `codex exec` 0.164.0 is
UNVERIFIED. The example shows shape, not a tested hardening.

---

## 5. `cairn invoke --owner LABEL`

### 5.1 Flag

- Added to the shared `run` parser (`invoke.py:2510-2528`), so all three peers take it.
- Regex `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`. This keeps the value safe to emit
  unquoted in front matter (`exchange.py:220-226`). Error:
  `--owner must be 1 to 128 characters from A-Z, a-z, 0-9, '.', '_', ':' and '-', got 'X'`.
- Convention: `routine:<name>`. cairn does not parse the label.
- On resume, omitted means the recorded owner. A different value is refused:
  `thread <id> is owned by routine:daily; --owner cannot change it`.

### 5.2 Where it is stored

1. `ThreadState.owner: str | None = None` (`exchange.py:66-93`), set in `_new_state`
   (`invoke.py:1506-1516`).
2. Each round: `"owner"` key in the round dict (`invoke.py:1613-1643`).
3. The result line: `"owner"` key in `round_result` (`invoke.py:1187-1218`).
4. The exchange document: an `owner:` line in the preserved tail (see 5.3).
5. The peer session document front matter: `owner: routine:daily`, written during the
   `exported` publication step.

### 5.3 Front matter mechanics

The peer session document is written by the source exporter, not by invoke
(`invoke.py:508-522` calls `export_and_index`). A re-export re-renders the head and
re-emits only the preserved tail from the old file (`frontmatter.py:271-320`). A head
key added by a mutation would be lost on the next export. So `owner` joins the
preserved tail:

- `PRESERVED_FIELDS` (`frontmatter.py:28`) gains `"owner"`.
- `_TAIL_KEYS` (`frontmatter.py:32`) gains `"owner:"` at the end.
- `preserved_tail` (`frontmatter.py:271-320`) emits `owner: <value>` after `links`,
  only when the existing front matter has a non-empty `owner`. A document with no owner
  stays byte-identical to today's output, the same guarantee `links` gives
  (`frontmatter.py:275-277`).
- New `mutations.set_owner(find_file, session_id, owner) -> bool`, built on
  `_mutate_file` (`mutations.py:55-85`) like `add_link` (`mutations.py:151-189`). It
  strips newlines, validates with the 5.1 regex, sets `fm["owner"]`, and is idempotent.
- `_step_exported` (`invoke.py:508-522`): after `export_and_index`, if `state.owner`,
  call `set_owner` on the peer document. Failure raises `RuntimeError("owner not
  written on the peer document")`, so the step stays pending in the ledger and `cairn
  invoke publish` retries it (`invoke.py:442-450`).
- `render_exchange` (`exchange.py:306-348`): pass
  `{**(existing_fm or {}), "owner": state.owner}` to `preserved_tail` when
  `state.owner` is set (`exchange.py:336`). `validate_head` (`exchange.py:229-242`)
  then rejects any head key that starts with `owner`, through `_TAIL_NAMES`
  (`exchange.py:52`). No current head key does (checked by grep).

Naming clash to avoid: `exchange.document_owner` (`exchange.py:185-194`) means "the
thread that owns an exchange file". Do not rename it, but the new field docstrings must
say "owner label" to keep the two apart.

### 5.4 How digest consumers read it

A session record's `metadata` is the parsed front matter
(`store.py:203-219`, the `fm` argument of `SessionRecord`). A consumer reads
`record.metadata.get("owner")`. The `cairn recall digest` spec (elsewhere) should skip
or label rows whose owner starts with `routine:`. Otherwise tomorrow's journal digests
today's journal run. Built: `recall digest` now excludes these sessions by default
(`docs/journal-verify-spec.md`, section 4.6). Exchange documents are `agent-exchange` and never become records
(`store.py:182-184`), so only the peer session document matters.

Timing: the routine runs at 05:20 on day D+1 for day D. Its own peer session is active
on D+1. The owner is written during that run's publication, long before the D+1 digest
runs a day later.

### 5.5 Interaction with `caller_session`

- `caller_session` is a session reference that the publisher exports and links
  (`invoke.py:525-556`). It must match `^(claude|codex):\S+$` (`invoke.py:152`,
  checked at `invoke.py:1271-1273`). `--parent routine:daily` is refused today, and
  must stay refused. `--owner` is the label channel.
- The two coexist. A manual `cairn invoke --owner x` from inside a Claude session
  records both.
- A run with no caller is a warning, not a refusal: `resolve_caller` returns `None` and
  a warning (`invoke.py:1282`), and `run` only appends it (`invoke.py:1436`). Verified.
- `routine run` removes the `SCRUBBED_ENV` names (`invoke.py:134-147`) from the invoke
  child's environment, so a manual `routine run` typed inside a Claude session does not
  make that session the caller. The round's caller is then `None`, the exchange
  document anchors in the peer folder (`exchange.py:171-174`), and the result carries
  the warning `caller unknown: no single host session detected; pass --parent SOURCE:ID`.
  The routine log does not treat that warning as an error.
- Refinement: the preamble (`invoke.py:1257-1266`) says "invoked by an operator" when
  there is no caller. With an owner and no caller it says "invoked by routine:daily".
  `preamble` gains an `owner` parameter.

---

## 6. The `routine` subcommands

Module: `src/cairn/routine.py`, entry `main(argv=None) -> int`. Dispatcher: add
`"routine": "cairn.routine:main"` to `_ADMIN` (`__main__.py:24-34`) and these lines to
`_HELP` under "orchestration / admin" (`__main__.py:78-87`):

```text
  cairn routine install | uninstall NAME     add / remove the launchd job (macOS)
  cairn routine run NAME [--day D] [--force] [--dry-run]   run due days now
  cairn routine list | status NAME [--json]  routines, stamps, last run, cost, log tail
  cairn routine kickstart NAME [--kill]      ask launchd to start the job now
```

### 6.0 Shared paths

| Item | Path |
|---|---|
| state dir | `<state_home>/cairn/routines/<name>/` |
| stamps | `<state dir>/done-<DAY>.json`, `<state dir>/failed-<DAY>.json` |
| last run | `<state dir>/last-run.json` |
| lock | `storage.lock_path(<state dir>, "routine")` |
| attempt dir | `<data_root>/routines/<name>/<DAY>/attempt-<n>/` |
| log | `<data_root>/logs/routine-<name>.log` |
| plist | `~/Library/LaunchAgents/dev.cairn.routine.<name>.plist` |
| launchd label | `dev.cairn.routine.<name>` |

`lock_path` hashes the resolved resource into `~/.local/state/cairn/locks/`
(`storage.py:18-25`), independent of `XDG_STATE_HOME` by design. That is fine: the lock
identity is the state dir path.

Every file write in the state dir goes through `atomic_write` (`storage.py:43-59`).

### 6.1 `cairn routine install NAME`

argv: `cairn routine install NAME`.

Steps:

1. Load config. Unknown routine: exit 2, `routine 'NAME' is not in routines; known: a, b`.
2. If `sys.platform != "darwin"`: print the crontab line below to stdout and this text
   to stderr, then exit 2:
   `cairn routine install is launchd-only in this version; add this line with 'crontab -e' to run it under cron:`
   The line, with `MM HH` from `at`:
   `MM HH * * * <shlex.join(/usr/bin/env K=V ... <cairn invocation> routine run NAME)> >> <log> 2>&1 # cairn routine NAME`
   with `%` escaped as `admin.py:514` does.
3. Pre-flight checks, each a failure with exit 2:
   - `prompt_file` is a readable file: `prompt_file not found: <path>`.
   - `cd` is a directory: `cd is not a directory: <path>`.
   - A dry render for yesterday succeeds (unknown variables caught now, not at 05:20).
4. Pre-flight warnings (printed, install continues): the peer executable is not on the
   recorded `PATH`. The first `validate` element is not on the recorded `PATH` and is
   not an absolute path.
5. Build the environment block (6.7).
6. Render the plist with `plistlib.dumps(..., fmt=plistlib.FMT_XML, sort_keys=True)`.
   `plistlib` escapes XML, so no hand escaping.
7. `mkdir -p` the log directory and `~/Library/LaunchAgents`.
8. `launchctl bootout gui/<uid>/dev.cairn.routine.<name>`. Ignore a non-zero exit (not
   loaded). This makes re-install a replace: `bootstrap` of a loaded label fails.
9. `atomic_write` the plist.
10. `launchctl bootstrap gui/<uid> <plist>`. Non-zero: exit 1 with its stderr.
11. Print `Installed dev.cairn.routine.<name>: daily at HH:MM local, log <log>`.

`<uid>` is `os.getuid()`.

Plist (as `plistlib` writes it, keys sorted):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>EnvironmentVariables</key>
	<dict>
		<key>CAIRN_CONFIG</key>
		<string>{CAIRN_CONFIG}</string>
		<key>INDEX_PATH</key>
		<string>{INDEX_PATH}</string>
		<key>PATH</key>
		<string>{PATH}</string>
		<key>QMD_CONFIG_DIR</key>
		<string>{QMD_CONFIG_DIR}</string>
		<key>XDG_CACHE_HOME</key>
		<string>{XDG_CACHE_HOME}</string>
		<key>XDG_CONFIG_HOME</key>
		<string>{XDG_CONFIG_HOME}</string>
		<!-- plus XDG_STATE_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME only when 6.7 says so -->
	</dict>
	<key>Label</key>
	<string>dev.cairn.routine.{NAME}</string>
	<key>ProcessType</key>
	<string>Background</string>
	<key>ProgramArguments</key>
	<array>
		<string>{CAIRN_INVOCATION_0}</string>
		<!-- the rest of _cairn_invocation(): one element, or python, -m, cairn -->
		<string>routine</string>
		<string>run</string>
		<string>{NAME}</string>
	</array>
	<key>RunAtLoad</key>
	<false/>
	<key>StandardErrorPath</key>
	<string>{DATA_ROOT}/logs/routine-{NAME}.log</string>
	<key>StandardOutPath</key>
	<string>{DATA_ROOT}/logs/routine-{NAME}.log</string>
	<key>StartCalendarInterval</key>
	<dict>
		<key>Hour</key>
		<integer>{HH}</integer>
		<key>Minute</key>
		<integer>{MM}</integer>
	</dict>
</dict>
</plist>
```

The XML comments above are notes for this spec. The rendered file has none.

`ProgramArguments` is `_cairn_invocation()` (`admin.py:51-58`) followed by `routine
run NAME`. Unlike cron (`admin.py:502-507`), launchd takes the environment as a dict,
so there is no `/usr/bin/env` prefix.

`StartCalendarInterval` uses local time. Per `launchd.plist(5)`, a job whose interval
passes while the machine sleeps runs once on wake, and several missed intervals
coalesce into one run. A job whose interval passes while the machine is powered off
does not run. Catch-up (section 7) covers both.

Exit codes: 0 installed, 1 `launchctl` failure, 2 usage, unknown routine, pre-flight
failure or non-macOS.

### 6.2 `cairn routine run NAME [--day DATE] [--force] [--dry-run]`

Flags:

- `--day YYYY-MM-DD`: run only this UTC day. It must be before today in UTC, else exit
  2 with `day 2026-10-28 has not ended in UTC`. It may be outside the catch-up window.
- `--force`: run even a done day or a day at the attempt cap. Always a new attempt dir.
- `--dry-run`: no lock, no directories, no stamps, no subprocesses. Section 6.2.3.

#### 6.2.1 Steps

1. Load config and the routine. Unknown routine: exit 2.
2. Start a run: `run_id` = `YYYYMMDDTHHMMSSZ-<8 hex>` (UTC). Log `event=start`.
3. Take the lock: `locked(lock_path(state_dir, "routine"), blocking=False)`
   (`storage.py:28-36`, `LOCK_NB` at `storage.py:32`). On `BlockingIOError`: log
   `event=skipped-lock`, print `routine NAME: skipped, another run holds the lock`, exit
   0. Hold the lock until the end of the run.
4. Compute the day set (section 7). Empty: log `event=nothing-due`, write
   `last-run.json`, exit 0.
5. Refresh sources (section 7.3). If any refresh fails, write a failed stamp with
   reason `incomplete-input` for every day in the set, log, write `last-run.json`,
   exit 1. No invoke runs.
6. For each day, oldest first:
   1. Attempt number `n` = 1 + the highest `attempt-<k>` under
      `<data_root>/routines/<name>/<DAY>/`, else 1.
   2. Create the directories (3.4). Failure: failed stamp, reason `render-error`, not
      counted as an attempt. The run then stops (step 11), and the remaining days are
      deferred.
   3. Render the prompt to `<ATTEMPT_DIR>/prompt.md`. Render `prepare`, `validate` and
      the profile args (to `<ATTEMPT_DIR>/profile-args.json`). An unknown variable here
      ends the run: exit 2, no stamp (3.5).
   4. Budget (Claude only, when `budget_usd_per_day` is set): `remaining` =
      budget minus the sum of `cost_usd` over this day's earlier attempts (from the
      failed stamp `history`, or the done stamp when `--force` re-runs a done day).
      If `remaining <= 0`: failed stamp, reason `budget-exhausted`, not counted as an
      attempt, no invoke. A `null` cost counts as 0.
   5. `prepare` (when set): run the rendered argv with `cwd=cd`, the child environment
      (6.2.2), stdout to `<ATTEMPT_DIR>/prepare.out`, stderr to the routine log (its
      head and tail), timeout 600 s through `procs.run_capped` (process
      group kill, as `sync.py:139` uses it). Exit 0: continue. Exit 3: the day has no
      input; skip invoke and `validate`, write a done stamp with status `empty`. Any
      other exit or a timeout: failed stamp, reason `prepare-exit-<code>` or
      `prepare-timeout`, counted as an attempt.
   6. Invoke: run, with the child environment and `cwd=cd`:

      ```text
      <cairn invocation> invoke <peer> --prompt-file <ATTEMPT_DIR>/prompt.md --cd <cd>
        --profile <profile> [--profile-args-file <ATTEMPT_DIR>/profile-args.json]
        [--model <model>] [--timeout-s <timeout_s>] --owner routine:<name> --json
        [--extra --max-budget-usd <remaining, two decimals>]
      ```

      `--profile-args-file` only for a config profile. `--model` and `--timeout-s` only
      when set (omitted means the invoke policy decides, `invoke.py:1347-1378`).
      `--extra` is `argparse.REMAINDER` (`invoke.py:2537`), so it must be last.
      Stdout goes to `<ATTEMPT_DIR>/invoke-result.json`, stderr to
      `<ATTEMPT_DIR>/invoke.stderr`. No timeout on the child beyond invoke's own
      watchdogs. The result is the last non-empty stdout line parsed as JSON. An
      unparsable or missing line gives `result = null`.
   7. Invoke exit non-zero: do not run `validate`. Failed stamp, reason
      `invoke-exit-<code>` (`invoke.py:96-115`: 1 fatal, 2 usage or denied, 75 busy,
      124 timeout, 125 idle, 130 cancelled; a `failed` round keeps the peer's code),
      counted as an attempt. Cost from the result if present.
   8. Invoke exit 0: run `validate` with `cwd=cd`, the child environment, stdout to
      `<ATTEMPT_DIR>/validate.out`, stderr to `<ATTEMPT_DIR>/validate.err`, timeout
      900 s. Exit 0: done stamp, status `done`. Exit 3: done stamp, status `empty`.
      Anything else, a timeout, or a missing executable (exit 127): failed stamp,
      reason `validate-exit-<code>` or `validate-timeout`, counted as an attempt.
   9. Write `<ATTEMPT_DIR>/attempt.json` (the same object as the stamp's attempt entry).
   10. On a done stamp, delete `failed-<DAY>.json` if present (its history is copied
       into the done stamp).
   11. Refinement: after the first day that fails in this run (any reason other than
       `budget-exhausted`), stop. Log `event=day-deferred` for the remaining days.
       A systemic fault (not logged in, a broken validator) then costs one attempt per
       run, not one per pending day.
7. Write `last-run.json`, log `event=end`, exit.

Exit codes for `run`: 0 when every day in the set ended done or empty, when the lock
was held, or when nothing was due. 1 when any day failed or was deferred. 2 for usage,
config or render errors. `last-run.json` and the `--json` report carry the registry `code`
of a failed run; see `docs/error-codes.md`.

#### 6.2.2 The child environment

`os.environ` of the run, minus every name in `SCRUBBED_ENV` (`invoke.py:134-147`) and
minus `CAIRN_INVOKE_DEPTH` and `CAIRN_INVOKE_CALLER` (`invoke.py:148-149`), plus:

```text
CAIRN_ROUTINE=<name>
CAIRN_ROUTINE_DAY=<DAY>
CAIRN_ROUTINE_ATTEMPT_DIR=<ATTEMPT_DIR>
```

Removing the depth variable matters for a manual run typed inside a peer: invoke
refuses a nested run when `CAIRN_INVOKE_DEPTH >= 1` (`invoke.py:1399-1404`). Open
question 8 asks whether a routine started from inside a peer should be refused instead.

#### 6.2.3 `--dry-run`

For each day in the set (or the `--day`), print:

```text
day 2026-10-27 attempt 2 (would create ~/.context/routines/daily/2026-10-27/attempt-2)
refresh: <argv>            (once, for the whole window)
prepare: <argv>
invoke:  <argv>
validate: <argv>
--- rendered prompt (1234 bytes) ---
<the rendered prompt text>
--- end ---
```

Argv lines use `shlex.join`. Nothing runs, nothing is written, no lock is taken. A
render error still exits 2. Otherwise exit 0.

#### 6.2.4 Log lines

The run appends its own lines to `<data_root>/logs/routine-<name>.log`, one line per
event, and prints the same line to stdout only when stdout is a TTY. Under launchd
stdout is the same log file (6.1), so this rule prevents double lines. launchd's
redirect then catches only unexpected output (a traceback).

Format: `<UTC ISO time> routine=<name> run=<run_id> event=<event> key=value ...`.
Time as `exchange.now_iso()` (`exchange.py:56-58`). A value with a space, `=` or `"`
is written as a JSON string. Events and keys:

| event | keys |
|---|---|
| `start` | `days=2026-10-25,2026-10-26` `force=false` `version=2.9.0` |
| `skipped-lock` | |
| `nothing-due` | |
| `refresh` | `source=claude` `since=2026-10-24` `exit=0` `seconds=4.1` |
| `day-start` | `day=` `attempt=` |
| `prepare` | `day=` `exit=` `seconds=` |
| `invoke` | `day=` `exit=` `thread=` `outcome=` `cost_usd=` (number or `null`) `seconds=` |
| `validate` | `day=` `exit=` `result=valid|empty|invalid` `seconds=` |
| `day-done` | `day=` `status=done|empty` `attempt=` `cost_usd=` |
| `day-failed` | `day=` `reason=` `attempts=` `cost_usd=` |
| `day-skipped` | `day=` `reason=done|attempts-exhausted|corrupt-stamp` |
| `day-superseded` | `day=` `attempt=` (a forced re-run of a done day: the done stamp moves into history until this attempt validates) |
| `day-deferred` | `day=` |
| `error` | `message=` |
| `end` | `exit=` `done=` `empty=` `failed=` `deferred=` `cost_usd=` |

Example:

```text
2026-10-28T05:20:41+00:00 routine=daily run=20261028T052003Z-1a2b3c4d event=invoke day=2026-10-27 exit=0 thread=6f0c1e2a-... outcome=succeeded cost_usd=0.84 seconds=612.3
```

Note: `docs/routine-hardening-spec.md` section 5 adds the events `skipped-paused`,
`skipped-disabled`, `stale-source`, `hook` and `disabled`. With `run --json` the log
lines are never echoed to stdout, which then holds the run report alone.

On a failure, the event line is followed by up to 20 lines of the failing child's
stderr tail, each prefixed with `      [stderr] `, as `sync.py:144-145` does.

Log rotation: reuse the 5 MB rule of `sync.py:80` with a routine-local copy of
`rotate_log` (UNVERIFIED that the sync helper takes a path argument; if it does not,
add one).

#### 6.2.5 Stamp files

Done stamp `done-2026-10-27.json`:

```json
{
  "routine": "daily",
  "day": "2026-10-27",
  "status": "done",
  "attempt": 2,
  "attempt_dir": "~/.context/routines/daily/2026-10-27/attempt-2",
  "peer": "claude",
  "profile": "scheduled",
  "thread_id": "6f0c1e2a-0000-4000-8000-000000000000",
  "prepare_exit": 0,
  "invoke_exit": 0,
  "validate_exit": 0,
  "cost_usd": 0.84,
  "cost_usd_day_total": 1.31,
  "started_at": "2026-10-28T05:20:05+00:00",
  "finished_at": "2026-10-28T05:31:02+00:00",
  "run_id": "20261028T052003Z-1a2b3c4d",
  "cairn_version": "2.9.0",
  "history": [
    {"attempt": 1, "reason": "validate-exit-1", "cost_usd": 0.47, "finished_at": "2026-10-27T05:29:10+00:00"}
  ]
}
```

`status` is `done` or `empty`. For `empty` from `prepare`, `invoke_exit`,
`validate_exit`, `thread_id` and `cost_usd` are `null`.

Failed stamp `failed-2026-10-27.json`:

```json
{
  "routine": "daily",
  "day": "2026-10-27",
  "status": "failed",
  "reason": "invoke-exit-124",
  "attempts": 2,
  "last_attempt_dir": "~/.context/routines/daily/2026-10-27/attempt-2",
  "prepare_exit": 0,
  "invoke_exit": 124,
  "validate_exit": null,
  "cost_usd_total": 1.31,
  "updated_at": "2026-10-28T06:20:00+00:00",
  "run_id": "20261028T052003Z-1a2b3c4d",
  "cairn_version": "2.9.0",
  "history": [
    {"attempt": 1, "reason": "validate-exit-1", "cost_usd": 0.47, "finished_at": "2026-10-27T05:29:10+00:00"},
    {"attempt": 2, "reason": "invoke-exit-124", "cost_usd": 0.84, "finished_at": "2026-10-28T06:20:00+00:00"}
  ]
}
```

Reasons: `incomplete-input`, `render-error`, `budget-exhausted`, `prepare-exit-<n>`,
`prepare-timeout`, `invoke-exit-<n>`, `validate-exit-<n>`, `validate-timeout`.
`attempts` counts only history entries with a counted reason (everything except
`incomplete-input`, `render-error` and `budget-exhausted`). Those three still go into
`history` with `"counted": false`.

`last-run.json`:

```json
{"run_id": "...", "started_at": "...", "finished_at": "...", "exit": 0, "days": ["2026-10-27"], "done": 1, "empty": 0, "failed": 0, "deferred": 0, "cost_usd": 0.84, "error": null}
```

Note: since `docs/routine-hardening-spec.md`, `last-run.json` is the run report of that
spec's section 5: these keys keep their names and meanings, and `schema_version`,
`routine`, `status`, `reason`, `stale_sources`, `breaker` and per-day `rows` are added.
`stale-source` joins the uncounted reasons.

Cost: Claude's result line has `usage.total_cost_usd` (`claude.py:156`, surfaced in the
result at `invoke.py:1211`). Codex usage is token counts with no cost
(`codex.py:209-217`), so a Codex routine always records `cost_usd: null` and the log
says `cost_usd=null`.

#### 6.2.6 Lock behaviour

- One lock per routine, non-blocking. A second `run` (launchd fire plus a manual run,
  or `kickstart` during a run) exits 0 with one log line.
- The lock is released when the process exits, even after a crash (`storage.py:3-4`).
- `routine run` takes no other cairn lock itself. Invoke takes its per-thread lock
  (`invoke.py:1486-1491`). The exporters take the export operation lock that `cairn
  sync` also meets (`sync.py:19`), so a refresh during an hourly sync waits or
  serialises there. That is the existing order. The routine lock sits outside all of
  them and never waits.

### 6.3 `cairn routine list [--json]`

One line per configured routine:

```text
daily  peer=claude profile=scheduled at=05:20 installed=yes last=2026-10-28T05:31:02+00:00 exit=0 pending=0 failed=0
```

`installed` is `yes` (plist on disk and `launchctl print gui/<uid>/<label>` exits 0),
`unloaded` (plist on disk, not loaded), `no`, `n/a` (not macOS), or `unknown` (the
probe did not answer in 5 s). `pending` counts
window days with no done stamp. `--json` prints a list of objects with the same keys.
Exit 0. No routines: `no routines configured` and exit 0.

### 6.4 `cairn routine status NAME [--json] [--lines N]`

Prints:

1. Config: peer, profile (and base), model, at, sources, catch_up_days, timeout_s,
   budget.
2. Resolved paths: prompt_file, cd, each `outputs` key and path, state dir, log, plist.
3. Install state (as in `list`).
4. Last run from `last-run.json`.
5. Per day in the window plus any older day with a stamp in the last 30 days, newest
   first: `2026-10-27 done attempt 2 cost 0.84`, `2026-10-26 failed (validate-exit-1) attempts 3/3`,
   `2026-10-25 pending`.
6. Last cost: `cost_usd` of the newest done stamp.
7. Log tail: the last `N` lines (default 20).

`--json` prints one object with those sections. Exit 0, or 2 for an unknown routine.

Note: `docs/routine-hardening-spec.md` section 3 replaces this exit rule. `status` now
exits 0 when the routine is fresh or running, 1 when it needs attention (never run,
stale, a failed day, a non-zero last run), and 2 when it is paused or disabled, or for
an unknown routine. The JSON gains `schema_version`, `state`, `exit`, `attention`,
`paused`, `disabled`, `last_run_age_s` and `stale_after_s`.

### 6.5 `cairn routine uninstall NAME`

1. Not macOS: exit 2, `cairn routine uninstall is launchd-only in this version`.
2. `launchctl bootout gui/<uid>/<label>`. Ignore "not loaded".
3. Remove the plist if present.
4. Keep stamps, attempt dirs and the log.
5. Print `Removed dev.cairn.routine.NAME (stamps kept in <state dir>)`, or
   `dev.cairn.routine.NAME is not installed; nothing to remove`. Exit 0.

The routine name need not still be in config, so a deleted routine can be uninstalled.
The name is still checked against the name regex before it becomes a path.

### 6.6 `cairn routine kickstart NAME [--kill]`

Runs `launchctl kickstart gui/<uid>/<label>`. With `--kill`, adds `-k`.

Refinement: the brief wraps `kickstart -k`. `-k` kills a running instance first, which
here means killing an invoke mid-round: the peer's spend is lost and the day records a
failed attempt (`invoke-exit-130`). Without `-k`, launchd leaves a running instance
alone, and the routine lock already makes a second start harmless. So the default is
without `-k`, and `--kill` is opt-in. Open question 9.

Not installed: exit 1, `dev.cairn.routine.NAME is not loaded; run 'cairn routine install NAME'`.
Not macOS: exit 2.

### 6.7 The environment block

Start from `background_environment(config)` (`runtime.py:30-54`), then:

1. Drop `CLAUDE_CONFIG_DIR` unless the install-time `os.environ` sets it.
2. Drop `CODEX_HOME` unless the install-time `os.environ` sets it.
3. Add `XDG_STATE_HOME` (resolved) when the install-time environment sets it, so the run
   finds the same state dir as the shell that installed it.

The result is `CAIRN_CONFIG`, `PATH`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
`QMD_CONFIG_DIR`, `INDEX_PATH`, plus the three conditional names. It holds no secrets,
the same promise as `runtime.py:33-34`.

Why steps 1 and 2. `background_environment` pins `CODEX_HOME` and `CLAUDE_CONFIG_DIR`
to `~/.codex` and `~/.claude` even when the host never set them (`runtime.py:37-42`,
emitted at `runtime.py:46-49`). With `CLAUDE_CONFIG_DIR` exported, `claude -p` looks for
its account file at `<CLAUDE_CONFIG_DIR>/.claude.json` instead of `~/.claude.json`,
prints `Claude configuration file not found at: ~/.claude/.claude.json`, then reports
`Not logged in`. The maintainer's session reproduced this from a LaunchAgent: the run
with the variable failed, and the same run without it logged in. This author did not
re-run that reproduction. `CODEX_HOME=~/.codex` equals Codex's default, so it is
probably harmless, but it is dropped for the same reason: a routine must not change
where a peer CLI finds its login.

Implementation: add a keyword to the shared helper rather than a copy:
`background_environment(config, *, pin_agent_homes: bool = True)`. With `False`, the
two names are included only when set in `os.environ`. Routines pass `False`. Existing
callers keep today's behaviour in this change (open question 2).

The same defect sits in the existing callers:

- the cron line (`admin.py:500-507`), which never runs `claude -p`, so it has not
  shown up there.
- the hook launcher file (`hooks.py:336-344`), which exports both names to every hook
  run;
- the Codex and Antigravity hook registrations (`harnesses/codex_registration.py:46`,
  `harnesses/antigravity_registration.py:58`).

Any of these that one day spawns `claude -p` would fail the same way.
`tests/test_runtime.py:26-28` asserts both names are present in the hook environment,
so changing the default for all callers also changes that test.

`HOME` is not in the block: launchd sets it for a gui-domain agent (UNVERIFIED on the
target macOS version; the test plan's manual check confirms it with `launchctl print`).

---

## 7. Catch-up and the refresh window

### 7.1 Day set

```text
today_utc = datetime.now(UTC).date()
window    = [today_utc - k days for k in catch_up_days .. 1]     # oldest first
```

With `--day D`: the set is `[D]`. A done day is skipped (`day-skipped reason=done`)
unless `--force`. A day at the cap is skipped (`reason=attempts-exhausted`) unless
`--force`.

Without `--day`, for each day in `window`:

- `done-<DAY>.json` exists: not in the set (no log line, to keep daily logs short).
- `failed-<DAY>.json` with `attempts >= 3`: skipped, `day-skipped reason=attempts-exhausted`.
- otherwise: in the set.

`--force` without `--day` re-runs every window day, done or not. That is rarely wanted,
so it prints a warning first: `--force without --day re-runs every day in the window`.

A day older than the window is never picked up again by the schedule. `status` shows it
as `expired` when it has no done stamp. `run --day` handles it.

### 7.2 Why ISO dates for `--since`

The export window is `--since`, parsed by `parse_since` (`timeutil.py:30-55`). A
duration such as `3d` is relative to now (`timeutil.py:46-49`). An ISO date is
`datetime.fromisoformat(raw).timestamp()` with a naive datetime (`timeutil.py:50-51`),
so it means local midnight of that date. The Claude exporter keeps a session when its
newest mtime is at or after the cutoff (`sources/claude.py:907-911`).

A UTC day `D` starts at `D 00:00 UTC`. Local midnight of `D` is later than that when the
local zone is behind UTC (local midnight at UTC-8 is `D 08:00 UTC`), which would miss
eight hours of `D`. Local midnight of `D - 1` is at most `D 00:00 UTC` for every offset
from UTC-24 to UTC+24. So:

```text
since = (min(day set) - 1 day).isoformat()       # "YYYY-MM-DD"
```

This is deterministic: it does not depend on when the run starts, unlike `Nd`.

### 7.3 Refresh

For each entry in `sources`, in order, run
`[sys.executable, "-m", "cairn", <source>, "export", "--since", since]` (the
`sync._export` shape, `sync.py:52-54`) through `procs.run_capped` with a 600 s timeout
(sync uses 120 s, `sync.py:132`, but a multi-day window can be larger). Environment:
the child environment of 6.2.2 without the three `CAIRN_ROUTINE_*` names.

Every listed source is required in v1. A non-zero exit or a timeout fails the refresh,
and step 5 of 6.2.1 applies. The routine never calls `cairn sync` and never runs
`qmd update`: a digest reads the exported files through the catalog (`store.py:252-264`),
not through the index.

### 7.4 Worked example: machine asleep for two days

Setup: London, November, so local time equals UTC. Routine `daily`, `at` `05:20`,
`catch_up_days` 3, sources `claude, codex`.

1. Monday 2026-11-09 05:20: the job runs. Window is 11-06, 11-07, 11-08. All three
   have done stamps except 11-08. It runs 11-08: refresh `--since 2026-11-07`, invoke,
   validate exit 0. Stamp `done-2026-11-08.json`.
2. Monday evening: the lid closes. The machine sleeps through Tuesday 05:20 and
   Wednesday 05:20.
3. Thursday 2026-11-12 08:10: the lid opens. launchd coalesces the two missed intervals
   and starts the job once, now (`launchd.plist(5)`). Thursday's own 05:20 interval also
   passed during sleep, so it is part of the same coalesced start.
4. `today_utc` = 11-12. Window = 11-09, 11-10, 11-11. No stamps for any of them. Day
   set = `[11-09, 11-10, 11-11]`.
5. `since` = 11-09 minus one day = `2026-11-08`. One refresh per source:
   `cairn claude export --since 2026-11-08`, `cairn codex export --since 2026-11-08`.
   Local midnight 2026-11-08 = 00:00 UTC, which covers all of 11-09 onward.
6. Day 11-09: attempt 1, prepare, invoke, validate exit 0, done stamp. Day 11-10:
   validate exit 3 (nothing happened that day), done stamp with status `empty`. Day
   11-11: invoke exit 124 (wall clock), failed stamp `attempts: 1`, and the run stops
   (6.2.1 step 6.11). Exit 1.
7. Friday 2026-11-13 05:20: window = 11-10, 11-11, 11-12. 11-10 is done. 11-11 has a
   failed stamp with 1 attempt, so it runs again as attempt 2. 11-12 runs after it.
   `since` = `2026-11-10`.

Variant: the machine is powered off from Monday evening to Thursday 08:10. launchd does
not run a job for intervals missed while powered off, so nothing runs at 08:10. Friday
05:20 sees window 11-10, 11-11, 11-12. Day 11-09 is now outside the window. `status`
shows it as `expired`, and `cairn routine run daily --day 2026-11-09` recovers it.
This is the case that `catch_up_days` sizes.

---

## 8. The `journal` collection

### 8.1 Mechanism

`_ensure_collections` (`sync.py:242-272`, called under the index lock at
`sync.py:343-347`) loops over `config.collections`, skips disabled ones, and calls
`ensure_collection(name, config.output_dir(name), ...)`. `ensure_collection`
(`qmd.py:151-190`) skips a directory without markdown, returns `exists` for a known
collection, and otherwise runs `qmd collection add <path> --name <name>`
(`qmd.py:181`). `output_dir(name)` is `data_root / name` (`config.py:234-236`).

So a config entry `"journal": {"sync": {"enabled": true}}` registers
`<data_root>/journal` as the qmd collection `journal` on the first `cairn sync` after it
holds a `.md` file, and self-heals like every other collection. No loop change.

Refinement: the brief asks for "type notes, pattern `**/*.md`". cairn passes no mask to
`qmd collection add` (`qmd.py:181`), so the pattern is qmd's default. That default is
UNVERIFIED as `**/*.md`; check `qmd collection add --help` before writing the docs. If
it differs, add an optional `extra.mask` to the collection and pass `--mask` (or
qmd's equivalent flag, UNVERIFIED) in `ensure_collection`. "Type notes" has no meaning
in qmd or in cairn today. It becomes the label in 8.2.

Other code that walks collections and how a store-less, source-less collection behaves:

- `_plan` (`sync.py:57-77`) walks the registry bindings, not config, so nothing tries to
  export `journal`.
- Doctor's source check skips a collection with no store (`admin.py:855-856`).
- `_collection_view` labels every store-less collection `service-docs`
  (`admin.py:127`). Change: `"service-docs" if coll.service_sources or name ==
  "service-docs" else "notes" if coll.store is None else "sessions"`.
- `config set journal.enabled false` works through the two-part key path
  (`admin.py:268-281`).
- `_collection_of` and `_qmd_folder` (`invoke.py:453-460`, `exchange.py:356-363`) only
  map paths to names. No effect.

A routine whose `journal_dir` is not under `<data_root>/journal` is allowed. The
collection then does not index it, and doctor says so (8.2).

### 8.2 Doctor lines

The existing registration loop (`admin.py:865-884`) already prints, for `journal`:

```text
  ok   collection [journal] - registered in qmd
  warn collection [journal] - has markdown but is not a qmd collection; run 'cairn sync'
```

The new routines section (6.8 below) adds one line per routine:

```text
  ok   routine [daily] journal - ~/.context/journal is the 'journal' collection
  warn routine [daily] journal - journal_dir ~/.context/journal has no 'journal' collection in config; add collections.journal to index it
  warn routine [daily] journal - journal_dir ~/notes/journal is outside <data_root>/journal; qmd does not index it through cairn
```

### 8.3 (6.8) Doctor routines section

Placed after `_check_invoke` (`admin.py:886-887`). Warn-only, never a critical failure,
like the invoke checks (`admin.py:708-713`). Per routine:

```text
  ok   routine [daily] launchd - dev.cairn.routine.daily loaded, daily at 05:20 local
  warn routine [daily] launchd - not installed; run 'cairn routine install daily'
  warn routine [daily] launchd - plist present but not loaded; run 'cairn routine install daily'
  warn routine [daily] launchd - launchd only; not installed on this OS
  ok   routine [daily] last run - 2026-10-28T05:31:02+00:00 exit 0 (done 1, empty 0, failed 0)
  warn routine [daily] last run - 2026-10-28T05:31:02+00:00 exit 1 (failed 1: 2026-10-27 validate-exit-1)
  warn routine [daily] last run - never ran
  ok   routine [daily] stamps - window 2026-10-25..2026-10-27 all done
  warn routine [daily] stamps - pending 2026-10-26; failed 2026-10-27 (invoke-exit-124, 3/3 attempts)
  <journal line from 8.2>
```

With no routines configured, the section prints nothing. "Loaded" is `launchctl print
gui/<uid>/<label>` exiting 0. Exit codes of `launchctl print` for an unknown label are
UNVERIFIED beyond "non-zero".

---

## 9. `config set` and `config show`

### 9.1 Today

`_set_dotted` (`admin.py:234-288`) handles flat keys: top-level scalars, `cron.schedule`,
`invoke.<peer>.<field>`, `invoke.antigravity.billing.<field>` and
`<collection>.<field>`. Values are strings, coerced per field (`admin.py:210-231`).
`_edit_config` writes, re-validates and restores on failure (`admin.py:291-329`).

### 9.2 New keys

| Key | Value | Effect |
|---|---|---|
| `routines.<name>` | JSON object | create or replace the whole routine |
| `routines.<name>.<scalar>` | string | set one field of an existing routine |
| `routines.<name>.sources` | JSON array, or comma list `claude,codex` | set the list |
| `routines.<name>.prepare` | JSON array or `null` | set or clear |
| `routines.<name>.validate` | JSON array | set |
| `routines.<name>.outputs.<key>` | string path, or `null` | set or remove one output |
| `invoke.<peer>.profiles.<pname>` | JSON object | create or replace a profile |
| `invoke.<peer>.profiles.<pname>.base` | string | set the base |
| `invoke.<peer>.profiles.<pname>.extra_args` | JSON array | set the list |

Scalars for `routines.<name>.<scalar>`: `peer`, `prompt_file`, `cd`, `profile`,
`model`, `at`, `day`, `catch_up_days`, `timeout_s`, `budget_usd_per_day`. Coercion:
`catch_up_days` and `timeout_s` to int (`null` allowed for `timeout_s`),
`budget_usd_per_day` to float or `null`, `profile` and `model` accept `null`. Everything
else stays a string.

Arrays: a value whose first non-space character is `[` is parsed as JSON and must be a
list of strings. An argv field (`prepare`, `validate`, `extra_args`) accepts only JSON,
because an argv element may contain a comma. `sources` also accepts a comma list.
Objects: a value whose first non-space character is `{` is parsed as JSON.

A field-level key on a routine or profile that does not exist yet is refused, because
the half-built entry would fail validation and `_edit_config` would restore anyway:
`routine 'daily' does not exist; create it with: cairn config set routines.daily '{"peer": ..., ...}'`.

Example:

```bash
cairn config set invoke.claude.profiles.scheduled '{"base": "edit", "extra_args": ["--permission-mode", "dontAsk", "--add-dir", "{{JOURNAL_DIR}}"]}'
cairn config set routines.daily '{"peer": "claude", "prompt_file": "~/projects/site/prompts/daily.md", "cd": "~/projects/site", "profile": "scheduled", "at": "05:20", "validate": ["true"]}'
cairn config set routines.daily.sources claude,codex
cairn config set routines.daily.validate '["pnpm", "--dir", "~/projects/site", "validate", "--day", "{{DAY}}"]'
```

New action `cairn config unset KEY` for `routines.<name>`,
`routines.<name>.<optional field>`, `routines.<name>.outputs.<key>` and
`invoke.<peer>.profiles.<pname>`. It removes the key and re-validates through
`_edit_config`. Removing a profile a routine uses fails validation and is restored,
with the 2.8 message. `unset` of a routine prints a reminder:
`removed routines.daily; its launchd job stays until 'cairn routine uninstall daily'`.

The unsupported-key message at `admin.py:283-288` and the `KEY` help at
`admin.py:423-430` gain the new forms.

### 9.3 `config show`

JSON (`admin.py:163-177`) gains a top-level `"routines"` object. Each value holds the
raw fields plus `"resolved"`: `prompt_file`, `cd`, `outputs` (expanded paths),
`state_dir`, `log`, `plist`, `label`. `invoke.<peer>.profiles` appears through
`asdict` (2.7).

Text (`admin.py:179-194`): after the `invoke` lines, one line per peer profile and one
block per routine:

```text
  claude profile scheduled = base edit + --permission-mode dontAsk --add-dir {{JOURNAL_DIR}}
routines    :
  daily                  claude/scheduled at 05:20 local, catch-up 3d, sources claude,codex
      prompt_file = ~/projects/site/prompts/daily.md
      cd          = ~/projects/site
      outputs.journal_dir = ~/.context/journal
```

---

## 10. Test plan

`unittest`, in the style of the existing suites. New file `tests/test_routine.py`, and
additions to existing files as named. All fixtures use temp dirs, `x@y.z`-style values
and `/tmp/...` paths. launchctl, `claude` and `codex` are never run for real: stub
`subprocess.run` for launchctl, and reuse the peer stubs under `tests/fixtures/invoke/`
for invoke.

`tests/test_config.py` (or a new `tests/test_config_routines.py`):

- `test_routine_minimal_parses_with_defaults`: profile from the peer policy, day
  `yesterday-utc`, catch-up 3, `journal_dir` default added.
- `test_routine_name_regex_rejects_bad_names`: `Daily`, `a/b`, `..`, `-x`, 65 chars.
- `test_routine_unknown_key_rejected`: exact 2.8 message.
- `test_routine_relative_path_rejected`: `prompt_file: "prompts/a.md"` fails with the
  absolute-path message.
- `test_routine_path_with_spaces_allowed`: `~/My Notes/p.md` loads.
- `test_routine_dollar_var_not_expanded`: `$HOME/p.md` is rejected as relative.
- `test_routine_path_rejects_day_variable`: `{{DAY}}` in `outputs` fails.
- `test_routine_outputs_key_collision_rejected`: `day`, `attempt_dir`.
- `test_routine_sources_must_be_enabled_windowed`: `service-docs` and a disabled
  collection both fail with their messages.
- `test_routine_budget_is_claude_only`.
- `test_routine_validate_must_be_argv_list`: a string fails.
- `test_routine_profile_must_resolve_for_peer`.
- `test_profiles_parse_and_shadow_rejected`: `review` as a name fails.
- `test_profiles_extra_args_must_be_list`: a string fails.
- `test_profiles_antigravity_rejected`.
- `test_peer_profile_may_name_config_profile`: `invoke.claude.profile: "scheduled"` loads.

`tests/test_invoke_foundations.py`:

- `TestClaudeArgv.test_config_profile_base_then_extra_args`: base `edit`, extras pass
  through after the base tuple, before `--model`, prompt after `--`.
- `TestClaudeArgv.test_permission_mode_extra_replaces_base_value`: one
  `--permission-mode` in argv, value `dontAsk`.
- `TestClaudeArgv.test_disallowed_tools_extra_merges_into_review_base`.
- `TestClaudeArgv.test_looser_permission_mode_rejected`: `auto` on `edit`.
- `TestClaudeArgv.test_allowed_tools_takes_one_joined_element`: a split list fails
  with the 4.3 message.
- `TestClaudeArgv.test_space_form_bash_rule_warns`: `Bash(cairn *)` gives the warning,
  `Bash(cairn:*)` does not.
- `TestClaudeArgv.test_forbidden_flags_rejected`: `--model`, `-p`,
  `--dangerously-skip-permissions` on `edit`.
- `TestCodexArgv.test_config_profile_extra_args_after_sandbox`.
- `TestCodexArgv.test_resume_drops_add_dir_and_keeps_config_pairs`: warning recorded.
- `TestCodexArgv.test_forbidden_config_keys_rejected`: `-c sandbox_mode=...`, `-s`.
- `TestOwnerTail.test_owner_emitted_after_links_only_when_set`: byte-identical without.
- `TestOwnerTail.test_set_owner_survives_reexport`: mirror of
  `TestLinksSurviveReexport` (`tests/test_invoke_foundations.py:221`).
- `TestOwnerTail.test_exchange_head_rejects_owner_key`.

`tests/test_invoke.py`:

- `TestClaudeRun.test_profile_name_resolution_unknown_profile_usage_error`: exit 2 and
  the 4.2 message.
- `TestClaudeRun.test_permission_mode_flag_wins_over_config_profile_mode`: the
  argv has one `--permission-mode` with the flag's value and still carries the
  profile's `--allowedTools` and `--disallowedTools`.
- `TestClaudeRun.test_permission_mode_flag_with_builtin_profile_unchanged`: today's
  whole-tuple replacement.
- `TestCodexRun.test_sandbox_flag_with_config_profile_keeps_extra_args`.
- `TestClaudeRun.test_template_profile_without_args_file_refused`.
- `TestClaudeRun.test_profile_args_file_replaces_extra_args`: recorded in
  `thread.json` `profile_args`, rendered in the round `native_policy`.
- `TestClaudeRun.test_resume_reuses_recorded_profile_args_not_config`: edit the config
  profile between rounds, resume without `--profile`, argv unchanged.
- `TestClaudeRun.test_resume_explicit_profile_wins`.
- `TestClaudeRun.test_owner_in_state_result_and_peer_front_matter`: `owner:
  routine:daily` in the exported peer document and in the result line.
- `TestClaudeRun.test_owner_change_on_resume_refused`.
- `TestClaudeRun.test_owner_without_caller_is_warning_not_refusal`: exit 0, warning in
  `warnings`, preamble names the owner.
- `TestCodexRun.test_codex_config_profile_end_to_end`.

`tests/test_routine.py`:

- `TestTemplate.test_renders_all_variables`: each of `DAY`, `DATA_ROOT`,
  `ATTEMPT_DIR`, `DIGEST_DIR`, `JOURNAL_DIR`.
- `TestTemplate.test_unknown_variable_errors_with_location`: exit 2, file and line in
  the message, no stamp, no attempt dir.
- `TestTemplate.test_single_pass_no_rescan`: an output path containing `{{DAY}}`
  literally is not expanded twice.
- `TestTemplate.test_spaced_braces_are_literal`.
- `TestTemplate.test_tilde_expanded_only_at_element_start`.
- `TestDays.test_window_oldest_first_skips_done`.
- `TestDays.test_attempt_cap_skips_day`: three counted failures, day skipped.
- `TestDays.test_uncounted_failures_do_not_consume_attempts`.
- `TestDays.test_day_flag_rejects_today_and_future`.
- `TestDays.test_force_new_attempt_numbering`: attempts 1 and 2 exist, `--force` makes
  `attempt-3`, done stamp history keeps the earlier entries.
- `TestRefresh.test_since_is_earliest_day_minus_one_iso`: argv shows
  `--since 2026-11-08` for set `[11-09, 11-10, 11-11]`.
- `TestRefresh.test_failed_refresh_marks_incomplete_input_and_skips_invoke`.
- `TestRefresh.test_never_calls_cairn_sync`: no `sync` in any recorded argv.
- `TestRun.test_stamp_written_only_after_validate`: patch validate to block on a
  fifo, assert no done stamp exists while it waits, then release.
- `TestRun.test_validate_exit_3_is_empty_day`.
- `TestRun.test_validate_failure_failed_stamp`.
- `TestRun.test_invoke_nonzero_skips_validate`: validate stub never runs, reason
  `invoke-exit-124`.
- `TestRun.test_prepare_exit_3_skips_invoke`.
- `TestRun.test_first_failure_defers_remaining_days`.
- `TestRun.test_cost_recorded_in_log_and_stamp`: Claude stub result with
  `total_cost_usd`.
- `TestRun.test_codex_cost_is_null`.
- `TestRun.test_budget_passes_remaining_via_extra_last`: argv ends with
  `--extra --max-budget-usd 3.53`.
- `TestRun.test_budget_exhausted_no_invoke`.
- `TestRun.test_child_env_scrubbed`: no `CLAUDE_CODE_SESSION_ID`, no
  `CAIRN_INVOKE_DEPTH`, has `CAIRN_ROUTINE`.
- `TestRun.test_invoke_argv_shape`: `--owner routine:daily`, `--json`, `--cd`,
  `--profile-args-file` only for a config profile.
- `TestRun.test_dry_run_writes_nothing`: no dirs, no stamps, no lock file contention.
- `TestRun.test_log_line_format`: regex over every line, no duplicate lines when
  stdout is not a TTY.
- `TestLock.test_second_run_exits_zero_with_skip_line`: hold the lock in a child
  process (pattern of `tests/test_runtime.py:138`), run, assert exit 0 and one
  `skipped-lock` line.
- `TestPlist.test_render_matches_template`: `plistlib.loads` of the output has Label,
  ProgramArguments ending `routine run daily`, `StartCalendarInterval` Hour 5 Minute 20,
  ProcessType Background, both log paths, RunAtLoad false.
- `TestPlist.test_env_block_omits_agent_homes_unless_set`: no `CLAUDE_CONFIG_DIR` or
  `CODEX_HOME` with them unset; present and resolved when set.
- `TestPlist.test_xml_escaping`: a `data_root` with `&` survives a round trip.
- `TestInstall.test_install_boots_out_then_bootstraps`: recorded launchctl calls in
  order.
- `TestInstall.test_non_macos_prints_crontab_line_exit_2`.
- `TestInstall.test_preflight_missing_prompt_exit_2`.
- `TestUninstall.test_keeps_stamps`.
- `TestKickstart.test_default_without_k_and_kill_flag_adds_k`.
- `TestStatus.test_status_json_sections`.

`tests/test_admin_config.py`:

- `TestConfigSetRoutines.test_set_whole_routine_then_show_round_trip`: `set` the
  object, `show --json` returns the same raw fields plus `resolved`.
- `TestConfigSetRoutines.test_field_on_missing_routine_refused`.
- `TestConfigSetRoutines.test_sources_comma_list_and_json`.
- `TestConfigSetRoutines.test_validate_requires_json_array`.
- `TestConfigSetRoutines.test_outputs_key_set_and_null_removes`.
- `TestConfigSetRoutines.test_unset_profile_in_use_restored`.
- `TestConfigSetProfiles.test_set_profile_object_round_trip`.
- `TestConfigSetProfiles.test_profiles_scalar_key_refused`: `invoke.claude.profiles x`.
- `TestDoctorRoutines.test_routines_section_lines`: stub launchctl, stamps on disk.
- `TestDoctorRoutines.test_journal_line_variants`.
- `TestConfigShow.test_notes_type_for_storeless_collection`.

`tests/test_sync.py`:

- `test_journal_collection_registers_when_markdown_present`: `qmd collection add` for
  `<data_root>/journal --name journal` once a `.md` exists; skipped before.

`tests/test_runtime.py`:

- `test_background_environment_can_omit_agent_homes`.

`tests/test_config_examples.py`: the existing byte-equality test
(`tests/test_config_examples.py:20-29`) covers the example edits. Add
`test_packaged_example_loads`: `load_config_from` on a temp copy of the template
succeeds, so the example routine and profile pass validation.

Manual check before release (not CI): on a Mac, `cairn routine install`, then
`launchctl print gui/$(id -u)/dev.cairn.routine.<name>` shows the env block and `HOME`,
then `cairn routine kickstart <name>` and `cairn routine status <name>`.

---

## 11. Docs to update

- `README.md`: a "Scheduled routines" subsection under "Keeping it fresh"
  (`README.md:274`), and the `routines` and `invoke.<peer>.profiles` blocks under
  "Configuration" (`README.md:234`).
- `CHANGELOG.md`: a `## [2.9.0]` entry above `## [2.8.1]` with Added (routine commands,
  config profiles, `--owner`, `--profile-args-file`, the journal collection label,
  `config unset`) and Changed (`--profile` accepts config profile names,
  `config set` takes JSON objects and arrays for the new keys).
- `skills/cairn/SKILL.md`: the routine commands under "Top-level commands"
  (`SKILL.md:87`), the profiles and owner under "Peer agents and fan-out health"
  (`SKILL.md:104`), and a pointer to the new workflow page next to the
  `workflows/log-session.md` pointer (`SKILL.md:153`).
- `skills/cairn/workflows/routine.md` (new): set up a routine end to end, the template
  variables, the validate exit contract (0, 3, other), the `//` path rule for Claude
  permission rules, reading `status`, recovering an expired day with `--day`, and what
  `--force` does.
- `cairn.config.example.json` and `src/cairn/config.example.json`: section 2.9.
- Version 2.8.1 to 2.9.0 in all four places: `src/cairn/__init__.py:11`,
  `plugin.json:4`, `.claude-plugin/plugin.json:3`, `.codex-plugin/plugin.json:3`
  (`tests/test_plugin_manifest.py:24-28` locks the manifests to `__version__`).
- `__main__.py` help text (section 6).

---

## 12. Open questions for the maintainer

1. Keep `prepare` (2.2)? Without it the peer must run `cairn recall digest` itself,
   which needs a `Bash(cairn recall digest:*)` allow rule in the scheduled profile and
   spends peer tokens on an empty day.
2. Fix the `CLAUDE_CONFIG_DIR` / `CODEX_HOME` pinning for every caller of
   `background_environment` (cron, hook launcher, Codex and Antigravity hook
   registrations), or only for routines in this change? A global fix changes
   `tests/test_runtime.py:26-28` and the recorded hook environment of existing installs
   (they refresh on the next `cairn hook install`).
3. Allow `peer: antigravity` later? It needs the billing and permission pre-checks
   (`invoke.py:1442-1471`) surfaced in `routine install`, and it reports no cost.
4. Should `day` gain `yesterday-local`? A user far from UTC may expect a local day.
5. Should some `sources` be optional (refresh failure logged but not
   `incomplete-input`)? A syntax could be `"cursor?"`.
6. Should the config template ship an example routine (2.9), or only the profile and a
   comment?
7. Template escape: is a literal `{{DAY}}` in a prompt ever needed? If so, pick a form
   (`{{{{DAY}}}}` or `\{{DAY}}`).
8. A `routine run` started from inside a peer (`CAIRN_INVOKE_DEPTH >= 1`): this spec
   strips the variable so the run works. Refuse instead?
9. `kickstart`: default without `-k` (this spec) or with `-k` (the brief)?
10. Fixed timeouts: `prepare` 600 s, `validate` 900 s, refresh 600 s per source.
    Make them routine fields? Also `idle_timeout_s` comes from `invoke.<peer>` (900 s
    by default, `config.py:190`). A long quiet tool call in a journal run could hit it.
    Add a routine-level override?
11. The attempt cap is a constant 3. Make it a field?
12. Side finding: `cairn invoke claude --permission-mode` offers `default`
    (`invoke.py:2535`). `claude` 2.1.312 lists `acceptEdits, auto, bypassPermissions,
    manual, dontAsk, plan`, with no `default`. Update the choices in a separate change?
13. Attempt dirs under `<data_root>/routines/` are never pruned. Add a retention rule
    (for example keep 30 days)?

---

## 13. How subagents implement this

Three stages in dependency order. Each stage ends with a green `make check` (ruff, ruff
format, mypy, unittest, `CLAUDE.md` "Gate"). Each stage is one commit on `feat/routine`
with no attribution trailer. Before each commit, re-read the diff for home paths, names
and emails (`CLAUDE.md` "Public-repo hygiene").

### Stage C: profiles, `--owner`, config

1. `config.py`: `ProfileSpec`, `PeerPolicy.profiles`, `resolve_profile`, profile parsing
   and the 2.8 profile messages. Peer `profile` check through `resolve_profile`.
2. `config.py`: `RoutineSpec`, `CairnConfig.routines`, routine parsing after `invoke`,
   the routine path helper (2.4), the 2.8 routine messages.
3. Harnesses: `policy_argv(..., profile_args)` with the 4.3 merge and validation rules.
   Put the rule tables in each harness module, called from config validation too.
4. `harnesses/__init__.py`: `InvokeRequest.profile_args`.
5. `invoke.py`: `--profile` without `choices`, `--profile-args-file`, `--owner`,
   `Settings` fields, resume precedence (4.5), native flag over a config profile
   (mode only, 4.2), template refusal,
   preamble owner, round and result `owner`.
6. `exchange.py`: `ThreadState.owner`, `profile_base`, `profile_args`; owner in the
   preserved tail of the exchange document.
7. `frontmatter.py` and `mutations.py`: `owner` tail key, `set_owner`;
   `_step_exported` calls it.
8. `admin.py`: `_INVOKE_FIELDS` minus `profiles`; `_set_dotted` routine and profile
   keys; `config unset`; `config show` routines and profiles; `_collection_view`
   `notes` type.
9. Tests from section 10 for config, harness argv, invoke, owner, admin config.
10. `make check`.

### Stage D: the routine command

Needs stage C (`resolve_profile`, `--owner`, `--profile-args-file`, `RoutineSpec`).

1. `runtime.py`: `pin_agent_homes` keyword (6.7).
2. `src/cairn/routine.py`: paths (6.0), template renderer (3), day set (7.1), refresh
   (7.3), the run loop (6.2.1), child environment (6.2.2), stamps and `last-run.json`
   (6.2.5), log writer (6.2.4), `--dry-run`.
3. `routine.py`: `render_plist` (pure), `install`, `uninstall`, `kickstart`, `list`,
   `status`, with all launchctl calls behind one small wrapper that tests stub.
4. `__main__.py`: `_ADMIN` entry and help lines.
5. Tests from section 10 for `tests/test_routine.py` and `tests/test_runtime.py`.
6. `make check`.

### Stage E: doctor, docs, examples

Needs stage D (stamp and `last-run.json` shapes, plist label).

1. `admin.py`: the doctor routines section and journal lines (8.2, 8.3).
2. Both example configs (2.9), copied byte for byte. Run
   `tests/test_config_examples.py`.
3. Check qmd's default mask for `collection add` (8.1) and record the result in the
   workflow page.
4. README, CHANGELOG, `SKILL.md`, `skills/cairn/workflows/routine.md`.
5. Version 2.9.0 in the four files (section 11).
6. Doctor and journal tests from section 10.
7. `make check`, then the manual launchd check (section 10, last paragraph).

## 14. Maintainer answers (proposed by the implementing agent, 2026-10-28, pending the owner's review)

1. Keep `prepare`. The digest is deterministic and belongs before the peer; exit 3 skipping invoke on an empty day is the right economy.
2. Fix the `CLAUDE_CONFIG_DIR` / `CODEX_HOME` pinning for every caller, as its own commit inside this PR, with the test update. It is a defect, not a routine feature.
3. `peer: antigravity` later; reject it with a clear error in v1.
4. No `day: yesterday-local`; cairn dates are UTC and the docs say so.
5. `sources` stays required.
6. Ship an example routine in both config examples, named `example-daily`, so `config show` and the docs have something to point at; `routine run` on it must fail clearly because its prompt file does not exist.
7. No template escape in v1.
8. Refuse a routine run started inside a peer.
9. `kickstart` without `-k` by default, `--force-restart` adds `-k`.
10. `timeout_s` per routine as specified; idle timeout stays the invoke config value.
11. Attempt cap stays a constant (3).
12. The `--permission-mode` choices mismatch is a separate follow-up issue.
13. No retention rule in v1; note it as a follow-up.
