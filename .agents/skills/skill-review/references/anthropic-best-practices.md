# Anthropic: skill authoring best practices

Source: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
Fetched: 2026-10-30

A condensed reading of the page, in our own words. Quotes are short and marked.

## Contents

- Concise is key
- Degrees of freedom
- Frontmatter limits
- Naming
- Descriptions
- Progressive disclosure
- Workflows and feedback loops
- Content rules
- Evaluation and iteration
- Anti-patterns
- Checklist

## Concise is key

- The context window is shared. At startup only `name` and `description` of every
  skill load; the body loads when the skill triggers, and other files load when read.
- Assume the model already knows general facts. Ask of each paragraph: "Does this
  paragraph justify its token cost?"
- The page contrasts a 50-token instruction with a 150-token one that explains what a
  PDF is. Cut the explanation.

## Degrees of freedom

- High freedom (prose heuristics) when many approaches are valid.
- Medium freedom (a template or pseudocode with parameters) when a preferred pattern
  exists.
- Low freedom (one exact command, "Do not modify the command") when the operation is
  fragile or the order matters. The page's image: a narrow bridge needs guardrails, an
  open field needs only a direction.

## Frontmatter limits

- `name`: at most 64 characters, lowercase letters, digits and hyphens, no XML tags,
  must not contain the reserved words `anthropic` or `claude`.
- `description`: non-empty, at most 1,024 characters, no XML tags, says what the skill
  does and when to use it.

## Naming

- Prefer the gerund form (`processing-pdfs`); noun phrases (`pdf-processing`) and
  action forms (`process-pdfs`) are acceptable.
- Avoid vague (`helper`, `utils`), generic (`documents`) and reserved names, and
  inconsistent patterns across one collection.

## Descriptions

- The description is how the model picks one skill out of "potentially 100+".
- "Always write in third person": "Processes Excel files", not "I can help you" or
  "You can use this".
- Name the task and the triggers: key terms, file types, contexts the user mentions.
- Good: "Extract text and tables from PDF files, fill forms, merge documents. Use when
  working with PDF files or when the user mentions PDFs, forms, or document extraction."
- Bad: "Helps with documents", "Processes data", "Does stuff with files".

## Progressive disclosure

- Keep the `SKILL.md` body under 500 lines; split before the limit.
- `SKILL.md` is a table of contents that points at files the model reads when needed.
  Organise extra files by domain (`reference/finance.md`, `reference/sales.md`) so one
  task loads one file.
- Keep references one level deep from `SKILL.md`. A file reached through another file
  can be read only partly (for example with `head -100`).
- A reference file over 100 lines starts with a table of contents.
- Name files for their content (`form_validation_rules.md`, not `doc2.md`). Use forward
  slashes.

## Workflows and feedback loops

- Break a complex task into numbered steps. For a long one, give a checklist the model
  copies and ticks.
- Add a validate-fix-repeat loop for quality-critical work: run the validator, fix,
  run it again, continue only when it passes.
- For a batch or destructive change, use plan-validate-execute: write a plan file,
  check it with a script, then apply it.
- Say whether a script is to be run ("Run `x.py`") or read ("See `x.py` for ...").

## Content rules

- No time-sensitive text ("before August 2025 use ..."). Put old behaviour in a marked
  "old patterns" section.
- One term per concept throughout a skill ("Always 'field'", never a mix of field, box,
  element).
- Give templates for strict output formats, and input/output examples where the style
  matters.
- Offer a default with one escape hatch, not a list of five equal options.
- Use fully qualified MCP tool names (`Server:tool`).
- Do not assume a package or binary is installed; say what to install or how to probe.

## Evaluation and iteration

- Write evaluations before long documentation: find the failures without the skill,
  write three scenarios, measure a baseline, then write the minimum text that passes.
- Iterate with two instances: one helps write the skill, a fresh one uses it on real
  tasks. Watch where it reads, what it misses and what it never opens.
- Test with every model the skill will run on; a smaller model may need more detail.

## Anti-patterns

- Windows-style paths.
- Too many options without a default.
- Deeply nested references.
- Scripts that fail and leave the model to guess ("Solve, don't defer"), and unexplained
  constants.

## Checklist (from the page)

- Description is specific, has key terms, says what and when.
- Body under 500 lines; detail in separate files; references one level deep.
- No time-sensitive text; consistent terms; concrete examples; clear workflow steps.
- Scripts handle errors; no magic numbers; dependencies listed; validation steps for
  critical operations.
- At least three evaluations; tested on each target model and on real usage.
