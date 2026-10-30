# 16 skills-refresh: every stacked capability reachable through the skills

Status: implemented in this PR (branch `shav/mem-16-skills-refresh`), on top of the
2.20.0 stack (memory import). Section 11 lists where the implementation differs from the
text around it; where they differ, section 11 wins.

Every `path:line` in sections 2-10 cites the tree at 2.19.3 (layer 14), whose `skills/`
and `references/` trees matched the layer 15 tree when this was written, except for the
memory-import text layer 15 added (section 11).

**Dependencies.** 04 (routine status exit 0/1/2, pause/resume, `last_run` in `--json`),
05 (`cairn recall gaps`, gap kinds, `status`), 06 (`recall verify`, citation grammar),
07 (`decide`, `supersede`, `recall decisions`), 08 (`forget`, the ledger), 09
(`cairn instructions`), 10 (`cairn errors CODE`, `code:` lines, the code list), 11 (the
routing test this layer extends), 12 (the docs-CLI test every new example must pass), 13
(the rg-line contract in the recall and search skills), 14 (docs index, discrimination
evidence), 15 (`cairn claude memory import`, `--source memory`, `memory:<label>` session
ids). This layer changes no Python under `src/`.

## 1. Purpose and non-goals

The owner drives cairn through skills (`/cairn:*` in Claude Code, `$name` in Codex), not
through the CLI. Layers 01-15 added verbs and states that a skill can reach only if the
model happens to read the right workflow file. This layer makes each capability
reachable: a quoted trigger in a description, an imperative step with the exact command
and what to read from its output, a routing-eval row, and the Codex spelling. It adds two
workflows (daily check, memory import) and one repo-local developer skill, `skill-review`,
that all three agents use to review a skill before it ships.

Non-goals: no CLI change; no new shipped skill; no change to the recall skill's
temporal/topic flow or One Thing, the search skill's rg pass, the citation grammar block,
or the setup skill's consent questions (section 9.4).

## 2. Part 1: rules that apply (best practices)

Condensed references for every source ship with `skill-review` in
`.agents/skills/skill-review/references/` (Part 5). The rules this layer applies:

| Rule | Where it comes from |
|---|---|
| The description is the only text that decides whether a skill loads; it says what and when, in the third person, with the words users say | [best practices, "Writing effective descriptions"](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices); [overview, "Level 1: Metadata"](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) |
| `description` at most 1,024 characters, no XML tags; `name` lowercase-hyphen, at most 64, equals the folder | [agentskills.io specification](https://agentskills.io/specification); best practices, "Skill structure" |
| Claude Code truncates description plus `when_to_use` at 1,536 characters and drops descriptions of least-used skills when the listing (about 1% of context) overflows: put the key use case first | [Claude Code skills, "Skill descriptions are cut short"](https://code.claude.com/docs/en/skills) |
| Codex lists skills in at most 2% of context (8,000 characters when unknown) and "shortens skill descriptions first": front-load trigger words | [Codex skills](https://learn.chatgpt.com/docs/build-skills) (redirect from developers.openai.com/codex/skills) |
| Body under 500 lines; detail in files linked one level deep; a linked file over 100 lines opens with a contents list | best practices, "Progressive disclosure patterns", "Structure longer reference files"; specification, "Progressive disclosure" |
| Numbered imperative steps; exact commands for fragile work; validate-fix-repeat loops; one term per concept; no time-sensitive text | best practices, "Workflows and feedback loops", "Set appropriate degrees of freedom", "Content guidelines"; [skill-creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md) ("Prefer using the imperative form") |
| `allowed-tools` grants permission for the invoking turn; it "does not restrict which tools are available". Experimental in the open spec. Codex documents only `name` and `description` | Claude Code skills, "Pre-approve tools for a skill"; specification, "allowed-tools field"; Codex skills, "Optional metadata" |
| `when_to_use` is Claude Code only; the repo already forbids it in the invoke skills (`tests/test_plugin_manifest.py:94-101`) | Claude Code skills, "Frontmatter reference" |
| Plugin skills are `/plugin:skill` in Claude Code; Codex uses `$skill` or `/skills`; explicit-only in Codex is `policy.allow_implicit_invocation: false` in `agents/openai.yaml` | Claude Code skills, "How a skill gets its command name"; Codex skills |
| Naming: gerund preferred, noun or action forms acceptable; avoid vague names | best practices, "Naming conventions" |
| Versioning: the open spec has `metadata.version` only; cairn versions every user-facing file through `__version__` (`.github/workflows/ci.yml:50-61`) | specification, "metadata field" |
| Testing: build evaluations first; trigger evals of 8-10 should-trigger and 8-10 near-miss should-not rows; test on each target model | best practices, "Evaluation and iteration"; skill-creator, "Description Optimization"; Claude Code skills, "Skill not triggering" (`claude plugin eval` with a `tool_used: Skill` grader) |

Good and bad descriptions (best practices): "Extract text and tables from PDF files, fill
forms, merge documents. Use when working with PDF files or when the user mentions PDFs,
forms, or document extraction." against "Helps with documents". A YAML trap the docs do
not name: a plain-scalar description that contains ` #` or `: ` is cut by a YAML parser.
The draft recall description hit the first (`## Decisions`); the final text avoids both.

## 3. Part 2: current behaviour, the audit

Line counts: `skills/cairn/SKILL.md` 227 (body 222), `skills/recall/SKILL.md` 272,
`skills/search/SKILL.md` 144, `skills/setup/SKILL.md` 329, `skills/invoke-antigravity/SKILL.md`
253. No body is near 500 lines, so no split is required. Five linked files exceed 100
lines without a contents list: `workflows/routine.md` (273), `workflows/log-session.md`
(164), `reference/claude.md` (145), `reference/codex.md` (135), `reference/antigravity.md`
(124). Description lengths: cairn 684, recall 672, invoke-codex 511, invoke-antigravity
508, search 454, invoke-claude 426, setup 291.

### 3.1 Capability table

R = routing-eval row isolates the capability. "Codex" notes what differs on that host.

| Capability (layer) | Skill(s) | Quoted trigger | Step text | R | Codex | Gap |
|---|---|---|---|---|---|---|
| Export per source (02) | cairn | "export codex/cursor/granola" (`skills/cairn/SKILL.md:3`); no antigravity | `SKILL.md:39-61` | yes | same | G1: Antigravity missing from the description, the source table (`:16-22`) and the reference list (`:216-220`) |
| `export --json`, parser drift (02) | cairn | none (diagnostic) | `:66-86` | n/a | same | none |
| note/close/log | cairn | "add session note", "close session", "log session" | `workflows/log-session.md:15-62` | yes | `--session-id` from host (`references/harnesses/codex.md:62-66`) | none |
| decide (07) | cairn | none | `log-session.md:64-90` | no | same | G2 |
| supersede (07) | cairn | none | `log-session.md:74,84-86` | no | same | G2 |
| forget (08) | cairn | none | `log-session.md:92-120` | no | same | G2 |
| recall decisions, overdue (07) | recall | none; the section title says "what did we decide about X" (`skills/recall/SKILL.md:97`) | `:97-115` | no | same | G3; also Step 1 (`:20-29`) never routes to it |
| list/expand/digest/frame, provenance (01, 06) | recall | ten phrases (`:3`) | `:31-95` | yes | `$recall` | none |
| recall verify (06) | recall | none ("cite the frame" is digest) | `:117-129` | no | same | G4 |
| recall gaps (05) | recall, search | none standalone | recall `:202-218`, search `:119-132` | no | same | G5: no "why can't recall see X" route |
| `cairn errors CODE` (10) | cairn, recall (digest only), invoke-* | none | cairn `:88-99`; recall `:87`; invoke-codex `:84-85`; invoke-claude `:88`; invoke-antigravity `:197` | no | same | G6: search none; recall only for digest. The assessment's claim about the invoke skills is false: all three name `cairn errors CODE` |
| routine install/run/kickstart/uninstall (04) | cairn | "scheduled routine" | `workflows/routine.md` | yes | launchd only; same | none |
| routine status 0/1/2 (04) | cairn | none | `routine.md:156-186` (exit table) | no | same | G7: no step acts on the exit; no daily check |
| routine pause/resume (04) | cairn | none | `routine.md:224-236` | no | same | G2 |
| instructions install/remove/status (09) | cairn (list only `:138-141`), setup step 6 (explicit) | none | setup `:251-260` | no | writes `AGENTS.md` or the override (`codex.md:55-60`) | G8 |
| hook install `--dry-run`/`--no-trust`, status (03) | setup (explicit), codex reference | none | `codex.md:145-155` | n/a | trust table, `/hooks` | none: setup is explicit-only by design |
| doctor, sync, config, cron | cairn | "sync sessions" | `:101-156` | yes | same | none |
| invoke-* and agents | invoke-*, cairn | per skill | per skill | yes | `$invoke-codex`; `allowed-tools` ignored | G9: harness references list two of the three invoke skills (`claude.md:108`, `codex.md:175`); `codex.md:51` lists four skills |
| memory import (15) | none | none | none | no | Claude-only source; the command runs from either host | G10 |
| Decisions rows from memory, `--source memory` (15) | none | none | none | no | same | G10 |
| Cross-skill names | recall, search, cairn | n/a | `/search` (recall `:184`), `/cairn:recall` (cairn `:124,156`), `/cairn:setup` in three descriptions | n/a | Codex spells them `$search`, `$recall`, `$setup` | G11 |
| Session-id rule for mutations | contract | n/a | `references/harnesses/contract.md:13-15` says note/close/log only | n/a | same | G12: decide/supersede/forget/memory missing |
| Codex policy files | invoke-*, setup | n/a | `agents/openai.yaml` exists for four skills only | n/a | cairn, recall, search rely on the default `true` | G13: implicit, not tested |

### 3.2 The user's assessment, checked

| Claim | Verdict |
|---|---|
| No quoted triggers for decide/supersede/forget/instructions/routine pause in the cairn skill | True (`skills/cairn/SKILL.md:3` has thirteen phrases, none of these) |
| No standalone trigger for `recall gaps` | True |
| `cairn errors CODE` not referenced from recall, search and invoke on failure | True for search; partly true for recall (digest only, `:87`); false for invoke (`invoke-codex/SKILL.md:84-85`, `invoke-claude/SKILL.md:88`, `invoke-antigravity/SKILL.md:197`) |
| No daily-check step that runs `routine status` and acts on 0/1/2 | True: `routine.md:163-181` documents the codes, no step acts on them |
| No skill text for memory import | True (layer 15 is mid-implementation; its spec section 8 plans a `log-session.md` section) |

Further gaps found: G1, G3, G4, G9, G11, G12, G13 above, plus G14: the routing test caps
fixtures at 15 rows per skill (`tests/test_skill_routing.py:33`); cairn already has 14,
so G2-G10 cannot land without raising the cap.

## 4. Surface

No CLI flag, config key, frontmatter key or exit code changes. New and changed files:

| Path | Change |
|---|---|
| `skills/cairn/SKILL.md` | description; source table; three short sections; command block; reference list |
| `skills/cairn/workflows/daily-check.md` | new |
| `skills/cairn/workflows/memory-import.md` | new |
| `skills/cairn/workflows/log-session.md`, `routine.md` | contents list; trigger-phrase map |
| `skills/cairn/reference/claude.md` | memory import paragraph |
| `skills/cairn/agents/openai.yaml`, `skills/recall/agents/openai.yaml`, `skills/search/agents/openai.yaml` | new, policy only |
| `skills/recall/SKILL.md` | description; Step 1 bullets; coverage section; decisions lines; on-failure line |
| `skills/search/SKILL.md` | `allowed-tools`; one failure line; one Codex line |
| `skills/cairn/routing-eval.jsonl`, `skills/recall/routing-eval.jsonl` | 13 and 4 new rows (A.11) |
| `references/harnesses/contract.md`, `claude.md`, `codex.md` | mutation verbs; skill names per host |
| `tests/test_skill_routing.py` | `MAX_ROWS`, one overlap pair, the recall count |
| `tests/test_plugin_manifest.py` | policy file for every routable skill |
| `.agents/skills/skill-review/**`, `.claude/skills/skill-review` (symlink) | new developer skill (Part 5) |
| `tests/test_skill_review.py` | new |
| `tests/test_docs_cli.py` | scan `.agents/skills` too |
| `Makefile`, `pyproject.toml` | lint and type-check the review script |
| `CONTRIBUTING.md`, `docs/skills-refresh-spec.md`, `docs/README.md` | paragraph; spec; index row |

## 5. Behaviour, step by step

1. A user says "record that decision" in Claude Code. The listing holds the new cairn
   description; the phrase is in its first 600 characters. The model loads
   `skills/cairn/SKILL.md`, whose "Decisions" section points to
   `workflows/log-session.md`, which now opens with a phrase-to-section map.
2. A user says "is my routine healthy" or "cairn daily check". The cairn skill loads and
   its "Daily check" section sends the model to `workflows/daily-check.md`. The workflow
   is read-only until the user approves a spend or a resume.
3. A user says "why can't recall see yesterday's Codex session". The recall skill loads;
   Step 1 classifies it as a coverage question; the new "Coverage question" section runs
   `cairn recall gaps --json` and the window's `recall list --coverage`.
4. A command fails with a `code:` line in any of recall, search or cairn: the skill runs
   `cairn errors CODE` and reports its next step before trying anything else.
5. "import claude memories": the cairn skill sends the model to
   `workflows/memory-import.md`: dry run, counts, consent, import, then
   `recall decisions --source memory`.

## 6. Data formats

No new data. The routing rows follow `docs/skill-routing-eval-spec.md` 4.1. The daily
check reads `cairn routine status NAME --json` (`state`, `exit`, `attention`, `code`,
`paused`, `disabled`, `last_run`, `days`; `src/cairn/routine.py:398-433`,
`src/cairn/routine_state.py:238-248`); `last_run` is the parsed `last-run.json`, so the
workflow never opens the file by path.

## 7. Failure modes

| Case | Result |
|---|---|
| A new trigger collides with another skill's phrase | `test_each_row_routes_to_its_skill` fails with the pair table; reword |
| A description exceeds 1,024 characters | `test_skill_review.test_shipped_descriptions_fit` fails |
| A plain-scalar description holds `: ` or ` #` | the same test fails (YAML would cut it) |
| A new `cairn ...` example drifts from the CLI | `tests/test_docs_cli.py` fails with path:line |
| Someone adds a second `rg -l ` line to recall or search | `tests/test_recall_qrels.py:228-237` fails |
| The `.claude/skills/skill-review` symlink checks out as a file (Windows without symlinks) | `test_claude_link_resolves` fails; cairn's supported platforms are macOS and Linux |
| Layer 15 lands its own `log-session.md` section | the implementer moves it into `workflows/memory-import.md` (one copy) |

## 8. Tests

`tests/test_skill_routing.py` (edits):

- `MAX_ROWS = 30` with a comment: the umbrella cairn skill carries about 26 phrases.
- `DECLARED_OVERLAPS` gains `("cairn", "recall")`, exercised by the recall row "list the
  decisions we recorded this week".
- `test_quoted_phrases_after_marker_only`: recall count 10 to 15.

`tests/test_plugin_manifest.py`:

- `test_resources_resolve_from_copied_cache`: every skill except setup has
  `agents/openai.yaml` with `allow_implicit_invocation: true`.

`tests/test_skill_review.py` (new):

- `test_reference_files_carry_source_and_date`: each `references/*.md` except
  `rubric.md` has `Source: https://` and `Fetched: YYYY-MM-DD` (a real date, not in the
  future) in its first six lines.
- `test_skill_frontmatter_parses`: `read_head` from `tests/test_skill_routing.py` reads
  `SKILL.md`; `name` is `skill-review`; the description is at most 1,024 characters.
- `test_skill_body_under_limit`: the body is under 500 lines; every linked file exists.
- `test_rubric_citations_resolve`: each `<file>.md "Section"` cell names an existing
  file with a `## Section` heading; each `` cairn: `path` `` exists in the repo.
- `test_claude_link_resolves`: `.claude/skills/skill-review` is a symlink whose
  resolved path is `.agents/skills/skill-review`.
- `test_not_shipped`: no `skills/skill-review`; the Codex manifest's `skills` is
  `./skills/`.
- `test_shipped_descriptions_fit`: every `skills/*/SKILL.md` description is at most
  1,024 characters and holds no `: ` and no ` #`.
- `test_measure_reports_a_fixture`: `scripts/measure.py` on an invented skill under a
  temporary directory prints the expected counts and flags (section A.10).

`tests/test_docs_cli.py`: `doc_files` adds `.agents/skills` to its folders, so the review
skill's examples are checked like any other skill.

Evidence: run `make discriminates` for `test_claude_link_resolves`,
`test_rubric_citations_resolve` and `test_shipped_descriptions_fit` (layer 14 rule).

## 9. Docs to update and design constraints

### 9.1 Docs

- `docs/skills-refresh-spec.md`: this spec without the stack header; a row at the top of
  `docs/README.md` (2026-10-30, spec, decision line in A.13).
- `CONTRIBUTING.md` "Changing a skill description": the paragraph in A.13.
- No README change: the README already points to the skills.

### 9.2 Codex parity

- `allowed-tools` is not read by Codex. The new `Bash(cairn errors:*)` grant in the search
  skill helps Claude Code only; under Codex the sandbox and approval policy decide.
- Cross-skill references use the skill name ("the search skill") or both spellings.
  The harness references carry the full map (A.8, A.9).
- Hook trust stays in `references/harnesses/codex.md:95-155`; the daily check calls
  `cairn hook status --harness HOST` and reads `codex_trust` only on a Codex host.

### 9.3 Routing constraints (layer 11)

Each new phrase has one row that matches it and no other phrase of its skill, the rows
copy no phrase, and every pair that both match a row is declared. A prototype of the
final descriptions and rows against the real `tests/test_skill_routing.py` (with the
three edits above) passed all 23 tests on 2026-10-30 (cairn 27 rows, recall 16).
Rejected phrases: "we decided" alone (it matches the existing recall row "cite the exact
frame where we decided on BM25"), "forget that" (fires on "forget that, do X"), "what
does this error mean" (fires on any traceback), "import my memories" (near miss for a
general assistant-memory import skill; "import claude memories" is specific).

### 9.4 What must not change

- `skills/recall/SKILL.md` Step 2 and Step 3 command blocks and order (`:31-200`), the
  Gaps rules (`:202-218`), One Thing (`:220-236`), the fallback (`:238-254`) and the
  "Resolve before asking" rule (`:256-262`). This layer only adds bullets and sections.
- `skills/search/SKILL.md:86-112`: exactly one `rg -l ` line and one `rg -n ` line
  (`tests/test_recall_qrels.py:537-560`). No new line in either file starts with `rg `.
- The citation grammar paragraph `skills/recall/SKILL.md:117-129` and
  `skills/cairn/workflows/routine.md:100-122`.
- `skills/setup/SKILL.md`: untouched, including the step 2 consent questions.

### 9.5 Version

`skills/**` and `references/**` changed: CI requires a version increase
(`.github/workflows/ci.yml:59-61`). The orchestrator bumps it (patch) and writes the
CHANGELOG entry (draft in A.13). `.agents/skills/**` and `.claude/**` are outside that
list: the review skill alone needs no bump.

## 10. Implementation plan

1. Routing test edits and fixture rows (A.11), then the cairn and recall descriptions
   (A.1, A.4). Run `uv run python -m unittest tests.test_skill_routing -v`. 30 minutes.
2. cairn `SKILL.md` body, `daily-check.md`, `memory-import.md`, contents lists, the
   claude reference paragraph (A.1-A.3, A.7). About 260 lines. 2 hours.
3. recall and search edits (A.4, A.5). 40 minutes.
4. Harness references and contract (A.8, A.9); policy files (A.6). 20 minutes.
5. Part 5: copy the condensed references verbatim into
   `.agents/skills/skill-review/references/`, write `SKILL.md` (A.10) and
   `scripts/measure.py` (about 110 lines), create the symlink
   (`ln -s ../../.agents/skills/skill-review .claude/skills/skill-review`), tests,
   `Makefile` `LINTED` and `pyproject.toml` mypy `files`. 2 hours.
6. `make check`; `make discriminates` for the three tests in section 8.
7. Dogfood (P5.3): run `skill-review` on `skills/cairn`, `skills/recall`,
   `skills/search` and on itself; record the four VERDICT lines in the IMPL summary.

Total about a day.

### 10.1 Open questions (answered in section 11)

1. Raise `MAX_ROWS` to 30, or make the cap per skill (triggers + 5)? Recommended: 30. A
   per-skill formula hides growth; a flat cap a reviewer must raise keeps it visible.
2. Ship `skill-review` as a plugin skill as well? Recommended: no. It reviews cairn's
   own repo rules (F1-F8) and would add a description to every user's listing.
3. Use `when_to_use` in the cairn skill to hold the long trigger list for Claude Code?
   Recommended: no. Codex and Antigravity do not read it, and the routing test reads only
   `description`.

## 11. As implemented

Open questions: the recommended defaults were taken. `MAX_ROWS` is a flat 30;
`skill-review` is repo-local only (`test_not_shipped`); no skill uses `when_to_use`.

Differences from the sections above:

- **Memory import text, one copy.** Layer 15 landed an "Import Claude memories" section
  in `workflows/log-session.md` and a "Memory import (Claude-only)" section in
  `reference/claude.md`. The steps and the privacy rule now live only in
  `workflows/memory-import.md`, which also carries the kind mapping and the default
  project rule from the old section. `log-session.md` keeps a three-line pointer.
  `reference/claude.md` keeps its directory, kind, placement and exit-code facts and
  replaces its privacy paragraph with a pointer, so A.7's paragraph was not added a
  second time.
- **Routing rows.** Layer 15 added the cairn row "import the auto memories that claude
  saved for this repo", which already reaches "import claude memories" alone, so A.11's
  memory row was dropped: cairn has 27 rows (15 + 12), recall 16.
- **Contents lists.** Besides `log-session.md` and `routine.md`, the three per-source
  references over 100 lines (`reference/claude.md`, `codex.md`, `antigravity.md`) open
  with a contents list (rubric C4).
- **Source count.** The cairn skill's verb table says "five conversation sources" and
  names antigravity, matching the new source-table row.
- **Review step 3** also runs `tests.test_skill_review`.
- **Plugin manifest test.** The policy check walks every `skills/*/SKILL.md` except
  setup, so a new skill without `agents/openai.yaml` fails.
- **Dogfood fixes** (the `skill-review` pass over cairn, recall and search): British
  spelling in the touched skills (`Behaviour`, `customises`, `synthesised`,
  `Prioritise`, `Organise`); `/search` and `/cairn:recall` in prose became "the search
  skill" and "the recall skill", and the search skill's Codex line also maps `/recall`
  to `$recall`; the `forget` steps say to read the `Withdrew row #N` line;
  `memory-import.md` uses "memory" for one auto-memory file throughout. The rubric item
  F7 (version bump) stays open for the maintainer.
- **README.** One line in the repository layout names `.agents/skills/skill-review/`.
- **Headless reviews** in P5.3 use the layer below as the diff base, not a branch name.

## Part 5: the `skill-review` developer skill

### P5.1 Location and discovery

| Host | Repo-local skill path it reads | Evidence | Invocation |
|---|---|---|---|
| Claude Code | `.claude/skills/<name>/SKILL.md`; the entry may be a symlink | 16-refs/claude-code-skills.md "Where skills load" | `/skill-review skills/cairn` |
| Codex | `.agents/skills` from the cwd up to the repo root | 16-refs/codex-skills.md "Discovery" | `$skill-review skills/cairn`, or "read .agents/skills/skill-review/SKILL.md and apply it to skills/cairn" |
| Antigravity | `<workspace>/.agents/skills/<name>/`; the CLI makes it `/skill-review` | 16-refs/antigravity-skills.md "Discovery" | interactive `/skill-review`; headless `cairn invoke antigravity --prompt-file FILE --cd REPO --mode plan` with the SKILL.md path named in the prompt |

Smallest layout all three find: the real directory at `.agents/skills/skill-review/`
(Codex and Antigravity) and one relative symlink `.claude/skills/skill-review ->
../../.agents/skills/skill-review` (Claude Code follows it). One copy, no generator.

It does not ship. The plugin's skills come from `skills/` only (`.codex-plugin/plugin.json`
`"skills": "./skills/"`; Claude Code loads `<plugin>/skills/`). The marketplaces use
`"source": "./"`, so the plugin cache holds the whole repo, but no host loads
`.claude/skills` or `.agents/skills` from a plugin cache: Claude Code reads project
skills from the session's repository, Codex scans from its cwd, Antigravity from its
workspace. `tests/test_public_hygiene.py:41-53` skips the directory symlink
(`is_file()` is false); `shutil.copytree` in `test_plugin_manifest.py:65` follows it.

### P5.2 Content

`SKILL.md` (A.10) is about 90 lines: inputs, six numbered steps, the output shape. The
rubric is `references/rubric.md` (86 lines): 41
items in six groups (frontmatter, description, structure, instructions, host parity,
cairn rules), each with a severity, a check and a citation. The seven source files hold
the condensed docs. `scripts/measure.py` makes the deterministic part a command, as the
best-practices page recommends ("Prefer scripts for deterministic operations").

### P5.3 Dogfooding

- Layer 16's own reviews (Codex, Antigravity, the Opus triage) receive a brief that says:
  "Read `.agents/skills/skill-review/SKILL.md` and its `references/rubric.md`, apply them
  to every `skills/` file in this diff, and report in its output shape." The brief also
  attaches the saved test log, because a read-only Codex sandbox may not run `uv`.
- The implementer runs it once on the refreshed skills and records the verdicts.

## Appendix A: per-file edits

Text inside a `text` fence is the exact new content. "After line N" means after the
current line N.

### A.1 `skills/cairn/SKILL.md`

Line 3, replace the description (958 characters; key use case first; no `: `, no ` #`):

```text
description: Exports, lists, annotates and resumes local AI coding sessions (Claude Code, Codex, Antigravity, Cursor), Granola meetings and service docs as searchable markdown through the cairn CLI; records, supersedes and withdraws decisions; imports Claude Code auto memory; runs and checks scheduled routines. Use when the user says "sync sessions", "export sessions", "export codex", "export cursor", "export granola", "export antigravity", "resume session", "add session note", "close session", "log session", "record that decision", "log what we decided", "replace that decision", "forget that decision", "withdraw that claim", "import claude memories", "sync docs", "sync service docs", "meeting transcripts", "scheduled routine", "pause my journal", "resume the routine", "is my routine healthy", "cairn daily check", "instructions block", or "cairn error code". Also covers doctor, config, cron and hooks. Requires the cairn CLI; install it with the setup skill.
```

Line 4 `allowed-tools: Bash, Read`: no change. The skill drives every verb of a general
CLI, so a per-verb list would only add prompts; rubric A5 allows this case.

Line 14: "drives five sources" becomes "drives six sources". After line 18 add:

```text
| `antigravity`  | `antigravity-sessions/` | Google Antigravity (agy) conversations |
```

Line 24: replace "Install first with `/cairn:setup`." with "Install first with the setup
skill (`/cairn:setup` in Claude Code, `$setup` in Codex)."

Inside the top-level block (after line 129, the `recall gaps` line) add, aligned with the
neighbours:

```text
cairn recall decisions [--overdue] [--json]  # active ## Decisions rows (drives "what did we decide")
cairn errors CODE                      # meaning, next step and verify command for a failure code
cairn hook status --harness claude     # last hook requests and their state (codex: also codex_trust)
cairn claude memory import --dry-run   # plan an import of Claude Code auto memory; writes nothing
```

Line 210: replace the sentence "`decide`, `supersede` and `forget` are described in
`workflows/log-session.md`." with nothing, and insert these sections after line 210
(before "## Per-source reference"):

```text
## Decisions

A claim the session settled goes in the `## Decisions` table, one row per claim.

1. "record that decision", "log what we decided": run
   `cairn claude decide "CLAIM" --kind decision` (any conversation source in place of
   `claude`; `--kind fact`, `commitment` or `hunch` as fits). Read the `Added #N` line.
2. "replace that decision": find N with `cairn recall decisions --json` (the `ref` ends
   in `#N`), then run `cairn claude supersede N "NEW CLAIM"`.
3. "forget that decision", "withdraw that claim": find N the same way, then run
   `cairn claude forget N --reason stale` with the reason that fits. Exit 3 later means
   someone tried to add a withdrawn claim again.

Never guess N. Kinds, citations, reasons and `--force` rules:
[workflows/log-session.md](workflows/log-session.md).

## Daily check

"is my routine healthy", "cairn daily check": follow
[workflows/daily-check.md](workflows/daily-check.md). It reads
`cairn routine status NAME --json` per routine and acts on exit 0, 1 or 2, then reads the
gaps report and the overdue decisions. "pause my journal":
`cairn routine pause NAME --reason "TEXT"`. "resume the routine": `cairn routine resume NAME`.
Ask before either: both change what tomorrow's run does.

## Import Claude memories

"import claude memories": follow [workflows/memory-import.md](workflows/memory-import.md).
Dry run first, show the counts, then import. The rows are private notes Claude wrote; they
stay under `data_root`.

## Recall-first block

"instructions block": run `cairn instructions status --json` and read each harness's
state. To add or remove the block, run the command with `--dry-run` first, show the diff,
and run it without `--dry-run` only after the user says yes: it edits the user's own
`CLAUDE.md` or `AGENTS.md`.
```

Line 88, the heading "### On failure": add one sentence after line 99: "\"cairn error
code\" with a code the user pastes: run `cairn errors CODE` and give its `next_step` and
`verify` lines."

After line 220 (the service-docs reference line) add:

```text
- `reference/antigravity.md` - Google Antigravity (agy) conversations + `resume`
```

Lines 224-227: replace with:

```text
To log/annotate the current session (CLI forms, natural-language parsing, auto-summary,
status and tag values): see `workflows/log-session.md`. To schedule a daily peer run, see
`workflows/routine.md`; to check it each day, `workflows/daily-check.md`; to import Claude
Code auto memory, `workflows/memory-import.md`. Valid tags live in `schema/tags.yaml`.
```

Result: about 268 lines, under the 500-line rule.

### A.2 `skills/cairn/workflows/daily-check.md` (new)

````text
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
````

### A.3 `skills/cairn/workflows/memory-import.md` (new)

If layer 15 added an "Import Claude memories" section to `log-session.md`, move it here
and keep one copy.

````text
# Import Claude memories

`cairn claude memory import` turns Claude Code auto-memory notes into `## Decisions`
rows: into the session that wrote them when cairn exported it, else into
`<data_root>/decisions/<project>.md`. A re-run adds only new memories. Memory files are
never edited.

## Privacy

A memory holds whatever Claude chose to save, including private names. The import
writes only under `data_root`, which QMD indexes. Never copy a decisions document or a
memory row into a tracked file. To hide a row from recall, withdraw it (step 6); the text
stays in the file.

## Steps

1. Plan the import for the current project:

   ```bash
   cairn claude memory import --dry-run
   ```

   Read each `Would add` line and the summary `Dry run: would import N memories;
   nothing written`. Exit 1 with `MEMORY_DIR_MISSING`: run
   `cairn errors MEMORY_DIR_MISSING`. If the user moved auto memory with
   `autoMemoryDirectory`, ask for that path and repeat with `--memory-dir DIR`.

2. Show the user the count and the kinds, and ask whether to import. Use
   `--all-projects` or `--include-reference` only when the user asks for them.

3. Import:

   ```bash
   cairn claude memory import --json
   ```

   Read `added`, `skipped_existing`, `skipped_withdrawn`, `changed`, `invalid` and
   `failed`. Exit 1 means at least one memory was invalid or failed; each `Warning:`
   line on stderr names the file and the reason.

4. For each `changed` memory, the warning names the row. Ask whether the new text
   replaces the old claim. If yes, run
   `cairn claude supersede N "NEW CLAIM" --session-id ID` with the id from the warning.

5. Check the result:

   ```bash
   cairn recall decisions --source memory --json
   ```

   Rows in a project document have refs such as `memory:example-app#3`; rows in a
   session end in `[memory]` in text output. A `[memory]` row is model-written: cite it
   as a hint, not as a decision the user made.

6. Withdraw a wrong or private row:

   ```bash
   cairn claude forget 3 --reason private --session-id memory:example-app
   ```
````

### A.4 `skills/recall/SKILL.md`

Line 3, replace the description (834 characters):

```text
description: Loads context from previous sessions and notes. Temporal queries (yesterday, last week, a date) list sessions chronologically from the shared exported store via the cairn CLI; topic queries search across QMD collections (BM25) plus an exact-match ripgrep pass; decision queries list recorded decisions. Every recall ends with "One Thing" - the single highest-leverage next action synthesized from the results. Use when the user says "recall", "what did we work on", "load context about", "remember when we", "prime context from past sessions", "what was I doing", "session history", "digest yesterday", "daily digest", "cite the frame", "what did we decide", "list our decisions", "verify journal citations", "missing sessions", or "why can't recall see". Requires the cairn CLI and qmd (install with the setup skill); rg is optional.
```

Line 5 `allowed-tools`: no change (`Bash(cairn:*)` covers `cairn errors`).

After line 29 (the "Both" bullet) add:

```text
- **Decision** - "what did we decide about X", "list our decisions" → the Decisions
  block in Step 2.
- **Journal check** - "verify the journal citations" → `recall verify` in Step 2.
- **Coverage** - "why can't recall see X", a session the user expected is missing →
  "Coverage question" below.
```

After line 115 (end of the Decisions paragraph) add:

```text
Rows imported from Claude Code auto memory (`cairn claude memory import`) carry a
`memory:<project>/<file>` source cell and end in `[memory]`; a project document's rows
have refs such as `memory:example-app#3`, listed with `--source memory`. Claude wrote
these claims: present them as notes, not as decisions the user made.
```

After line 254 (end of "Fallback: no results") add:

```text
## Coverage question

For "why can't recall see X" or a session the user expected:

1. Run `cairn recall gaps --json`. Read `status` and each gap's `kind`, `severity`,
   `collection`, `message` and `fix`.
2. If the user named a day or a source, run
   `cairn recall list DATE --json --coverage --all-projects` and read
   `coverage.verdict` and whether the session is in `sessions`.
3. Explain each `warn` gap in one line. `export-lag`, `never-exported`,
   `source-disabled` and `store-missing` mean the session is not exported;
   `index-lag`, `index-count`, `collection-missing`, `index-stamp-missing` and
   `index-unknown` mean QMD has not indexed it (rg still reads the file); `rg-missing`
   means exact search is off.
4. Print the Gaps line as in Step 4. The One Thing is the first warn gap's `fix`; do not
   run it unasked, because `cairn sync` exports and re-indexes.

## On failure

If a cairn command exits non-zero, read the `code:` line on stderr (or the `code` key in
its JSON) and run `cairn errors CODE`. Report its next step and stop; do not retry in a
loop. An empty digest day is exit 3 with `DIGEST_EMPTY_DAY`, not a failure.
```

Do not touch lines 31-254 otherwise.

### A.5 `skills/search/SKILL.md`

Line 5, replace:

<!-- cairn-cli: skip -->
```text
allowed-tools: Bash(qmd:*), Bash(rg:*), Bash(head:*), Bash(cairn recall gaps:*), Bash(cairn errors:*)
```

After line 36 (end of the Usage fence) add: "In Codex, type `$search` in place of
`/search`."

After line 132 (end of step 5) add:

```text
   If `cairn recall gaps --json` itself exits non-zero, read the `code:` line on stderr
   and run `cairn errors CODE`; report its next step and stop. A failing `qmd` command
   has no cairn code: report qmd's own message and suggest `cairn doctor`.
```

No new line may start with `rg `.

### A.6 Codex policy files (new)

`skills/cairn/agents/openai.yaml`, `skills/recall/agents/openai.yaml`,
`skills/search/agents/openai.yaml`, each:

```text
policy:
  allow_implicit_invocation: true
```

### A.7 `skills/cairn/workflows/log-session.md`, `routine.md`, `reference/claude.md`

`log-session.md`, after line 6 add:

```text
## Contents

- Via CLI: note, close, log
- Via voice/text (agent workflow)
- Record a decision: "record that decision", "log what we decided", "replace that decision"
- Withdraw a claim: "forget that decision", "withdraw that claim"
- Finding the current session
- Status values, tags, frontmatter
```

and after line 75 (end of the decide fence) add: "\"replace that decision\" is
`supersede`: find N with `cairn recall decisions --json` first."

`routine.md`, after line 6 add:

```text
## Contents

1. Define the routine · 2. Write the prompt and the commands · 3. Dry-run · 4. Install ·
5. Kickstart · 6. Read status (exit 0/1/2) · Pause and resume · Notify on each day ·
7. Re-run a day · 8. Uninstall. For a daily read of every routine, see `daily-check.md`.
```

`reference/claude.md`, after line 114 (end of Resume) add:

```text
## Memory import (Claude-only)

`cairn claude memory import` adds Claude Code auto-memory notes as `## Decisions` rows.
`--session-id memory:<project>` targets the project decisions document with `decide`,
`supersede` and `forget`. Steps and privacy rules: `../workflows/memory-import.md`.
```

### A.8 `references/harnesses/contract.md`

Lines 13-15, replace "For note/close/log use the selected source command" with "For
note, close, log, decide, supersede and forget use the selected source command", and add
after line 15: "A project decisions document from `cairn claude memory import` takes
`--session-id memory:<project>`."

### A.9 `references/harnesses/codex.md` and `claude.md`

`codex.md` lines 51-53, replace with:

```text
Use `/skills` or the `$` picker to select an installed skill: `$setup`, `$cairn`,
`$recall`, `$search`, `$invoke-codex`, `$invoke-claude` or `$invoke-antigravity`. Skill
text that says `/search` or `/cairn:recall` means `$search` or `$recall` here. Codex does
not read `allowed-tools`; its sandbox and approval policy decide. Run setup only on
explicit request or explicit onboarding. The setup policy
`allow_implicit_invocation: false` is packaged in `agents/openai.yaml`; every other
skill carries `true`.
```

`codex.md` line 66: `cairn codex note|close|log --session-id ID` becomes
`cairn codex note|close|log|decide|supersede|forget --session-id ID`.
`codex.md` line 175: "The `invoke-codex` and `invoke-claude` skills" becomes "The
`invoke-codex`, `invoke-claude` and `invoke-antigravity` skills".

`claude.md` lines 60-61: "other workflows are `/cairn:cairn`, `/cairn:recall`, and
`/cairn:search`" becomes "other workflows are `/cairn:cairn`, `/cairn:recall`,
`/cairn:search`, `/cairn:invoke-codex`, `/cairn:invoke-claude` and
`/cairn:invoke-antigravity`". Line 108: "`/cairn:invoke-codex` and `/cairn:invoke-claude`.
Both" becomes "`/cairn:invoke-codex`, `/cairn:invoke-claude` and
`/cairn:invoke-antigravity`. All three".

### A.10 `.agents/skills/skill-review/` (new, not shipped)

Tree:

```text
.agents/skills/skill-review/
├── SKILL.md
├── references/          condensed source docs, each with Source and Fetched lines
│   ├── rubric.md
│   ├── anthropic-best-practices.md
│   ├── anthropic-skills-overview.md
│   ├── anthropic-skill-creator.md
│   ├── agentskills-spec.md
│   ├── claude-code-skills.md
│   ├── codex-skills.md
│   └── antigravity-skills.md
└── scripts/
    └── measure.py
.claude/skills/skill-review -> ../../.agents/skills/skill-review
```

`SKILL.md`:

````text
---
name: skill-review
description: Reviews an Agent Skill (a SKILL.md and the files it links) against the authoring rules of Claude Code, Codex and Antigravity and against cairn's own tests, then reports each finding with file and line, severity, the rule it breaks and the fix, and ends with a verdict. Use when the user says "review this skill", "is this SKILL.md well written", "skill audit", or "check the skill before shipping", or when a diff touches skills/ or .agents/skills/. Repo-local developer skill for cairn; it does not ship in the plugin and never edits the skill it reviews.
allowed-tools: Read, Grep, Glob, Bash(git diff:*), Bash(uv run python:*), Bash(python3:*), Bash(wc:*)
---

# Skill review

Review one or more skills and report findings. Do not edit the files you review.

## Inputs

- A skill directory, such as `skills/cairn` or `.agents/skills/skill-review`, or
- A branch or PR: the skill directories its diff touches.

## Steps

1. Resolve the targets. For a branch, run
   `git diff --name-only origin/main...HEAD -- skills references .agents/skills` and
   keep each distinct `skills/<name>` or `.agents/skills/<name>` directory.
2. Measure each target:
   `python3 .agents/skills/skill-review/scripts/measure.py skills/NAME`.
   Read the description length, body lines, quoted triggers, linked files with their
   line counts, and the flags it prints (`OVER_500`, `DESC_OVER_1024`, `YAML_HAZARD`,
   `NO_CONTENTS`, `NESTED_LINK`, `CLAUDE_ONLY`, `MISSING_LINK`).
3. Run the repository's checks once and save the output:
   `uv run python -m unittest tests.test_skill_routing tests.test_docs_cli tests.test_plugin_manifest tests.test_public_hygiene tests.test_recall_qrels > /tmp/skill-review-tests.log 2>&1`.
   Read the last line (`OK` or `FAILED`) and every failure that names a target file.
   If the command cannot run (a read-only sandbox), write "checks not run" and use a
   log the requester attached; never infer a pass.
4. Read the target's `SKILL.md`, every file it links, `agents/openai.yaml` and
   `routing-eval.jsonl`.
5. Apply [references/rubric.md](references/rubric.md) item by item (A1-F8). A failed
   test from step 3 or a flag from step 2 is a finding under its rubric item. For a rule
   you are unsure of, read the source file the item cites, then decide.
6. Write the report in the shape below. One finding per broken rule per place.

## Report shape

```
skill-review: skills/cairn (body 262 lines, description 958/1024, 26 triggers)
Checks: test_skill_routing OK, test_docs_cli OK, test_plugin_manifest OK, test_public_hygiene OK, test_recall_qrels OK

| # | Severity | Where | Rule | Finding | Fix |
|---|---|---|---|---|---|
| 1 | fix | skills/cairn/SKILL.md:3 | B6 | "cairn error code" also matches ... | reword to ... |

VERDICT: fix-then-ship (0 block, 1 fix, 0 note)
```

- Severity is the rubric item's: `block`, `fix` or `note`.
- `Where` is `path:line` of the text that breaks the rule.
- The VERDICT is `block` if any finding is `block`, else `fix-then-ship` if any is
  `fix`, else `ship`. Count each severity.
- With several targets, write one report per target and one VERDICT line each.

## Sources

The rubric cites these condensed references, each headed by its source URL and fetch
date. Re-fetch a source before you change a rule that depends on it.

- [Anthropic best practices](references/anthropic-best-practices.md)
- [Anthropic skills overview](references/anthropic-skills-overview.md)
- [skill-creator and the skills repository](references/anthropic-skill-creator.md)
- [Agent Skills specification](references/agentskills-spec.md)
- [Claude Code skills](references/claude-code-skills.md)
- [Codex skills](references/codex-skills.md)
- [Antigravity skills](references/antigravity-skills.md)
````

`scripts/measure.py` (stdlib, about 110 lines, linted and type-checked):

- Argument: one skill directory. Exit 2 if it has no `SKILL.md`.
- Reads the frontmatter with the same rules as `read_head` (`tests/test_skill_routing.py:106-126`);
  prints `name`, `description` length, quoted triggers after `Use when the user says`
  (count and list), body line count.
- Lists every relative markdown link in `SKILL.md` with its line count and whether a
  file over 100 lines has a `## Contents` heading in its first 20 lines.
- Flags, one per line: `OVER_500`, `DESC_OVER_1024`, `YAML_HAZARD` (`: ` or ` #` in a
  plain-scalar description, or `<`/`>`), `NO_CONTENTS`, `NESTED_LINK` (a linked file that
  links a further `.md` in the skill), `CLAUDE_ONLY` (`$ARGUMENTS`, `CLAUDE_SKILL_DIR`,
  `when_to_use`, a `!` command fence), `MISSING_LINK`, `NAME_MISMATCH`.
- Exit 0 always after a successful read; the flags are data, not a verdict.
- Add `.agents/skills/skill-review/scripts/measure.py` to `LINTED` (`Makefile:10`) and to
  mypy `files` (`pyproject.toml:51`).

Headless reviews (P5.3), brief file `/tmp/l16-review.md`:

```text
Read .agents/skills/skill-review/SKILL.md and its references/rubric.md. Apply them to
every skills/ and .agents/skills/ file in the diff of this branch against
the layer below. The test log is /tmp/l16-tests.log. Report in the skill's
report shape, one VERDICT line per skill.
```

```bash
cairn invoke codex --prompt-file /tmp/l16-review.md --cd /tmp/l16-worktree --profile review
cairn invoke antigravity --prompt-file /tmp/l16-review.md --cd /tmp/l16-worktree --mode plan
cairn invoke claude --prompt-file /tmp/l16-review.md --cd /tmp/l16-worktree --profile review
```

### A.11 Routing rows

Append to `skills/cairn/routing-eval.jsonl`:

```text
{"intent": "export my antigravity conversations from today", "expected": "cairn"}
{"intent": "please record the decision to ship one PR per layer", "expected": "cairn"}
{"intent": "log that we decided to keep BM25", "expected": "cairn"}
{"intent": "replace the old cache decision with the new rule", "expected": "cairn"}
{"intent": "forget the second decision in this session, it was wrong", "expected": "cairn"}
{"intent": "withdraw the claim about the cache, it is stale", "expected": "cairn"}
{"intent": "import the claude code memories for this repo as decision rows", "expected": "cairn"}
{"intent": "pause the journal routine until monday", "expected": "cairn"}
{"intent": "resume the daily routine now that I am back", "expected": "cairn"}
{"intent": "check whether my journal routine is healthy", "expected": "cairn"}
{"intent": "do the daily check on cairn before standup", "expected": "cairn"}
{"intent": "remove the block of cairn instructions from AGENTS.md", "expected": "cairn"}
{"intent": "cairn printed error code ROUTINE_HELD, what does it mean", "expected": "cairn"}
```

Append to `skills/recall/routing-eval.jsonl`:

```text
{"intent": "so what did we finally decide on the cache key", "expected": "recall"}
{"intent": "list the decisions we recorded this week", "expected": "recall", "ambiguous_with": ["cairn"]}
{"intent": "check that the citations in today's journal verify", "expected": "recall"}
{"intent": "a session from this morning seems to be missing", "expected": "recall"}
```

"why can't recall see" needs no row: it holds every word of "recall", so the coverage
rule exempts it (`tests/test_skill_routing.py:314,335`).

`tests/test_skill_routing.py`: line 33 `MAX_ROWS = 30`; in `DECLARED_OVERLAPS` add
`("cairn", "recall"),`; line 133 `10` becomes `15`. Update
`docs/skill-routing-eval-spec.md` 4.7 ("8 to 30") and its failure row F6.

### A.12 Tests

As listed in section 8. `tests/test_docs_cli.py:727`:
`for folder in ("skills", "references", "docs", ".agents/skills"):`.

### A.13 CONTRIBUTING, docs index, CHANGELOG draft

`CONTRIBUTING.md`, at the end of "Changing a skill description":

```text
Before you open a PR that touches `skills/` or `.agents/skills/`, review each changed
skill with the repo-local `skill-review` skill (`/skill-review skills/NAME` in Claude
Code, `$skill-review skills/NAME` in Codex) and paste its VERDICT lines in the PR body.
Write skill text plainly: imperative steps, British spelling, no marketing adjectives.
```

`docs/README.md`, new first row:

```text
| 2026-10-30 | [Skills refresh](skills-refresh-spec.md) | spec | Every stacked capability gets a quoted trigger, a step and a routing row: decisions, routine pause, resume and a daily check, coverage questions, error codes, the recall-first block and memory import; a repo-local skill-review skill with a cited rubric. Rejected: `when_to_use`, a per-skill row cap, shipping skill-review. |
```

CHANGELOG draft (orchestrator, patch bump):

```text
### Changed
- Skills: the cairn skill triggers on recording, replacing and withdrawing decisions,
  pausing and resuming a routine, a daily routine check, the recall-first block, error
  codes and importing Claude memories; the recall skill on decision questions, journal
  checks and missing sessions. New workflows `daily-check.md` and `memory-import.md`.
  The search and recall skills run `cairn errors CODE` on a failure. Codex skill names
  and policy files for every skill.
### Added
- `.agents/skills/skill-review` (repo-local, not shipped): a skill review with a rubric
  cited to the Claude Code, Codex, Antigravity and Agent Skills documentation.
```
