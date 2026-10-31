"""Locks coordinate resource aliases without creating files in the user's corpus."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cairn import config, hooks
from cairn.harnesses import get_harness
from cairn.storage import (
    ConcurrentChangeError,
    atomic_write,
    document_lock,
    lock_path,
    locked,
)
from tests.test_shared_store import StoreFixture


class TestExternalLocks(StoreFixture):
    def test_resource_aliases_contend_across_profiles(self):
        resource = self.root / "corpus"
        resource.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(resource, target_is_directory=True)
        probe = (
            "import sys\nfrom pathlib import Path\n"
            "from cairn.storage import lock_path, locked\n"
            "try:\n with locked(lock_path(Path(sys.argv[1]), sys.argv[2]), blocking=False): pass\n"
            "except BlockingIOError: sys.exit(42)\n"
        )
        for purpose in ("document", "operation", "export", "queue", "worker", "registration"):
            with self.subTest(purpose=purpose), locked(lock_path(resource, purpose)):
                result = subprocess.run(
                    [sys.executable, "-c", probe, str(alias), purpose],
                    env={
                        **self.env,
                        "XDG_CACHE_HOME": str(self.root / "other-cache"),
                        "XDG_CONFIG_HOME": str(self.root / "other-config"),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(list(resource.iterdir()), [])
        self.assertNotEqual(lock_path(resource, "export"), lock_path(resource, "operation"))

    def test_export_annotation_and_index_leave_no_lock_sidecars(self):
        cfg = config.get_config()
        cfg.data_root.mkdir()
        subprocess.run(["git", "init", "-q", str(cfg.data_root)], check=True)
        native = self.native("codex", "lock-cleanliness")
        self.run_cli("codex", "export", "--all")
        self.run_cli("codex", "note", "Keep this annotation", "--session-id", "lock-cleanliness")
        self.run_cli("codex", "decide", "Withdraw me", "--session-id", "lock-cleanliness")
        self.run_cli(
            "codex", "forget", "1", "--reason", "other", "--session-id", "lock-cleanliness"
        )
        self.assertTrue((cfg.data_root / "withdrawn.jsonl").is_file())
        probe = self.root / "qmd"
        probe.write_text(f"#!{sys.executable}\nprint('qmd://codex-sessions/')\n")
        probe.chmod(0o755)
        data = json.loads(self.config.read_text())
        data["qmd_binary"] = str(probe)
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        cfg = config.get_config()
        hooks.enqueue(
            cfg,
            get_harness("codex").decode(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": "lock-cleanliness",
                    "cwd": self.cwd,
                    "transcript_path": str(native),
                }
            ),
        )
        self.assertEqual(hooks.worker(cfg), 0)
        self.assertIn("Keep this annotation", self.records()[0].path.read_text())
        locks = list(self.root.rglob("*.lock"))
        self.assertTrue(locks)
        lock_dir = Path.home() / ".local/state/cairn/locks"
        self.assertTrue(all(path.parent == lock_dir for path in locks), locks)
        result = subprocess.run(
            ["git", "-C", str(cfg.data_root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(".lock", result.stdout)

    def test_document_lock_identity_survives_atomic_replacement(self):
        document = self.root / "session.md"
        document.write_text("before")
        before = document_lock(document)
        replacement = self.root / "replacement.md"
        replacement.write_text("after")
        replacement.replace(document)
        self.assertEqual(document_lock(document), before)
        with locked(before):
            self.assertTrue(before.is_file())
        self.assertTrue(before.is_file())  # permanent inode, safe for existing waiters


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    @staticmethod
    def mode_of(path: Path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_atomic_write_default_keeps_original_behaviour(self):
        target = self.root / "real.txt"
        target.write_text("old")
        link = self.root / "nested" / "link.txt"
        link.parent.mkdir()
        link.symlink_to(target)
        atomic_write(link, "new")
        self.assertFalse(link.is_symlink())  # a symlink is replaced by a regular file
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(self.mode_of(link), 0o600)
        atomic_write(self.root / "a" / "b" / "c.txt", "x")  # parents are created
        self.assertEqual((self.root / "a/b/c.txt").read_text(), "x")

    def test_atomic_write_applies_mode(self):
        path = self.root / "mode.txt"
        atomic_write(path, "one", mode=0o644)
        self.assertEqual(self.mode_of(path), 0o644)
        atomic_write(path, "two", mode=0o640)
        self.assertEqual(self.mode_of(path), 0o640)
        self.assertEqual(path.read_text(), "two")

    def test_atomic_write_follows_symlink_when_asked(self):
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "notes.md"
        target.write_text("old")
        link = self.root / "notes.md"
        link.symlink_to(target)
        atomic_write(link, "new", follow_symlinks=True, mode=0o644)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(sorted(p.name for p in dotfiles.iterdir()), ["notes.md"])
        dangling = self.root / "dangling.md"
        dangling.symlink_to(self.root / "missing-dir" / "x.md")
        with self.assertRaises(FileNotFoundError):
            atomic_write(dangling, "x", follow_symlinks=True)
        self.assertFalse((self.root / "missing-dir").exists())

    def test_atomic_write_check_unchanged_aborts_on_a_concurrent_change(self):
        path = self.root / "race.md"
        path.write_bytes(b"edited by someone else")
        with self.assertRaises(ConcurrentChangeError):
            atomic_write(path, "mine", check_unchanged=True, expected=b"what I read")
        self.assertEqual(path.read_bytes(), b"edited by someone else")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["race.md"])
        atomic_write(path, "mine", check_unchanged=True, expected=b"edited by someone else")
        self.assertEqual(path.read_text(), "mine")
        fresh = self.root / "fresh.md"
        atomic_write(fresh, "new", check_unchanged=True, expected=None)
        self.assertEqual(fresh.read_text(), "new")
        with self.assertRaises(ConcurrentChangeError):
            atomic_write(fresh, "again", check_unchanged=True, expected=None)
