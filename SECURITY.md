# Security Policy

## Supported versions

cairn is developed on a rolling basis. Security fixes are applied to the latest
`main` and released from there; please ensure you are running the most recent
version before reporting an issue.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub's built-in vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** to open a private advisory.

This keeps the details confidential until a fix is available. We aim to
acknowledge reports within a few days and will coordinate a fix and disclosure
timeline with you.

## Sensitive surface

cairn runs entirely on your machine and is designed so that nothing leaves it.
A few areas handle sensitive local data and warrant extra care when reporting or
testing:

- **Granola decryption** — the Granola source reads the local application store
  and decrypts it (optional `cryptography` extra). Avoid pasting decrypted
  meeting content or keys into reports.
- **Local session data** — exported markdown under `data_root/` (and the QMD
  index) contains the full text of your AI coding sessions and meetings. Scrub
  any real session content from reproduction steps.
- **Config** — `config.json` describes your machine's layout and paths. Redact
  it before sharing.

When in doubt, describe the issue abstractly in the private advisory and we will
follow up for the minimum detail needed to reproduce. Do not include working
exploit payloads in public channels.
