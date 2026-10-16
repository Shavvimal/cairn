"""Host-specific discovery and truthful version diagnostics."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cairn import admin
from cairn.harnesses import detect_harness


class TestHarnessDiagnostics(unittest.TestCase):
    def test_both_host_version_directions_and_compatibility_environment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".claude-plugin").mkdir()
            for harness, path in (
                ("claude", root / ".claude-plugin/plugin.json"),
                ("codex", root / "plugin.json"),
            ):
                path.write_text(json.dumps({"version": "1.0.0"}))
                with patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(root)}, clear=True):
                    self.assertIsNone(detect_harness())
                    self.assertEqual(admin._plugin_manifest_path(None, harness), path)
                for version, expected in (
                    ("1.0.0", "/plugin update" if harness == "claude" else "codex plugin"),
                    ("999.0.0", "uv tool upgrade cairn"),
                ):
                    path.write_text(json.dumps({"version": version}))
                    output = io.StringIO()
                    with redirect_stdout(output):
                        admin._check_plugin_drift(path, harness)
                    self.assertIn(expected, output.getvalue())

    def test_conflicting_identity_is_not_a_detected_host(self):
        with patch.dict(
            os.environ, {"CODEX_THREAD_ID": "one", "CODEX_SESSION_ID": "two"}, clear=True
        ):
            self.assertIsNone(detect_harness())
