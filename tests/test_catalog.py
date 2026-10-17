"""The stat-validated catalog: a derived cache that may never lie about the files."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

from cairn import config, mutations
from cairn.catalog import RACY_GUARD_NS, Catalog, catalog_path, load_catalog
from cairn.frontmatter import FrontmatterParseError, parse_frontmatter, read_frontmatter_head
from cairn.store import SessionStore, read_record
from tests.test_shared_store import StoreFixture


class CatalogFixture(StoreFixture):
    def source(self, name="codex"):
        """An in-process source bound to this fixture's config (module reload)."""
        module = importlib.reload(importlib.import_module(f"cairn.sources.{name}"))
        return getattr(module, f"{name.title()}Source")()

    def output_dir(self, collection="codex-sessions") -> Path:
        return config.get_config().output_dir(collection)

    def full_scan(self, collection="codex-sessions", source="codex", session_type="codex-session"):
        found = []
        for path in sorted(self.output_dir(collection).rglob("*.md")):
            record = read_record(path, source, "session_id", session_type)
            if record:
                found.append(record)
        return found

    def age(self, path: Path, seconds: float = 10) -> None:
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))


class TestCatalog(CatalogFixture):
    def test_cold_build_matches_full_scan(self):
        for sid in ("alpha", "beta"):
            self.native("codex", sid)
        self.run_cli("codex", "export", "--all")
        catalog_path(self.output_dir()).unlink()
        self.assertEqual(load_catalog(self.output_dir()).stats["rejected"], 0)
        self.assertEqual(self.records(), self.full_scan())
        saved = json.loads(catalog_path(self.output_dir()).read_text())
        self.assertEqual(len(saved["entries"]), 2)

    def test_warm_load_parses_nothing(self):
        for sid in ("alpha", "beta"):
            self.native("codex", sid)
        self.run_cli("codex", "export", "--all")
        for path in self.output_dir().rglob("*.md"):
            self.age(path)
        first = load_catalog(self.output_dir())
        first.save()
        second = load_catalog(self.output_dir())
        self.assertEqual((second.stats["parsed"], second.stats["reused"]), (0, 2))
        self.assertEqual(second.stats["rejected"], 0)
        self.assertFalse(second.dirty)

    def test_changed_file_is_reparsed(self):
        self.native("codex", "edited")
        self.run_cli("codex", "export", "--all")
        path = self.records()[0].path
        self.age(path)
        load_catalog(self.output_dir()).save()
        content = path.read_text()
        path.write_text(content.replace("title:", 'title: "Edited by hand"\nold_title:', 1))
        self.age(path)
        catalog = load_catalog(self.output_dir())
        self.assertEqual(catalog.stats["parsed"], 1)
        source = self.source()
        record = source.find_record("edited")
        assert record is not None
        self.assertEqual(record.title, "Edited by hand")

    def test_mtime_preserving_edit_is_reparsed(self):
        self.native("codex", "sneaky")
        self.run_cli("codex", "export", "--all")
        path = self.records()[0].path
        self.age(path)
        load_catalog(self.output_dir()).save()
        before = path.stat()
        content = path.read_text()
        self.assertIn("status: active", content)
        path.write_text(content.replace("status: active", "status: paused", 1))  # same size
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual((after.st_mtime_ns, after.st_size), (before.st_mtime_ns, before.st_size))
        catalog = load_catalog(self.output_dir())
        self.assertEqual(catalog.stats["parsed"], 1)  # ctime moved
        self.assertEqual(self.records()[0].metadata["status"], "paused")

    def test_deleted_file_is_dropped(self):
        self.native("codex", "gone")
        self.run_cli("codex", "export", "--all")
        source = self.source()
        record = source.find_record("gone")
        assert record is not None
        record.path.unlink()
        self.assertIsNone(source.find_record("gone"))
        self.assertIsNone(source.find_file("gone"))
        self.assertEqual(load_catalog(self.output_dir()).entries, {})

    def test_symlink_policy(self):
        self.native("codex", "real")
        self.run_cli("codex", "export", "--all")
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "linked.md"
        target.write_text(
            '---\ntype: codex-session\nsession_id: linked\ntitle: "v1"\ndate: 2026-03-28\n---\nbody'
        )
        (self.output_dir() / "linked-dir").symlink_to(outside, target_is_directory=True)
        (self.output_dir() / "linked.md").symlink_to(target)
        ids = {r.session_id for r in self.records()}
        self.assertEqual(
            ids, {"real", "linked"}
        )  # the file link counts; the dir link is not walked
        for path in self.output_dir().rglob("*.md"):
            self.age(path)
        self.age(target)
        load_catalog(self.output_dir()).save()
        target.write_text(target.read_text().replace('"v1"', '"v2"'))
        self.age(target)
        catalog = load_catalog(self.output_dir())
        self.assertEqual(catalog.stats["parsed"], 1)
        self.assertEqual(catalog.entries["linked.md"].frontmatter["title"], "v2")

    def test_racy_window_entry_is_reparsed(self):
        self.native("codex", "racy")
        self.run_cli("codex", "export", "--all")
        path = self.records()[0].path
        self.age(path)
        load_catalog(self.output_dir()).save()
        saved_path = catalog_path(self.output_dir())
        saved = json.loads(saved_path.read_text())
        (_rel, entry), *_ = saved["entries"].items()
        entry["frontmatter"]["title"] = "FROM CATALOG"
        saved["snapshot_ns"] = entry["mtime_ns"] + RACY_GUARD_NS // 2
        saved_path.write_text(json.dumps(saved))
        self.assertNotEqual(self.records()[0].title, "FROM CATALOG")
        saved["snapshot_ns"] = entry["mtime_ns"] + 2 * RACY_GUARD_NS
        saved_path.write_text(json.dumps(saved))
        self.assertEqual(self.records()[0].title, "FROM CATALOG")

    def test_corrupt_or_wrong_version_rebuilds(self):
        self.native("codex", "rebuild")
        self.run_cli("codex", "export", "--all")
        saved_path = catalog_path(self.output_dir())
        for content in ("not json", json.dumps({"version": 99, "entries": {}})):
            saved_path.write_text(content)
            self.assertEqual([r.session_id for r in self.records()], ["rebuild"])
            self.assertEqual(json.loads(saved_path.read_text())["version"], 1)

    def test_valid_json_with_invalid_cache_shapes_rebuilds(self):
        self.native("codex", "rebuild-shapes")
        self.run_cli("codex", "export", "--all")
        saved_path = catalog_path(self.output_dir())
        valid = json.loads(saved_path.read_text())
        rel = next(iter(valid["entries"]))
        malformed: list[object] = [
            [],
            None,
            1,
            "cache",
            {**valid, "entries": []},
            {**valid, "snapshot_ns": []},
        ]
        replacement: object
        for replacement in (None, [], {"unknown": True}):
            malformed.append({**valid, "entries": {rel: replacement}})
        for field, value in (
            ("mtime_ns", []),
            ("size", "123"),
            ("frontmatter", []),
            ("frontmatter", {"type": {"nested": "mapping"}}),
            ("frontmatter", {"activity_dates": [123]}),
        ):
            bad = deepcopy(valid)
            bad["entries"][rel][field] = value
            malformed.append(bad)
        for payload in malformed:
            with self.subTest(payload=payload):
                saved_path.write_text(json.dumps(payload))
                self.assertEqual(load_catalog(self.output_dir()).stats["rejected"], 1)
                self.assertEqual([r.session_id for r in self.records()], ["rebuild-shapes"])
                self.assertEqual(json.loads(saved_path.read_text())["version"], 1)

    def test_export_write_through_without_rewalk(self):
        paths = {sid: self.native("codex", sid) for sid in ("one", "two")}
        source = self.source()
        for sid, path in paths.items():
            source.export_target(sid, str(path))
        for sid in paths:
            record = source.find_record(sid)
            assert record is not None
            self.assertTrue(record.path.exists())
        self.assertEqual(source.catalog.stats["walks"], 1)
        source.flush_catalog()
        saved = json.loads(catalog_path(self.output_dir()).read_text())
        self.assertEqual(len(saved["entries"]), 2)
        self.assertEqual(len(self.records()), 2)

    def test_collision_suffix_path_is_catalogued(self):
        for sid in ("abcdef12-one-wxyz", "abcdef12-two-wxyz"):  # same head8 + tail4
            self.native("codex", sid)
        self.run_cli("codex", "export", "--all")
        source = self.source()
        paths = {sid: source.find_file(sid) for sid in ("abcdef12-one-wxyz", "abcdef12-two-wxyz")}
        self.assertEqual(len(set(paths.values())), 2)
        self.assertTrue(any(len(p.stem) > len("2026-03-28-abcdef12wxyz") for p in paths.values()))
        self.assertEqual(len(source.catalog.entries), 2)

    def test_overwritten_file_with_new_identity(self):
        self.native("codex", "old-id")
        self.run_cli("codex", "export", "--all")
        source = self.source()
        record = source.find_record("old-id")
        assert record is not None
        record.path.write_text(record.path.read_text().replace("old-id", "new-id"))
        self.assertIsNone(source.find_record("old-id"))
        found = source.find_record("new-id")
        assert found is not None
        self.assertEqual(found.path, record.path)

    def test_mutation_then_lookup_sees_new_identity(self):
        self.native("codex", "annotated")
        self.run_cli("codex", "export", "--all")
        source = self.source()
        self.assertIsNotNone(source.find_record("annotated"))
        self.assertTrue(mutations.add_comment(source.find_file, "annotated", "in-process note"))
        record = source.find_record("annotated")
        assert record is not None
        self.assertIn("in-process note", str(record.metadata["comments"]))
        self.run_cli("codex", "close", "--session-id", "annotated")
        self.assertEqual(self.records()[0].metadata["status"], "done")

    def test_duplicate_native_id_stays_ambiguous(self):
        self.native("codex", "twin")
        self.run_cli("codex", "export", "--all")
        path = self.records()[0].path
        shutil.copy(path, path.with_name("copy-" + path.name))
        with self.assertRaises(FileExistsError):
            self.source().find_file("twin")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            SessionStore(config.get_config()).resolve("twin")

    def test_concurrent_exports_notes_and_recall_never_lie(self):
        self.native("codex", "race")
        self.run_cli("codex", "export", "--all")
        processes = []
        for n in range(6):
            self.native("codex", "race", text=f"native revision {n}")
            for args in (
                ["codex", "note", f"comment-{n}", "--session-id", "race"],
                ["recall", "list", "2026-03-28", "--all-projects", "--min-msgs", "1", "--json"],
            ):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "cairn", *args],
                        env=self.env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            self.run_cli("codex", "export", "--all")
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, err)
            if out.startswith("["):
                for row in json.loads(out):
                    self.assertTrue(Path(row["path"]).exists())
        comments = str(self.records()[0].metadata["comments"])
        for n in range(6):
            self.assertIn(f"comment-{n}", comments)
        self.assertEqual(self.records(), self.full_scan())

    def test_frontmatter_head_reader(self):
        path = self.root / "big.md"
        lines = ["---", 'title: "big"', "items:"] + [f"  - item {n}" for n in range(3000)] + ["---"]
        path.write_text("\n".join(lines) + "\n" + "x" * (5 * 1024 * 1024))
        self.assertEqual(read_frontmatter_head(path), parse_frontmatter(path.read_text()))
        unterminated = self.root / "open.md"
        unterminated.write_text("---\ntitle: never closed\nbody")
        self.assertEqual(
            read_frontmatter_head(unterminated), parse_frontmatter(unterminated.read_text())
        )
        huge = self.root / "huge.md"
        huge.write_text("---\n" + "k: v\n" * 100 + "y" * (5 * 1024 * 1024))
        with self.assertRaises(FrontmatterParseError):
            read_frontmatter_head(huge)
        catalog = Catalog(self.root)
        catalog.refresh()  # must not raise
        self.assertIn("huge.md", catalog.errors)
        self.assertNotIn("huge.md", catalog.entries)

    def test_listing_still_warns_on_unparseable_file(self):
        self.native("codex", "fine")
        self.run_cli("codex", "export", "--all")
        (self.output_dir() / "broken.md").write_text("---\ntags: [a, b]\n---\n")
        result = self.run_cli("codex", "list", "--all")
        self.assertIn("could not read", result.stderr)
        self.assertIn("broken.md", result.stderr)
        self.assertIn(
            "fine", result.stdout + self.run_cli("codex", "list", "--all", "--json").stdout
        )

    def test_readonly_commands_do_not_create_missing_collection(self):
        missing = self.output_dir()
        self.assertFalse(missing.exists())
        result = self.run_cli("recall", "list", "today", "--source", "codex", "--json")
        self.assertEqual(json.loads(result.stdout), [])
        self.assertFalse(missing.exists())
        self.assertFalse(catalog_path(missing).exists())


class TestWriteGuards(CatalogFixture):
    def test_export_one_refuses_unparseable_render_and_keeps_the_file(self):
        from unittest import mock

        self.native("codex", "guard")
        self.run_cli("codex", "export", "--all")
        source = self.source()
        path = source.find_file("guard")
        assert path is not None
        before = path.read_text()
        bad = before.replace("title:", "title: {flow}\ntitle:", 1)
        data = {"date": "2026-03-28", "session_id": "guard", "messages": []}
        with mock.patch.object(type(source), "render_document", return_value=bad):
            result = source.export_one("guard", data, path)
        self.assertIsNone(result)
        self.assertEqual(path.read_text(), before)
        wrong_id = before.replace("session_id: guard", "session_id: other", 1)
        with mock.patch.object(type(source), "render_document", return_value=wrong_id):
            result = source.export_one("guard", data, path)
        self.assertIsNone(result)
        self.assertEqual(path.read_text(), before)

    def test_vanished_file_is_dropped_without_an_error(self):
        from unittest import mock

        from cairn import catalog as catalog_mod

        self.native("codex", "gone")
        self.run_cli("codex", "export", "--all")
        catalog_path(self.output_dir()).unlink()
        real = catalog_mod.read_frontmatter_head

        def vanish(path, *args, **kwargs):
            if path.name.endswith("gone.md") or "gone" in path.name:
                raise FileNotFoundError(path)
            return real(path, *args, **kwargs)

        with mock.patch.object(catalog_mod, "read_frontmatter_head", side_effect=vanish):
            loaded = load_catalog(self.output_dir())
        self.assertEqual(loaded.errors, {})
        self.assertFalse(any("gone" in rel for rel in loaded.entries))
