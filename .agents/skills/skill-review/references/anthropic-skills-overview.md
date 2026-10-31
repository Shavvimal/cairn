# Anthropic: Agent Skills overview and SKILL.md structure

Source: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
Fetched: 2026-10-30

## Contents

- What a skill is
- Three loading levels
- Required structure
- Security
- Surfaces

## What a skill is

A skill is a directory with a `SKILL.md` file (YAML frontmatter plus markdown) and
optional files: more instructions, scripts, and resources such as schemas or templates.
The model reads the files with ordinary file tools when it needs them.

## Three loading levels

| Level | When it loads | Cost the page gives | Content |
|---|---|---|---|
| 1 Metadata | always, at startup | "~100 tokens per Skill" | `name` and `description` |
| 2 Instructions | when the skill triggers | "Under 5k tokens" | the `SKILL.md` body |
| 3 Resources | when read or run | none until accessed | extra files; a script's code never enters context, only its output |

Consequences for an author:

- The description is the only text that decides whether the skill loads: "it must say
  both what the Skill does and when to use it".
- Bundled files cost nothing until read, so detailed material belongs in them, not in
  the body.
- A script is cheaper and more reliable than instructions that make the model write the
  same code each time.

## Required structure

```markdown
---
name: your-skill-name
description: Brief description of what this Skill does and when to use it
---
```

- `name`: at most 64 characters, lowercase letters, digits and hyphens, no XML tags, not
  `anthropic` or `claude`.
- `description`: non-empty, at most 1,024 characters, no XML tags.

## Security

The page treats a skill like installed software: audit every bundled file, be wary of a
skill that fetches external URLs, and expect that a malicious skill can misuse tools.
For a repository's own skills this means: no network fetch in a script without a reason,
and no instruction that surprises the user.

## Surfaces

Skills do not sync between the API, claude.ai and Claude Code. Claude Code reads them
from the filesystem (personal, project, or a plugin) with full network access, so a
skill should not install packages globally.
