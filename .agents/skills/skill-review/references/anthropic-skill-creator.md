# Anthropic skills repository: README and the skill-creator skill

Source: https://github.com/anthropics/skills (README) and https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md
Fetched: 2026-10-30

## Contents

- Template
- Description: the trigger surface
- Anatomy and size
- Writing style
- Test prompts
- Trigger evaluation
- Iteration

## Template

The README's minimal skill: frontmatter with `name` and `description`, then
instructions, examples and guidelines. "Always test skills thoroughly in your own
environment before relying on them for critical tasks."

## Description: the trigger surface

- The description is "the primary triggering mechanism". All "when to use" text goes
  there, "not in the body".
- Models tend to "undertrigger", so the description can be "a little bit 'pushy'":
  name the contexts where the skill applies even when the user does not name the skill.

## Anatomy and size

- `SKILL.md` plus optional `scripts/` (deterministic or repeated work), `references/`
  (read when needed) and `assets/` (files used in the output).
- Metadata about 100 words; body "<500 lines ideal"; resources unlimited.
- Near 500 lines, add a level of hierarchy with clear pointers. A reference file over
  300 lines gets a table of contents. Split variants into one file each.

## Writing style

- "Prefer using the imperative form in instructions."
- Explain why a rule matters instead of stacking capitalised MUSTs: "If you find
  yourself writing ALWAYS or NEVER in all caps ... that's a yellow flag."
- Give output templates and input/output examples where they help.
- A skill "should not surprise the user in their intent if described".

## Test prompts

- Write two or three realistic prompts first, run them with and without the skill (or
  with the old version), then add objectively checkable assertions with descriptive
  names. Leave subjective qualities to human review.
- If every test run writes the same helper code, bundle it in `scripts/`.

## Trigger evaluation

- About 20 queries: 8-10 that should trigger and 8-10 that should not.
- Should-trigger rows are different phrasings of one intent, some without the skill's
  name. Should-not rows are near misses, not unrelated requests.
- Queries are concrete and detailed, like real user messages. Trivial tasks make poor
  tests because the model does not consult a skill for them.
- Tune the description against a train split and pick the best by a held-out split, to
  avoid overfitting.

## Iteration

Generalise from feedback instead of adding narrow rules, keep the prompt lean, and stop
when the feedback is empty or no change helps.
