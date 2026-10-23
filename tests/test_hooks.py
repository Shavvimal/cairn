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
            # cron always sets HOME; without it Path.home() falls back to the real home.
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": os.path.abspath("src"),
                "HOME": self.env["HOME"],
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({r.source for r in self.records()}, {"claude", "codex"})


class TestAntigravityHook(StoreFixture):
    """agy's ``Stop`` hook: the camelCase payload, the ``{}`` reply contract, and the
    named-hook registration in ``~/.gemini/config/hooks.json``."""

    def stop_payload(self, sid="agy-hook-session"):
        transcript = self.native("antigravity", sid)
        return {
            "conversationId": sid,
            "workspacePaths": [self.cwd],
            "transcriptPath": str(transcript),
            "artifactDirectoryPath": str(transcript.parents[2]),
            "modelName": "gemini-4-argon-high",
            "executionNum": 1,
            "terminationReason": "NO_TOOL_CALL",
            "error": "",
            "fullyIdle": True,
        }

    def test_stop_event_exports_the_conversation(self):
        cfg = config.get_config()
        event = get_harness("antigravity").decode(self.stop_payload())
        self.assertTrue(hooks.allowed(cfg, event))
        coll = cfg.collections["antigravity-sessions"]
        changed = replace(
            cfg,
            collections={
                **cfg.collections,
                "antigravity-sessions": replace(coll, sync=replace(coll.sync, enabled=False)),
            },
        )
        with patch("cairn.hooks.launch") as launch:
            self.assertEqual(
                hooks.handle(changed, "antigravity", self.stop_payload())["state"], "disabled"
            )
            launch.assert_not_called()
        self.fake_qmd()
        hooks.enqueue(cfg, event)
        self.assertEqual(hooks.worker(config.get_config()), 0)
        records = self.records()
        self.assertEqual(
            [(r.source, r.session_id) for r in records], [("antigravity", "agy-hook-session")]
        )
        self.assertEqual(records[0].title, "Orchard agy-hook")
        text = records[0].path.read_text()
        self.assertIn("type: antigravity-session", text)
        self.assertIn("Shared orchard marker", text)

    def test_handle_always_answers_with_an_empty_decision(self):
        self.fake_qmd()
        good = self.stop_payload()
        cases = [
            (json.dumps(good), ""),
            ("not json", "cairn hook:"),
            (json.dumps({"conversationId": ""}), "cairn hook:"),
        ]
        for stdin, stderr_marker in cases:
            with self.subTest(stdin=stdin[:20]):
                result = subprocess.run(
                    [sys.executable, "-m", "cairn", "hook", "handle", "--harness", "antigravity"],
                    input=stdin,
                    env=self.env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "{}")
                if stderr_marker:
                    self.assertIn(stderr_marker, result.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if hooks.status(config.get_config())["counts"]["completed"] == 1:
                break
            time.sleep(0.05)
        self.assertEqual(hooks.status(config.get_config(), "antigravity")["counts"]["completed"], 1)
        self.assertEqual(len(self.records()), 1)

    def test_registration_preserves_foreign_hooks_and_removes_only_its_own(self):
        from cairn.harnesses import antigravity_registration as reg

        path = self.root / "home" / ".gemini" / "config" / "hooks.json"
        self.assertEqual(reg.hooks_file(), path)  # HOME points into the fixture
        foreign = {
            "lint": {
                "PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]
            }
        }
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(foreign))
        message = reg.configure(config.get_config())
        self.assertIn(str(path), message)
        data = json.loads(path.read_text())
        self.assertEqual(data["lint"], foreign["lint"])
        self.assertEqual(data["cairn"]["enabled"], True)
        handlers = data["cairn"]["Stop"]
        self.assertEqual(len(handlers), 1)
        self.assertIn("hook handle --harness antigravity", handlers[0]["command"])
        self.assertIn("CAIRN_CONFIG=", handlers[0]["command"])
        self.assertEqual(handlers[0]["timeout"], reg.HOOK_TIMEOUT_S)
        self.assertEqual(reg.registration()["registered"], True)
        reg.configure(config.get_config())  # idempotent
        self.assertEqual(len(json.loads(path.read_text())["cairn"]["Stop"]), 1)
        # The registered command works from a bare environment and answers {}.
        self.fake_qmd()
        result = subprocess.run(
            ["/bin/sh", "-c", handlers[0]["command"]],
            input=json.dumps(self.stop_payload()),
            env={"PATH": "/usr/bin:/bin", "HOME": self.env["HOME"]},
            capture_output=True,
            text=True,
            cwd=path.parent,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{}")
        self.run_cli("hook", "worker")
        self.assertEqual(hooks.status(config.get_config())["counts"]["completed"], 1)
        # status reports the host side; uninstall removes only the cairn key.
        report = json.loads(self.run_cli("hook", "status", "--harness", "antigravity").stdout)
        self.assertTrue(report["host_hooks"]["registered"])
        reg.configure(None, remove=True)
        data = json.loads(path.read_text())
        self.assertNotIn("cairn", data)
        self.assertEqual(data["lint"], foreign["lint"])
        self.assertFalse(reg.registration()["registered"])
        self.assertFalse(reg.ownership_path().exists())

    def test_grant_commands_writes_allow_and_deny_rules_and_prints_them(self):
        from cairn.harnesses.antigravity import DENY_FILES, TEST_COMMANDS, permission_rules

        settings = self.antigravity / "settings.json"
        settings.write_text(
            json.dumps(
                {"permissions": {"allow": ["command(python)"], "deny": ["command(git push)"]}}
            )
        )
        out = self.run_cli("hook", "install", "--harness", "antigravity", "--grant-commands").stdout
        self.assertIn("Wrote", out)
        self.assertIn("  + allow command(rg)", out)
        self.assertIn("  + deny  command(curl)", out)
        self.assertIn("  + deny  read_file(.env.prod)", out)
        self.assertNotIn("+ deny  command(git push)", out)  # it was there already
        # The user allows python explicitly; the default deny must not reverse that.
        self.assertIn("  - skipped deny command(python): it is in permissions.allow", out)
        self.assertIn("agy -p='/permissions'", out)
        allow, deny = permission_rules(settings)
        self.assertEqual(allow[0], "command(python)")
        self.assertIn("command(rg)", allow)
        self.assertIn("command(uv run pytest)", allow)
        self.assertGreaterEqual(len(allow), len(TEST_COMMANDS))
        self.assertEqual(deny[0], "command(git push)")
        self.assertIn("command(supabase)", deny)
        self.assertIn("command(git reset)", deny)
        self.assertNotIn("command(python)", deny)
        for name in DENY_FILES:
            self.assertIn(f"read_file({name})", deny)
        out = self.run_cli("hook", "install", "--harness", "antigravity", "--grant-commands").stdout
        self.assertIn("already allows and denies", out)
        self.assertIn("skipped deny command(python)", out)
        result = self.run_cli(
            "hook", "install", "--harness", "codex", "--grant-commands", check=False
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("applies to --harness antigravity", result.stderr)

    def test_install_and_uninstall_commands(self):
        from cairn.harnesses import antigravity_registration as reg

        self.assertIn(
            "Registered", self.run_cli("hook", "install", "--harness", "antigravity").stdout
        )
        self.assertTrue(reg.registration()["registered"])
        self.assertIn(
            "Removed", self.run_cli("hook", "uninstall", "--harness", "antigravity").stdout
        )
        self.assertFalse(reg.registration()["registered"])
        self.run_cli("hook", "install", "--harness", "antigravity")
        self.run_cli("hook", "uninstall")  # the bare form removes every owned registration
        self.assertFalse(reg.registration()["registered"])


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
