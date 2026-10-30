# Skill review rubric

The checklist that `skill-review` applies. Each item names the rule, how to check it,
and its source: a file in this `references/` folder and its section, or a cairn test.
Severity: **block** (fix before merge), **fix** (fix in this PR unless the author
argues otherwise), **note** (judgement call; say why).

## Contents

- A. Frontmatter and naming (A1-A6)
- B. Description: the trigger surface (B1-B8)
- C. Structure and progressive disclosure (C1-C6)
- D. Instructions (D1-D8)
- E. Host parity: Claude Code, Codex, Antigravity (E1-E5)
- F. cairn repository rules (F1-F8)

## A. Frontmatter and naming

| ID | Severity | Rule | Check | Source |
|---|---|---|---|---|
| A1 | block | Frontmatter opens and closes with `---` and parses as YAML: a plain-scalar `description` holds no `: ` and no ` #` (YAML reads the rest as a mapping or a comment) | `read_head` in `tests/test_skill_routing.py` returns without error; grep the line for `: ` after the key and for ` #` | agentskills-spec.md "Frontmatter"; claude-code-skills.md "Troubleshooting triggers" |
| A2 | block | `name` is 1-64 characters of `a-z0-9-`, no leading, trailing or double hyphen, equals the folder name | read the line; compare with the folder | agentskills-spec.md "Frontmatter" |
| A3 | fix | `name` is specific, not `helper`, `utils`, `tools`; it follows the collection's pattern | compare with sibling skills | anthropic-best-practices.md "Naming" |
| A4 | block | `description` is 1-1,024 characters with no `<` or `>` | count characters | agentskills-spec.md "Frontmatter"; claude-code-skills.md "Command names" |
| A5 | fix | `allowed-tools` lists only what the steps run, in the repo's form (`Bash(cairn recall gaps:*)`), and grants no broad `Bash` unless the skill is a general CLI driver | compare the list with every command in the body | claude-code-skills.md "allowed-tools grants, it does not restrict" |
| A6 | fix | A side-effectful or onboarding skill is explicit-only in both hosts: `disable-model-invocation: true` and `policy.allow_implicit_invocation: false` | read both files | claude-code-skills.md "Frontmatter that matters here"; codex-skills.md "agents/openai.yaml" |

## B. Description: the trigger surface

| ID | Severity | Rule | Check | Source |
|---|---|---|---|---|
| B1 | block | Says what the skill does and when to use it | read it | anthropic-best-practices.md "Descriptions"; agentskills-spec.md "Frontmatter" |
| B2 | fix | Third person ("Exports ...", not "I can" or "You can") | read it | anthropic-best-practices.md "Descriptions"; antigravity-skills.md "Best practices" |
| B3 | fix | The key use case and trigger words come first, because Codex shortens descriptions and Claude Code truncates at 1,536 characters | read the first sentence alone | codex-skills.md "Listing budget"; claude-code-skills.md "Listing budget and truncation" |
| B4 | fix | Quoted trigger phrases paraphrase how a user speaks ("record that decision"), not CLI verbs ("decide") | read each quote aloud as a user request | anthropic-skill-creator.md "Description: the trigger surface" |
| B5 | block | Every quoted phrase after `Use when the user says` has a fixture row that reaches it alone; overlaps are declared | `uv run python -m unittest tests.test_skill_routing` | cairn: `tests/test_skill_routing.py`; `CONTRIBUTING.md` "Changing a skill description" |
| B6 | fix | No phrase so generic that it fires on ordinary work ("what does this error mean", "forget that") | imagine three unrelated requests that contain the phrase | anthropic-skill-creator.md "Trigger evaluation" (near misses) |
| B7 | note | A skill that should stay quiet for nearby requests says so ("Do not activate for ordinary web searches") | read it | claude-code-skills.md "Troubleshooting triggers" |
| B8 | fix | No `when_to_use`: Codex and Antigravity do not read it | grep | codex-skills.md "Fields Codex reads" |

## C. Structure and progressive disclosure

| ID | Severity | Rule | Check | Source |
|---|---|---|---|---|
| C1 | block | `SKILL.md` body under 500 lines | `awk` line count after the frontmatter | anthropic-best-practices.md "Progressive disclosure"; agentskills-spec.md "Progressive disclosure" |
| C2 | fix | Detail that one task in five needs lives in a linked file, not the body | list body sections by how often a request needs them | anthropic-skills-overview.md "Three loading levels" |
| C3 | fix | Linked files are one level deep from `SKILL.md` | follow each link | anthropic-best-practices.md "Progressive disclosure"; agentskills-spec.md "File references" |
| C4 | fix | A linked file over 100 lines starts with a contents list | `wc -l`; read the top | anthropic-best-practices.md "Progressive disclosure" |
| C5 | fix | The most important instructions sit near the top of `SKILL.md` | read the first 40 lines | claude-code-skills.md "Size and lifecycle" |
| C6 | note | File names say what they hold; paths use forward slashes | read the tree | anthropic-best-practices.md "Progressive disclosure" |

## D. Instructions

| ID | Severity | Rule | Check | Source |
|---|---|---|---|---|
| D1 | fix | Steps are numbered and imperative, one bounded action each | read the workflow | anthropic-skill-creator.md "Writing style"; anthropic-best-practices.md "Workflows and feedback loops" |
| D2 | block | Each command step gives the exact command and says what to read from its output (key, exit code, line) | read each fenced command and the line after it | anthropic-best-practices.md "Degrees of freedom" |
| D3 | fix | A fragile or ordered operation uses low freedom: the exact command, no improvisation | find destructive and multi-step operations | anthropic-best-practices.md "Degrees of freedom" |
| D4 | fix | A quality-critical step has a validate-fix-repeat loop; a destructive step has a dry run or a confirmation first | find writes, deletes and spends | anthropic-best-practices.md "Workflows and feedback loops" |
| D5 | fix | One term per concept across the skill and its files | list the nouns for each concept | anthropic-best-practices.md "Content rules" |
| D6 | fix | No explanation of what the model already knows; no time-sensitive text outside an old-patterns section | read each paragraph for its token cost | anthropic-best-practices.md "Concise is key", "Content rules" |
| D7 | note | Reasons beat capitalised MUST and NEVER | grep for capitals | anthropic-skill-creator.md "Writing style" |
| D8 | fix | External binaries are probed, never assumed; the skill says what to do when one is missing | grep for `command -v` or a fallback line | anthropic-best-practices.md "Content rules" |

## E. Host parity: Claude Code, Codex, Antigravity

| ID | Severity | Rule | Check | Source |
|---|---|---|---|---|
| E1 | fix | The body does not depend on Claude-only syntax (`$ARGUMENTS`, `CLAUDE_SKILL_DIR`, a `!` command fence, `when_to_use`) | grep | codex-skills.md "Fields Codex reads"; cairn: `tests/test_plugin_manifest.py` |
| E2 | fix | A reference to another skill names it by skill name ("the recall skill") or gives both spellings (`/cairn:recall`, `$recall`) | grep for `/cairn:` and `/[a-z]` | codex-skills.md "Invocation"; claude-code-skills.md "Command names" |
| E3 | fix | A rule that depends on `allowed-tools` also holds under Codex's sandbox, where the field is not read | read each permission-sensitive step | codex-skills.md "Fields Codex reads" |
| E4 | fix | Host-specific steps (long processes, hook trust, session ids) point to `references/harnesses/<host>.md` instead of inlining one host's recipe | read the steps | cairn: `references/harnesses/contract.md` |
| E5 | note | A repo-local skill sits where each host finds it (`.agents/skills/` for Codex and Antigravity, a `.claude/skills/` symlink for Claude Code) | `ls -l` | codex-skills.md "Discovery"; antigravity-skills.md "Discovery"; claude-code-skills.md "Where skills load" |

## F. cairn repository rules

| ID | Severity | Rule | Check | Source |
|---|---|---|---|---|
| F1 | block | Every `cairn ...` example parses against the real CLI | `uv run python -m unittest tests.test_docs_cli` | cairn: `tests/test_docs_cli.py` |
| F2 | block | No home path, private name, personal email or cloud project id | `uv run python -m unittest tests.test_public_hygiene` | cairn: `CLAUDE.md`, `tests/test_public_hygiene.py` |
| F3 | block | The skill is thin: logic lives in the CLI; the skill drives commands and reads their output | read for logic the CLI should own | cairn: `skills/recall/SKILL.md` ("this skill just drives them") |
| F4 | block | `skills/search/SKILL.md` keeps one `rg -l ` line and one `rg -n ` line; `skills/recall/SKILL.md` keeps one `rg -l ` line | `uv run python -m unittest tests.test_recall_qrels` | cairn: `tests/test_recall_qrels.py` |
| F5 | fix | A failure step reads `code` and runs `cairn errors CODE` | grep | cairn: `docs/error-codes.md` |
| F6 | block | Each shipped skill links `../../references/harnesses/contract.md` | `uv run python -m unittest tests.test_plugin_manifest` | cairn: `tests/test_plugin_manifest.py` |
| F7 | fix | A change under `skills/**` or `references/**` comes with a version bump (the orchestrator or maintainer does it) | `git diff --name-only origin/main...HEAD` | cairn: `.github/workflows/ci.yml` |
| F8 | fix | Prose is plain and factual, British spelling, no marketing adjectives | read it | cairn: `CONTRIBUTING.md` "Changing a skill description" (the style line this layer adds) |
