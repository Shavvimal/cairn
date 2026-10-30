# Agent Skills open specification

Source: https://agentskills.io/specification
Fetched: 2026-10-30

The format that Claude Code, Codex, Antigravity and other agents share. Where a host
adds fields, they are extensions of this base.

## Contents

- Layout
- Frontmatter
- Body
- Progressive disclosure
- File references
- Validation

## Layout

```
skill-name/
├── SKILL.md      required
├── scripts/      optional, executable code
├── references/   optional, documents read on demand
└── assets/       optional, templates and data
```

## Frontmatter

| Field | Required | Rule |
|---|---|---|
| `name` | yes | 1-64 characters, `a-z`, `0-9` and `-`; no leading, trailing or double hyphen; "Must match the parent directory name" |
| `description` | yes | 1-1,024 characters; what the skill does and when to use it; "specific keywords that help agents identify relevant tasks" |
| `license` | no | a licence name or a bundled file |
| `compatibility` | no | at most 500 characters; only for real environment requirements |
| `metadata` | no | string-to-string map; use distinctive keys |
| `allowed-tools` | no | space-separated pre-approved tools; "Experimental. Support for this field may vary" |

Good description: "Extracts text and tables from PDF files, fills PDF forms, and merges
multiple PDFs. Use when working with PDF documents or when the user mentions PDFs,
forms, or document extraction." Poor: "Helps with PDFs."

## Body

No format rules. Recommended: step-by-step instructions, input and output examples,
edge cases. The whole file loads on activation, so split long content out.

## Progressive disclosure

1. Metadata, about 100 tokens, loaded for every skill at startup.
2. Instructions, under 5,000 tokens recommended, loaded on activation.
3. Resources, loaded when needed.

"Keep your main `SKILL.md` under 500 lines."

## File references

Use relative paths from the skill root and keep references "one level deep". Keep each
reference file focused: smaller files cost less context.

## Validation

`skills-ref validate ./my-skill` checks the frontmatter and naming rules.
