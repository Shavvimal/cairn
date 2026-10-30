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
   `NO_CONTENTS`, `NESTED_LINK`, `CLAUDE_ONLY`, `MISSING_LINK`, `NAME_MISMATCH`).
3. Run the repository's checks once and save the output:
   `uv run python -m unittest tests.test_skill_routing tests.test_docs_cli tests.test_plugin_manifest tests.test_public_hygiene tests.test_recall_qrels tests.test_skill_review > /tmp/skill-review-tests.log 2>&1`.
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
Checks: test_skill_routing OK, test_docs_cli OK, test_plugin_manifest OK, test_public_hygiene OK, test_recall_qrels OK, test_skill_review OK

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
