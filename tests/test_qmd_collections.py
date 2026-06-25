"""Self-healing qmd collection registration: ``cairn.qmd`` helpers + sync wiring.

These cover the bootstrap that makes a fresh index non-empty without any manual
``qmd collection add`` - the gap that previously left every search returning nothing.
The ``qmd`` CLI is always mocked; no real qmd binary is invoked.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cairn import admin, config, qmd, sync


def _completed(returncode=0, stdout="", stderr=""):
    """A stand-in for ``subprocess.run``'s CompletedProcess result."""
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


# Sample of qmd's human-readable (no --json) `collection list` output. Each
# collection anchors on a `qmd://<name>/` URI; that is what the parser keys off.
_LIST_OUTPUT = """\
claude-code-sessions (qmd://claude-code-sessions/)
  Pattern:  **/*.md
  Files:    9

codex-sessions (qmd://codex-sessions/)
  Pattern:  **/*.md
  Files:    3
"""


class TestListCollections(unittest.TestCase):
    def test_parses_names_from_uris(self):
        with mock.patch.object(qmd.subprocess, "run", return_value=_completed(stdout=_LIST_OUTPUT)):
            self.assertEqual(
                qmd.list_collections("qmd"), {"claude-code-sessions", "codex-sessions"}
            )

    def test_empty_when_no_collections(self):
        with mock.patch.object(
            qmd.subprocess, "run", return_value=_completed(stdout="No collections.\n")
        ):
            self.assertEqual(qmd.list_collections("qmd"), set())

    def test_raises_on_nonzero_exit(self):
        # A broken qmd must not be read as "nothing registered" (that would re-add forever).
        with (
            mock.patch.object(
                qmd.subprocess, "run", return_value=_completed(returncode=1, stderr="boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            qmd.list_collections("qmd")


class TestEnsureCollection(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _with_markdown(self) -> Path:
        (self.dir / "a.md").write_text("# hi", encoding="utf-8")
        return self.dir

    def test_skipped_when_dir_missing(self):
        with mock.patch.object(qmd.subprocess, "run") as run:
            self.assertEqual(
                qmd.ensure_collection("c", self.dir / "nope", "qmd", known=set()), "skipped"
            )
            run.assert_not_called()

    def test_skipped_when_dir_has_no_markdown(self):
        (self.dir / "note.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(qmd.subprocess, "run") as run:
            self.assertEqual(qmd.ensure_collection("c", self.dir, "qmd", known=set()), "skipped")
            run.assert_not_called()

    def test_exists_is_idempotent_noop(self):
        self._with_markdown()
        with mock.patch.object(qmd.subprocess, "run") as run:
            self.assertEqual(qmd.ensure_collection("c", self.dir, "qmd", known={"c"}), "exists")
            run.assert_not_called()

    def test_added_invokes_collection_add(self):
        self._with_markdown()
        with mock.patch.object(qmd.subprocess, "run", return_value=_completed(0)) as run:
            self.assertEqual(qmd.ensure_collection("c", self.dir, "qmd", known=set()), "added")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["qmd", "collection", "add"])
        self.assertIn("--name", argv)
        self.assertEqual(argv[argv.index("--name") + 1], "c")

    def test_failed_on_nonzero_add(self):
        self._with_markdown()
        with mock.patch.object(qmd.subprocess, "run", return_value=_completed(1, stderr="nope")):
            self.assertEqual(qmd.ensure_collection("c", self.dir, "qmd", known=set()), "FAILED")

    def test_fetches_known_when_not_supplied(self):
        self._with_markdown()
        # First call = `collection list` (empty), second = `collection add` (ok).
        with mock.patch.object(
            qmd.subprocess, "run", side_effect=[_completed(0, stdout=""), _completed(0)]
        ) as run:
            self.assertEqual(qmd.ensure_collection("c", self.dir, "qmd"), "added")
        self.assertEqual(run.call_count, 2)


class _SyncConfigFixture(unittest.TestCase):
    """A real config (claude+codex enabled) with a temp data_root."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.data_root = Path(self._tmp.name) / "ctx"
        self._prev = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "CAIRN_CONFIG")}
        os.environ.pop("CAIRN_CONFIG", None)
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        config.get_config.cache_clear()
        admin._config_init(data_root=str(self.data_root), enable=["claude", "codex"])
        config.get_config.cache_clear()
        self.cfg = config.get_config()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.get_config.cache_clear()
        self._tmp.cleanup()

    def _seed_markdown(self, collection: str):
        d = self.cfg.output_dir(collection)
        d.mkdir(parents=True, exist_ok=True)
        (d / "s.md").write_text("# session", encoding="utf-8")


class TestEnsureCollectionsWiring(_SyncConfigFixture):
    def test_only_enabled_collections_considered(self):
        seen: list[str] = []

        def fake_ensure(name, path, qmd_binary, known):
            seen.append(name)
            return "skipped"

        with (
            mock.patch.object(sync, "list_collections", return_value=set()),
            mock.patch.object(sync, "ensure_collection", side_effect=fake_ensure),
        ):
            sync._ensure_collections(self.cfg)

        # claude+codex enabled; cursor/granola/service-docs disabled by _config_init.
        self.assertEqual(set(seen), {"claude-code-sessions", "codex-sessions"})

    def test_summary_counts_added_and_skipped(self):
        self._seed_markdown("claude-code-sessions")

        def fake_ensure(name, path, qmd_binary, known):
            return "added" if name == "claude-code-sessions" else "skipped"

        with (
            mock.patch.object(sync, "list_collections", return_value=set()),
            mock.patch.object(sync, "ensure_collection", side_effect=fake_ensure),
        ):
            summary = sync._ensure_collections(self.cfg)

        self.assertIn("registered 1 new", summary)
        self.assertIn("claude-code-sessions", summary)
        self.assertIn("skipped=1", summary)

    def test_failure_raises(self):
        with (
            mock.patch.object(sync, "list_collections", return_value=set()),
            mock.patch.object(sync, "ensure_collection", return_value="FAILED"),
            self.assertRaises(RuntimeError),
        ):
            sync._ensure_collections(self.cfg)

    def test_run_inproc_catches_and_reports_failure(self):
        # run_inproc must never let a step exception abort the whole sync.
        def boom():
            raise RuntimeError("kaboom")

        with mock.patch.object(sync, "log"), mock.patch.object(sync, "_log_output"):
            self.assertFalse(sync.run_inproc("qmd-collections", boom))
            self.assertTrue(sync.run_inproc("qmd-collections", lambda: "ok"))


class TestDoctorVectorCount(unittest.TestCase):
    def test_parses_vectors(self):
        status = "Documents\n  Total:    9 files indexed\n  Vectors:  2,364 embedded\n"
        with mock.patch.object(admin.subprocess, "run", return_value=_completed(stdout=status)):
            self.assertEqual(admin._qmd_vector_count("qmd"), 2364)

    def test_zero_vectors(self):
        with mock.patch.object(
            admin.subprocess, "run", return_value=_completed(stdout="  Vectors:  0 embedded")
        ):
            self.assertEqual(admin._qmd_vector_count("qmd"), 0)

    def test_none_when_unreadable(self):
        with mock.patch.object(admin.subprocess, "run", return_value=_completed(returncode=1)):
            self.assertIsNone(admin._qmd_vector_count("qmd"))


if __name__ == "__main__":
    unittest.main()
