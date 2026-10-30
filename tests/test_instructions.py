"""`cairn instructions`: the recall-first block in the user instruction file."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cairn import instructions
from cairn.instructions import BLOCK_VERSION, END_LINE, render
from tests.test_shared_store import StoreFixture

# A change to the block text must bump BLOCK_VERSION and these pins together.
PINNED = {
    1: {
        "claude": "ea874ffc519b5243f6b10a91483b434a9ce394fa83e1f5326c2dd94caed2efea",
        "codex": "fcf0742b6140625fcc0d20e586379ffee88b2c0651e4324f5f50270b3908c7dd",
    }
}


class InstructionsFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.claude_home = Path(os.environ["CLAUDE_CONFIG_DIR"])
        self.codex_home = Path(os.environ["CODEX_HOME"])
        self.claude_home.mkdir()
        self.codex_home.mkdir()
        self.claude_md = self.claude_home / "CLAUDE.md"
        self.agents_md = self.codex_home / "AGENTS.md"
        self.ledger = Path(os.environ["XDG_STATE_HOME"]) / "cairn" / "recall-first.json"

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = instructions.main(list(args))
            except SystemExit as exc:  # argparse usage errors
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def install(self, harness: str = "claude", *extra: str) -> tuple[int, str, str]:
        return self.cli("install", "--harness", harness, *extra)

    def remove(self, harness: str = "claude", *extra: str) -> tuple[int, str, str]:
        return self.cli("remove", "--harness", harness, *extra)

    def ledger_record(self, harness: str = "claude") -> dict:
        return json.loads(self.ledger.read_text())["harnesses"][harness]


class TestRender(InstructionsFixture):
    def test_render_is_byte_stable_and_at_most_twelve_lines(self):
        for harness, (recall, search) in instructions.SKILL_NAMES.items():
            with self.subTest(harness=harness):
                block = render(harness)
                self.assertEqual(block, render(harness))
                self.assertLessEqual(len(block.splitlines()), 12)
                self.assertTrue(
                    block.startswith(f"<!-- cairn:recall-first:begin v{BLOCK_VERSION} -->\n")
                )
                self.assertTrue(block.endswith(END_LINE + "\n"))
                self.assertIn(f"`{recall}`", block)
                self.assertIn(f"`{search}` with `--exact`", block)
                self.assertIn(f"`cairn instructions remove --harness {harness}`", block)
        self.assertNotIn("/cairn:", render("codex"))
        self.assertNotIn("$recall", render("claude"))

    def test_block_text_is_pinned_to_block_version(self):
        self.assertIn(BLOCK_VERSION, PINNED, "BLOCK_VERSION changed: pin the new block shas")
        for harness, expected in PINNED[BLOCK_VERSION].items():
            with self.subTest(harness=harness):
                actual = hashlib.sha256(render(harness).encode("utf-8")).hexdigest()
                self.assertEqual(actual, expected, "block text changed: bump BLOCK_VERSION")

    def test_show_prints_exact_block(self):
        for harness in ("claude", "codex"):
            code, out, _ = self.cli("show", "--harness", harness)
            self.assertEqual(code, 0)
            self.assertEqual(out, render(harness))
        self.assertFalse(self.claude_md.exists())
        self.assertFalse(self.ledger.exists())


class TestInstall(InstructionsFixture):
    def test_install_appends_after_one_blank_line(self):
        self.claude_md.write_text("# Mine\n\nKeep this.\n")
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(out, f"added: recall-first block v1 appended to {self.claude_md}\n")
        self.assertEqual(self.claude_md.read_text(), "# Mine\n\nKeep this.\n\n" + render("claude"))
        record = self.ledger_record()
        self.assertEqual(record["status"], "installed")
        self.assertEqual(record["block_version"], 1)
        self.assertFalse(record["created_file"])
        self.assertEqual(record["block_sha256"], PINNED[1]["claude"])

    def test_install_twice_writes_once_and_keeps_mtime(self):
        self.claude_md.write_text("mine\n")
        self.install()
        os.utime(self.claude_md, (1_000_000, 1_000_000))
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(out, f"unchanged: recall-first block v1 in {self.claude_md} is current\n")
        self.assertEqual(self.claude_md.stat().st_mtime, 1_000_000)

    def test_install_then_remove_restores_original_bytes(self):
        for original in ("mine\n", "a\n\n", "", "x\n\n\ny\n"):
            with self.subTest(original=original):
                self.claude_md.write_text(original)
                self.ledger.unlink(missing_ok=True)
                self.assertEqual(self.install()[0], 0)
                self.assertEqual(self.remove()[0], 0)
                self.assertEqual(self.claude_md.read_text(), original)
        self.claude_md.write_text("no final newline")
        self.ledger.unlink()
        self.install()
        self.remove()
        self.assertEqual(self.claude_md.read_text(), "no final newline\n")

    def test_install_creates_missing_file_and_says_so(self):
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            f"created: {self.claude_md} did not exist; cairn created it with only the "
            "recall-first block\n",
        )
        self.assertEqual(self.claude_md.read_text(), render("claude"))
        self.assertEqual(stat.S_IMODE(self.claude_md.stat().st_mode), 0o644)
        self.assertTrue(self.ledger_record()["created_file"])

    def test_missing_harness_home_refuses_with_exit_1(self):
        self.codex_home.rmdir()
        code, out, err = self.install("codex")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err,
            f"Error: {self.codex_home} does not exist; is Codex installed? cairn edits "
            f"{self.agents_md} only inside an existing Codex home\n",
        )
        self.assertFalse(self.codex_home.exists())
        code, out, _ = self.install("codex", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["harness"], "codex")
        self.assertIn("does not exist", json.loads(out)["error"])

    def test_stale_block_is_upgraded_and_outside_text_kept(self):
        old_block = (
            "<!-- cairn:recall-first:begin v0 -->\nold text\n<!-- cairn:recall-first:end -->\n"
        )
        self.claude_md.write_text("before\n" + old_block + "after\n")
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(
            out, f"updated: recall-first block in {self.claude_md} upgraded from v0 to v1\n"
        )
        self.assertEqual(self.claude_md.read_text(), "before\n" + render("claude") + "after\n")

    def test_hand_edited_block_refused_without_force_and_replaced_with_force(self):
        self.claude_md.write_text("top\n")
        self.install()
        edited = self.claude_md.read_text().replace("past coding sessions", "past sessions")
        self.claude_md.write_text(edited)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"Error: the recall-first block in {self.claude_md} was edited by hand since cairn "
            "wrote it; move your text outside the markers, or pass --force to replace it\n",
        )
        self.assertEqual(self.claude_md.read_text(), edited)
        code, out, _ = self.install("claude", "--force")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("updated: "))
        self.assertEqual(self.claude_md.read_text(), "top\n\n" + render("claude"))

    def test_edited_without_a_ledger_record_is_judged_by_version(self):
        self.claude_md.write_text(render("claude").replace("this machine", "my laptop"))
        self.assertEqual(self.install()[0], 1)  # v1 on disk, not cairn's text: a hand edit

    def test_damaged_markers_refused(self):
        begin = "<!-- cairn:recall-first:begin v1 -->\n"
        end = "<!-- cairn:recall-first:end -->\n"
        cases = {
            "duplicate begin": (begin + begin + end, 2, 1),
            "orphan end": ("text\n" + end, 0, 1),
            "end before begin": (end + begin, 1, 1),
        }
        for name, (text, begins, ends) in cases.items():
            with self.subTest(name):
                self.claude_md.write_text(text)
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertEqual(
                    err,
                    f"Error: recall-first markers in {self.claude_md} are damaged ({begins} "
                    f"begin, {ends} end; expected one of each, begin first); fix or delete "
                    "them by hand, then re-run\n",
                )
                self.assertEqual(self.claude_md.read_text(), text)
                self.assertEqual(self.remove()[0], 1)

    def test_crlf_and_bom_preserved(self):
        original = "﻿line one\r\nline two\r\n".encode()
        self.claude_md.write_bytes(original)
        self.assertEqual(self.install()[0], 0)
        written = self.claude_md.read_bytes()
        self.assertTrue(written.startswith(b"\xef\xbb\xbfline one\r\n"))
        self.assertIn(render("claude", "\r\n").encode(), written)
        self.assertNotIn(b"\n\n", written.replace(b"\r\n", b""))
        os.utime(self.claude_md, (1_000_000, 1_000_000))
        self.assertTrue(self.install()[1].startswith("unchanged: "))
        self.assertEqual(self.claude_md.stat().st_mtime, 1_000_000)
        self.assertEqual(self.remove()[0], 0)
        self.assertEqual(self.claude_md.read_bytes(), original)

    def test_symlink_target_written_through_link(self):
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "CLAUDE.md"
        real.write_text("mine\n")
        self.claude_md.symlink_to(real)
        self.assertEqual(self.install()[0], 0)
        self.assertTrue(self.claude_md.is_symlink())
        self.assertEqual(real.read_text(), "mine\n\n" + render("claude"))
        self.assertEqual(self.remove()[0], 0)
        self.assertTrue(self.claude_md.is_symlink())
        self.assertEqual(real.read_text(), "mine\n")

    def test_file_mode_preserved(self):
        self.claude_md.write_text("mine\n")
        self.claude_md.chmod(0o640)
        self.install()
        self.assertEqual(stat.S_IMODE(self.claude_md.stat().st_mode), 0o640)
        self.remove()
        self.assertEqual(stat.S_IMODE(self.claude_md.stat().st_mode), 0o640)

    def test_codex_nonempty_override_is_target_and_empty_override_is_not(self):
        override = self.codex_home / "AGENTS.override.md"
        override.write_text("  \n\n")
        self.assertEqual(self.install("codex")[0], 0)
        self.assertEqual(self.agents_md.read_text(), render("codex"))
        self.assertEqual(override.read_text(), "  \n\n")
        override.write_text("override rules\n")
        code, out, err = self.install("codex")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(f"added: recall-first block v1 appended to {override}"))
        self.assertEqual(
            err,
            f"note: a recall-first block is still in {self.agents_md}, which Codex no longer "
            "loads; run 'cairn instructions remove --harness codex' first to clear it\n",
        )
        self.assertEqual(self.remove("codex")[0], 0)
        self.assertEqual(override.read_text(), "override rules\n")
        self.assertFalse(self.agents_md.exists())  # cairn created it; nothing else was in it

    def test_claude_config_dir_and_codex_home_respected(self):
        self.install("claude")
        self.install("codex")
        self.assertTrue(self.claude_md.is_file())
        self.assertTrue(self.agents_md.is_file())
        self.assertFalse((self.root / "home" / ".claude").exists())
        self.assertFalse((self.root / "home" / ".codex").exists())
        other = self.root / "other-claude"
        other.mkdir()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.install("claude", "--force")
        self.assertEqual((other / "CLAUDE.md").read_text(), render("claude"))

    def test_concurrent_change_aborts_without_writing(self):
        self.claude_md.write_text("mine\n")
        real_scan = instructions.scan

        def scan_then_edit(path):
            found = real_scan(path)
            path.write_text("an editor saved this\n")
            return found

        with patch.object(instructions, "scan", scan_then_edit):
            code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"Error: {self.claude_md} changed while cairn was updating it; nothing was "
            "written. Re-run\n",
        )
        self.assertEqual(self.claude_md.read_text(), "an editor saved this\n")
        self.assertFalse(self.ledger.exists())
        self.assertEqual(sorted(p.name for p in self.claude_home.iterdir()), ["CLAUDE.md"])

    def test_non_utf8_file_refused(self):
        self.claude_md.write_bytes(b"caf\xe9\n")
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(
            err, f"Error: {self.claude_md} is not UTF-8 text; cairn will not edit it\n"
        )
        self.assertEqual(self.claude_md.read_bytes(), b"caf\xe9\n")

    def test_dry_run_prints_unified_diff_and_changes_neither_file_nor_ledger(self):
        self.claude_md.write_text("mine\n")
        code, out, _ = self.install("claude", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(f"--- a/{self.claude_md}\n+++ b/{self.claude_md}\n"))
        self.assertIn("+<!-- cairn:recall-first:begin v1 -->\n", out)
        self.assertEqual(self.claude_md.read_text(), "mine\n")
        self.assertFalse(self.ledger.exists())
        self.install()
        before = self.ledger.read_bytes()
        code, out, _ = self.remove("claude", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("-<!-- cairn:recall-first:end -->\n", out)
        self.assertEqual(self.claude_md.read_text(), "mine\n\n" + render("claude"))
        self.assertEqual(self.ledger.read_bytes(), before)


class TestRemoveAndOptOut(InstructionsFixture):
    def test_remove_deletes_file_cairn_created_when_only_whitespace_remains(self):
        self.install()
        code, out, _ = self.remove()
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            f"removed: recall-first block from {self.claude_md}; opt-out recorded, install "
            "will not add it back without --force (file deleted: cairn created it and nothing "
            "else was in it)\n",
        )
        self.assertFalse(self.claude_md.exists())

    def test_remove_keeps_file_cairn_did_not_create(self):
        self.claude_md.write_text("")
        self.install()
        self.remove()
        self.assertTrue(self.claude_md.exists())
        self.assertEqual(self.claude_md.read_text(), "")

    def test_remove_records_opt_out_and_install_skips(self):
        self.claude_md.write_text("mine\n")
        self.install()
        self.remove()
        record = self.ledger_record()
        self.assertEqual(record["status"], "declined")
        self.assertEqual(record["reason"], "cairn instructions remove")
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        date = record["updated_at"][:10]
        self.assertEqual(
            out,
            f"skipped: the recall-first block for claude was removed on {date} (cairn "
            "instructions remove); pass --force to add it back\n",
        )
        self.assertEqual(self.claude_md.read_text(), "mine\n")
        code, out, _ = self.remove()
        self.assertEqual(
            out, f"absent: no recall-first block in {self.claude_md}; opt-out recorded\n"
        )

    def test_hand_deleted_block_becomes_opt_out(self):
        self.claude_md.write_text("mine\n")
        self.install()
        self.claude_md.write_text("mine\n")  # the user deletes the block in an editor
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn("(block deleted by hand); pass --force to add it back", out)
        self.assertEqual(self.claude_md.read_text(), "mine\n")
        self.assertEqual(self.ledger_record()["reason"], "block deleted by hand")

    def test_force_overrides_opt_out(self):
        self.claude_md.write_text("mine\n")
        self.remove()
        code, out, _ = self.install("claude", "--force")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("added: "))
        self.assertEqual(self.ledger_record()["status"], "installed")
        self.assertIsNone(self.ledger_record()["reason"])

    def test_corrupt_or_unknown_schema_ledger_fails_loud(self):
        self.ledger.parent.mkdir(parents=True)
        for content, detail in (
            ("{not json", "Expecting property name"),
            ('{"schema_version": 2, "harnesses": {}}', "unknown schema_version 2"),
        ):
            with self.subTest(detail=detail):
                self.ledger.write_text(content)
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith(f"Error: {self.ledger} is not valid JSON ("))
                self.assertIn(detail, err)
                self.assertIn("move it aside to reset the recall-first opt-outs", err)
                self.assertFalse(self.claude_md.exists())
                self.assertEqual(self.ledger.read_text(), content)


class TestStatusAndCli(InstructionsFixture):
    def test_status_json_shape(self):
        self.install("claude")
        code, out, _ = self.cli("status", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([row["harness"] for row in rows], ["claude", "codex"])
        self.assertEqual(
            rows[0],
            {
                "harness": "claude",
                "path": str(self.claude_md),
                "state_before": "current",
                "state_after": "current",
                "action": "none",
                "block_version": 1,
                "dry_run": False,
            },
        )
        self.assertEqual(rows[1]["state_before"], "no-file")
        code, out, _ = self.cli("status")
        self.assertEqual(
            out, f"claude current v1 {self.claude_md}\ncodex no-file - {self.agents_md}\n"
        )
        self.codex_home.rmdir()
        code, out, _ = self.cli("status", "--json")
        self.assertEqual([row["harness"] for row in json.loads(out)], ["claude"])
        code, out, _ = self.install("claude", "--force", "--json")
        self.assertEqual(json.loads(out)["action"], "unchanged")

    def test_antigravity_harness_is_a_usage_error(self):
        for args in (
            ("install", "--harness", "antigravity"),
            ("status", "--harness", "antigravity"),
        ):
            result = self.run_cli("instructions", *args, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("invalid choice: 'antigravity'", result.stderr)

    def test_dispatcher_routes_instructions(self):
        result = self.run_cli("instructions", "show", "--harness", "claude")
        self.assertEqual(result.stdout, render("claude"))
        self.assertIn("cairn instructions install", self.run_cli("help").stdout)
