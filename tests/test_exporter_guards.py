"""Exporter guards: the parser stamp, the parser-drift guard and the findings store.

Every source store and output directory lives under a temp root (see
:class:`tests.test_shared_store.StoreFixture`); every transcript is invented.
"""

from __future__ import annotations

import importlib

from cairn import config
from cairn.catalog import load_catalog
from cairn.frontmatter import parse_frontmatter
from cairn.sources.spec_targets import SPEC_TARGETS
from tests.test_shared_store import StoreFixture


class GuardFixture(StoreFixture):
    def source(self, name: str = "codex"):
        """An in-process source bound to this fixture's config (module reload)."""
        module = importlib.reload(importlib.import_module(f"cairn.sources.{name}"))
        return getattr(module, f"{name.title()}Source")()

    def output_dir(self, collection: str = "codex-sessions"):
        return config.get_config().output_dir(collection)


class TestParserStamp(GuardFixture):
    def test_rendered_frontmatter_has_exporter_keys_and_catalog_accepts_it(self):
        self.native("codex", "stamp-0001")
        self.run_cli("codex", "export", "--all")
        [path] = sorted(self.output_dir().rglob("*.md"))
        fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(fm["exporter_version"], SPEC_TARGETS["codex"].adapter_version)
        self.assertEqual(fm["exporter_spec_target"], SPEC_TARGETS["codex"].checked)
        catalog = load_catalog(self.output_dir())
        self.assertEqual(catalog.errors, {})
        [(_, cached)] = list(catalog.items())
        self.assertEqual(cached["exporter_spec_target"], SPEC_TARGETS["codex"].checked)
        # A re-render regenerates the keys: exactly one stamp, never a second copy.
        source = self.source("codex")
        record = source.find_record("stamp-0001")
        assert record is not None
        data = {
            "date": "2026-03-28",
            "session_id": "stamp-0001",
            "messages": [],
            "message_count": 0,
        }
        source.export_one("stamp-0001", data, record.path)  # type: ignore[arg-type]
        self.assertEqual(path.read_text(encoding="utf-8").count("exporter_version:"), 1)
