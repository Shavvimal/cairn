# Claude Code: skills

Source: https://code.claude.com/docs/en/skills (redirect from https://docs.claude.com/en/docs/claude-code/skills)
Fetched: 2026-10-30

## Contents

- Where skills load
- Command names
- Frontmatter that matters here
- allowed-tools grants, it does not restrict
- Listing budget and truncation
- Size and lifecycle
- Troubleshooting triggers
- Measuring triggers

## Where skills load

| Location | Path | Loads in |
|---|---|---|
| Personal | `~/.claude/skills/<name>/SKILL.md` | every project on the machine |
| Project | `.claude/skills/<name>/SKILL.md` | sessions in that repository |
| Nested | `<subdir>/.claude/skills/<name>/SKILL.md` | sessions that work under `<subdir>` |
| Plugin | `<plugin>/skills/<name>/SKILL.md` | wherever the plugin is enabled, as `/plugin-name:skill-name` |

A project `<name>` entry "can be a symlink to a directory elsewhere on disk"; Claude Code
reads the target and loads it once. The page does not list `.agents/skills`.

## Command names

- A plugin skill is `/<plugin>:<skill>`; the frontmatter `name` replaces the folder name
  in the last segment.
- A name at the start of a message runs the skill. After other text it only gives
  permission.
- `<` and `>` in a description display as entities: write placeholders as `[branch]`.

## Frontmatter that matters here

| Field | Meaning (paraphrased) |
|---|---|
| `name` | the `/` command; defaults to the folder name |
| `description` | what it does and when to use it; the model matches requests against it |
| `when_to_use` | extra trigger text appended to the description. Claude Code only: other hosts ignore it |
| `argument-hint` | autocomplete hint |
| `disable-model-invocation: true` | only the user can start it; its description leaves the model's context |
| `user-invocable: false` | only the model can start it |
| `allowed-tools` | tools usable without a prompt during the invoking turn |
| `disallowed-tools` | tools removed while the skill is active |

Claude Code ignores an unknown field without an error.

## allowed-tools grants, it does not restrict

"It does not restrict which tools are available: every tool remains callable, and your
permission settings still govern tools that are not listed." The grant ends with the
user's next message. Syntax: `Bash(git add *)` (space or comma separated, or a YAML
list). A project skill's `allowed-tools` applies even in an untrusted `-p` run, so review
grants in repository skills.

## Listing budget and truncation

- The combined `description` and `when_to_use` is cut at 1,536 characters in the listing.
- The whole listing has a budget of about 1% of the context window. When it overflows,
  descriptions of the least used skills are dropped first.
- Advice: "Put the key use case first".

## Size and lifecycle

- "Keep `SKILL.md` under 500 lines. Move detailed reference material to separate files."
- Once invoked, the body stays in context and is not re-read. After compaction only the
  start of a skill may survive, so put the most important instructions near the top.
- A rule that must hold every time belongs in a hook, not in skill prose.

## Troubleshooting triggers

Not triggering:

1. Check the description holds the words users say.
2. Check the skill is listed (`What skills are available?`).
3. Rephrase the request; invoke it by name.
4. Malformed YAML loads the body with empty metadata: the name works, matching does not.
   `claude plugin validate <dir>` finds frontmatter that does not parse.

Triggering too often: make the description more specific, or set
`disable-model-invocation: true`.

## Measuring triggers

For a plugin skill, write eval cases with a `tool_used: Skill` grader and run
`claude plugin eval` after each description change. The page also points to a
skill-creator workflow that generates should-trigger and should-not-trigger prompts and
measures the hit rate.
