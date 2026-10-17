"""Background environment fidelity, resource locking, and failed-job recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from cairn import config, hooks, qmd
from cairn.harnesses import get_harness
from cairn.harnesses.codex_registration import configure
from cairn.runtime import background_environment
from cairn.storage import lock_path, locked
from tests.test_shared_store import StoreFixture


class TestBackgroundEnvironment(StoreFixture):
    def test_installed_hooks_restore_setup_environment(self):
        probe = self.root / "qmd"
        log = self.root / "environment.jsonl"
        probe.write_text(
            f"#!{sys.executable}\nimport json,os\n"
            "keys = ('CAIRN_CONFIG', 'PATH', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', "
            "'QMD_CONFIG_DIR', 'INDEX_PATH', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR')\n"
            f"with open({str(log)!r}, 'a') as f: f.write(json.dumps({{k: os.environ.get(k) for k in keys}}) + '\\n')\n"
            "print('qmd://codex-sessions/')\n"
        )
        probe.chmod(0o755)
        data = json.loads(self.config.read_text())
        data["qmd_binary"] = str(probe)
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        home = self.root / "codex-home"
        manifest = home / "plugins/cache/cairn/cairn/2.4.0/.codex-plugin/plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
        (home / "config.toml").write_text('[plugins."cairn@cairn"]\nenabled = true\n')
        setup = {
            **self.env,
            "CODEX_HOME": str(home),
            "XDG_CACHE_HOME": str(self.root / "selected cache"),
            "QMD_CONFIG_DIR": str(self.root / "selected 'qmd'"),
            "INDEX_PATH": str(self.root / "selected database/index.sqlite"),
        }
        with (
            patch.dict(os.environ, setup),
            patch("cairn.harnesses.codex_registration.Codex.inspect_hooks", return_value=[]),
        ):
            expected = background_environment(config.get_config())
            hooks.install_launcher(config.get_config())
            configure(config.get_config())
        fallback = json.loads((home / "hooks.json").read_text())["hooks"]["SessionEnd"][0]["hooks"][
            0
        ]["command"]
        launcher = str(Path(__file__).resolve().parents[1] / "hooks/launch.sh")
        # Both entry points must recover the setup environment even when the host
        # changes or drops XDG_CONFIG_HOME before discovering the recorded settings.
        runtime = {
            **self.env,
            "CODEX_HOME": str(self.root / "wrong-codex"),
            "QMD_CONFIG_DIR": str(self.root / "wrong-qmd"),
            "XDG_CONFIG_HOME": str(self.root / "wrong-config"),
        }
        for name, command in (
            ("bundled", ["/bin/sh", launcher, "--harness", "codex"]),
            ("bundled-no-xdg", ["/bin/sh", launcher, "--harness", "codex"]),
            ("fallback", ["/bin/sh", "-c", fallback]),
        ):
            with self.subTest(hook=name):
                native = self.native("codex", name)
                payload = {
                    "hook_event_name": "SessionEnd",
                    "session_id": name,
                    "cwd": self.cwd,
                    "transcript_path": str(native),
                }
                log.unlink(missing_ok=True)
                host_env = {**runtime}
                if name == "bundled-no-xdg":
                    host_env.pop("XDG_CONFIG_HOME")
                subprocess.run(
                    command,
                    input=json.dumps(payload),
                    env=host_env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                deadline = time.monotonic() + 10
                request: dict = {}
                while time.monotonic() < deadline:
                    requests = hooks.status(config.get_config())["requests"]
                    request = next((r for r in requests if r["event"]["session_id"] == name), {})
                    if request.get("state") in ("completed", "failed"):
                        break
                    time.sleep(0.05)
                self.assertEqual(request.get("state"), "completed", request)
                observed = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertTrue(observed)
                for environment in observed:
                    for key, value in expected.items():
                        self.assertEqual(environment[key], value, key)
                self.assertEqual(hooks.status(config.get_config())["counts"]["failed"], 0)

    def test_defaults_are_pinned_and_relative_paths_resolve_before_detach(self):
        with patch.dict(os.environ):
            for key in ("QMD_CONFIG_DIR", "INDEX_PATH"):
                os.environ.pop(key, None)
            expected = background_environment(config.get_config())
            self.assertEqual(expected["QMD_CONFIG_DIR"], str((self.root / "xdg/qmd").resolve()))
            self.assertEqual(
                expected["INDEX_PATH"], str((self.root / "cache/qmd/index.sqlite").resolve())
            )
            os.environ["QMD_CONFIG_DIR"] = "relative-qmd"
            os.environ["INDEX_PATH"] = "relative-index.sqlite"
            relative = background_environment(config.get_config())
            self.assertEqual(relative["QMD_CONFIG_DIR"], os.path.abspath("relative-qmd"))
            self.assertEqual(relative["INDEX_PATH"], os.path.abspath("relative-index.sqlite"))

    def test_recorded_config_directory_preserves_a_symlinked_config_file(self):
        directory = self.root / "xdg/qmd"
        directory.mkdir(parents=True)
        target = self.root / "tracked-settings.yml"
        target.write_text("collections: {}\n")
        (directory / "index.yml").symlink_to(target)
        environment = background_environment(config.get_config())
        self.assertEqual(
            (Path(environment["QMD_CONFIG_DIR"]) / "index.yml").resolve(), target.resolve()
        )


class TestIndexLocks(StoreFixture):
    def test_shared_resources_contend_across_processes(self):
        probe = (
            "import sys\nfrom cairn.qmd import locked_index\n"
            "try:\n with locked_index(blocking=False): pass\n"
            "except BlockingIOError: sys.exit(42)\n"
        )
        alias = self.root / "alias-cache"
        (self.root / "cache").mkdir()
        alias.symlink_to(self.root / "cache", target_is_directory=True)
        default = {k: v for k, v in self.env.items() if k not in ("QMD_CONFIG_DIR", "INDEX_PATH")}
        cases = [
            ("explicit defaults", default, 42),
            (
                "same config, different database",
                {**default, "XDG_CACHE_HOME": str(self.root / "cache2")},
                42,
            ),
            (
                "same database, different config",
                {**default, "QMD_CONFIG_DIR": str(self.root / "qmd2")},
                42,
            ),
            (
                "database symlink",
                {
                    **default,
                    "QMD_CONFIG_DIR": str(self.root / "qmd2"),
                    "XDG_CACHE_HOME": str(alias),
                },
                42,
            ),
            (
                "independent resources",
                {
                    **default,
                    "QMD_CONFIG_DIR": str(self.root / "qmd2"),
                    "XDG_CACHE_HOME": str(self.root / "cache2"),
                },
                0,
            ),
        ]
        with qmd.locked_index():
            for name, env, expected in cases:
                with self.subTest(case=name):
                    result = subprocess.run(
                        [sys.executable, "-c", probe],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
        # Failed acquisition of the second lock must release the first.
        with patch.dict(os.environ, cases[-1][1]), qmd.locked_index(blocking=False):
            pass


class TestCollectionTimeouts(StoreFixture):
    def test_list_and_add_timeout_release_locks_and_allow_retry(self):
        probe = self.root / "qmd"
        data = json.loads(self.config.read_text())
        data["qmd_binary"] = str(probe)
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        cfg = config.get_config()
        for operation in ("list", "add"):
            with self.subTest(operation=operation):
                native = self.native("codex", operation)
                event = get_harness("codex").decode(
                    {
                        "hook_event_name": "SessionEnd",
                        "session_id": operation,
                        "cwd": self.cwd,
                        "transcript_path": str(native),
                    }
                )
                path = hooks.enqueue(cfg, event)
                assert path is not None
                probe.write_text(
                    f"#!{sys.executable}\nimport sys,time\n"
                    f"if sys.argv[1:3] == ['collection', {operation!r}]: time.sleep(2)\n"
                )
                probe.chmod(0o755)
                with patch("cairn.qmd.COLLECTION_TIMEOUT", 0.25):
                    self.assertEqual(hooks.worker(cfg), 1)
                request = json.loads(path.read_text())
                self.assertEqual(request["state"], "failed")
                self.assertIn(f"collection {operation} timed out", request["error"])
                with (
                    qmd.locked_index(blocking=False),
                    locked(lock_path(hooks.job_dir(cfg), "worker"), blocking=False),
                ):
                    pass
                probe.write_text(f"#!{sys.executable}\nprint('qmd://codex-sessions/')\n")
                self.assertEqual(hooks.worker(cfg), 0)
                self.assertEqual(json.loads(path.read_text())["state"], "completed")
                self.assertEqual(json.loads(path.read_text())["attempts"], 2)
