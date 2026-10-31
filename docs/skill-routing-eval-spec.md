# Layer 11: skill-routing-eval

Status: implemented in this PR (branch `shav/mem-11-skill-routing-eval`). Section 11 lists
where the implementation departs from the text below; where they differ, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0. The fixtures in
Appendix A were run against a prototype of the test (a copy of the seven `SKILL.md`
files with the search description change from section 3.3), and again against the
shipped test on top of layers 01-10 (section 11). Result: 77 rows,
0 failures, 4 declared overlap pairs.

**Dependencies:** none for code. The test reads whatever skill descriptions exist at the
layer's base. Layers 05, 06, 07, 08 and 09 might add quoted trigger phrases to the
`recall`, `search` or `cairn` descriptions. If they do, the implementer reruns the test
after the rebase and adjusts Appendix A rows (section 6, row F7). Layer 14 also edits
`CONTRIBUTING.md`; this layer adds its own section in a separate hunk (section 8).

---

## 1. Purpose and non-goals

### 1.1 Purpose

A harness picks a skill from the frontmatter `description`. cairn writes the trigger
phrases into that text as a quoted list after "Use when the user says". Nothing checks
that list. Two skill pairs overlap in vocabulary (recall and search, the three invoke
skills), and a description edit can move a request from one skill to another without any
test noticing.

This layer adds:

1. `skills/<name>/routing-eval.jsonl` for each of the seven skills: 8 to 13 paraphrased
   user intents per skill, plus a few negative rows.
2. `tests/test_skill_routing.py`: a deterministic stdlib check that each intent matches
   the trigger set of its expected skill and of no other skill, unless the row declares
   the overlap. A failure message ends with every overlap pair as a table.
3. A lint in the same test: an intent that equals or contains a whole trigger phrase
   fails with "copied from trigger".
4. A `CONTRIBUTING.md` section: a description change updates the fixture and runs the
   test.
5. One description change: `search` gets a quoted trigger list (section 3.3), because
   today it has none and the test cannot route anything to it.

A comparable tool keeps the same kind of per-skill fixture with a trigger-substring
check, an `ambiguous_with` exemption and a copy lint. cairn's version differs in the
matcher (token set, not substring) for the reason in section 4.3.

### 1.2 The limit (stated in the test docstring too)

This test checks the trigger text, not the router. Claude Code and Codex route with a
model that reads the whole description, the body of nearby skills and the conversation.
A row that passes here can still route wrongly in a harness, and a row that fails here
can route correctly. The test catches a narrower fault: a description edit that drops a
phrase, adds a phrase that another skill already owns, or leaves a skill with no quoted
triggers. It is a regression guard on the text the harness reads.

### 1.3 Non-goals

- No model call, no harness run, no accuracy number for the real router.
- No new CLI command and no config key. Nothing ships to the runtime.
- No new frontmatter key (`triggers:`). The description is what both harnesses read, so
  the description stays the single source.
- No change to `recall`, `cairn` or the invoke descriptions. The test reports their
  overlaps. Section 10 asks whether to change them.

---

## 2. Current behaviour (main at 2.9.0)

| Fact | Evidence |
|---|---|
| Seven skills, one `SKILL.md` each, frontmatter `name` and `description` on lines 2-3 | `skills/*/SKILL.md:2-3` |
| `recall`, `cairn` and the three invoke skills list quoted triggers after "Use when the user says" | `skills/recall/SKILL.md:3`, `skills/cairn/SKILL.md:3`, `skills/invoke-codex/SKILL.md:3`, `skills/invoke-claude/SKILL.md:3`, `skills/invoke-antigravity/SKILL.md:3` |
| `recall` also quotes "One Thing" before that clause, which is not a trigger | `skills/recall/SKILL.md:3` |
| `search` has no quoted trigger. It says "Use for explicit Cairn search or locating material in personal session history" and excludes web search, repository code search and "find/look up" | `skills/search/SKILL.md:3` |
| `setup` is never model-invoked: `disable-model-invocation: true` for Claude Code, `allow_implicit_invocation: false` for Codex | `skills/setup/SKILL.md:4`, `skills/setup/agents/openai.yaml:2`, `references/harnesses/codex.md:50-53` |
| The invoke skills allow implicit invocation in Codex | `skills/invoke-*/agents/openai.yaml:2`, `references/harnesses/codex.md:97-100` |
| `parse_frontmatter` cannot read four of the seven files: `argument-hint: [..]` raises `FrontmatterParseError` ("flow/folded YAML is unsupported") | `src/cairn/frontmatter.py:70`, `:143-147`, `skills/recall/SKILL.md:4`, `skills/invoke-*/SKILL.md:4` |
| The only test that reads `SKILL.md` checks literal substrings, not frontmatter | `tests/test_plugin_manifest.py:77-102` |
| The plugin ships the whole repo (`source: "./"`) and Codex reads `./skills/` | `.claude-plugin/marketplace.json:8`, `.codex-plugin/plugin.json:5` |
| Tests run with `python -m unittest discover -s tests`; mypy and ruff cover `tests` | `Makefile:19-20`, `pyproject.toml:37`, `:51` |
| `CONTRIBUTING.md` has no section about skills | `CONTRIBUTING.md:1-63` |

Consequence: the test reads the frontmatter with its own small reader (section 4.1),
not with `parse_frontmatter`.

---

## 3. Surface

### 3.1 Files

| Path | Kind |
|---|---|
| `skills/{cairn,recall,search,setup,invoke-codex,invoke-claude,invoke-antigravity}/routing-eval.jsonl` | new, 77 rows in total |
| `tests/test_skill_routing.py` | new |
| `skills/search/SKILL.md` | line 3 changed |
| `CONTRIBUTING.md` | new section |
| `docs/skill-routing-eval-spec.md` | this spec |

The fixtures sit in the skill directories, so they ship in the plugin cache. Neither
harness loads a file from a skill directory unless `SKILL.md` links it, so the file is
inert there. That claim is UNVERIFIED for Codex; section 10 Q2.

### 3.2 No CLI, no exit code

The only entry point is `make check` / `uv run python -m unittest tests.test_skill_routing`.

### 3.3 search description, line 3

Replace the sentence `Use for explicit Cairn search or locating material in personal
session history.` with:

```
Use when the user says "cairn search", "search my sessions", "search past sessions", "search my meetings", "grep my sessions", or "exact search", or otherwise asks to locate material in personal session history.
```

The rest of the line stays, including the "Do not activate for ..." sentence. A trigger
"search my notes" was tried and dropped: its token set `{search, note}` matched the
negative row "search the web for the python 3.13 release notes".

### 3.4 Failure message headings (exact)

- `routing-eval: N problem(s)` followed by a table with columns
  `file:line | intent | expected | matched (skill <- trigger) | problem`.
- Problem strings, exact: `missed`, `also matched: <skill>[, <skill>]`,
  `ambiguous_with not matched: <skill>`, `copied from trigger: <skill> "<trigger>"`,
  `bad row: unknown skill: <name>`, `bad row: <reason>`, `duplicate intent (also <file>:<line>)`.
  A `bad row` line shows `-` in the intent, expected and matched columns.
- `bad row` reasons for the shape checks, exact: `blank line`, `not a JSON object`,
  `unknown keys [...]`, `intent must be a non-empty string`, `expected is required`,
  `expected must be a skill name or null`, `ambiguous_with must be a list of strings`,
  `ambiguous_with has duplicates`, `ambiguous_with contains expected`,
  `ambiguous_with must be empty when expected is null or not routable`,
  `note must be a string`, `fixture of unknown skill '<dir>'` (a fixture whose directory
  has no `SKILL.md`). An intent with no words fails as `intent must be a non-empty string`.
- `skill <name>: unbalanced quotes after "Use when the user says"` and
  `skill <name>: a quoted trigger has no words`.
- `skill <name>: no row matches "<trigger>" alone` and
  `skill <name>: "<trigger>" has the tokens of another trigger` (section 4.8).
- `undeclared overlap pairs` followed by a table
  `skill A | skill B | rows | example intent | triggers`.
- `skill <name>: description has no quoted trigger after "Use when the user says"`.
- `skill <name>: N fixture rows, need 8 to 30`.

---

## 4. Behaviour

### 4.1 Read the skills

1. Glob `skills/*/SKILL.md`. Skip the class (`skipUnless`) if `skills/` is absent, as
   `tests/test_plugin_manifest.py:22` does for an sdist run.
2. Read the lines between the first `---` and the next `---`. Take unindented
   `key: value` lines; an indented line folds into the previous value with one space, and
   a block indicator (`>`, `|`, `>-`, `|-`) starts an empty value. Take `name`,
   `description` and `disable-model-invocation`. Assert `name` equals the directory name.
3. A skill is routable unless `disable-model-invocation` is `true`.
4. Triggers: every double-quoted span in the description after the literal marker
   `Use when the user says`. Text before the marker is ignored, so `"One Thing"` in
   recall is not a trigger. A routable skill with zero triggers fails. An odd number of
   double quotes after the marker fails as `unbalanced quotes`, and a quoted span with no
   words fails as `a quoted trigger has no words`.

### 4.2 Normalise and tokenise

- `normalise(s)`: lowercase, replace every run of `[\W_]` with one space, strip.
- `stem(w)`: drop a final `s` if `len(w) > 3` and `w` does not end in `ss`, `us` or `is`.
  Then drop `ing` if `len(w) > 5`, else drop `ed` if `len(w) > 4`. This is deliberately
  crude: `sessions`, `meetings`, `working`, `exported` reduce to `session`, `meet`,
  `work`, `export`. It does not equate `schedule` with `scheduled`.
- Intent tokens: the stem of every word in the normalised intent.
- Trigger tokens: the stems of the words that are not in a fixed stopword list (articles,
  prepositions, pronouns, auxiliaries, `what`, `please`: 58 words in the module constant `STOPWORDS` in `tests/test_skill_routing.py`).
  Weak-trigger rule: if fewer than two tokens remain, keep all the trigger's words, so a
  one-word stopword trigger keeps its word. A trigger with no words matches nothing. This applies today to "what did we work on" and "what was I
  doing". Without it they reduce to `{work}` and `{doing}` (section 4.3 table).

### 4.3 Match: token set, not substring

A trigger matches an intent if every trigger token is in the intent token set. A skill
matches if any of its triggers matches.

Why not substring:

1. The copy lint (item 3 of the feature) rejects an intent that contains a whole trigger
   phrase. Under substring matching, every passing row contains a whole trigger phrase
   by definition. The two rules together reject every row. The comparable tool avoids
   this by linting only exact equality. That lets a row copy the trigger and add one
   word, which makes the check a tautology.
2. Substring misses ordinary paraphrase: "I want to export my sessions" does not
   contain "export sessions".

The price is false positives. The probe table below shows both matchers on intents that
are not fixtures (`noweak` is token set without the weak-trigger rule):

| Probe intent | substring | token set | noweak | Verdict |
|---|---|---|---|---|
| I want to export my sessions | none | cairn | cairn | token set right |
| can you ask the codex agent to review this | none | invoke-codex | invoke-codex | token set right |
| resume my last codex session | none | cairn, invoke-codex | same | real overlap, declared |
| what is codex doing right now | none | none | recall | weak-trigger rule removes a false positive |
| wire up the hooks and check qmd works | none | none | recall | same |
| what were we working on yesterday | none | none | recall | false negative: "were" is not "did" |
| close the session codex opened for the review | none | cairn, invoke-codex | same | false positive for invoke-codex (`{codex, review}`) |
| run the tests and log the session id | none | cairn | cairn | false positive (`{log, session}`) |
| get a second opinion on this plan | none | none | none | gap: no peer named, no quoted trigger |

So the token matcher over-matches when two content words of a trigger appear anywhere in
a longer request. The fixtures are short user requests, and the failure output names the
trigger, so a reviewer can judge each hit.

### 4.4 Copy lint

A row fails `copied from trigger` if its normalised intent equals any trigger of any
skill, or contains a trigger of two or more words as a contiguous word sequence
(`f" {t} " in f" {intent} "`). Single-word triggers (`recall`) are exempt from the
"contains" half, because a user types the bare word in a longer request and no
paraphrase exists. The lint makes each row exercise the token matcher, not repeat the
trigger.

### 4.5 Route check, per row

1. Shape: keys within `{intent, expected, ambiguous_with, note}`; `intent` a non-empty
   string; `expected` a skill name (a string) or `null`; `expected` is the owning
   directory or `null`; `ambiguous_with` a list of known skills, no duplicates, not
   containing `expected`, and empty when `expected` is `null` or not routable; `note`, if
   present, a string. A blank line before the last row is a bad row.
2. Normalised intents are unique across all files.
3. Copy lint (4.4).
4. If `expected` is routable: `expected` must match, else `missed`.
5. If `expected` is `setup` (not routable): the test cannot show setup is chosen. It
   checks only that no routable skill claims the intent.
6. Every other matched skill must be in `ambiguous_with`, else `also matched`.
7. Every `ambiguous_with` entry must match, else `ambiguous_with not matched`. A stale
   exemption fails.
8. `expected: null`: no routable skill may match. For a `null` or `setup` row the check
   ignores `ambiguous_with`, so an exemption cannot turn a negative row into a pass.

### 4.6 Overlap report

The test collects every unordered pair of skills that both match one row. The module
constant `DECLARED_OVERLAPS` holds the pairs this layer accepts:

| Pair | Example row | Triggers |
|---|---|---|
| cairn / invoke-codex | resume my last codex session | "resume session" / "codex resume" |
| invoke-claude / invoke-codex | ask both claude and codex to review the plan | "ask claude", "claude review" / "ask codex", "codex review" |
| invoke-antigravity / invoke-codex | review the diff with antigravity and with codex | "antigravity review" / "codex review" |
| recall / search | search the history of my sessions for the launchd plist error | "session history" / "search my sessions" |

A pair outside the constant fails with the full pair table, even when each row declares
it in `ambiguous_with`. A new overlap therefore needs two edits a reviewer sees: the row
and the constant.

### 4.7 Count check

Each skill has 8 to 30 rows whose `expected` is that skill. `null` rows do not count. The cap was 15 until the skills refresh (`skills-refresh-spec.md`) raised it for the umbrella cairn skill.

### 4.8 Trigger coverage

Section 6 row F1 holds only if each quoted phrase has a row that reaches its skill
through that phrase alone. For every routable skill, every trigger must be the only
trigger of that skill that matches some row naming the skill (`expected` or
`ambiguous_with`); deleting it then makes that row fail `missed` or
`ambiguous_with not matched`. Two exceptions:

- A trigger whose tokens hold all the tokens of another trigger of the same skill is
  subsumed: every intent it matches also matches the shorter trigger, so no row can
  isolate it and its deletion changes no route. At this layer: "sync service docs"
  (subsumed by "sync docs") and "search past sessions" (subsumed by "search my sessions").
- Two triggers of one skill with the same tokens fail with `has the tokens of another
  trigger`, because each hides the other.

---

## 5. Data format

One JSON object per line, UTF-8, no comments, no blank lines inside the file:

```json
{"intent": "resume my last codex session", "expected": "cairn", "ambiguous_with": ["invoke-codex"]}
{"intent": "get a second opinion on this plan", "expected": null, "note": "no peer named"}
```

| Key | Type | Required | Meaning |
|---|---|---|---|
| `intent` | string | yes | what a user types, never a trigger copy |
| `expected` | string or null | yes | owning skill, or null for "no skill" |
| `ambiguous_with` | list of strings | no | other skills this row may also match, each must match |
| `note` | string | no | why the row exists; ignored by the check |

---

## 6. Failure modes

| ID | Cause | Result |
|---|---|---|
| F1 | A description drops a quoted phrase that no other trigger of the skill subsumes | its isolating row fails `missed` or `ambiguous_with not matched` (4.8) |
| F2 | A description adds a phrase another skill's rows contain | `also matched`, and a new pair in the overlap table |
| F3 | A routable skill loses its "Use when the user says" clause | `description has no quoted trigger` |
| F4 | A row is a trigger copy | `copied from trigger` |
| F5 | Malformed JSON line | `bad row: <json error>` with `file:line` |
| F6 | A new skill directory without a fixture | `0 fixture rows, need 8 to 30` |
| F7 | A lower layer adds triggers | rerun; fix rows or declare the overlap |
| F8 | Frontmatter not closed by `---` | `skill <name>: frontmatter not closed` |
| F9 | A trigger gains a phrase but no row reaches the skill through it alone | `no row matches "<trigger>" alone` |

The test never touches `~/.local/state/cairn` or `~/.cache/cairn`. It reads repo files
only.

---

## 7. Tests (`tests/test_skill_routing.py`)

Module docstring states the limit from section 1.2 in two sentences.

| Test | Asserts |
|---|---|
| `TestTriggerExtraction.test_quoted_phrases_after_marker_only` | "One Thing" in recall is not a trigger; recall has 10 triggers |
| `TestTriggerExtraction.test_every_routable_skill_has_triggers` | F3 |
| `TestTriggerExtraction.test_setup_is_not_routable` | `setup` has `disable-model-invocation: true` |
| `TestMatcher.test_normalise_and_stem` | inline cases: `sessions`, `meetings`, `working`, `exported` |
| `TestMatcher.test_weak_trigger_keeps_stopwords` | "what was I doing" needs all four tokens; a one-word stopword trigger keeps its word; an empty trigger matches nothing |
| `TestMatcher.test_token_set_ignores_order` | inline synthetic triggers, no repo read |
| `TestMatcher.test_copy_lint` | equal, contained, single-word exemption, inline |
| `TestRoutingEval.test_rows_are_well_formed` | 4.5 step 1 and 2 |
| `TestRoutingEval.test_no_row_copies_a_trigger` | 4.4, one table of all offenders |
| `TestRoutingEval.test_each_row_routes_to_its_skill` | 4.5 steps 4 to 8, one table of all offenders |
| `TestRoutingEval.test_overlap_pairs_are_declared` | 4.6 |
| `TestRoutingEval.test_fixture_counts` | 4.7 |
| `TestRoutingEval.test_every_trigger_has_its_own_row` | 4.8 on the repository rows |
| `TestRowChecks.test_parse_rejects_each_bad_shape` | every `bad row` reason of 4.5 step 1, inline rows |
| `TestRowChecks.test_triggers_need_the_marker_and_balanced_quotes` | no marker gives no trigger, quote balance |
| `TestRowChecks.test_load_skills_reports_bad_descriptions` | unbalanced quotes, empty trigger, `name` mismatch, folded description, temp directory |
| `TestRowChecks.test_fixture_of_an_unknown_skill_is_a_bad_row` | orphan fixture directory |
| `TestRowChecks.test_blank_line_inside_a_fixture_is_a_bad_row` | blank line rule, temp directory |
| `TestRowChecks.test_route_problems` | `missed`, `also matched`, stale exemption, null and setup rows that try an exemption |
| `TestRowChecks.test_read_head_needs_both_delimiters` | `frontmatter not opened`, `frontmatter not closed`, temp file |
| `TestRowChecks.test_overlap_problems` | an undeclared pair and a stale declared pair |
| `TestRowChecks.test_uncovered_triggers` | uncovered trigger, subsumed trigger exempt, twin triggers |

The `TestMatcher` cases use inline data so a description edit cannot break them. Use
`self.fail(table)` once per test with every offender, not one assertion per row, so one
run shows the whole picture. Put the helpers (`normalise`, `stem`, `read_head`,
`triggers`, `trigger_matches`, `matched`, `copied`) at module level with type hints for mypy.

---

## 8. Docs to update

- `CONTRIBUTING.md`: new section `## Changing a skill description`, after
  "Adding a new source" (`CONTRIBUTING.md:44-51`), text in Appendix B.
- `docs/skill-routing-eval-spec.md`: this spec.
- `skills/search/SKILL.md:3`: section 3.3.
- No README change. No CHANGELOG or version change (the orchestrator does those).

---

## 9. Implementation plan

| Step | Files | Lines (est.) |
|---|---|---|
| 1. Edit the search description | `skills/search/SKILL.md` | 1 |
| 2. Write the helpers and `TestMatcher` | `tests/test_skill_routing.py` | 110 |
| 3. Write `TestTriggerExtraction` and `TestRoutingEval` with the table formatter | same | 130 |
| 4. Add the seven fixtures from Appendix A | `skills/*/routing-eval.jsonl` | 74 |
| 5. Run `uv run python -m unittest tests.test_skill_routing -v`; save the output to a file and read it | none | 0 |
| 6. Break one description on purpose (delete "codex review"); confirm the test fails with a readable table; revert | none | 0 |
| 7. Add the CONTRIBUTING section and copy this spec | `CONTRIBUTING.md`, `docs/skill-routing-eval-spec.md` | 15 + spec |
| 8. `make check`, then read the diff for hygiene shapes | none | 0 |

About 2 hours if the prototype carries over directly; half a day if a lower layer
changed descriptions and rows need rework.

---

## 10. Open questions

1. **Change weak or loose triggers in recall, cairn and the invoke skills?** The probe
   table shows "log session" and "codex review" over-matching, and "what did we work on"
   missing "what were we working on". Recommended default: no change in this layer. The
   real router handles these. Revisit only if a harness misroutes in practice.
2. **Ship the fixtures inside the plugin?** Recommended default: yes, in
   `skills/<name>/` as the feature asks. If Codex turns out to list extra files in a skill
   directory, move them to `tests/fixtures/routing/<name>.jsonl` and change one glob.
3. **Generic "second opinion" with no peer named.** No invoke skill matches it today, and
   the row records that as `expected: null`. Recommended default: keep it as a recorded
   gap. Adding "second opinion" to all three skills would make every such request an
   undeclared three-way overlap.

---

## 11. Implementation notes

Where the implementation departs from sections 1-10:

- Layers 05 to 09 changed the bodies of `recall`, `search`, `cairn` and `setup`, and the
  unquoted part of the `cairn` description (`decide/supersede/forget`). No quoted trigger
  changed, so the original 74 rows in Appendix A shipped unchanged. Review added three
  rows (section 4.8) for 77 in total. The test was run on top of layers 01-10: 23 tests,
  77 rows, 0 failures, the 4 declared pairs in section 4.6.
- The new `cairn` subcommands (`decide`, `supersede`, `forget`, `instructions`, `routine
  pause`) have no quoted trigger, so no row routes to them. Adding a row would need a
  `cairn` description change, which this layer does not make (section 1.3).
- `TestTriggerExtraction.test_frontmatter_is_readable` is an extra test. It reports F8
  (`frontmatter not closed`), a missing opening `---`, and a `name` that differs from the
  directory, for every skill in one list.
- `test_overlap_pairs_are_declared` also fails when `DECLARED_OVERLAPS` holds a pair that
  no row exercises, so a stale declaration cannot outlive its row.
- Every failure table from `TestRoutingEval` ends with the full overlap table, and the pair
  table carries a `status` column (`declared` or `UNDECLARED`).
- A row whose `expected` names another skill's directory fails as `bad row: expected
  '<skill>' in the fixture of '<owner>'`.
- Review added section 4.8, the `TestRowChecks` class with inline skills, and the shape
  rules for `expected` type, `note` type, blank lines and exemptions on negative rows.
  The second review added the empty-trigger and stopword-trigger rules, the quote-balance
  and orphan-fixture checks, and folding of indented frontmatter lines.
- The helpers live in the test module, not in `src/cairn`: this is test infrastructure and
  nothing ships to the runtime.

---

## Appendix A: fixture files (verified, 77 rows, 0 failures)

### skills/recall/routing-eval.jsonl

```jsonl
{"intent": "what did we end up working on yesterday afternoon?", "expected": "recall"}
{"intent": "can you recall where we got to with the auth refactor", "expected": "recall"}
{"intent": "load up the context about the qmd timeout bug before we start", "expected": "recall"}
{"intent": "do you remember back when we moved the config to json?", "expected": "recall"}
{"intent": "prime yourself with context from my past sessions on the exporter", "expected": "recall"}
{"intent": "what was I actually doing last tuesday", "expected": "recall"}
{"intent": "show me the history of my sessions this week", "expected": "recall"}
{"intent": "give me a digest of what happened yesterday", "expected": "recall"}
{"intent": "write the daily journal digest for 2026-10-28", "expected": "recall"}
{"intent": "cite the exact frame where we decided on BM25", "expected": "recall"}
{"intent": "what did we get done on the sync robustness work last week", "expected": "recall"}
{"intent": "pull up the history of my coding sessions about launchd", "expected": "recall"}
```

### skills/search/routing-eval.jsonl

```jsonl
{"intent": "do a search in cairn for 'trusted_hash'", "expected": "search"}
{"intent": "search through my old sessions for the word keychain", "expected": "search"}
{"intent": "search all my past claude sessions for the qmd embed error", "expected": "search"}
{"intent": "search my session notes from last month for 'quota'", "expected": "search"}
{"intent": "grep across my old sessions for example-quota-project", "expected": "search"}
{"intent": "do an exact-match search for SessionEnd in my memory", "expected": "search"}
{"intent": "can you search cairn for the granola decrypt discussion", "expected": "search"}
{"intent": "search my meeting notes for the pricing decision", "expected": "search"}
{"intent": "search the history of my sessions for the launchd plist error", "expected": "search", "ambiguous_with": ["recall"]}
{"intent": "search the web for the python 3.13 release notes", "expected": null, "note": "description excludes ordinary web search"}
{"intent": "find where parse_frontmatter is defined in this repo", "expected": null, "note": "description excludes repository code search"}
{"intent": "look up the docs for argparse subparsers", "expected": null, "note": "description excludes unrelated find/look up"}
```

### skills/cairn/routing-eval.jsonl

```jsonl
{"intent": "sync all my claude and codex sessions now", "expected": "cairn"}
{"intent": "export the last week of sessions to markdown", "expected": "cairn"}
{"intent": "export my codex threads from today", "expected": "cairn"}
{"intent": "export the cursor chats for this workspace", "expected": "cairn"}
{"intent": "export yesterday's granola meetings", "expected": "cairn"}
{"intent": "resume the session I had open on the hooks branch", "expected": "cairn"}
{"intent": "add a note to this session saying the fix is in PR 33", "expected": "cairn"}
{"intent": "close out this session as done", "expected": "cairn"}
{"intent": "log this session with a one-line summary", "expected": "cairn"}
{"intent": "sync the service docs from the config", "expected": "cairn"}
{"intent": "pull in the transcripts from today's meetings", "expected": "cairn"}
{"intent": "is my scheduled journal routine still running", "expected": "cairn"}
{"intent": "resume my last codex session", "expected": "cairn", "ambiguous_with": ["invoke-codex"]}
{"intent": "sync the docs listed in my config", "expected": "cairn"}
```

### skills/setup/routing-eval.jsonl

Setup is not routable (section 2). These rows check only that no routable skill claims
an install or update request.

```jsonl
{"intent": "install cairn on this machine", "expected": "setup"}
{"intent": "set up cairn for me", "expected": "setup"}
{"intent": "update cairn to the latest version", "expected": "setup"}
{"intent": "onboard me to cairn", "expected": "setup"}
{"intent": "reinstall the cairn engine and the hourly sync", "expected": "setup"}
{"intent": "wire up the cairn hooks and check qmd works", "expected": "setup"}
{"intent": "upgrade the cairn plugin and keep my config", "expected": "setup"}
{"intent": "I just cloned cairn, get it working", "expected": "setup"}
```

### skills/invoke-codex/routing-eval.jsonl

```jsonl
{"intent": "have codex take a look at this diff and review it", "expected": "invoke-codex"}
{"intent": "can you ask the codex agent whether this migration is safe", "expected": "invoke-codex"}
{"intent": "invoke a headless codex run on the plan", "expected": "invoke-codex"}
{"intent": "invoke a headless codex process on the plan", "expected": "invoke-codex"}
{"intent": "run this prompt through codex in the background", "expected": "invoke-codex"}
{"intent": "get codex to audit the hook installer", "expected": "invoke-codex"}
{"intent": "I want a second opinion on the schema from codex", "expected": "invoke-codex"}
{"intent": "resume the codex thread from the review earlier", "expected": "invoke-codex"}
{"intent": "use astra on high effort to check the refactor", "expected": "invoke-codex"}
{"intent": "review this with codex before I commit", "expected": "invoke-codex"}
{"intent": "ask both claude and codex to review the plan", "expected": "invoke-codex", "ambiguous_with": ["invoke-claude"]}
{"intent": "get a second opinion on this plan", "expected": null, "note": "no peer named: no invoke skill's quoted triggers match; routing rests on the unquoted cross-model clause"}
```

### skills/invoke-claude/routing-eval.jsonl

```jsonl
{"intent": "have claude look at the change and review it before I merge", "expected": "invoke-claude"}
{"intent": "ask the claude agent if this plan holds up", "expected": "invoke-claude"}
{"intent": "invoke a fresh claude run on the diff", "expected": "invoke-claude"}
{"intent": "I'd like a second opinion from a fresh claude on this design", "expected": "invoke-claude"}
{"intent": "resume that review claude did this morning", "expected": "invoke-claude"}
{"intent": "get claude code to review the tests headlessly", "expected": "invoke-claude"}
{"intent": "ask a fresh claude to poke holes in this migration", "expected": "invoke-claude"}
{"intent": "send the plan to claude for review", "expected": "invoke-claude"}
{"intent": "let claude pick up and resume where the peer review stopped", "expected": "invoke-claude"}
{"intent": "resume the claude conversation from earlier", "expected": "invoke-claude"}
```

### skills/invoke-antigravity/routing-eval.jsonl

```jsonl
{"intent": "have antigravity look over this pull request and review it", "expected": "invoke-antigravity"}
{"intent": "can you ask the antigravity agent whether the plan is sound", "expected": "invoke-antigravity"}
{"intent": "ask the agy cli to check the exporter", "expected": "invoke-antigravity"}
{"intent": "get gemini to review the hook code", "expected": "invoke-antigravity"}
{"intent": "invoke an antigravity run on the failing test", "expected": "invoke-antigravity"}
{"intent": "I want a second opinion on the schema from antigravity", "expected": "invoke-antigravity"}
{"intent": "resume the antigravity conversation from earlier", "expected": "invoke-antigravity"}
{"intent": "run a gemini pass to review the plan", "expected": "invoke-antigravity"}
{"intent": "review the diff with antigravity and with codex", "expected": "invoke-antigravity", "ambiguous_with": ["invoke-codex"]}
```

## Appendix B: CONTRIBUTING.md section

```markdown
## Changing a skill description

A harness picks a skill from the `description` in its `SKILL.md` frontmatter. The
quoted phrases after "Use when the user says" are the trigger list.

1. If you add, remove or reword a quoted phrase, update `skills/<name>/routing-eval.jsonl`.
   Each line is `{"intent": ..., "expected": ..., "ambiguous_with": [...]}`. Write the
   intent as a user would type it. Never copy a phrase of two or more words into it; a
   single-word trigger such as "recall" may appear inside a longer request. Each phrase
   needs one row that matches it and no other phrase of the same skill, unless it holds
   every word of a shorter phrase of that skill.
2. Run `uv run python -m unittest tests.test_skill_routing -v`. If you add or remove a
   `recall` phrase on purpose, also update the count in
   `test_quoted_phrases_after_marker_only`.
3. If the test reports a new overlap pair, decide whether the overlap is real. If it is,
   add the skill to the row's `ambiguous_with` and the pair to `DECLARED_OVERLAPS`. If it
   is not, reword the trigger.

The test checks the trigger text, not the harness router. A pass means the description
still names each request; it does not prove the harness picks that skill.
```
