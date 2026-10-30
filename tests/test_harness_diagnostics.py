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
from cairn.harnesses import detect_harness, get_harness
from cairn.invoke import resolve_caller


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

    def test_claude_code_session_variable_identifies_the_host(self):
        # Claude Code 2.1.x exports CLAUDE_CODE_SESSION_ID to every tool process and
        # never CLAUDE_SESSION_ID (that one is only a skill-text template variable).
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc"}, clear=True):
            self.assertEqual(detect_harness(), "claude")
            self.assertEqual(get_harness("claude").current_id(), "abc")
            self.assertEqual(resolve_caller(None), ("claude:abc", []))
        with patch.dict(os.environ, {"CLAUDE_SESSION_ID": "manual"}, clear=True):
            self.assertEqual(get_harness("claude").current_id(), "manual")
        both = {"CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_SESSION_ID": "abc"}
        with patch.dict(os.environ, both, clear=True):
            self.assertEqual(get_harness("claude").current_id(), "abc")

    def test_conflicting_claude_hints_are_not_a_detected_host(self):
        both = {"CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_SESSION_ID": "other"}
        with patch.dict(os.environ, both, clear=True):
            self.assertIsNone(detect_harness())
            with self.assertRaises(ValueError):
                get_harness("claude").current_id()
            caller, warnings = resolve_caller(None)
            self.assertIsNone(caller)
            self.assertIn("no single host session detected", warnings[0])


class TestCodexTrustDoctor(unittest.TestCase):
    def test_doctor_reports_trust_states(self):
        from cairn.harnesses import codex_registration as registration
        from cairn.harnesses import codex_trust as trust

        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "codex-home"
            home.mkdir()
            environment = {
                "HOME": str(root / "home"),
                "CODEX_HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "xdg"),
            }
            hooks_path = home.resolve() / "hooks.json"
            handler = {
                "type": "command",
                "command": "echo cairn",
                "timeout": 3,
                "statusMessage": "Cairn automatic session sync",
            }
            hooks_path.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [handler]}]}}))
            key = f"{hooks_path}:session_end:0:0"
            good = trust.trusted_hash("SessionEnd", handler)
            entry = {
                "kind": "config_toml_table",
                "key": key,
                "trusted_hash": good,
                "sha256": trust.canonical_sha({"trusted_hash": good}),
            }
            receipt = {
                "receipt_version": 2,
                "hooks_path": str(hooks_path),
                "commands": ["echo cairn"],
            }
            header = trust.table_header(key)
            cases = [
                (
                    "0.165.0",
                    f'{header}\ntrusted_hash = "{good}"\n',
                    [{**entry, "origin": "written"}],
                    "ok   Codex hook trust - trusted (written by cairn)",
                ),
                (
                    "0.165.0",
                    f'{header}\ntrusted_hash = "{good}"\n',
                    [],
                    "ok   Codex hook trust - trusted (written by Codex)",
                ),
                (
                    "0.165.0",
                    f'{header}\ntrusted_hash = "sha256:old"\n',
                    [],
                    "warn Codex hook trust - stale: SessionEnd group 0 changed after it was trusted",
                ),
                (
                    "0.165.0",
                    "",
                    [],
                    "warn Codex hook trust - missing: no trust table for SessionEnd group 0",
                ),
                (
                    "0.165.0",
                    f'{header}\ntrusted_hash = "{good}"\nenabled = false\n',
                    [],
                    "warn Codex hook trust - disabled: SessionEnd group 0 has enabled = false",
                ),
                (
                    "0.170.0",
                    "",
                    [],
                    "warn Codex hook trust - manual: Codex 0.170.0 is outside the verified range "
                    "(>= 0.165.0, < 0.166.0); Review/trust the Cairn SessionEnd hook in /hooks.",
                ),
                (
                    None,
                    "",
                    [],
                    "warn Codex hook trust - manual: the Codex version is unknown (codex is not "
                    "on PATH), so cairn cannot check it against the verified range",
                ),
            ]
            for version, toml, entries, expected in cases:
                with self.subTest(expected=expected), patch.dict(os.environ, environment):
                    (home / "config.toml").write_text(toml)
                    (home / registration.RECEIPT_NAME).write_text(
                        json.dumps({**receipt, "entries": entries})
                    )
                    output = io.StringIO()
                    with (
                        patch.object(
                            trust,
                            "codex_version",
                            return_value=(version, "" if version else "codex is not on PATH"),
                        ),
                        redirect_stdout(output),
                    ):
                        admin._check_codex_trust()
                    line = output.getvalue().strip()
                    self.assertTrue(line.startswith(expected), line)
                    self.assertEqual(line.count("\n"), 0)
