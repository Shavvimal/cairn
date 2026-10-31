"""Locks coordinate resource aliases without creating files in the user's corpus."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cairn import config, hooks
from cairn.harnesses import get_harness
from cairn.storage import document_lock, lock_path, locked
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
