# Google Antigravity: agent skills

Source: https://antigravity.google/docs/skills
Fetched: 2026-10-30

## Contents

- Discovery
- Format
- Activation
- Best practices

## Discovery

| Surface | Workspace | Global | Plugin |
|---|---|---|---|
| CLI (`agy`) | `<workspace-root>/.agents/skills/<skill>/` | `~/.gemini/antigravity-cli/skills/<skill>/` | `~/.gemini/antigravity-cli/plugins/<name>/skills/` |
| App | `<workspace-root>/.agents/skills/<skill>/` | `~/.gemini/config/skills/<skill>/` | |
| IDE | `<workspace-root>/.agents/skills/<skill>/` | `~/.gemini/config/skills/<skill>/` (legacy `~/.gemini/antigravity/skills/`) | |

"Antigravity defaults to `.agents/skills`, but still maintains backward compatibility
for `.agent/skills`."

## Format

A folder with a required `SKILL.md` that starts with YAML frontmatter, plus optional
`scripts/`, `examples/` and `resources/`. `name` is optional (defaults to the folder
name); `description` is required and is "what the agent sees when deciding whether to
apply the skill".

## Activation

- At the start of a conversation the agent sees each skill's name and description; it
  reads the full `SKILL.md` when the skill looks relevant.
- The CLI turns every skill into a slash command (`/<skill-name>`); the App takes
  `/<skill-name>` in the prompt panel.

## Best practices

- "Each skill should do one thing well."
- Write the description in the third person with the keywords that signal relevance.
- Treat scripts as black boxes: tell the agent to run `--help` first instead of reading
  the source.
- For a complex skill, add a decision tree that picks the approach from the situation.
- The page gives no size limit; the shared specification's 500-line body limit applies.
