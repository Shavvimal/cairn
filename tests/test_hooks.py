"""Durability, process lifetime, policy and event adapter regression tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
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

        launch = Path(__file__).resolve().parents[1] / "hooks/launch.py"
        result = subprocess.run(
            [sys.executable, str(launch), "--harness", "codex"],
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
