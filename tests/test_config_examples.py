"""The two ``config.example.json`` copies must stay byte-identical.

``src/cairn/config.example.json`` is the canonical, packaged template (shipped as
package data and read by ``config init``); ``cairn.config.example.json`` at the repo
root is a dev-checkout fallback. Nothing keeps them in sync automatically, so this
test fails loudly the moment one drifts from the other.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cairn import config

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestConfigExamplesInSync(unittest.TestCase):
    def test_repo_copy_matches_packaged(self):
        packaged = config.example_template_text()
        repo_copy = (_REPO_ROOT / "cairn.config.example.json").read_text(encoding="utf-8")
        self.assertEqual(
            repo_copy,
            packaged,
            "cairn.config.example.json has drifted from the packaged "
            "src/cairn/config.example.json - keep the two byte-identical.",
        )

    def test_packaged_example_is_valid_json_with_required_keys(self):
        data = json.loads(config.example_template_text())
        for key in ("data_root", "collections"):
            self.assertIn(key, data)

    def test_packaged_example_loads(self):
        """The example routine and profile pass load-time validation."""
        with TemporaryDirectory() as tmp:
            copy = Path(tmp) / "config.json"
            copy.write_text(config.example_template_text(), encoding="utf-8")
            loaded = config.load_config_from(copy)
        routine = loaded.routines["example-daily"]
        self.assertEqual((routine.peer, routine.profile), ("claude", "scheduled"))
        self.assertEqual(routine.outputs["journal_dir"], loaded.data_root / "journal")
        self.assertIn("scheduled", loaded.invoke.claude.profiles)
        self.assertIn("journal", loaded.collections)
        self.assertIsNone(loaded.collections["journal"].store)


if __name__ == "__main__":
    unittest.main()
