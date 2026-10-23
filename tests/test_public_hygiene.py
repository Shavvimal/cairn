"""This repository is public: nothing in it may name a private project, a person's
machine or an account. The patterns below are generic (a home directory path, a
personal email, a Google Cloud project id); the names that must never appear are not
listed here, because listing them would publish them. They live in an untracked file,
``$XDG_CONFIG_HOME/cairn/private-terms`` (one term per line, matched
case-insensitively), which the test reads when present, so the local ``make check``
catches a private name before a push and CI catches the generic shapes.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIPPED_SUFFIXES = {".lock", ".png", ".jpg", ".gif", ".ico", ".woff", ".woff2", ".db", ".sqlite"}
# A placeholder user (``/Users/x/Code``, ``/home/alice/``) is documentation, not a person.
PLACEHOLDER_USERS = ("x", "alice", "bob", "me", "you", "user", "name", "example", "<name>")
GENERIC_PATTERNS = {
    "a home directory path": re.compile(
        r"(?<![\w/])/(?:Users|home)/(?!(?:"
        + "|".join(re.escape(u) for u in PLACEHOLDER_USERS)
        + r")/)[A-Za-z0-9_.<>-]+/"
    ),
    "a personal email": re.compile(
        r"[\w.+-]+@(?:gmail|googlemail|outlook|hotmail|icloud|proton|protonmail|yahoo)\.",
        re.IGNORECASE,
    ),
    "a Google Cloud project id": re.compile(r"gen-lang-client-\d+"),
}


def private_terms_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cairn" / "private-terms"


def tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    files = []
    for name in listed.split("\0"):
        if not name:
            continue
        path = ROOT / name
        if path.suffix in SKIPPED_SUFFIXES or not path.is_file():
            continue
        files.append(path)
    return files


def scan(patterns: dict[str, re.Pattern[str]]) -> list[str]:
    hits = []
    for path in tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in patterns.items():
                if pattern.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {label}: {line.strip()[:120]}")
    return hits


class TestPublicHygiene(unittest.TestCase):
    def test_no_generic_private_shapes(self):
        self.assertEqual(scan(GENERIC_PATTERNS), [])

    def test_no_local_private_terms(self):
        path = private_terms_path()
        if not path.is_file():
            self.skipTest(f"no {path}; add one term per line to check private names locally")
        terms = [line.strip() for line in path.read_text().splitlines()]
        terms = [t for t in terms if t and not t.startswith("#")]
        patterns = {f"private term {t!r}": re.compile(re.escape(t), re.IGNORECASE) for t in terms}
        self.assertEqual(scan(patterns), [])


if __name__ == "__main__":
    unittest.main()
