# cairn: repository rules

This repository is public. Every file, commit message, PR title and PR body is
published the moment it is pushed, and a force-push does not take it back.

## Public-repo hygiene

- Never write a private project name, a private repository link, a client or employer
  name, a handover document name, a function or module name from a private codebase,
  an absolute home directory path (`/Users/<name>/...`, `/home/<name>/...`), a personal
  email, or a cloud account or project id into a tracked file, a commit message or a PR.
- A post-mortem, plan or review that comes from work on a private repo is anonymised
  before it lands here: say "a private Python/Postgres application repo", "the handover
  document", "a manager module"; keep the harness facts, the timestamps and the log
  shapes, which are the point of the document.
- Fixture values are invented: `example-quota-project`, `x@y.z`, `/tmp/...`, `~/...`.
- `tests/test_public_hygiene.py` scans every tracked file for the generic shapes
  (home paths, personal emails, Google Cloud project ids) in CI, and for the names in
  the untracked file `$XDG_CONFIG_HOME/cairn/private-terms` (one per line) locally. Run
  `make check` before every push; add a new private name to that file, never to a
  tracked one.
- Before `git push`, read the diff once more for these shapes. If one slipped into a
  pushed commit, rewrite the branch history (`git reset --soft`, recommit, force-push
  with lease) and tell the owner that the old objects stay reachable on GitHub by SHA
  until support purges them.

## Gate

`make check` (ruff, ruff format, mypy, unittest) mirrors CI across Python 3.11 to 3.13.
Commits carry no attribution trailer.
