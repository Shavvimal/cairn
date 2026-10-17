"""Durability, process lifetime, policy and event adapter regression tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cairn import config, hooks
from cairn.harnesses import detect_harness, get_harness
from tests.test_shared_store import StoreFixture


class TestHooks(StoreFixture):
    def event(self, source="codex", sid="hook-session"):
        path = self.native(source, sid)
        return {
            "hook_event_name": "SessionEnd",
            "session_id": sid,
            "cwd": self.cwd,
            "transcript_path": str(path),
            "reason": "other",
        }

    def fake_qmd(self):
        path = self.root / "qmd"
        path.write_text(
            "#!"
            + sys.executable
            + '\nimport sys,time\ntime.sleep(0.04)\nprint("qmd://codex-sessions/\\nqmd://claude-code-sessions/")\n'
        )
        path.chmod(0o755)
        data = json.loads(self.config.read_text())
        data["qmd_binary"] = str(path)
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    def test_payload_validation_and_authoritative_event_identity(self):
        payload = self.event()
        with patch.dict(
            os.environ,
            {"CLAUDE_SESSION_ID": "wrong", "CODEX_THREAD_ID": "stale", "CODEX_SESSION_ID": "stale"},
        ):
            event = get_harness("codex").decode(payload)
            self.assertEqual(event.source, "codex")
            self.assertEqual(event.session_id, payload["session_id"])
            self.assertIsNone(detect_harness())
        bad: object
        for bad in (
            [],
            {},
            {**payload, "session_id": ""},
            {**payload, "hook_event_name": "Stop"},
            {**payload, "transcript_path": 1},
            {**payload, "cwd": "relative"},
        ):
            with self.assertRaises(ValueError):
                get_harness("codex").decode(bad)
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "one", "CODEX_SESSION_ID": "two"}),
            self.assertRaises(ValueError),
        ):
            get_harness("codex").current_id()

    def test_originating_policy_and_explicit_opt_out(self):
        cfg = config.get_config()
        event = get_harness("codex").decode(self.event())
        self.assertTrue(hooks.allowed(cfg, event))
        coll = cfg.collections["codex-sessions"]
        for policy in (replace(coll.sync, enabled=False), replace(coll.sync, on_hook=False)):
            changed = replace(
                cfg, collections={**cfg.collections, "codex-sessions": replace(coll, sync=policy)}
            )
            with patch("cairn.hooks.launch") as launch:
                self.assertEqual(hooks.handle(changed, "codex", self.event())["state"], "disabled")
                launch.assert_not_called()

    def test_failed_launch_retains_durable_work(self):
        with (
            patch("cairn.hooks.launch", side_effect=OSError("cannot spawn")),
            self.assertRaisesRegex(RuntimeError, "request saved"),
        ):
            hooks.handle(config.get_config(), "codex", self.event())
        self.assertEqual(hooks.status(config.get_config())["counts"]["queued"], 1)

    def test_qmd_failure_retry_and_crash_recovery(self):
        event = get_harness("codex").decode(self.event())
        cfg = config.get_config()
        path = hooks.enqueue(cfg, event)
        self.assertEqual(hooks.worker(cfg), 1)
        self.assertEqual(hooks.status(cfg)["counts"]["failed"], 1)
        self.fake_qmd()
        assert path is not None
        request = json.loads(path.read_text())
        request["state"] = "running"  # stale after a crashed worker; flock is released
        path.write_text(json.dumps(request))
        self.assertEqual(hooks.worker(config.get_config()), 0)
        request = json.loads(path.read_text())
        self.assertEqual(request["state"], "completed")
        self.assertEqual(request["attempts"], 2)

    def test_new_generation_during_work_is_not_lost(self):
        cfg = config.get_config()
        event = get_harness("codex").decode(self.event())
        path = hooks.enqueue(cfg, event)
        with patch("cairn.hooks._process", side_effect=lambda *_: hooks.enqueue(cfg, event)):
            self.assertEqual(hooks.worker(cfg), 0)
        assert path is not None
        self.assertEqual(json.loads(path.read_text())["state"], "queued")
        with patch("cairn.hooks._process") as process:
            self.assertEqual(hooks.worker(cfg), 0)
            process.assert_called_once()
        self.assertEqual(json.loads(path.read_text())["state"], "completed")

    def test_failed_requests_stop_retrying_and_can_be_explicitly_retried(self):
        cfg = config.get_config()
        event = get_harness("codex").decode(self.event())
        path = hooks.enqueue(cfg, event)
        assert path is not None
        with patch(
            "cairn.hooks._process", side_effect=RuntimeError("persistent failure")
        ) as process:
            for _ in range(hooks.MAX_ATTEMPTS):
                self.assertEqual(hooks.worker(cfg), 1)
            exhausted = json.loads(path.read_text())
            self.assertEqual(exhausted["state"], "exhausted")
            self.assertEqual(exhausted["attempts"], hooks.MAX_ATTEMPTS)
            self.assertEqual(exhausted["error"], "persistent failure")
            for _ in range(3):
                self.assertEqual(hooks.worker(cfg), 0)
            self.assertEqual(process.call_count, hooks.MAX_ATTEMPTS)
            self.assertEqual(json.loads(path.read_text()), exhausted)
        self.assertEqual(hooks.status(cfg)["counts"]["exhausted"], 1)
        self.fake_qmd()
        self.run_cli("hook", "retry", f"codex:{event.session_id}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            retried = json.loads(path.read_text())
            if retried["state"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(retried["state"], "completed")
        self.assertEqual(retried["attempts"], 1)
        self.assertNotEqual(retried["generation"], exhausted["generation"])

    def test_last_attempt_crash_and_old_failed_requests_are_capped(self):
        cfg = config.get_config()
        for state, attempts in (("running", hooks.MAX_ATTEMPTS), ("failed", 100)):
            with self.subTest(state=state):
                event = get_harness("codex").decode(self.event(sid=state))
                path = hooks.enqueue(cfg, event)
                assert path is not None
                request = json.loads(path.read_text())
                request.update(state=state, attempts=attempts)
                path.write_text(json.dumps(request))
                with patch("cairn.hooks._process") as process:
                    self.assertEqual(hooks.worker(cfg), 1)
                    process.assert_not_called()
                self.assertEqual(json.loads(path.read_text())["state"], "exhausted")

    def test_final_failure_cannot_overwrite_new_generation(self):
        cfg = config.get_config()
        event = get_harness("codex").decode(self.event())
        path = hooks.enqueue(cfg, event)
        assert path is not None
        request = json.loads(path.read_text())
        request["attempts"] = hooks.MAX_ATTEMPTS - 1
        path.write_text(json.dumps(request))

        def newer_request(*_):
            hooks.enqueue(cfg, event)
            raise RuntimeError("older generation failed")

        with patch("cairn.hooks._process", side_effect=newer_request):
            self.assertEqual(hooks.worker(cfg), 1)
        newest = json.loads(path.read_text())
        self.assertEqual(newest["state"], "queued")
        self.assertEqual(newest["attempts"], 0)
        self.assertNotEqual(newest["generation"], request["generation"])

    def test_detached_workers_survive_parent_exit_and_coalesce(self):
        self.fake_qmd()
        payload = self.event()
        started = time.monotonic()
        for _ in range(4):
            result = subprocess.run(
                [sys.executable, "-m", "cairn", "hook", "handle", "--harness", "codex"],
                input=json.dumps(payload),
                env=self.env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout)["state"], "queued")
        self.assertLess(time.monotonic() - started, 3)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if hooks.status(config.get_config())["counts"]["completed"] == 1:
                break
            time.sleep(0.05)
        self.assertEqual(hooks.status(config.get_config())["counts"]["completed"], 1)
        self.assertEqual(len(self.records()), 1)
        # A new enqueue after the worker's last scan still launches a successor.
        payload = self.event(sid="next-session")
        subprocess.run(
            [sys.executable, "-m", "cairn", "hook", "handle", "--harness", "codex"],
            input=json.dumps(payload),
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if hooks.status(config.get_config())["counts"]["completed"] == 2:
                break
            time.sleep(0.05)
        self.assertEqual(hooks.status(config.get_config())["counts"]["completed"], 2)

    def test_limited_path_bootstrap_uses_recorded_engine(self):
        self.fake_qmd()
        self.run_cli("hook", "install")
        from pathlib import Path

        launch = Path(__file__).resolve().parents[1] / "hooks/launch.sh"
        result = subprocess.run(
            ["/bin/sh", str(launch), "--harness", "codex"],
            input=json.dumps(self.event()),
            env={**self.env, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Drain before temporary paths are removed.
        self.run_cli("hook", "worker")
        self.assertEqual(hooks.status(config.get_config())["counts"]["completed"], 1)

    def test_cron_command_retains_config_and_index_environment(self):
        from cairn import admin

        self.fake_qmd()
        self.native("codex", "cron-recovery")
        self.native("claude", "cron-claude")
        with (
            patch("cairn.admin._current_crontab", return_value=["# unrelated"]),
            patch("cairn.admin._cairn_invocation", return_value=[sys.executable, "-m", "cairn"]),
            patch("cairn.admin._write_crontab") as write,
        ):
            self.assertEqual(admin._cron_install(), 0)
        lines = write.call_args.args[0]
        self.assertEqual(lines[0], "# unrelated")
        command = lines[1].split(" ", 5)[5]
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=self.root,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": os.path.abspath("src")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({r.source for r in self.records()}, {"claude", "codex"})


class TestCodexRegistration(StoreFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.codex_env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.codex_env.start()
        self.addCleanup(self.codex_env.stop)

    def test_idempotent_fallback_preserves_unrelated_and_removes_owned_only(self):
        from cairn.harnesses.codex_registration import configure

        unrelated = {"type": "command", "command": "echo other"}
        path = self.home / "hooks.json"
        path.write_text(
            json.dumps(
                {
                    "description": "keep",
                    "hooks": {"SessionEnd": [{"hooks": [unrelated]}], "Stop": []},
                }
            )
        )
        with patch("cairn.harnesses.codex_registration.Codex.inspect_hooks", return_value=[]):
            configure(config.get_config())
            configure(config.get_config())
        data = json.loads(path.read_text())
        self.assertEqual(len(data["hooks"]["SessionEnd"]), 2)
        self.assertEqual(data["description"], "keep")
        configure(None, remove=True)
        data = json.loads(path.read_text())
        self.assertEqual(data["hooks"]["SessionEnd"], [{"hooks": [unrelated]}])
        self.assertIn("Stop", data["hooks"])

    def test_bundled_discovery_removes_previous_fallback(self):
        from cairn.harnesses.codex_registration import configure

        with patch("cairn.harnesses.codex_registration.Codex.inspect_hooks", return_value=[]):
            configure(config.get_config())
        with patch(
            "cairn.harnesses.codex_registration.Codex.inspect_hooks",
            return_value=[{"pluginId": "cairn@cairn", "eventName": "sessionEnd"}],
        ):
            message = configure(config.get_config())
        self.assertIn("no fallback", message)
        self.assertEqual(
            json.loads((self.home / "hooks.json").read_text())["hooks"]["SessionEnd"], []
        )

    def test_failed_inspection_does_not_mutate_hooks(self):
        from cairn.harnesses.codex_registration import configure

        with (
            patch(
                "cairn.harnesses.codex_registration.Codex.inspect_hooks",
                side_effect=RuntimeError("unavailable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            configure(config.get_config())
        self.assertFalse((self.home / "hooks.json").exists())

    def test_disable_and_removal_guard(self):
        from cairn.harnesses.codex import Codex

        cache = self.home / "plugins/cache/cairn/cairn/2.4.0/.codex-plugin"
        cache.mkdir(parents=True)
        (cache / "plugin.json").write_text("{}")
        settings = self.home / "config.toml"
        settings.write_text('[plugins."cairn@cairn"]\nenabled = true\n')
        self.assertTrue(Codex().plugin_enabled(self.cwd))
        settings.write_text('[plugins."cairn@cairn"]\nenabled = false\n')
        self.assertFalse(Codex().plugin_enabled(self.cwd))
        settings.write_text('[plugins."cairn@cairn"]\nenabled = true\n')
        (cache / "plugin.json").unlink()
        self.assertFalse(Codex().plugin_enabled(self.cwd))


class TestLauncher(StoreFixture):
    """hooks/launch.sh must find an engine in every supported install state."""

    launch = str(Path(__file__).resolve().parents[1] / "hooks/launch.sh")

    def event(self):
        return {
            "hook_event_name": "SessionEnd",
            "session_id": "launcher-session",
            "cwd": self.cwd,
            "transcript_path": None,
            "reason": "other",
        }

    def fake_cairn(self, script: str) -> str:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        path = bin_dir / "cairn"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return str(bin_dir)

    def run_launcher(self, bin_dir: str | None):
        env = {**self.env, "PATH": (bin_dir + ":" if bin_dir else "") + "/usr/bin:/bin"}
        env.pop("CAIRN_PYTHON", None)
        return subprocess.run(
            ["/bin/sh", self.launch, "--harness", "codex"],
            input=json.dumps(self.event()),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_falls_back_to_cairn_on_path_without_launcher_file(self):
        log = self.root / "fake.log"
        bin_dir = self.fake_cairn(f'printf "%s\\n" "$@" > {log}\ncat > /dev/null\nexit 0\n')
        result = self.run_launcher(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().split(), ["hook", "handle", "--harness", "codex"])

    def test_degrades_to_legacy_sync_for_an_older_engine(self):
        marker = self.root / "legacy-sync-ran"
        bin_dir = self.fake_cairn(
            'if [ "$1" = hook ]; then\n'
            "  echo \"cairn: unknown command 'hook'.\" >&2\n"
            "  exit 2\n"
            "fi\n"
            f'[ "$1" = sync ] && [ "$2" = --hook ] && touch {marker}\n'
            "exit 0\n"
        )
        result = self.run_launcher(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("older than the plugin", result.stderr)
        self.assertIn("uv tool upgrade cairn", result.stderr)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "legacy `cairn sync --hook` was not started")

    def test_passes_other_engine_failures_through(self):
        bin_dir = self.fake_cairn('echo "Error: bad payload" >&2\nexit 1\n')
        result = self.run_launcher(bin_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad payload", result.stderr)

    def test_reports_missing_engine(self):
        result = self.run_launcher(None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no cairn engine found", result.stderr)
        self.assertIn("cairn hook install", result.stderr)

    def test_recorded_launcher_is_posix_sh_and_exports_environment(self):
        self.run_cli("hook", "install")
        text = hooks.launcher_path().read_text()
        self.assertEqual((Path.home() / ".config/cairn/hook-launcher.env").read_text(), text)
        for key in ("CAIRN_PYTHON=", "CAIRN_CONFIG=", "CAIRN_PATH="):
            self.assertIn(key, text)
        probe = subprocess.run(
            ["/bin/sh", "-c", f'. "{hooks.launcher_path()}"; echo "$CAIRN_CONFIG"'],
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.stdout.strip(), str(self.config.resolve()))

    def test_uninstall_removes_both_launcher_copies(self):
        self.run_cli("hook", "install")
        paths = hooks.launcher_paths()
        self.assertEqual(len(paths), 2)
        self.run_cli("hook", "uninstall")
        self.assertTrue(all(not path.exists() for path in paths))


class TestRetentionAndStatus(StoreFixture):
    def write_request(self, name: str, **fields) -> Path:
        cfg = config.get_config()
        hooks.job_dir(cfg).mkdir(parents=True, exist_ok=True)
        path = hooks.job_dir(cfg) / f"{name}.json"
        request = {
            "event": {
                "harness": "codex",
                "source": "codex",
                "session_id": name,
                "transcript_path": None,
                "cwd": self.cwd,
                "event": "SessionEnd",
            },
            "generation": name,
            "attempts": 1,
            **fields,
        }
        path.write_text(json.dumps(request))
        return path

    def test_prune_uses_terminal_timestamps_only(self):
        now = time.time()
        week = 7 * 24 * 3600
        old_done = self.write_request(
            "old-done", state="completed", queued_at=now - 9 * 86400, completed_at=now - 8 * 86400
        )
        fresh_done = self.write_request(
            "fresh-done", state="completed", queued_at=now - 30 * 86400, completed_at=now
        )
        old_failed = self.write_request("old-failed", state="failed", queued_at=now - 3 * week)
        old_disabled = self.write_request(
            "old-disabled", state="disabled", queued_at=now - 3 * week, disabled_at=now - 2 * week
        )
        old_exhausted = self.write_request(
            "old-exhausted",
            state="exhausted",
            queued_at=now - 3 * week,
            exhausted_at=now - 2 * week,
            error="failed three times",
        )
        fresh_exhausted = self.write_request(
            "fresh-exhausted",
            state="exhausted",
            queued_at=now - 3 * week,
            exhausted_at=now,
            error="just exhausted",
        )
        self.assertEqual(hooks.prune(config.get_config(), now=now), 3)
        self.assertFalse(old_done.exists())
        self.assertFalse(old_disabled.exists())
        self.assertTrue(fresh_done.exists())  # completed today, however old its queue entry
        self.assertTrue(old_failed.exists())  # attempts remain
        self.assertFalse(old_exhausted.exists())
        self.assertTrue(fresh_exhausted.exists())

    def test_status_shows_problems_and_recent_completed_only(self):
        now = time.time()
        for n in range(12):
            self.write_request(
                f"done-{n:02}", state="completed", queued_at=now, completed_at=now + n
            )
        self.write_request("broken", state="failed", queued_at=now, error="boom")
        report = hooks.compact_status(hooks.status(config.get_config()), show_all=False)
        states = [r["state"] for r in report["requests"]]
        self.assertEqual(states.count("failed"), 1)
        self.assertEqual(states.count("completed"), hooks.RECENT_COMPLETED)
        self.assertEqual(report["hidden_completed"], 2)
        newest = [r["event"]["session_id"] for r in report["requests"] if r["state"] == "completed"]
        self.assertEqual(newest[0], "done-11")
        everything = hooks.compact_status(hooks.status(config.get_config()), show_all=True)
        self.assertEqual(len(everything["requests"]), 13)
        self.assertEqual(hooks.iso(0), "1970-01-01T00:00:00+00:00")
