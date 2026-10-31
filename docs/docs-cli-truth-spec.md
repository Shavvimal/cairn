# Layer 12 `docs-cli-truth`: every documented `cairn` invocation parses

Status: implemented in this PR, on top of the 2.18.0 stack (the provenance, exporter
guards, Codex trusted hash, routine hardening, recall gaps, journal verify, decisions
table, forget and recall-first block changes). Section 11 records where the
implementation differs from this text. Sections 2 to 4 cite `path:line` on `main` at
2.9.0 (`f0ea1e1`), where the design started.

**Dependencies.** Layers 01-11. This layer sits above every CLI change in the stack, so
its test validates the final docs against the final parsers. It assumes each lower layer
(a) adds its new commands through `_ADMIN` or the source registry, (b) builds each parser
in a function and does not parse inline in `main()`. If a lower layer added a module that
parses inline (for example an `errors` or a `recall verify` entry point), this
layer extracts its builder with the same seam rule (section 3.1). The implementer rebases
on layer 11, runs the test, and fixes all drift found at that point, not only the drift
listed in section 4.4.

## 1. Purpose and non-goals

README.md, skills, references and docs hold about 530 `cairn ...` invocations (153 in
fenced blocks and 378 in inline code on main, counted with the section 3.2 rules).
Agents copy them and run them. Nothing checks them against the parsers. A renamed flag
leaves a stale doc, and the agent then gets exit 2 from argparse. A comparable tool
validates each documented invocation against its real argument parser in a unit test, and
exempts historical examples with an HTML comment marker. This layer does the same for
cairn.

Goals:

1. One parser seam per dispatch target, so a test can build every parser without running
   a command.
2. `tests/test_docs_cli.py`: it extracts every invocation, routes it through the
   dispatcher table, and parses it with the real parser. A mismatch fails with
   `path:line` and the parser's own error.
3. Fix the drift on main (section 4.4).
4. A cheap check of skill `allowed-tools` lines.

Non-goals:

- Runtime validation after parsing. For example, `recall expand --max-msgs 0` parses
  but `recall.py:134-136` rejects it. The test checks argparse only.
- Prose that names a flag outside code (plain text), CHANGELOG.md (history by design),
  and output samples that do not start at command position.
- Checking other programs (`qmd`, `rg`, `agy`, `launchctl`).
- A new dependency, or a shell parser. The tokeniser is about 120 lines of stdlib code.

## 2. Current behaviour

Dispatcher. `src/cairn/__main__.py:21` maps each registry source to
`Source.entrypoint` (`src/cairn/registry.py:20`, the string
`cairn.sources.<name>:main`). `__main__.py:24-35` `_ADMIN` maps 10 commands to
`"module:function"` strings. `main()` (`__main__.py:118`) prints `_HELP`
(`__main__.py:37-103`) for `help`/`-h`/`--help`. For an unknown command it prints
`cairn: unknown command 'X'.` and exits 2 (`__main__.py:129-137`). Otherwise it rewrites
`sys.argv` to `["cairn <command>", *rest]` (`__main__.py:141`) and calls the target.

Parser construction per dispatch target:

| Target | Where the parser is built | Builder function today |
|---|---|---|
| 5 conversation sources | `cli.build_parser(source)` `src/cairn/cli.py:57-120`; `cli.run` builds and parses at `cli.py:129-130`; per-source extras via `extend_export_parser` (`claude.py:897`) and `add_extra_parsers` (`claude.py:942`, `codex.py:794`, `antigravity.py:740` via `resume.add_source_parser` `resume.py:48-53`) | yes, but it needs a source instance; each module's `main()` only calls `cli.run(XSource())` (`claude.py:1033-1035`, `codex.py:822-824`, `antigravity.py:770-772`, `cursor.py:495-497`, `granola.py:874-876`) |
| `service-docs` | inline in `main()` `src/cairn/sources/service_docs.py:182-201`, reads `sys.argv`, `prog="service-docs"` | no |
| `recall` | inline `src/cairn/recall.py:51-106` | no |
| `sync` | inline `src/cairn/sync.py:275-293` | no |
| `resume` | inline `src/cairn/resume.py:56-60` | no |
| `hook` | inline `src/cairn/hooks.py:384-421` | no |
| `config` | inline `src/cairn/admin.py:685-739` | no |
| `cron` | inline `src/cairn/admin.py:831-838` | no |
| `doctor` | inline `src/cairn/admin.py:1089-1102` | no |
| `invoke` | `build_parser()` `src/cairn/invoke.py:2626-2716` | yes |
| `routine` | `build_parser()` `src/cairn/routine.py:536-563` | yes |
| `agents` | `build_parser()` `src/cairn/agents.py:579-602` | yes |

Each source module calls `get_config()` at import time (`claude.py:67`, `codex.py:46`,
`antigravity.py:63`, `cursor.py:44`, `granola.py:52`, `service_docs.py:25`). The function
is cached once per process (`config.py:906-909`). In a checkout with no user config it
falls back to the repo example (`config.py:342-361`). `tests/test_sources.py:28-29` and
`:64` already import sources and call `cli.build_parser` this way, so the new test adds
no new filesystem dependency. No parser reads config: every `choices` list comes from
constants (`recall.py:96-105`, `hooks.py:388`, `invoke.py:2668-2700`).

Existing docs tests: `tests/test_config_examples.py:20-55` compares the two example
copies byte-for-byte and loads them. No test reads markdown for commands.

## 3. Surface

### 3.1 Parser seams to add (no behaviour change)

Seam rule: the builder for dispatch spec `module:main` is `module:build_parser`. The
builder for `module:<name>_main` is `module:build_<name>_parser`. Each builder takes no
arguments, returns an `argparse.ArgumentParser`, and does no I/O. Each `main()` becomes
`args = build_parser().parse_args(argv)` and keeps its body.

| Module | Change |
|---|---|
| `src/cairn/cli.py` | `build_parser(source, prog: str \| None = None)`. Passes `prog` to `ArgumentParser`. `cli.run` still passes nothing, so `prog` stays derived from `sys.argv[0]` (`cli.py:61-62`). |
| `src/cairn/sources/claude.py` | add `build_parser()` = `cli.build_parser(ClaudeSource(), prog="cairn claude")` |
| `src/cairn/sources/codex.py` | add `build_parser()`, same shape, `prog="cairn codex"` |
| `src/cairn/sources/antigravity.py` | add `build_parser()`, `prog="cairn antigravity"` |
| `src/cairn/sources/cursor.py` | add `build_parser()`, `prog="cairn cursor"` |
| `src/cairn/sources/granola.py` | add `build_parser()`, `prog="cairn granola"` |
| `src/cairn/sources/service_docs.py` | extract `build_parser()` from `main()` (lines 183-199). `main(argv: list[str] \| None = None)` parses `argv`. Keep `prog="service-docs"`. |
| `src/cairn/recall.py` | extract `build_parser()` from lines 52-105 |
| `src/cairn/sync.py` | extract `build_parser()` from lines 276-292 |
| `src/cairn/resume.py` | extract `build_parser()` from lines 57-59 |
| `src/cairn/hooks.py` | extract `build_parser()` from lines 385-420 |
| `src/cairn/admin.py` | extract `build_config_parser()` (686-738), `build_cron_parser()` (832-837), `build_doctor_parser()` (1090-1101) |
| `src/cairn/invoke.py`, `routine.py`, `agents.py` | none, `build_parser()` exists |
| lower-layer modules | any new dispatch target follows the rule; the seam test enforces it |

### 3.2 Tokeniser rules

The scanner turns each code region into zero or more invocations, each an argv list after
the word `cairn`.

| # | Input shape | Example | Rule |
|---|---|---|---|
| 1 | Fenced block | ```` ```bash ````, ```` ```text ````, `~~~`, indented in a list | Every line is scanned, whatever the info string. A fence opens on three or more backticks or tildes, `^\s*(`{3,}\|~{3,})`, and closes on the same character at least as long, so a ```` ```` ```` block can hold a ```` ``` ```` line. A blockquote prefix (`> `) is removed first. |
| 2 | Inline code | `` `cairn sync --all` `` | Only spans whose text starts with `cairn `. A span can cross a soft line break inside one paragraph; the break becomes one space and the reported line is the opening one. A blank line, a heading, a list item or a table row starts a new paragraph. Blockquote prefixes are removed. |
| 3 | Continuation | `... --effort <level> \` | A trailing `\` joins the next line with one space, not past a closing fence. The reported line is the first. |
| 4 | Command position | `$ cairn`, `&& cairn`, `VAR=x cairn`, `python -m cairn`, `uv run cairn` | `cairn` counts at line start (after an optional `$ ` prompt), after `&&`, `\|\|`, `;`, a spaced `\|`, `$(`, a backtick or `(`, and after env assignments and the wrappers `uv run`, `python[3] -m`, `nohup`, `exec`, `command`, `time`, `timeout N`. Elsewhere it is prose and is ignored. `.../cairn` and `cairn.foo` are ignored. |
| 5 | End of invocation | `# comment`, `\| head`, `2>&1`, `> out`, `<<EOF`, `;`, `&`, `)` | An unquoted `#` at word start, a spaced pipe, `;`, `&`, `)`, a backtick, or a redirect ends the argv. Rule 4 never matches inside a quoted literal or a comment, and a match inside the previous invocation's argv is skipped. A `$(…)` inside double quotes still counts. |
| 6 | Description column | `cairn routine run NAME   run due days now` | Two or more spaces then a lowercase letter, outside brackets and quotes, ends the argv. This handles help listings. |
| 7 | Quotes | `--prompt "…"`, `'last week'` | One literal word. It is never a placeholder, so `"…"` is a value. |
| 8 | Angle placeholder | `<thread-id>`, `<child file>`, `claude:<id>` | `<` opens a span through the next `>`, spaces included. A word that contains such a span is a placeholder. |
| 9 | Name placeholder | `NAME`, `DATE`, `THREAD_ID`, `SOURCE:ID`, `{{DAY}}`, `$VAR`, `${VAR}`, unquoted `$(…)` | Placeholder. Upper-case shape: `^[A-Z][A-Z0-9_]*(:[A-Z0-9_]+)?$`. Hyphens are allowed (`YYYY-MM-DD`), and so are `Nd`/`Nw` (`^N[a-z]$`) and a lower-case `{name}` from a registry verify argv. In `--opt=<value>` only the value is a placeholder; the option name stays literal, so an unknown option still fails. |
| 10 | Ellipsis | `...`, `…`, `args…`, `S...` | A repeatable placeholder. It is dropped if the parser reports it as unrecognised. |
| 11 | Optional group | `[--day D]`, `[--since 7d \| --all]` | The brackets are removed. Inside a group, a spaced `\|` separates alternatives. Groups can nest. The scanner parses one argv per alternative index: argv `k` takes alternative `k` of every group (or its last), so each alternative is parsed at least once. |
| 12 | Bare alternation | `--harness claude\|codex\|antigravity` | `a\|b` with no spaces is one word with alternatives, expanded like a group. In a fence, if two alternatives are subcommand names and the word before is not an option, it is a violation under rule 13, because `install\|uninstall` pipes too. Inline, it is prose shorthand. |
| 13 | Spaced pipe into a subcommand | `cairn routine install \| uninstall NAME` <!-- cairn-cli: skip --> | If the first word after a spaced `\|` is a subcommand name anywhere in the route's parser tree, this is a violation, unless it is a filter (`tail`, `head`, `jq`, `grep`, `less`, `sort`, `wc`, `tee`, `cat`) followed by nothing or an option, which is a real pipe: `usage alternation 'a \| b' runs as a shell pipe; write one command per line`. |
| 14 | Help and version | `-h`, `--help`, `cairn sync --version` <!-- cairn-cli: skip --> | Parsed like any argv. A parser exit with status 0 (help, or a source parser's own `--version` action) is a pass. `cairn --version` and `cairn -V` are top-level dispatcher flags (`__main__.py:144`) and fall under rule 15. |
| 15 | Not a command | `cairn - local session…`, `` `cairn invoke: …` `` | If the first argv word does not match `^[a-z][a-z-]*$` and is not a placeholder, there is no invocation. |
| 16 | Template | `cairn <source> <verb>`, `cairn <command> [args…]` | If every argv word is a placeholder, the line is a usage template and is skipped. |

### 3.3 Placeholder substitution

Each placeholder becomes the sentinel `__cairn_ph_<n>__`, and the scanner parses the
argv. If the parser error names a sentinel, the scanner replaces that sentinel with each
candidate in order and keeps the first candidate that does not produce an error naming
it. The candidates are `1`, `2026-01-01`, `7d`, and then every `choices` value in the
route's parser tree, in declaration order. If the error names no sentinel but sentinels
remain (an `ArgumentTypeError` that does not echo its value), the scanner tries the
remaining sentinels left to right. A placeholder in the dispatcher slot tries the
`_SOURCES` keys, then the `_ADMIN` keys; a placeholder whose text holds `source` tries only
`_SOURCES`, and a literal verb after it keeps only routes that have subcommands, so
`cairn <source> no-such-verb` <!-- cairn-cli: skip --> cannot pass through `resume` or `errors`. For each sentinel,
a candidate that makes the argv parse outright wins over an earlier candidate that only
moves the error on, so `cairn routine <action> NAME --reason x` finds `pause`. The work is bounded by the number of
placeholders times the number of candidates.

### 3.4 Markers

| Marker | Where | Effect |
|---|---|---|
| `<!-- cairn-cli: historical -->` | the line directly above a fence | the whole block is exempt |
| `<!-- cairn-cli: historical -->` | on, or directly above, a line with inline code | the inline spans on that line are exempt |
| `<!-- cairn-cli: skip -->` | on a line with inline code, or alone on the line above it | the inline spans on that line are exempt |
| `<!-- cairn-cli: skip -->` | the line directly above a fence | the whole block is exempt; for a block that quotes a file body, not commands |

Use `historical` for an example of an older release's CLI. Use `skip` for a span that is
not a command, such as an exact error string or a label.

### 3.5 Inline relaxations

An inline span often names a command group or a flag in prose. In inline code only, three
parser errors are accepted: `the following arguments are required`,
`one of the arguments ... is required`, and `expected one argument` when the flag is the
last word. argparse reports a missing argument before an unknown option, so no relaxation
applies when an argv word that starts with `-` is not an option string in the route's
parser tree. A fenced line gets no relaxation.

### 3.6 Doc set

`README.md`, `CONTRIBUTING.md`, `skills/**/*.md`, `references/**/*.md`, `docs/**/*.md`,
and the `_HELP` string in `src/cairn/__main__.py`, scanned as one text fence with line
numbers in that file by its own test (`test_help_overview_invocations_parse`). Files are
sorted within each folder. `CHANGELOG.md` is excluded.

### 3.7 Failure message

```text
Docs name cairn invocations the CLI rejects:
skills/cairn/SKILL.md:97: cairn routine install | uninstall NAME: usage alternation 'install | uninstall' runs as a shell pipe; write one command per line
docs/routine-spec.md:778: cairn routine kickstart NAME --kill: cairn routine: error: unrecognized arguments: --kill

Fix the doc to the current CLI (check `cairn <command> --help`). If the example documents
an older release, put <!-- cairn-cli: historical --> on the line above its fence. For an
inline span that is not a command, put <!-- cairn-cli: skip --> on its line.
Rerun: uv run python -m unittest tests.test_docs_cli
```

The `cairn ...` text after `path:line:` is the source text with whitespace collapsed,
not the substituted argv.

## 4. Behaviour

### 4.1 The scan

1. List the doc set (3.6) in sorted order.
2. For each file, yield code regions (3.2 rules 1-3) with their line numbers and markers
   (3.4).
3. For each region, find each `cairn` at command position (rule 4) and tokenise up to the
   end (rules 5-12). Apply rule 13 to the remaining text, and rules 14-16 to the argv.
4. Route: the first argv word must be in `_SOURCES` or `_ADMIN`, else the violation is
   `cairn: unknown command 'X'` (the dispatcher's own text, `__main__.py:133`).
5. Build the parser through the seam (3.1). Cache it per command for the run.
6. Parse with substitution (3.3). Call `parser.parse_args(argv)` inside
   `contextlib.redirect_stderr`. Catch `SystemExit`. The error is the last stderr line,
   `<prog>: error: <message>`. Do not patch argparse.
7. Apply the inline relaxations (3.5). Record any other error as a violation.
8. Assert that the violation list is empty, with the 3.7 message.

### 4.2 The `allowed-tools` check

1. Read the frontmatter of every `skills/*/SKILL.md`. Split `allowed-tools` on commas at
   bracket depth 0.
2. Each `Bash(...)` entry must match
   `^Bash\(([A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*)(:\*| \*)\)$`. Both rule forms are
   accepted, and the prefix can hold more than one word (`Bash(cairn recall gaps:*)`). `Bash(cmd *)` is the canonical Claude Code form.
3. If the skill has no bare `Bash` entry, every program at command position in its
   `bash`, `sh`, `shell` or `console` fences (and after a spaced `|`, with continued
   lines joined first and `VAR=value` prefixes skipped) must have a
   `Bash(program...)` entry. On main this passes. `recall` runs `cairn`, `qmd`, `rg` and
   `head`. `search` runs `qmd`, `rg` and `head`. `invoke-antigravity` runs `cairn` and
   `agy`. The `/search` lines in `skills/search/SKILL.md` sit in an unlabelled fence and
   are not checked.

### 4.3 Order in the stack

The layer merges after layers 01-11. Its test fails on any lower-layer doc that names a
flag its layer renamed during review. This is intended. The fix lands in this layer and
not in the lower layer.

### 4.4 Drift on main at 2.9.0

A prototype of these rules found 16
violations in the doc set and 6 in `_HELP`. It found no false negatives among the README
fences that were checked by hand.

| Location | Finding | Fix |
|---|---|---|
| `docs/routine-spec.md:778`, `:1180`, prose `:1182`, `:1188`, header `:5` | `--kill` does not exist. The flag is `--force-restart` (`routine.py:558-562`). | Write `--force-restart`. Drop "6.6 names `--kill`" from the header. |
| `docs/routine-spec.md:752` (also `:680` and `docs/recall-digest-spec.md:533`, which pass only through the inline relaxation) | `cairn invoke --owner x`: `--owner` <!-- cairn-cli: historical --> belongs to the peer subcommands (`invoke.py:2651-2655`) | `cairn invoke <peer> --owner …` |
| `skills/cairn/SKILL.md:91, 92, 95, 96, 97, 99, 125` | spaced-pipe usage alternation (rule 13) | One command per line |
| `docs/routine-spec.md:775, 777` | spaced-pipe alternation in the help block the spec quotes | Copy the new `_HELP` lines |
| `src/cairn/__main__.py:83, 86, 91, 92, 94` (`_HELP`) | spaced-pipe alternation | Use the compact `install\|uninstall NAME` form (rule 12), so the overview keeps one line per group |
| `skills/cairn/workflows/log-session.md:5` | `cairn <source> note/close/log` | `` `cairn <source> note\|close\|log` `` | <!-- cairn-cli: skip -->
| `references/harnesses/claude.md:28` | a Monitor description label, not a command | `<!-- cairn-cli: skip -->` |
| `docs/routine-spec.md:809`, `:1170` | exact error strings that start with `cairn routine ...` | `<!-- cairn-cli: skip -->` |

## 5. Data formats

```python
@dataclass(frozen=True)
class Invocation:
    path: str            # repo-relative, or "src/cairn/__main__.py" for _HELP
    line: int            # 1-based, the first line of a continuation
    kind: str            # "fence" | "inline"
    text: str            # source text after `cairn`, whitespace collapsed
    words: tuple[tuple[str, bool], ...]  # (word, is_placeholder)
    pipe_into: str | None  # first word after a spaced pipe, for rule 13
```

A violation is the string `f"{path}:{line}: cairn {text}: {error}"`. The module writes
no file and no JSON.

## 6. Failure modes

| Mode | Behaviour |
|---|---|
| The user config is malformed and a source import raises `ConfigError` | The test errors at import, the same as `tests/test_sources.py`. CI has no user config and uses the repo example. |
| An extractor bug finds nothing | `test_scan_finds_the_doc_set` asserts at least 400 invocations (531 on main) and at least one per file in a fixed list (README, `skills/cairn/SKILL.md`, `skills/recall/SKILL.md`, `references/harnesses/codex.md`, `docs/routine-spec.md`). |
| argparse wording differs across 3.11-3.13 | Only the sentinel substring and the three relaxation phrases are matched. All are stable across 3.11-3.13. |
| `nargs=REMAINDER` (`--extra`, `invoke.py:2670`) swallows the rest | This is accepted. It matches the runtime. |
| `--help` in an argv | Rule 14. Parsed with stdout and stderr captured; exit 0 is a pass. |
| A builder does I/O | `test_builders_do_no_io` builds every parser with `builtins.open`, `io.open` (so `Path.read_text`), `os.open`, `os.listdir`, `os.scandir` and `subprocess.Popen` patched to raise. |
| The new spec doc holds deliberate bad examples | They carry the `historical` marker, and the test covers that. |

## 7. Tests

`tests/test_docs_cli.py`. The helper functions live in the same module: `code_regions`,
`invocations`, `tokenise`, `build_route_parser`, `parse_error`, `scan`.

- `TestTokeniser.test_prompt_prefix_and_continuation_are_joined`: `$ cairn x \`+next line gives one argv at the first line.
- `TestTokeniser.test_comment_redirect_and_pipe_end_the_argv`
- `TestTokeniser.test_angle_placeholder_with_space_is_one_word`
- `TestTokeniser.test_optional_group_keeps_first_alternative`
- `TestTokeniser.test_bare_alternation_keeps_first`
- `TestTokeniser.test_description_column_ends_the_argv`
- `TestTokeniser.test_quoted_ellipsis_is_a_literal_value`
- `TestTokeniser.test_ellipsis_word_is_dropped_when_unrecognised`
- `TestTokeniser.test_command_position_wrappers`: `python -m cairn`, `VAR=1 cairn`, `&& cairn` match. `a cairn b` and `org/cairn --ref` do not.
- `TestTokeniser.test_quoted_or_commented_cairn_is_not_a_command`: `; cairn` inside quotes or a `#` comment is not an invocation; `"$(cairn …)"` is.
- `TestRegions.test_historical_marker_exempts_next_fence_only`
- `TestRegions.test_skip_marker_exempts_its_inline_line`
- `TestRegions.test_inline_span_must_start_with_cairn`
- `TestRegions.test_inline_span_crosses_a_soft_break`
- `TestRegions.test_blockquoted_fence_is_scanned`
- `TestRegions.test_longer_fence_holds_a_shorter_one`
- `TestRegions.test_continuation_keeps_a_lowercase_argument`
- `TestRouting.test_every_dispatch_target_has_a_builder`: every `_SOURCES` and `_ADMIN` spec resolves to a builder under the 3.1 rule.
- `TestRouting.test_builders_do_no_io`
- `TestRouting.test_placeholder_in_command_slot_tries_registry_order`
- `TestScanner.test_self_check_catches_each_class`: a synthetic doc with an unknown command, an unknown flag, a missing required argument in a fence and a spaced-pipe alternation gives exactly 4 violations at the expected lines. The same doc under the `historical` marker gives 0.
- `TestScanner.test_inline_relaxations_apply_to_inline_only`
- `TestScanner.test_option_value_placeholder_keeps_the_option`: `--no-such-flag=<file>` fails; `--max-msgs=<count>` without the positional fails.
- `TestScanner.test_help_and_version_are_parsed_on_the_route`: `cairn sync --version` <!-- cairn-cli: skip --> fails, `cairn claude --version` and `cairn sync --help` pass.
- `TestScanner.test_inline_relaxation_refuses_an_unknown_option`
- `TestScanner.test_every_alternative_is_parsed`
- `TestScanner.test_compact_subcommand_alternation_is_a_pipe_in_a_fence`
- `TestScanner.test_a_pipe_into_a_filter_is_not_an_alternation`
- `TestScanner.test_source_placeholder_with_a_bogus_verb_fails`
- `TestScanner.test_verb_placeholder_tries_every_subcommand`
- `TestRegions.test_skip_marker_above_an_inline_line`
- `TestRegions.test_escaped_quote_stays_inside_the_literal`
- `TestShellCommands.test_continuation_and_env_prefix_are_read`
- `TestShellCommands.test_non_shell_fences_and_comments_are_ignored`
- `TestDocs.test_scan_finds_the_doc_set`
- `TestDocs.test_every_documented_invocation_parses`
- `TestDocs.test_help_overview_invocations_parse`
- `TestSkillFrontmatter.test_bash_rules_are_well_formed`
- `TestSkillFrontmatter.test_restricted_skills_allow_every_program_they_run`

Discrimination: revert one fix from 4.4 (for example, put `--kill` back). Then
`test_every_documented_invocation_parses` must fail and name that line. Save the output
of both runs to a file for the PR evidence line.

## 8. Docs to update

- `docs/docs-cli-truth-spec.md`: this spec, with its bad examples under the `historical`
  marker.
- `CONTRIBUTING.md`, "Before you open a PR" (`CONTRIBUTING.md:23`): one paragraph on
  the check, the two markers and the rerun command.
- The drift fixes in 4.4: `skills/cairn/SKILL.md`,
  `skills/cairn/workflows/log-session.md`, `references/harnesses/claude.md`,
  `docs/routine-spec.md`, `docs/recall-digest-spec.md`, `src/cairn/__main__.py` `_HELP`.
- Any drift found after the rebase on layer 11.

## 9. Implementation plan

1. Add the parser seams in 3.1, one commit, no behaviour change. `make check` stays
   green. About 60 lines changed in 11 files.
2. Add `tests/test_docs_cli.py` with the tokeniser, regions, routing and self-check tests
   first. About 380 lines.
3. Add `TestDocs` and run it. Paste the violation list into the PR notes.
4. Fix the drift (4.4 and any drift from lower layers). About 30 doc lines and 5 lines
   of `_HELP`.
5. Add `TestSkillFrontmatter`. About 50 lines.
6. Write the spec doc and the CONTRIBUTING paragraph. Run `make check`. Run the
   discrimination check and save the output.

Total estimate: about 520 lines, 4 to 6 hours.

## 10. Open questions

1. **Scan `_HELP` too?** It is code, but agents read `cairn help`. Recommended: yes.
   It costs 5 changed help lines.
2. **`docs/routine-spec.md` `--kill`: fix it or mark it historical?** The header says
   that the maintainer answers win. Recommended: fix it to `--force-restart`. Agents read
   the spec as current behaviour, and the marker should stay rare.
3. **`service-docs` prog.** Its errors print `service-docs:` and not
   `cairn service-docs:` (`service_docs.py:184`). Recommended: leave it in this layer
   (no behaviour change) and file a follow-up fix of 1 line.

## 11. As implemented

The implementation follows sections 3 to 7, with these differences.

- Seams. Besides the modules in 3.1, `cairn.instructions` already had `build_parser()`.
  `hooks.main()` still reports `--dry-run applies to --harness codex` from the
  `install` or `uninstall` subparser, so the message keeps its prog; it finds that
  subparser in the built parser. `recall.main()` recomputes the session-source list it
  used to share with the parser code.
- `_HELP` and `skills/cairn/SKILL.md`. Section 4.4 proposed the compact
  `install|uninstall NAME` form for `_HELP`. That form is still a shell pipe when it is
  pasted, so every spaced or compact action alternation became one line per action
  (`cairn routine install NAME`, then `cairn routine uninstall NAME`). Value
  alternations such as `--harness claude|codex` stay, under rule 12.
- Markers. `<!-- cairn-cli: skip -->` on the line above a fence also exempts the block.
  `docs/recall-first-block-spec.md` uses it for the quoted recall-first block, whose
  prose line starts with "cairn keeps".
- `allowed-tools`. The rule regex accepts a prefix of more than one word, because
  `skills/search/SKILL.md` allows `Bash(cairn recall gaps:*)`. A command is allowed
  when its leading words start with a rule's words. Continuation lines and `#`
  comments in shell fences are not read as commands.
- Doc set. 746 invocations in 40 files (214 fenced, 532 inline) and 63 in `_HELP`, at
  2.18.0. After review (multiline spans, layers 10 and 11 below): 876 in 44 files (214
  fenced, 662 inline) and 64 in `_HELP`.
- Placeholders. `{name}` in a registry verify argv (`docs/error-registry-spec.md`) is a
  placeholder under rule 9.

Drift found at 2.18.0, in addition to the 2.9.0 findings in 4.4:

| Location | Finding | Fix |
|---|---|---|
| `skills/cairn/SKILL.md`, "Top-level commands" | spaced-pipe alternations for `recall list/expand`, `recall digest/frame`, `config init/set/unset/path`, `instructions install/remove/show/status`, `routine pause/resume` and `invoke cancel/publish` | One command per line |
| `src/cairn/__main__.py` `_HELP` | spaced-pipe alternations for `hook`, `instructions`, `routine pause/resume` | One command per line |
| `docs/recall-first-block-spec.md:99` | quotes the old `instructions` help line | Names the four new help lines |
| `docs/recall-first-block-spec.md:186` | the quoted block's prose line starts with `cairn` | `skip` marker above the fence |
| `docs/note-forget-spec.md:250` | a normalised claim in inline code, not a command | `skip` marker |
| `docs/antigravity-invoke-postmortem.md:183` | a proposed `cairn invoke result <id> --files` <!-- cairn-cli: skip --> in a span that crossed a line break, found once rule 2 read multiline spans | span on one line, `skip` marker |
