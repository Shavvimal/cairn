"""Config resolution precedence + the shared --since parser.

These cover the decoupling work: $CAIRN_CONFIG > XDG > repo fallback, and that
find_repo_root degrades to None (rather than raising) when no config is above the
start path - the property that lets a global install fall through to XDG.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cairn import config
from cairn.timeutil import parse_since


class TestConfigPrecedence(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CAIRN_CONFIG", "XDG_CONFIG_HOME")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "my.json"
            p.write_text("{}", encoding="utf-8")
            os.environ["CAIRN_CONFIG"] = str(p)
            self.assertEqual(config._config_path(), p)

    def test_env_override_missing_raises(self):
        os.environ["CAIRN_CONFIG"] = "/nonexistent/cairn-xyz.json"
        with self.assertRaises(config.ConfigError):
            config._config_path()

    def test_xdg_preferred_over_repo(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ.pop("CAIRN_CONFIG", None)
            os.environ["XDG_CONFIG_HOME"] = d
            target = Path(d) / "cairn" / "config.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}", encoding="utf-8")
            self.assertEqual(config.user_config_path(), target)
            self.assertEqual(config._config_path(), target)


class TestFindRepoRoot(unittest.TestCase):
    def test_returns_none_when_no_config_above(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(config.find_repo_root(Path(d)))


class TestParseSince(unittest.TestCase):
    def test_accepts_keywords_durations_and_iso(self):
        for token in ("today", "yesterday", "7d", "2w", "2026-01-01"):
            self.assertIsInstance(parse_since(token), float)

    def test_today_is_after_yesterday(self):
        self.assertGreater(parse_since("today"), parse_since("yesterday"))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_since("whenever")


if __name__ == "__main__":
    unittest.main()
