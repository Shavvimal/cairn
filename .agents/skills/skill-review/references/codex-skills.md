# OpenAI Codex: skills

Source: https://learn.chatgpt.com/docs/build-skills (redirect from https://developers.openai.com/codex/skills)
Fetched: 2026-10-30

## Contents

- Discovery
- Listing budget
- Invocation
- agents/openai.yaml
- Fields Codex reads
- Best practices

## Discovery

- Codex reads skills from repository, user, admin and system locations. In a repository
  it scans `.agents/skills` "in every directory from your current working directory up
  to the repository root".
- Plugins can ship one or more skills (cairn's `.codex-plugin/plugin.json` points
  `skills` at `./skills/`).
- Two skills with the same `name` are not merged; both can appear.

## Listing budget

- Codex starts with each skill's name, description and file path, and loads the body
  later.
- The list uses at most 2% of the context window, or 8,000 characters when the window
  is unknown (`skills.max_context_tokens`, up to 10,000 tokens, changes it).
- Over budget, "Codex shortens skill descriptions first", then may omit skills with a
  warning. So: "Front-load the key use case and trigger words".

## Invocation

- Explicit: `/skills`, or type `$` and pick the skill (`$skill-name`).
- Implicit: Codex picks a skill "when your task matches the skill `description`".

## agents/openai.yaml

| Key | Meaning |
|---|---|
| `interface.display_name`, `short_description`, icons, `brand_color`, `default_prompt` | app UI metadata |
| `policy.allow_implicit_invocation` | default `true`; `false` keeps the skill explicit-only (`$name` still works) |
| `dependencies.tools` | declared tool dependencies, such as an MCP server |

## Fields Codex reads

The page documents only `name` and `description` in `SKILL.md`. It does not mention
`allowed-tools`, `argument-hint`, `disable-model-invocation` or `when_to_use`. Treat
those as Claude Code only: Codex permissions come from its sandbox and approval policy,
and explicit-only behaviour comes from `agents/openai.yaml`.

## Best practices

- "concise descriptions with clear scope and boundaries".
- Front-load the trigger words so a shortened description still matches.
- Test prompts against the description to confirm it triggers.
