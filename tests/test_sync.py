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
spawn = spec.get("spawn_sync")
if spawn and not (spec.get("once") and (here / "spawned").exists()):
    # A second `cairn sync` arriving while this one holds the sync lock.
    import subprocess
    (here / "spawned").touch()
    subprocess.run([sys.executable, "-m", "cairn", "sync", spawn], check=True)
if command == "collection list":
    print("qmd://codex-sessions/\\nqmd://claude-code-sessions/\\nqmd://antigravity-sessions/")
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

    def count(self, command: str) -> int:
        return sum(1 for c in self.calls() if c.split()[0] == command)


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
        # The skipped mode is recorded durably for the lock holder.
        self.assertTrue((cfg.data_root / "sync-requests/cron").is_file())

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
            "[qmd-embed] exited 0",
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
        self.assertIn("failed steps: qmd-embed, qmd-verify (incomplete)\n=== FAILED ===", text)
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

    def test_pending_after_a_clean_embed_is_incomplete_and_fails(self):
        self.scenario({"embed": {"out": "✓ Done!\n"}, "status": {"out": STATUS_PENDING}})
        self.assertEqual(self.sync_cron().returncode, 1)
        text = self.log_text
        self.assertIn("[qmd-embed] exited 0", text)
        self.assertIn("[qmd-verify] INCOMPLETE - 4 documents, 9 vectors, 2 pending", text)
        self.assertNotIn("corrupt", text)
        self.assertIn("failed steps: qmd-verify (incomplete)\n=== FAILED ===", text)

    def test_clean_exit_is_never_logged_ok_before_verification(self):
        self.scenario({"status": {"out": "Documents\n  Total:    4 files indexed\n"}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertNotIn("[qmd-embed] ok", self.log_text)
        self.assertIn("[qmd-embed] exited 0", self.log_text)
        self.assertIn("[qmd-verify] FAILED", self.log_text)

    def test_malformed_pending_line_is_unreadable_not_complete(self):
        self.scenario(
            {"status": {"out": STATUS_COMPLETE + "  Pending:  8 documents need embedding\n"}}
        )
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIn("[qmd-verify] FAILED - could not verify", self.log_text)

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
        timeout = subprocess.TimeoutExpired("qmd", 1, output="partial out", stderr=b"partial err")
        with mock.patch.object(sync, "run_capped", side_effect=timeout):
            self.assertEqual(sync.run_embed("qmd", timeout=1), (False, False))
        self.assertIn("[qmd-embed] TIMEOUT after 1s", self.log_text)
        self.assertIn("partial out", self.log_text)
        self.assertIn("[stderr] partial err", self.log_text)

    def test_cleanup_incomplete_is_logged_not_raised(self):
        error = procs.ProcessCleanupError("cleanup incomplete: pid 1 was not reaped")
        with mock.patch.object(sync, "run_capped", side_effect=error):
            self.assertEqual(sync.run_embed("qmd"), (False, False))
            self.assertFalse(sync.run_step("probe", ["qmd"]))
        self.assertIn("[qmd-embed] ERROR: cleanup incomplete", self.log_text)
        self.assertIn("[probe] ERROR: cleanup incomplete", self.log_text)


class TestCoalescedRequests(SyncFixture):
    def requests(self) -> list[str]:
        folder = self.root / "data/sync-requests"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def test_cron_arriving_during_a_hook_sync_gets_its_full_stages(self):
        self.scenario({"update": {"spawn_sync": "--cron", "once": True}})
        result = self.run_cli("sync", "--hook", check=False)
        self.assertEqual(result.returncode, 0, self.log_text)
        text = self.log_text
        skip = text.index("=== context-sync (cron)")
        coalesced = text.index("[coalesced] a skipped 'cron' sync arrived")
        self.assertLess(skip, coalesced)
        self.assertLess(text.index("[qmd-update] ok"), coalesced)
        self.assertLess(coalesced, text.index("[qmd-embed] exited 0"))
        self.assertLess(text.index("[qmd-verify] ok"), text.index("=== done ==="))
        self.assertEqual(self.count("embed"), 1)
        self.assertEqual(self.count("update"), 2)  # hook pass + follow-up pass
        self.assertEqual(self.requests(), [])

    def test_a_request_recorded_before_the_run_is_still_serviced(self):
        sync.request_sync(config.get_config(), "cron")
        self.assertEqual(self.run_cli("sync", "--hook", check=False).returncode, 0, self.log_text)
        self.assertEqual(self.count("embed"), 1)
        self.assertEqual(self.requests(), [])

    def test_requests_during_the_follow_up_wait_for_the_next_invocation(self):
        # Every update spawns another skipped --cron: one follow-up pass, no loop.
        self.scenario({"update": {"spawn_sync": "--cron"}})
        self.assertEqual(self.run_cli("sync", "--hook", check=False).returncode, 0, self.log_text)
        self.assertEqual(self.count("embed"), 1)
        self.assertEqual(self.requests(), ["cron"])  # left for the next invocation
        self.assertEqual(self.run_cli("sync", "--hook", check=False).returncode, 0, self.log_text)
        self.assertEqual(self.count("embed"), 2)

    def test_full_run_answers_a_full_request_with_a_worker_retry_only(self):
        self.scenario({"embed": {"spawn_sync": "--all", "once": True}})
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertIn(
            "[coalesced] a skipped 'all' sync arrived; retrying hook requests", self.log_text
        )
        self.assertEqual(self.count("embed"), 1)

    def test_narrower_request_is_consumed(self):
        self.scenario({"embed": {"spawn_sync": "--hook", "once": True}})
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        self.assertIn(
            "[coalesced] a skipped 'hook' sync arrived; this run covered it", self.log_text
        )
        self.assertEqual(self.requests(), [])


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
        self.assertIn("[qmd-verify] ok", text)
        self.assertTrue(text.rstrip().endswith("failed steps: broken-source\n=== FAILED ==="))


class TestIndexStamp(SyncFixture):
    def stamp(self) -> dict | None:
        path = self.root / "state/cairn/qmd-index.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_stamp_written_after_successful_update(self):
        self.native("codex", "stamped")
        before = time.time_ns()
        self.assertEqual(self.sync_cron().returncode, 0, self.log_text)
        stamp = self.stamp()
        assert stamp is not None
        self.assertEqual(stamp["version"], 1)
        self.assertGreaterEqual(stamp["started_ns"], before)
        self.assertLess(stamp["started_ns"], time.time_ns())
        self.assertEqual(stamp["database"], str((self.root / "cache/qmd/index.sqlite").resolve()))
        self.assertIn("codex-sessions", stamp["collections"])

    def test_no_stamp_after_failed_update(self):
        self.scenario({"update": {"rc": 1, "err": "boom\n"}})
        self.assertEqual(self.sync_cron().returncode, 1)
        self.assertIsNone(self.stamp())


# Writes the grandchild pid to a file before anything else (readiness handshake).
GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    """True while ``pid`` runs; a zombie (state Z) counts as gone."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


def _gone(pid: int, within: float = 10) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _wait_for(path: Path, within: float = 10) -> str:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


class TestProcessGroupTimeout(StoreFixture):
    def test_run_step_timeout_kills_the_grandchild(self):
        pid_file = self.root / "grandchild.pid"
        # 5 s leaves room for a slow interpreter start; the pid file proves readiness.
        ok = sync.run_step("probe", [sys.executable, "-c", GRANDCHILD, str(pid_file)], timeout=5)
        self.assertFalse(ok)
        log = (self.root / "data/logs/context-sync.log").read_text()
        self.assertIn("[probe] TIMEOUT after 5s", log)
        pid = int(_wait_for(pid_file))
        self.assertTrue(_gone(pid), f"grandchild {pid} survived the timeout")

    def test_escaped_grandchild_holding_the_pipes_keeps_partial_output(self):
        pid_file = self.root / "escaped.pid"
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
            " start_new_session=True)\n"  # setsid: outside the killed group, holds the pipes
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "print('partial stdout', flush=True)\n"
            "print('partial stderr', file=sys.stderr, flush=True)\n"
            "time.sleep(30)\n"
        )
        try:
            with (
                mock.patch.object(procs, "KILL_GRACE_S", 0.5),
                self.assertRaises(subprocess.TimeoutExpired) as caught,
            ):
                procs.run_capped([sys.executable, "-c", script, str(pid_file)], timeout=3)
            self.assertIn("partial stdout", str(caught.exception.stdout))
            self.assertIn("partial stderr", str(caught.exception.stderr))
        finally:
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), 9)

    def test_finish_reports_cleanup_incomplete_instead_of_waiting_forever(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with (
                mock.patch.object(process, "wait", side_effect=subprocess.TimeoutExpired("x", 0.1)),
                self.assertRaisesRegex(procs.ProcessCleanupError, "cleanup incomplete"),
            ):
                procs.finish(process, grace_s=0.1)
        finally:
            process.kill()
            process.wait()

    def test_run_capped_surfaces_cleanup_incomplete(self):
        error = procs.ProcessCleanupError("cleanup incomplete: pid 1")
        with (
            mock.patch.object(procs, "finish", side_effect=error),
            self.assertRaises(procs.ProcessCleanupError),
        ):
            procs.run_capped([sys.executable, "-c", "pass"], timeout=10)

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
