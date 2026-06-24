"""Config-as-data: integration enablement + sync policy live in the JSON, not code.

These lock the contract that the user's "which integrations" choice is recorded in
the config file and *consumed* by the sync planner - disabling a source removes it
from the plan, and `cairn config init --enable` flips exactly the chosen flags.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cairn import config, sync
from cairn.admin import _apply_choices


def _build(collections: dict, extra_top: dict | None = None) -> config.CairnConfig:
    """Build a CairnConfig from a minimal in-memory spec (no file I/O)."""
    data = {
        "data_root": "/tmp/cairn-test",
        "collections": collections,
        "project_groups": {},
        "repo_catalog": {},
        "project_descriptions": {},
        **(extra_top or {}),
    }
    return config._build(data, Path("/tmp/cairn-test/config.json"), None)


class TestSyncPolicyParsing(unittest.TestCase):
    def test_defaults_when_block_omitted(self):
        sp = (
            _build({"claude-code-sessions": {"store": "~/.claude/projects"}})
            .collection("claude-code-sessions")
            .sync
        )
        self.assertTrue(sp.enabled)
        self.assertEqual(sp.since, "1d")
        self.assertIsNone(sp.on_hook)  # None -> engine per-source default

    def test_explicit_values_parsed(self):
        sp = (
            _build(
                {
                    "codex-sessions": {
                        "store": "~/x",
                        "sync": {"enabled": False, "since": "2w", "on_hook": True},
                    }
                }
            )
            .collection("codex-sessions")
            .sync
        )
        self.assertFalse(sp.enabled)
        self.assertEqual(sp.since, "2w")
        self.assertTrue(sp.on_hook)

    def test_cron_schedule_default_and_override(self):
        self.assertEqual(_build({}).cron_schedule, config.DEFAULT_CRON_SCHEDULE)
        self.assertEqual(
            _build({}, {"cron": {"schedule": "0 */4 * * *"}}).cron_schedule, "0 */4 * * *"
        )


class TestSyncPlan(unittest.TestCase):
    def _all_enabled(self) -> config.CairnConfig:
        return _build(
            {
                "claude-code-sessions": {"store": "~/.claude/projects"},
                "codex-sessions": {"store": "~/.codex/sessions"},
                "cursor-sessions": {"store": "~/cursor"},
                "granola-sessions": {"store": "~/granola"},
                "service-docs": {},
            }
        )

    def test_cron_runs_every_configured_source(self):
        steps = sync._plan(self._all_enabled(), "cron")
        self.assertEqual({n for n, _ in steps}, set(sync.SOURCE_COLLECTIONS.values()))

    def test_hook_defaults_to_claude_only(self):
        # No on_hook in config -> binding defaults (Claude True, others False).
        self.assertEqual(
            [n for n, _ in sync._plan(self._all_enabled(), "hook")], ["claude-code-sessions"]
        )

    def test_disabled_source_is_dropped(self):
        cfg = _build(
            {
                "claude-code-sessions": {"store": "~/.claude", "sync": {"enabled": False}},
                "codex-sessions": {"store": "~/.codex"},
            }
        )
        names = {n for n, _ in sync._plan(cfg, "cron")}
        self.assertNotIn("claude-code-sessions", names)
        self.assertIn("codex-sessions", names)

    def test_absent_source_never_crashes_plan(self):
        # Only one source configured; the rest are simply skipped (no missing-store error).
        cfg = _build({"codex-sessions": {"store": "~/.codex"}})
        self.assertEqual([n for n, _ in sync._plan(cfg, "cron")], ["codex-sessions"])

    def test_window_is_data_driven(self):
        cfg = _build({"codex-sessions": {"store": "~/.codex", "sync": {"since": "3w"}}})
        ((_, command),) = sync._plan(cfg, "cron")
        self.assertIn("--since", command)
        self.assertEqual(command[command.index("--since") + 1], "3w")

    def test_service_docs_is_not_windowed(self):
        cfg = _build({"service-docs": {}})
        ((_, command),) = sync._plan(cfg, "cron")
        self.assertNotIn("--since", command)

    def test_config_on_hook_overrides_default(self):
        cfg = _build(
            {
                "claude-code-sessions": {"store": "~/.claude", "sync": {"on_hook": False}},
                "codex-sessions": {"store": "~/.codex", "sync": {"on_hook": True}},
            }
        )
        self.assertEqual([n for n, _ in sync._plan(cfg, "hook")], ["codex-sessions"])


class TestConfigInitChoices(unittest.TestCase):
    """`cairn config init --enable` bakes the integration choice into the data file."""

    def setUp(self):
        self.template = config.example_template_text()

    def test_no_choices_returns_template_verbatim(self):
        self.assertEqual(_apply_choices(self.template, None, None), self.template)

    def test_enable_flips_exactly_the_chosen_sources(self):
        out = json.loads(_apply_choices(self.template, "/tmp/ctx", ["claude", "granola"]))
        self.assertEqual(out["data_root"], "/tmp/ctx")
        colls = out["collections"]
        self.assertTrue(colls["claude-code-sessions"]["sync"]["enabled"])
        self.assertTrue(colls["granola-sessions"]["sync"]["enabled"])
        self.assertFalse(colls["codex-sessions"]["sync"]["enabled"])
        self.assertFalse(colls["cursor-sessions"]["sync"]["enabled"])
        self.assertFalse(colls["service-docs"]["sync"]["enabled"])

    def test_enable_none_disables_all_known_sources(self):
        out = json.loads(_apply_choices(self.template, None, []))
        for collection in sync.SOURCE_COLLECTIONS.values():
            self.assertFalse(out["collections"][collection]["sync"]["enabled"])


if __name__ == "__main__":
    unittest.main()
