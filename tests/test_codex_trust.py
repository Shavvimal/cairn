"""Codex hook trust written by ``cairn hook install --harness codex``.

Every test runs against a temporary CODEX_HOME. ``inspect_hooks`` (the Codex
app-server) and ``codex --version`` are patched, so no Codex process starts.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cairn import config, hooks
from cairn.harnesses import codex_registration as registration
from cairn.harnesses import codex_trust as trust
from tests.test_shared_store import StoreFixture

CAIRN_COMMAND = (
    "/usr/bin/env CAIRN_CONFIG=/tmp/cairn/config.json /tmp/venv/bin/python -m cairn hook "
    "handle --harness codex --require-plugin"
)
KNOWN_CAIRN_HASH = "sha256:0ec9f818c3b060ec3263662e9ba07558e1008dc000e285af0dcba792bac89743"
KNOWN_STOP_HASH = "sha256:f2aa169c7a1cf9b7cc9b61ce5ecec0315e0f7d13ac35935b1aa74440f9a839f8"
INSPECT = "cairn.harnesses.codex_registration.Codex.inspect_hooks"
VERSION = "cairn.harnesses.codex_trust.codex_version"


def cairn_handler(command: str = CAIRN_COMMAND) -> dict:
    return {
        "type": "command",
        "command": command,
        "timeout": 3,
        "statusMessage": "Cairn automatic session sync",
    }


class TestTrustHash(StoreFixture):
    def test_trusted_hash_known_answers(self):
        self.assertEqual(trust.trusted_hash("SessionEnd", cairn_handler()), KNOWN_CAIRN_HASH)
        stop = {"type": "command", "command": "echo hi"}
        self.assertEqual(trust.trusted_hash("Stop", stop), KNOWN_STOP_HASH)
        self.assertEqual(trust.snake_case("UserPromptSubmit"), "user_prompt_submit")

    def test_hash_includes_status_message_and_default_timeout(self):
        handler = cairn_handler()
        without_message = {k: v for k, v in handler.items() if k != "statusMessage"}
        self.assertNotEqual(trust.trusted_hash("SessionEnd", without_message), KNOWN_CAIRN_HASH)
        stop = {"type": "command", "command": "echo hi"}
        self.assertEqual(trust.trusted_hash("Stop", {**stop, "timeout": 600}), KNOWN_STOP_HASH)
        self.assertNotEqual(trust.trusted_hash("Stop", {**stop, "timeout": 3}), KNOWN_STOP_HASH)

    def test_version_range_accepts_verified_minor_only(self):
        for version in ("0.165.0", "0.165.9"):
            self.assertTrue(trust.in_range(version), version)
        outside: tuple[str | None, ...] = ("0.164.9", "0.166.0", "1.165.0", "garbage", "", None)
        for candidate in outside:
            self.assertFalse(trust.in_range(candidate), candidate)
        self.assertEqual(trust.range_text(), ">= 0.165.0, < 0.166.0")

    def test_version_probe_reports_each_failure(self):
        probe = "cairn.harnesses.codex_trust.run_capped"
        done = subprocess.CompletedProcess
        cases = [
            (FileNotFoundError(), (None, "codex is not on PATH")),
            (
                subprocess.TimeoutExpired(["codex"], 5),
                (None, "codex --version did not answer in 5s"),
            ),
            (done([], 2, "", ""), (None, "codex --version exited 2")),
            (
                done([], 0, "codex-cli dev\n", ""),
                (None, "unrecognised codex --version output 'codex-cli dev'"),
            ),
            (done([], 0, "codex-cli 0.165.0\n", ""), ("0.165.0", "")),
        ]
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                kwargs = (
                    {"side_effect": outcome}
                    if isinstance(outcome, BaseException)
                    else {"return_value": outcome}
                )
                with patch(probe, **kwargs):
                    self.assertEqual(trust.codex_version(), expected)


class TrustFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.hooks_path = self.home.resolve() / "hooks.json"
        self.toml = self.home / "config.toml"
        self.receipt = self.home.resolve() / registration.RECEIPT_NAME
        self.key = f"{self.hooks_path}:session_end:0:0"
        self.inspect = patch(INSPECT, return_value=[])
        self.inspect_mock = self.inspect.start()
        self.addCleanup(self.inspect.stop)
        self.version = patch(VERSION, return_value=("0.165.0", ""))
        self.version_mock = self.version.start()
        self.addCleanup(self.version.stop)

    def run_configure(self, **kwargs) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            message = registration.configure(
                None if kwargs.get("remove") else config.get_config(), **kwargs
            )
        return message + out.getvalue(), err.getvalue()

    def command(self) -> str:
        return registration._command(config.get_config())

    def expected_hash(self) -> str:
        return trust.trusted_hash("SessionEnd", cairn_handler(self.command()))

    def state(self) -> dict:
        return tomllib.loads(self.toml.read_text()).get("hooks", {}).get("state", {})

    def receipt_data(self) -> dict:
        return json.loads(self.receipt.read_text())


class TestInstall(TrustFixture):
    def test_install_writes_group_then_table_then_receipt(self):
        order: list[str] = []
        write_json, write_config = trust.write_json, trust.write_config
        write_receipt = registration._write_receipt

        def on_json(path, text):
            order.append("hooks.json")
            write_json(path, text)

        def on_config(snapshot, text):
            order.append("config.toml")
            write_config(snapshot, text)

        def on_receipt(state, receipt):
            order.append(f"receipt:{receipt['status']}")
            write_receipt(state, receipt)

        with (
            patch.object(trust, "write_json", on_json),
            patch.object(trust, "write_config", on_config),
            patch.object(registration, "_write_receipt", on_receipt),
        ):
            message, _ = self.run_configure()
        self.assertEqual(
            order, ["receipt:prepared", "hooks.json", "config.toml", "receipt:installed"]
        )
        self.assertEqual(
            message,
            f"Registered Cairn fallback in {self.hooks_path} (SessionEnd group 0) and trusted it "
            f"in {self.home.resolve() / 'config.toml'} for Codex 0.165.0. Restart Codex.",
        )
        self.assertEqual(self.state(), {self.key: {"trusted_hash": self.expected_hash()}})
        receipt = self.receipt_data()
        self.assertEqual(receipt["receipt_version"], 2)
        self.assertEqual(receipt["status"], "installed")
        self.assertEqual(receipt["commands"], [self.command()])
        kinds = [(e["kind"], e["pending"]) for e in receipt["entries"]]
        self.assertEqual(kinds, [("hooks_json_group", False), ("config_toml_table", False)])
        table = receipt["entries"][1]
        self.assertEqual(table["origin"], "written")
        self.assertEqual(
            table["sha256"], trust.canonical_sha({"trusted_hash": table["trusted_hash"]})
        )
        self.assertEqual(self.toml.stat().st_mode & 0o777, 0o600)

    def test_install_is_idempotent_and_keeps_index(self):
        self.run_configure()
        before = {p: p.read_bytes() for p in (self.hooks_path, self.toml, self.receipt)}
        message, _ = self.run_configure()
        self.assertEqual(
            message,
            f"Cairn fallback in {self.hooks_path} is already registered and trusted; nothing changed.",
        )
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_reinstall_with_new_command_replaces_in_place(self):
        with patch.object(registration, "_command", return_value="echo cairn-a"):
            self.run_configure()
        data = json.loads(self.hooks_path.read_text())
        foreign = {"type": "command", "command": "echo foreign"}
        data["hooks"]["SessionEnd"].append({"hooks": [foreign]})
        self.hooks_path.write_text(json.dumps(data))
        foreign_key = f"{self.hooks_path}:session_end:1:0"
        foreign_table = f'\n[hooks.state."{foreign_key}"]\ntrusted_hash = "sha256:feed"\n'
        self.toml.write_text(self.toml.read_text() + foreign_table)
        with patch.object(registration, "_command", return_value="echo cairn-b"):
            self.run_configure()
        groups = json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"]
        self.assertEqual(groups[0]["hooks"][0]["command"], "echo cairn-b")
        self.assertEqual(groups[1], {"hooks": [foreign]})
        state = self.state()
        self.assertEqual(state[foreign_key], {"trusted_hash": "sha256:feed"})
        self.assertEqual(
            state[self.key]["trusted_hash"],
            trust.trusted_hash("SessionEnd", cairn_handler("echo cairn-b")),
        )
        self.assertEqual(self.toml.read_text().count(trust.table_header(self.key)), 1)
        self.assertEqual(self.receipt_data()["commands"], ["echo cairn-b"])

    def test_out_of_range_version_writes_no_trust(self):
        self.version_mock.return_value = ("0.166.0", "")
        message, _ = self.run_configure()
        self.assertIn(
            "Codex 0.166.0 is outside the versions cairn verified the trust hash on "
            "(>= 0.165.0, < 0.166.0); trust not written. "
            "Review/trust the Cairn SessionEnd hook in /hooks.",
            message,
        )
        self.assertTrue(self.hooks_path.exists())
        self.assertFalse(self.toml.exists())
        self.version_mock.return_value = (None, "codex is not on PATH")
        message, _ = self.run_configure()
        self.assertIn(
            "Could not read the Codex version (codex is not on PATH); trust not written.", message
        )

    def test_no_trust_flag(self):
        message, _ = self.run_configure(trust_hook=False)
        self.assertIn(
            "--no-trust: trust not written. Review/trust the Cairn SessionEnd hook in /hooks.",
            message,
        )
        self.version_mock.assert_not_called()
        self.assertFalse(self.toml.exists())
        # A later --no-trust removes the table an earlier install wrote.
        self.run_configure()
        self.assertIn(self.key, self.state())
        self.run_configure(trust_hook=False)
        self.assertNotIn(self.key, self.state())

    def test_non_ascii_command_writes_no_trust(self):
        with patch.object(registration, "_command", return_value="echo caírn"):
            message, _ = self.run_configure()
        self.assertIn("non-ASCII character", message)
        self.assertFalse(self.toml.exists())

    def test_table_with_enabled_false_is_left_alone(self):
        original = f'[hooks.state."{self.key}"]\ntrusted_hash = "sha256:old"\nenabled = false\n'
        self.toml.write_text(original)
        message, warning = self.run_configure()
        self.assertEqual(self.toml.read_text(), original)
        self.assertIn("has keys cairn did not write (enabled); left unchanged.", warning)
        self.assertIn("Review/trust the Cairn SessionEnd hook in /hooks.", message)
        tables = [e for e in self.receipt_data()["entries"] if e["kind"] == "config_toml_table"]
        self.assertEqual(tables, [])

    def test_adopts_identical_existing_table(self):
        original = f'[hooks.state."{self.key}"]\ntrusted_hash = "{self.expected_hash()}"\n'
        self.toml.write_text(original)
        self.run_configure()
        self.assertEqual(self.toml.read_text(), original)
        table = self.receipt_data()["entries"][1]
        self.assertEqual(table["origin"], "adopted")
        # Uninstall removes an adopted table too: its hash is the one cairn recorded.
        self.run_configure(remove=True)
        self.assertEqual(self.state(), {})

    def test_replaces_foreign_stale_table_and_records_it(self):
        self.toml.write_text(f'[hooks.state."{self.key}"]\ntrusted_hash = "sha256:old"\n')
        self.run_configure()
        table = self.receipt_data()["entries"][1]
        self.assertEqual(table["replaced"], "sha256:old")
        self.assertEqual(self.state()[self.key]["trusted_hash"], self.expected_hash())

    def test_unparseable_config_changes_nothing(self):
        self.toml.write_text("[broken\n")
        with self.assertRaisesRegex(ValueError, "does not parse as TOML"):
            self.run_configure()
        self.assertFalse(self.hooks_path.exists())
        self.assertFalse(self.receipt.exists())

    def test_concurrent_change_refuses(self):
        self.toml.write_text("[tui]\ntheme = 'dark'\n")
        write_json = trust.write_json

        def codex_writes_meanwhile(path, text):
            write_json(path, text)
            self.toml.write_text("[tui]\ntheme = 'light'\n")

        with (
            patch.object(trust, "write_json", codex_writes_meanwhile),
            self.assertRaisesRegex(
                ValueError, "changed while cairn was writing; cairn changed nothing"
            ),
        ):
            self.run_configure()
        self.assertEqual(self.toml.read_text(), "[tui]\ntheme = 'light'\n")
        self.assertFalse(self.toml.with_name("config.toml.cairn-bak").exists())
        receipt = self.receipt_data()
        self.assertEqual(receipt["status"], "prepared")
        self.assertTrue(all(e["pending"] for e in receipt["entries"]))
        # The next run treats the pending entries as owned and finishes the job.
        self.run_configure()
        self.assertEqual(self.receipt_data()["status"], "installed")
        self.assertEqual(len(json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"]), 1)

    def test_post_write_untrusted_rolls_back_table(self):
        command = self.command()
        self.inspect_mock.side_effect = [[], [{"command": command, "trustStatus": "untrusted"}]]
        message, warning = self.run_configure()
        self.assertIn(
            "Codex reports the Cairn hook as untrusted after the write; trust table removed.",
            warning,
        )
        self.assertEqual(
            message, f"Registered Cairn fallback in {self.hooks_path} (SessionEnd group 0)."
        )
        self.assertNotIn(self.key, self.state())
        kinds = [e["kind"] for e in self.receipt_data()["entries"]]
        self.assertEqual(kinds, ["hooks_json_group"])

    def test_inspection_failure_after_write_keeps_table(self):
        self.inspect_mock.side_effect = [[], RuntimeError("timed out")]
        message, _ = self.run_configure()
        self.assertTrue(message.endswith("(Codex did not answer hooks/list: timed out)"))
        self.assertIn(self.key, self.state())

    def test_migrates_v1_ownership_file(self):
        legacy = self.root / "xdg/cairn/codex-hook-registration.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"path": str(self.hooks_path), "commands": ["echo old"]}))
        foreign = {"hooks": [{"type": "command", "command": "echo foreign"}]}
        old = {"hooks": [{"type": "command", "command": "echo old", "timeout": 3}]}
        self.hooks_path.write_text(json.dumps({"hooks": {"SessionEnd": [old, foreign]}}))
        self.run_configure()
        groups = json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"]
        self.assertEqual(groups[0]["hooks"][0]["command"], self.command())
        self.assertEqual(groups[1], foreign)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.receipt_data()["commands"], [self.command()])

    def test_legacy_file_for_another_home_is_refused(self):
        legacy = self.root / "xdg/cairn/codex-hook-registration.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"path": "/tmp/other/hooks.json", "commands": []}))
        with self.assertRaisesRegex(ValueError, "another CODEX_HOME"):
            self.run_configure()

    def test_bundled_discovery_removes_owned_trust_table(self):
        self.run_configure()
        self.assertIn(self.key, self.state())
        self.inspect_mock.return_value = [{"pluginId": "cairn@cairn", "eventName": "sessionEnd"}]
        message, _ = self.run_configure()
        self.assertIn("no fallback", message)
        self.assertEqual(json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"], [])
        self.assertNotIn(self.key, self.state())
        self.assertFalse(self.receipt.exists())

    def test_interrupted_reinstall_keeps_both_versions_owned(self):
        with patch.object(registration, "_command", return_value="echo cairn-a"):
            self.run_configure()
        for crash_in in ("write_json", "write_config"):
            with self.subTest(crash_in=crash_in):
                before = {p: p.read_bytes() for p in (self.hooks_path, self.toml)}
                with (
                    patch.object(registration, "_command", return_value="echo cairn-b"),
                    patch.object(trust, crash_in, side_effect=OSError("crash")),
                    self.assertRaises(OSError),
                ):
                    self.run_configure()
                self.assertEqual(self.receipt_data()["status"], "prepared")
                if crash_in == "write_json":
                    self.assertEqual({p: p.read_bytes() for p in before}, before)
                # Uninstall still knows both the old and the planned entries as cairn's.
                _, warning = self.run_configure(remove=True)
                self.assertNotIn("changed since cairn wrote it", warning)
                self.assertEqual(json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"], [])
                self.assertNotIn(self.key, self.state())
                self.assertFalse(self.receipt.exists())
                with patch.object(registration, "_command", return_value="echo cairn-a"):
                    self.run_configure()

    def test_reinstall_after_interrupt_finishes_the_job(self):
        with patch.object(registration, "_command", return_value="echo cairn-a"):
            self.run_configure()
        with (
            patch.object(registration, "_command", return_value="echo cairn-b"),
            patch.object(trust, "write_config", side_effect=OSError("crash")),
            self.assertRaises(OSError),
        ):
            self.run_configure()
        with patch.object(registration, "_command", return_value="echo cairn-b"):
            _, warning = self.run_configure()
        self.assertEqual(warning, "")
        receipt = self.receipt_data()
        self.assertEqual(receipt["status"], "installed")
        self.assertEqual(len(receipt["entries"]), 2)
        self.assertIsNone(receipt["entries"][1]["replaced"])
        self.assertEqual(
            self.state()[self.key]["trusted_hash"],
            trust.trusted_hash("SessionEnd", cairn_handler("echo cairn-b")),
        )

    def test_reinstall_refuses_an_edited_group(self):
        self.run_configure()
        for edit in ({"timeout": 99}, {"command": "echo edited"}):
            with self.subTest(edit=edit):
                data = json.loads(self.hooks_path.read_text())
                group = data["hooks"]["SessionEnd"][0]
                original = json.loads(json.dumps(group))
                group["matcher"] = "x"
                group["hooks"][0].update(edit)
                self.hooks_path.write_text(json.dumps(data))
                snapshot = {p: p.read_bytes() for p in (self.hooks_path, self.toml, self.receipt)}
                if "command" in edit:
                    # No longer cairn's command: a foreign group, and cairn appends its own.
                    self.run_configure()
                    groups = json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"]
                    self.assertEqual(groups[0], group)
                    self.assertEqual(groups[1]["hooks"][0]["command"], self.command())
                    continue
                with self.assertRaisesRegex(
                    ValueError,
                    f"SessionEnd group 0 in {self.hooks_path} changed since cairn wrote it; "
                    "cairn changed nothing. Restore or remove that group",
                ):
                    self.run_configure()
                self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
                data["hooks"]["SessionEnd"][0] = original
                self.hooks_path.write_text(json.dumps(data))

    def test_interrupted_v1_migration_can_finish_or_be_removed(self):
        legacy = self.root / "xdg/cairn/codex-hook-registration.json"
        old = {"hooks": [{"type": "command", "command": "echo old", "timeout": 3}]}
        for finish in ("install", "uninstall"):
            with self.subTest(finish=finish):
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_text(
                    json.dumps({"path": str(self.hooks_path), "commands": ["echo old"]})
                )
                self.hooks_path.write_text(json.dumps({"hooks": {"SessionEnd": [old]}}))
                self.receipt.unlink(missing_ok=True)
                with (
                    patch.object(trust, "write_json", side_effect=OSError("crash")),
                    self.assertRaises(OSError),
                ):
                    self.run_configure()
                self.assertEqual(self.receipt_data()["status"], "prepared")
                if finish == "install":
                    self.run_configure()
                    groups = json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"]
                    self.assertEqual([g["hooks"][0]["command"] for g in groups], [self.command()])
                else:
                    _, warning = self.run_configure(remove=True)
                    self.assertNotIn("changed since cairn wrote it", warning)
                    groups = json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"]
                    self.assertEqual(groups, [])
                self.assertFalse(legacy.exists())

    def test_refused_install_leaves_the_launchers_alone(self):
        self.run_configure()
        data = json.loads(self.hooks_path.read_text())
        data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 99
        self.hooks_path.write_text(json.dumps(data))
        for launcher in hooks.launcher_paths():
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("# before\n")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = hooks.main(["install", "--harness", "codex"])
        self.assertEqual(code, 1)
        self.assertIn("changed since cairn wrote it; cairn changed nothing", err.getvalue())
        for launcher in hooks.launcher_paths():
            self.assertEqual(launcher.read_text(), "# before\n")

    def test_concurrent_change_while_staging_refuses(self):
        self.toml.write_text("[tui]\ntheme = 'dark'\n")
        stage = trust._stage
        calls: list[Path] = []

        def codex_saves_during_backup(path, text, mode):
            calls.append(path)
            if len(calls) == 1:
                self.toml.write_text("[tui]\ntheme = 'light'\n")
            return stage(path, text, mode)

        with (
            patch.object(trust, "_stage", codex_saves_during_backup),
            self.assertRaisesRegex(ValueError, "changed while cairn was writing"),
        ):
            self.run_configure()
        self.assertEqual(calls[0].name, "config.toml.cairn-bak")
        self.assertEqual(self.toml.read_text(), "[tui]\ntheme = 'light'\n")
        self.assertFalse(self.toml.with_name("config.toml.cairn-bak").exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir() if p.name.startswith(".")), [])

    def test_malformed_receipt_is_a_flat_error(self):
        cases = {
            "[1]": "is not a JSON object",
            "{": "is not valid JSON",
            '{"receipt_version": 2, "entries": [{"kind": "x"}]}': "entry 0 has no known kind",
            '{"receipt_version": 2, "entries": [{"kind": "config_toml_table", "sha256": "a"}]}': (
                "entry 0 has no key"
            ),
            '{"receipt_version": 3}': "receipt_version 3, expected 2",
        }
        for text, detail in cases.items():
            with self.subTest(text=text):
                self.receipt.write_text(text)
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    code = hooks.main(["uninstall", "--harness", "codex"])
                self.assertEqual(code, 1)
                self.assertIn(str(self.receipt), err.getvalue())
                self.assertIn(detail, err.getvalue())
                self.assertIn(detail, registration.trust_report()["error"])


class TestTomlEdit(TrustFixture):
    def test_toml_edit_preserves_foreign_bytes(self):
        before = (
            "# kept comment\r\n[features]\r\nx = 1\r\n\r\n[hooks.state]\r\n\r\n"
            '[hooks.state."/tmp/other/hooks.json:stop:0:0"]\r\ntrusted_hash = "sha256:aa"\r\n'
        )
        after = '\r\n[tui]\r\ntheme = "dark" # trailing\r\n'
        original = before + after
        self.toml.write_bytes(original.encode())
        self.run_configure()
        block = f'\r\n{trust.table_header(self.key)}\r\ntrusted_hash = "{self.expected_hash()}"\r\n'
        self.assertEqual(self.toml.read_bytes(), (before + block + after).encode())
        backup = self.toml.with_name("config.toml.cairn-bak")
        self.assertEqual(backup.read_bytes(), original.encode())
        self.run_configure(remove=True)
        self.assertEqual(self.toml.read_bytes(), original.encode())

    def test_toml_insert_after_last_hooks_state_table(self):
        original = '[hooks.state."/tmp/a:stop:0:0"]\ntrusted_hash = "sha256:aa"\n\n[tui]\nx = 1'
        self.toml.write_text(original)
        self.run_configure()
        text = self.toml.read_text()
        self.assertLess(text.index(trust.table_header(self.key)), text.index("[tui]"))
        self.run_configure(remove=True)
        self.assertEqual(self.toml.read_text(), original)
        # With no hooks.state table the new table goes at the end, after a newline.
        self.toml.write_text("[tui]\nx = 1")
        self.run_configure()
        self.assertTrue(
            self.toml.read_text().startswith(f"[tui]\nx = 1\n\n{trust.table_header(self.key)}\n")
        )

    def test_toml_refuses_inline_hooks_state(self):
        original = 'hooks = { state = { "/tmp/a:stop:0:0" = { trusted_hash = "sha256:aa" } } }\n'
        self.toml.write_text(original)
        with self.assertRaisesRegex(ValueError, "did not parse back to the expected tables"):
            self.run_configure()
        self.assertEqual(self.toml.read_text(), original)
        self.assertFalse(self.hooks_path.exists())
        self.assertFalse(self.receipt.exists())

    def test_header_inside_multiline_string_is_not_a_table(self):
        original = 'note = """\n[hooks.state."fake"]\n"""\n[tui]\nx = 1\n'
        self.toml.write_text(original)
        self.run_configure()
        state = self.state()
        self.assertEqual(list(state), [self.key])
        self.assertEqual(tomllib.loads(self.toml.read_text())["note"], '[hooks.state."fake"]\n')

    def test_reparse_check_refuses_a_misread_header(self):
        # The fake header parses alone, so the line editor takes it for cairn's table;
        # the edit still parses, but changes the string, so the equality check refuses it.
        original = f'note = """\n{trust.table_header(self.key)}\nold\n[x]\n"""\n'
        self.toml.write_text(original)
        with self.assertRaisesRegex(ValueError, "did not parse back to the expected tables"):
            self.run_configure()
        self.assertEqual(self.toml.read_text(), original)
        self.assertFalse(self.hooks_path.exists())

    def test_symlinked_config_stays_symlink_and_mode_kept(self):
        real_dir = self.root / "dotfiles"
        real_dir.mkdir()
        real_config, real_hooks = real_dir / "config.toml", real_dir / "hooks.json"
        real_config.write_text("[tui]\nx = 1\n")
        real_config.chmod(0o640)
        real_hooks.write_text('{"hooks": {}}')
        real_hooks.chmod(0o644)
        self.toml.symlink_to(real_config)
        (self.home / "hooks.json").symlink_to(real_hooks)
        self.run_configure()
        self.assertTrue(self.toml.is_symlink())
        self.assertTrue((self.home / "hooks.json").is_symlink())
        self.assertEqual(real_config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(real_hooks.stat().st_mode & 0o777, 0o644)
        self.assertIn(self.key, tomllib.loads(real_config.read_text())["hooks"]["state"])
        backup = real_dir / "config.toml.cairn-bak"
        self.assertEqual(backup.read_text(), "[tui]\nx = 1\n")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o640)


class TestUninstall(TrustFixture):
    def test_uninstall_removes_only_unchanged_entries(self):
        self.toml.write_text("[tui]\nx = 1\n")
        self.run_configure()
        self.run_configure(remove=True)
        self.assertEqual(self.toml.read_text(), "[tui]\nx = 1\n")
        self.assertEqual(json.loads(self.hooks_path.read_text())["hooks"]["SessionEnd"], [])
        self.assertFalse(self.receipt.exists())

        self.run_configure()
        edited_toml = self.toml.read_text().replace(self.expected_hash(), "sha256:edited")
        self.toml.write_text(edited_toml)
        data = json.loads(self.hooks_path.read_text())
        data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 9
        self.hooks_path.write_text(json.dumps(data))
        _, warning = self.run_configure(remove=True)
        self.assertIn(
            f'warning: [hooks.state."{self.key}"] in {self.home.resolve() / "config.toml"} '
            "changed since cairn wrote it; left in place.",
            warning,
        )
        self.assertIn(
            f"SessionEnd group 0 in {self.hooks_path} changed since cairn wrote it", warning
        )
        self.assertEqual(self.toml.read_text(), edited_toml)
        self.assertEqual(json.loads(self.hooks_path.read_text()), data)
        self.assertTrue(self.receipt.exists())
        # Still cairn's on the next uninstall: it warns again rather than removing by command.
        _, warning = self.run_configure(remove=True)
        self.assertIn("changed since cairn wrote it", warning)
        self.assertEqual(json.loads(self.hooks_path.read_text()), data)

    def test_uninstall_warns_when_the_command_was_edited(self):
        self.run_configure()
        data = json.loads(self.hooks_path.read_text())
        data["hooks"]["SessionEnd"][0]["hooks"][0]["command"] = "echo mine now"
        self.hooks_path.write_text(json.dumps(data))
        _, warning = self.run_configure(remove=True)
        self.assertIn(
            f"warning: no SessionEnd group in {self.hooks_path} runs cairn's command any more "
            "(cairn wrote group 0); left as it is, and its receipt entry dropped.",
            warning,
        )
        self.assertEqual(json.loads(self.hooks_path.read_text()), data)

    def test_uninstall_warns_on_index_shift(self):
        self.run_configure()
        data = json.loads(self.hooks_path.read_text())
        data["hooks"]["SessionEnd"].append({"hooks": [{"type": "command", "command": "echo x"}]})
        self.hooks_path.write_text(json.dumps(data))
        _, warning = self.run_configure(remove=True)
        self.assertIn(
            "warning: removing the Cairn group shifted 1 later SessionEnd group(s) down one index; "
            "Codex will ask to trust them again.",
            warning,
        )

    def tree(self) -> dict[Path, bytes | None]:
        return {p: (p.read_bytes() if p.is_file() else None) for p in sorted(self.root.rglob("*"))}

    def test_dry_run_writes_nothing(self):
        self.toml.write_text("[tui]\nx = 1\n")
        before = self.tree()
        out = io.StringIO()
        with redirect_stdout(out):
            code = hooks.main(["install", "--harness", "codex", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), before)  # no lock file, launcher, receipt or edit
        plan = out.getvalue()
        launchers = hooks.launcher_paths()
        self.assertEqual(len(launchers), 2)  # XDG copy and the default-home discovery copy
        for launcher in launchers:
            self.assertIn(f"would write {launcher} (sha256 ", plan)
        self.assertIn(f"would write {self.hooks_path}: SessionEnd group 0 (sha256 ", plan)
        self.assertIn(f"trusted_hash = {self.expected_hash()} (sha256 ", plan)
        self.assertIn(f"would write {self.receipt} (2 entries)", plan)

        self.run_configure()
        lock = registration.lock_path(self.hooks_path, "registration")
        lock.unlink()
        before = self.tree()
        out = io.StringIO()
        with redirect_stdout(out):
            hooks.main(["uninstall", "--harness", "codex", "--dry-run"])
        self.assertIn(f"would remove {self.home.resolve() / 'config.toml'}: ", out.getvalue())
        self.assertIn(f"would remove {self.hooks_path}: SessionEnd group 0", out.getvalue())
        self.assertEqual(self.tree(), before)

    def test_flags_apply_to_codex_only(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            hooks.main(["install", "--harness", "antigravity", "--dry-run"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--dry-run applies to --harness codex", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            code = hooks.main(["install", "--harness", "antigravity", "--no-trust"])
        self.assertEqual(code, 1)
        self.assertIn("--no-trust applies to --harness codex", err.getvalue())


class TestTrustReport(TrustFixture):
    def test_status_reports_codex_trust(self):
        self.run_configure()
        report = registration.trust_report()
        self.assertEqual(report["codex_version"], "0.165.0")
        self.assertTrue(report["in_range"])
        self.assertEqual(
            report["groups"],
            [{"index": 0, "key": self.key, "state": "trusted", "origin": "written"}],
        )
        self.assertFalse(Path(os.environ["HOME"], ".codex").exists())

    def test_cli_status_reports_host_hooks_and_codex_trust(self):
        self.run_configure()
        listed = [{"command": self.command(), "trustStatus": "trusted"}]
        self.inspect_mock.return_value = listed
        out = io.StringIO()
        with redirect_stdout(out):
            code = hooks.main(["status", "--harness", "codex"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["host_hooks"], listed)
        self.assertEqual(report["codex_trust"]["groups"][0]["state"], "trusted")
        self.assertEqual(report["codex_trust"]["codex_version"], "0.165.0")
        self.assertIn("codex_trust (read from config.toml)", report["trust"])
