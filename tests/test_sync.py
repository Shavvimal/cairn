"""``cairn sync``: one run at a time, process-group timeouts, verified embedding, summary.

Every QMD call goes to a ``#!python`` stub whose behaviour per subcommand comes
from a JSON scenario file, so a later run in the same test can simulate recovery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from cairn import config, hooks, procs, sync
from cairn.storage import lock_path, locked
from tests.test_shared_store import StoreFixture

STATUS_COMPLETE = "Documents\n  Total:    4 files indexed\n  Vectors:  9 embedded\n"
STATUS_PENDING = STATUS_COMPLETE + "  Pending:  2 need embedding (run 'qmd embed')\n"
STATUS_EMPTY = "Documents\n  Total:    0 files indexed\n  Vectors:  0 embedded\n"
STATUS_NO_VECTORS = "Documents\n  Total:    4 files indexed\n  Vectors:  0 embedded\n"

# Records each call and whether the QMD index lock was held by someone else while
# it ran (it must be: sync holds it across update, embed and the status check).
SCENARIO_QMD = """
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
scenario = json.loads((here / "scenario.json").read_text())
command = " ".join(sys.argv[1:3]) if sys.argv[1] == "collection" else sys.argv[1]
held = "n/a"
if command in ("embed", "status"):
    from cairn.qmd import locked_index
    try:
        with locked_index(blocking=False):
            held = "free"
    except BlockingIOError:
        held = "held"
with open(here / "calls.log", "a") as log:
    log.write(f"{command} {held}\\n")
spec = scenario.get(command, {})
if command == "collection list":
    print("qmd://codex-sessions/\\nqmd://claude-code-sessions/")
sys.stdout.write(spec.get("out", ""))
sys.stderr.write(spec.get("err", ""))
sys.exit(spec.get("rc", 0))
"""


class SyncFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.fake_qmd(SCENARIO_QMD)
        self.scenario({})

    def scenario(self, spec: dict) -> None:
        spec.setdefault("status", {"out": STATUS_COMPLETE})
        (self.root / "scenario.json").write_text(json.dumps(spec))

    @property
    def log_text(self) -> str:
        path = self.root / "data/logs/context-sync.log"
        return path.read_text() if path.exists() else ""

    def calls(self) -> list[str]:
        path = self.root / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def sync_cron(self) -> subprocess.CompletedProcess[str]:
        return self.run_cli("sync", "--cron", check=False)


class TestOneSyncAtATime(SyncFixture):
    def test_second_sync_logs_one_skip_line_and_exits_zero(self):
        cfg = config.get_config()
        with locked(lock_path(cfg.data_root, "sync")):
            result = self.sync_cron()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.log_text.splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("skipped: another cairn sync is running", lines[0])
        self.assertNotIn("[claude-code-sessions]", self.log_text)
        self.assertEqual(self.calls(), [])

    def test_data_root_alias_contends_for_the_same_lock(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root / "data")
        (self.root / "data").mkdir(exist_ok=True)
        with locked(lock_path(alias, "sync")):
            self.assertEqual(self.sync_cron().returncode, 0)
        self.assertIn("skipped", self.log_text)

    def test_uncontended_run_logs_header_steps_and_done_in_order(self):
        result = self.sync_cron()
        self.assertEqual(result.returncode, 0, self.log_text)
        text = self.log_text
        order = [
            "=== context-sync (cron)",
            "[claude-code-sessions] ok",
            "[codex-sessions] ok",
            "[qmd-collections] ok",
            "[qmd-update] ok",
            "[qmd-embed] ok",
            "[qmd-verify] ok - 4 documents, 9 vectors, 0 pending",
            "=== done ===",
        ]
        positions = [text.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions), text)
        self.assertNotIn("failed steps", text)

    def test_hook_worker_and_targeted_export_do_not_wait_for_the_sync_lock(self):
        # Lock order: only sync takes the sync lock, so a held sync lock never blocks
        # the hook worker or hooks.export_and_index (used by cairn invoke).
        native = self.native("codex", "sync-lock-free")
        cfg = config.get_config()
        with locked(lock_path(cfg.data_root, "sync")):
            started = time.monotonic()
            document = hooks.export_and_index(
                cfg, "codex", "sync-lock-free", str(native), lock_timeout_s=5
            )
            self.assertEqual(hooks.worker(cfg), 0)
        self.assertTrue(document.is_file())
        self.assertLess(time.monotonic() - started, 30)


class TestVerifiedEmbedding(SyncFixture):
    def test_exit_zero_with_failed_chunks_fails_and_never_logs_ok(self):
        # The failure summary sits in the middle of long output, where the head/tail
        # log truncation would drop it: detection must read the full output.
        noise = "".join(f"progress line {i}\n" for i in range(200))
        self.scenario(
            {
                "embed": {
                    "out": noise + "⚠ 48,219 chunks still failed after retries\n" + noise,
                    "err": '⚠ Error embedding "a.md" chunk 3: SessionReleasedError: released\n',
                },
                "status": {"out": STATUS_PENDING},
            }
        )
        result = self.sync_cron()
        self.assertEqual(result.returncode, 1)
        text = self.log_text
        self.assertNotIn("[qmd-embed] ok", text)
        self.assertIn("[qmd-embed] FAILED", text)
        self.assertIn("qmd exited 0 but reported: 48,219 chunks failed", text)
        self.assertIn("[qmd-verify] INCOMPLETE - 4 documents, 9 vectors, 2 pending", text)
        self.assertIn("failed steps: qmd-embed\n=== FAILED ===", text)
        self.assertEqual([c for c in self.calls() if c.startswith("embed")], ["embed held"])
        # A later run with a clean embed recovers: no latched failure.
        self.scenario({"embed": {"out": "✓ Done! Embedded 2 chunks from 2 documents\n"}})
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertTrue(self.log_text.rstrip().endswith("=== done ==="))

    def test_embed_and_status_run_once_inside_the_index_lock(self):
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        held = [c for c in self.calls() if c.split()[0] in ("embed", "status")]
        self.assertEqual(held, ["embed held", "status held"])
        self.assertNotIn("embed -f", " ".join(self.calls()))

    def test_pending_after_a_clean_embed_is_reported_but_not_a_failure(self):
        self.scenario({"embed": {"out": "✓ Done!\n"}, "status": {"out": STATUS_PENDING}})
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertIn("[qmd-verify] INCOMPLETE", self.log_text)

    def test_unreadable_post_status_fails(self):
        self.scenario({"status": {"out": "no idea", "rc": 0}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIn("[qmd-verify] FAILED - could not verify", self.log_text)
        self.assertIn("failed steps: qmd-verify", self.log_text)

    def test_nonempty_index_without_vectors_fails(self):
        self.scenario({"status": {"out": STATUS_NO_VECTORS}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIn("no vectors after embedding", self.log_text)

    def test_empty_index_and_already_embedded_index_succeed(self):
        self.scenario(
            {
                "embed": {"out": "✓ No non-empty documents to embed.\n"},
                "status": {"out": STATUS_EMPTY},
            }
        )
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertIn("[qmd-verify] ok - empty index", self.log_text)
        self.scenario({"embed": {"out": "✓ All content hashes already have embeddings.\n"}})
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)

    def test_nonzero_embed_exit_fails(self):
        self.scenario({"embed": {"rc": 139, "err": "Segmentation fault\n"}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIn("[qmd-embed] FAILED", self.log_text)
        self.assertIn("exit 139", self.log_text)

    def test_update_failure_skips_embedding(self):
        self.scenario({"update": {"rc": 1, "err": "boom\n"}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIn("[qmd-embed] skipped - qmd-update failed", self.log_text)
        self.assertFalse(any(c.startswith("embed") for c in self.calls()))
        self.assertIn("failed steps: qmd-update", self.log_text)

    def test_embed_timeout(self):
        self.scenario({})
        with mock.patch.object(
            sync, "run_capped", side_effect=subprocess.TimeoutExpired("qmd", 1, output="partial")
        ):
            self.assertEqual(sync.run_embed("qmd", timeout=1), (False, False))
        self.assertIn("[qmd-embed] TIMEOUT after 1s", self.log_text)
        self.assertIn("partial", self.log_text)


class TestRunSummary(SyncFixture):
    def test_failed_source_is_named_and_index_still_runs(self):
        steps = [
            ("broken-source", [sys.executable, "-c", "import sys; sys.exit(2)"]),
            ("good-source", [sys.executable, "-c", "print('exported')"]),
        ]
        with mock.patch.object(sync, "_plan", return_value=steps):
            self.assertEqual(sync.main(["--cron"]), 1)
        text = self.log_text
        self.assertIn("[broken-source] FAILED", text)
        self.assertIn("[good-source] ok", text)
        self.assertIn("[qmd-embed] ok", text)
        self.assertTrue(text.rstrip().endswith("failed steps: broken-source\n=== FAILED ==="))


GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(f'grandchild={child.pid}', flush=True)\n"
    "time.sleep(60)\n"
)


def _gone(pid: int, within: float = 10) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


class TestProcessGroupTimeout(StoreFixture):
    def test_run_step_timeout_kills_the_grandchild(self):
        self.assertFalse(sync.run_step("probe", [sys.executable, "-c", GRANDCHILD], timeout=1))
        log = (self.root / "data/logs/context-sync.log").read_text()
        self.assertIn("[probe] TIMEOUT after 1s", log)
        pid = int(log.split("grandchild=")[1].split()[0])
        self.assertTrue(_gone(pid), f"grandchild {pid} survived the timeout")

    def test_run_capped_returns_output_and_exit_code(self):
        result = procs.run_capped(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
            ],
            timeout=10,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "out\n", "err\n"))

    def test_interrupt_kills_the_group_before_propagating(self):
        seen: list[int] = []

        def interrupted(process, *args, **kwargs):
            seen.append(process.pid)
            raise KeyboardInterrupt

        with (
            mock.patch.object(subprocess.Popen, "communicate", interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            procs.run_capped([sys.executable, "-c", "import time; time.sleep(60)"], timeout=30)
        self.assertTrue(_gone(seen[0]))


if __name__ == "__main__":
    import unittest

    unittest.main()
