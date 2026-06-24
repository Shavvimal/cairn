# Service Docs

Mirror markdown documentation from configured service repos into one searchable
`service-docs/` collection, alongside sessions and notes. **This source is a verbatim
mirror, not a transformer** - it differs from the windowed session sources.

## Commands

| Command                             | Does                                                               |
| ----------------------------------- | ------------------------------------------------------------------ |
| `cairn service-docs export`         | Incremental mirror (content-hash based; only changed files copied) |
| `cairn service-docs export --clean` | Full re-export - removes all mirrored files first                  |
| `cairn service-docs list`           | List synced docs by service                                        |
| `cairn service-docs context`        | Register QMD context descriptions per service                      |

**Only `export` / `list` / `context`.** Unlike the conversation sources, service-docs is a
file mirror, so it has **no annotation verbs** (`note` / `close` / `log`) and **no
`--since` / `--all`** time window - the window is the source tree itself. `export` takes
only `--clean` and `-q`; `list` / `context` take no flags.

## How it works

No transformation - copies markdown files verbatim, preserving directory structure.
Sync is **incremental**: it compares content hashes and copies only changed files.
Deleted source files are cleaned up from the mirror. `--clean` forces a full re-mirror.

Indexing happens automatically via `cairn sync --cron` (hourly). Manual reindex:
`qmd update && qmd embed`.

## Source repos (the source-repos concept)

`export` mirrors every directory in the config's `service_sources` into the single
`service-docs` collection. Each configured source maps a service name to a docs path:

| Service     | Source Path                          | Description                                         |
| ----------- | ------------------------------------ | --------------------------------------------------- |
| `api`       | `~/Code/your-product/api/docs`       | Backend API - describe this service for QMD context |
| `dashboard` | `~/Code/your-product/dashboard/docs` | Web dashboard - describe this service               |
| `daemons`   | `~/Code/your-product/daemons/docs`   | Async workers - describe this service               |

The set of services is configurable (`service_sources` in the config); the table above is
an example layout.
