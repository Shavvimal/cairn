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


class TestPortablePackage(unittest.TestCase):
    def test_all_versions_and_compat_projection(self):
        root = json.loads((_REPO_ROOT / "plugin.json").read_text())
        compat = json.loads((_REPO_ROOT / ".codex-plugin/plugin.json").read_text())
        expected = {k: root[k] for k in ("name", "version", "description")}
        expected.update(skills="./skills/", **root["extensions"]["com.openai"])
        self.assertEqual(compat, expected)
        self.assertEqual(root["version"], cairn.__version__)
        self.assertEqual(
            root["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        )

    def test_resources_resolve_from_copied_cache(self):
        import shutil
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "plugin"
            shutil.copytree(
                _REPO_ROOT, cache, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__")
            )
            root = json.loads((cache / "plugin.json").read_text())
            extension = root["extensions"]["com.openai"]
            for key in ("hooks", "onboardingSkill"):
                value = extension[key]
                self.assertTrue(value.startswith("./"))
                self.assertTrue((cache / value).resolve().is_relative_to(cache.resolve()))
                self.assertTrue((cache / value).is_file())
            self.assertEqual(extension["hooks"], "./hooks/codex.json")
            for skill in (cache / "skills").glob("*/SKILL.md"):
                self.assertTrue((skill.parent / "../../references/harnesses/contract.md").is_file())
            for name in ("contract", "claude", "codex"):
                self.assertTrue((cache / f"references/harnesses/{name}.md").is_file())
            policy = (cache / "skills/setup/agents/openai.yaml").read_text()
            self.assertIn("allow_implicit_invocation: false", policy)
            self.assertIn(
                "disable-model-invocation: true", (cache / "skills/setup/SKILL.md").read_text()
            )
            self.assertTrue((cache / "references/peer-review.md").is_file())
            for name in ("invoke-codex", "invoke-claude"):
                skill = cache / "skills" / name
                policy = (skill / "agents/openai.yaml").read_text()
                self.assertIn("allow_implicit_invocation: true", policy)
                text = (skill / "SKILL.md").read_text()
                self.assertTrue((skill / "../../references/peer-review.md").is_file())
                self.assertIn("references/peer-review.md", text)
                # Host-agnostic: no Claude-only skill syntax that Codex would not honour.
                for forbidden in (
                    "disable-model-invocation",
                    "$ARGUMENTS",
                    "CLAUDE_SKILL_DIR",
                    "```!",
                    "when_to_use",
                ):
                    self.assertNotIn(forbidden, text, f"{name} uses {forbidden}")

    def test_catalog_and_separate_hooks(self):
        market = json.loads((_REPO_ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(market["name"], "cairn")
        self.assertEqual(market["plugins"][0]["source"], {"source": "local", "path": "./"})
        for host, file in (("claude", "hooks.json"), ("codex", "codex.json")):
            groups = json.loads((_REPO_ROOT / "hooks" / file).read_text())["hooks"]["SessionEnd"]
            self.assertEqual(len(groups), 1)
            hook = groups[0]["hooks"][0]
            self.assertIn("--harness " + host, hook["command"])
            self.assertLessEqual(hook["timeout"], 3)
            self.assertNotIn("async", hook)


if __name__ == "__main__":
    unittest.main()
