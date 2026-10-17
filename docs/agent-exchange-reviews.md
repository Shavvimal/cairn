# Adversarial review log for the peer-agent and subagent visibility PR

Each phase of this PR was reviewed by Codex (`gpt-6-astra`, effort high, read-only) through
the standalone `invoke-codex` wrapper, with `docs/agent-exchange-plan.md` and
`docs/agent-exchange-research.md` as the brief. This file records every finding and what
was done about it, so a later reviewer can see the trail instead of re-deriving it.

## Phase 1: shared plumbing, subagent exports, invoke foundations

Reviewed range: `111881e..c5d974c`. Duration 403 s. Verdict: not merge-ready.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | blocker | `claude -p` argv ended with `--disallowedTools Edit,Write,NotebookEdit "<prompt>"`; the flag is variadic and swallowed the prompt. Reproduced live: "Input must be provided either through stdin or as a prompt argument". | Fixed. `Claude.exec_argv` emits `--` before the positional prompt (verified live with haiku; both `--` and prompt-after-`-p` work, `--` also guards variadic `--extra` flags). Regression test added. |
| 2 | blocker | A multiline Claude subagent `description` became a multiline `title:` and the exported file failed `parse_frontmatter`; `export_one` wrote before validating. | Fixed in two places. `frontmatter.title_line` collapses any whitespace run or line break to one space. `ConversationSource.export_one` parses the rendered frontmatter and checks the id before `atomic_write`; a document the catalog could not read is never written (error on stderr, `None` returned). Tests added. |
| 3 | should-fix | Targeted Codex export of a child or parent rewrote nested documents with incomplete `## Subagents` indexes (P → {A, B}, B → G: targeting A rewrote B without G). | Fixed by the Codex stream: candidate discovery includes ancestors and the full transitive descendants of every document that will be rewritten. Regression test for nested targeted exports. |
| 4 | should-fix | Two Codex children with colliding short ids got separate files through the collision suffix, but the parent index linked both to the planned path, and the revision excluded link paths so later exports could not repair it. | Fixed by the Codex stream: collision-safe destinations are resolved before the index is rendered and the destinations are part of the parent revision. Regression test with two colliding ids. |
| 5 | should-fix | One malformed Claude child `meta.json` blocked the parent and every healthy sibling because `subagent_index` read all metas before the per-child handling. | Fixed by the Claude stream: index discovery errors are collected per child, healthy documents export, and the aggregate failure is reported afterwards. Regression test. |
| 6 | should-fix | Codex `export_target` discarded the per-file failure flag: an unreadable child vanished from the parent index while the call succeeded, and the hook worker never saw it. | Fixed by the Codex stream: readable documents export, then an aggregate `ValueError` names the failed family members so the durable hook job retries. |
| 7 | should-fix | The replay cut locked into UUIDv7 mode after seeing an ancestor's turn id and ignored an exact `thread_settings_applied(thread_id=child)` marker followed by a legacy-format child turn. | Fixed by the Codex stream: an exact child marker is always authoritative; the first signal that resolves wins. Regression test with mixed ids. |
| 8 | should-fix | The Claude parent revision hashed only some index fields; changing `requestShape` or `workflow_run` changed the rendered row without re-rendering the parent. | Fixed by the Claude stream: the revision hashes one normalised representation of every rendered index row, including the path. Regression test. |
| 9 | should-fix | A hand-quoted `links:` entry (`- "peer claude:x"`) parsed with its quotes, so `add_link` appended a second, bare copy of the same relationship. | Fixed. `add_link` normalises existing entries to the bare form before comparing and re-emits them bare. Regression test. |
| 10 | should-fix | `_parse_invoke(data.get("invoke") or {})` let `false`, `[]`, `""` and `0` silently become defaults. | Fixed. `None` and absent mean defaults; any other non-object raises `ConfigError`. Regression test. |
| 11 | nit | `ruff format --check` failed on `registry.py` (the Makefile's `check` target runs lint, not format). | Fixed. Tree formatted; `ruff format --check` is part of the acceptance run. |

Found sound by the reviewer: first-meta identity keeps forked children out of their parent;
registry, store and adapter id filters include children while plain listings and the
active-session fallback keep the exact parent type; both resume entry points refuse children;
export, document and index lock ordering; Codex resume flags against the installed help;
item-level Codex errors as notices; lineage cache invalidation; custom tool output; unquoted
link round trips; schema 1 records go stale under schema 2.

Coverage the reviewer asked for and this PR adds: nested targeted exports, short-id
collisions, multiline descriptions, partial family failures, mixed replay ids, quoted links,
and the pre-write frontmatter validation.
