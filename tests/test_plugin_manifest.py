"""The plugin manifest version is locked to the Python package version.

cairn ships two artifacts from one repo - the Claude Code plugin (skills + hook) and
the `cairn` CLI engine - that update on different clocks. Pinning `plugin.json` to
`cairn.__version__` keeps them from drifting in the repo; this test fails the build the
moment they diverge, so `make check`/CI catch it before release.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import cairn

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_JSON = _REPO_ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON = _REPO_ROOT / ".claude-plugin" / "marketplace.json"


@unittest.skipUnless(_PLUGIN_JSON.is_file(), "plugin manifest not present (sdist-only run)")
class TestPluginManifestVersion(unittest.TestCase):
    def test_plugin_version_matches_package(self):
        manifest = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest.get("version"),
            cairn.__version__,
            "plugin.json version must equal cairn.__version__ - bump both together",
        )

    def test_marketplace_entries_have_no_version(self):
        # Version resolution is plugin.json -> marketplace entry -> git SHA, and the docs
        # warn that plugin.json wins silently if set in both. Keep the single source of
        # truth in plugin.json by leaving marketplace entries version-less.
        if not _MARKETPLACE_JSON.is_file():
            self.skipTest("no marketplace.json")
        marketplace = json.loads(_MARKETPLACE_JSON.read_text(encoding="utf-8"))
        for entry in marketplace.get("plugins", []):
            self.assertNotIn(
                "version",
                entry,
                f"marketplace entry {entry.get('name')!r} must not pin a version "
                "(set it only in plugin.json)",
            )


if __name__ == "__main__":
    unittest.main()
