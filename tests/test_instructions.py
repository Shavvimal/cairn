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
            f"{self.agents_md} only inside an existing Codex home\n"
            + "code: INSTRUCTIONS_HOME_MISSING (run 'cairn errors INSTRUCTIONS_HOME_MISSING')\n",
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
            "wrote it; move your text outside the markers, or pass --force to replace it\n"
            + "code: INSTRUCTIONS_BLOCK_EDITED (run 'cairn errors INSTRUCTIONS_BLOCK_EDITED')\n",
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
                    "them by hand, then re-run\n"
                    + "code: INSTRUCTIONS_MARKERS_DAMAGED (run 'cairn errors INSTRUCTIONS_MARKERS_DAMAGED')\n",
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
            "loads; delete it there by hand, or 'cairn instructions remove --harness codex' "
            "removes both copies and records the opt-out\n",
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
            code, _, err = self.install("claude", "--force")
            self.assertEqual((code, err), (0, ""))  # another profile is not an old copy
            self.assertEqual(self.remove("claude")[0], 0)
        self.assertFalse((other / "CLAUDE.md").exists())
        self.assertEqual(self.claude_md.read_text(), render("claude"))

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
            "written. Re-run\n"
            + "code: INSTRUCTIONS_FAILED (run 'cairn errors INSTRUCTIONS_FAILED')\n",
        )
        self.assertEqual(self.claude_md.read_text(), "an editor saved this\n")
        self.assertFalse(self.ledger.exists())
        self.assertEqual(sorted(p.name for p in self.claude_home.iterdir()), ["CLAUDE.md"])

    def test_non_utf8_file_refused(self):
        self.claude_md.write_bytes(b"caf\xe9\n")
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"Error: {self.claude_md} is not UTF-8 text; cairn will not edit it\n"
            + "code: INSTRUCTIONS_FAILED (run 'cairn errors INSTRUCTIONS_FAILED')\n",
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
        self.assertEqual(self.cli("status", "--harness", "claude")[1].split()[1], "declined")

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


class TestReviewFindings(InstructionsFixture):
    """Receipts, Codex target moves, previews and the error contract."""

    def move_to_override(self) -> Path:
        override = self.codex_home / "AGENTS.override.md"
        override.write_text("override rules\n")
        self.assertEqual(self.install("codex")[0], 0)
        return override

    def test_old_codex_copy_kept_through_hand_delete_and_cleared_by_remove(self):
        self.agents_md.write_text("mine\n")
        self.install("codex")
        override = self.move_to_override()
        override.write_text("override rules\n")  # the user deletes the new copy by hand
        code, out, _ = self.install("codex")
        self.assertIn("(block deleted by hand)", out)
        self.assertEqual(self.ledger_record("codex")["old_target"]["path"], str(self.agents_md))
        code, _, err = self.remove("codex")
        self.assertEqual(code, 0)
        self.assertEqual(self.agents_md.read_text(), "mine\n")
        self.assertIn(f"removed: recall-first block from {self.agents_md}", err)
        self.assertIsNone(self.ledger_record("codex")["old_target"])

    def test_damaged_old_codex_copy_is_reported_and_its_receipt_kept(self):
        self.agents_md.write_text("mine\n")
        self.install("codex")
        self.move_to_override()
        self.agents_md.write_text(self.agents_md.read_text() + END_LINE + "\n")
        code, _, err = self.remove("codex")
        self.assertEqual(code, 0)
        self.assertIn(f"note: cannot check {self.agents_md} for an old recall-first block", err)
        self.assertIn("damaged", err)
        self.assertEqual(self.ledger_record("codex")["old_target"]["path"], str(self.agents_md))

    def test_remove_dry_run_previews_every_file_it_changes(self):
        self.agents_md.write_text("mine\n")
        self.install("codex")
        override = self.codex_home / "AGENTS.override.md"
        override.write_text("override rules\n")  # moves the target; no install since
        before = self.ledger.read_bytes()
        code, out, _ = self.remove("codex", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"--- a/{self.agents_md}\n", out)
        self.assertIn("-<!-- cairn:recall-first:end -->\n", out)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertIn("cairn:recall-first:begin", self.agents_md.read_text())
        code, out, _ = self.remove("codex", "--dry-run", "--json")
        self.assertIn(str(self.agents_md), json.loads(out)["diff"])

    def test_ledger_write_failure_exits_1_and_a_retry_records_the_receipt(self):
        self.claude_md.write_text("mine\n")
        real = instructions.atomic_write

        def fail_ledger(path, text, **kwargs):
            if Path(path) == self.ledger:
                raise OSError(28, "No space left on device")
            return real(path, text, **kwargs)

        with patch.object(instructions, "atomic_write", fail_ledger):
            code, out, err = self.install("claude", "--json")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(f"Error: cannot write {self.ledger}: "))
        self.assertIn(f"{self.claude_md} was already changed, and a re-run records it", err)
        self.assertEqual(json.loads(out)["path"], str(self.claude_md))
        self.assertFalse(self.ledger.exists())
        os.utime(self.claude_md, (1_000_000, 1_000_000))
        code, out, _ = self.install()
        self.assertTrue(out.startswith("unchanged: "))
        self.assertEqual(self.claude_md.stat().st_mtime, 1_000_000)
        self.assertEqual(self.ledger_record()["status"], "installed")
        self.claude_md.write_text("mine\n")  # now a hand deletion is an opt-out
        self.assertIn("(block deleted by hand)", self.install()[1])

    def test_force_on_a_current_block_with_a_declined_receipt_reinstalls(self):
        self.claude_md.write_text("mine\n")
        self.install()
        self.remove()
        self.claude_md.write_text("mine\n\n" + render("claude"))
        self.assertTrue(self.install()[1].startswith("skipped: "))
        _, out, _ = self.install("claude", "--force")
        self.assertTrue(out.startswith("unchanged: "))
        self.assertEqual(self.ledger_record()["status"], "installed")

    def test_created_file_is_per_path_and_dropped_with_the_file(self):
        self.install("codex")  # cairn creates AGENTS.md
        override = self.codex_home / "AGENTS.override.md"
        override.write_text(render("codex"))  # the user pastes the block into an override
        self.assertEqual(self.remove("codex")[0], 0)
        self.assertEqual(override.read_text(), "")
        self.assertFalse(self.agents_md.exists())
        self.install()  # cairn creates CLAUDE.md, then remove deletes it
        self.remove()
        self.assertFalse(self.ledger_record()["created_file"])
        self.claude_md.write_text("")  # the user makes a new file
        self.install("claude", "--force")
        self.remove()
        self.assertTrue(self.claude_md.exists())

    def test_lock_failure_exits_1_with_the_json_error(self):
        with patch.object(instructions, "locked", side_effect=PermissionError(13, "denied")):
            code, out, err = self.install("claude", "--json")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error: cannot take the recall-first lock: "))
        self.assertEqual(json.loads(out)["harness"], "claude")

    def test_codex_override_whitespace_is_unicode_aware_and_read_errors_surface(self):
        override = self.codex_home / "AGENTS.override.md"
        override.write_text("\ufeff\u2003\n")  # BOM, EM SPACE
        self.assertEqual(instructions.target_path("codex"), self.agents_md)
        override.unlink()
        override.mkdir()
        code, _, err = self.install("codex")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(f"Error: cannot read {override}: "))

    def test_dry_run_diff_marks_a_missing_final_newline(self):
        self.claude_md.write_text("mine")
        _, out, _ = self.install("claude", "--dry-run")
        self.assertIn("-mine\n\\ No newline at end of file\n+mine\n", out)

    def test_malformed_ledger_record_fails_loud(self):
        self.ledger.parent.mkdir(parents=True)
        base = {"path": str(self.agents_md), "status": "installed"}
        for record, detail in (
            ({**base, "old_target": {"path": 123}}, "harnesses.codex.old_target.path is not"),
            ({**base, "status": "maybe"}, "harnesses.codex.status is not one of"),
            ({**base, "created_file": "yes"}, "harnesses.codex.created_file is not a bool"),
            ({"status": "installed"}, "harnesses.codex.path is missing"),
        ):
            with self.subTest(detail=detail):
                self.ledger.write_text(
                    json.dumps({"schema_version": 1, "harnesses": {"codex": record}})
                )
                code, _, err = self.install("codex")
                self.assertEqual(code, 1)
                self.assertIn(detail, err)
                self.assertFalse(self.agents_md.exists())


class TestAntigravityFindings(InstructionsFixture):
    """Second-review gaps: other Codex homes, dry-run wording, races, status and markers."""

    def test_changed_codex_home_is_not_an_old_copy(self):
        self.agents_md.write_text("mine\n")
        self.install("codex")
        other = self.root / "other-codex"
        other.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(other)}):
            code, _, err = self.install("codex")
            self.assertEqual((code, err), (0, ""))
            self.remove("codex")
        self.assertEqual(self.agents_md.read_text(), "mine\n\n" + render("codex"))

    def test_remove_dry_run_on_an_absent_block_records_nothing_and_says_so(self):
        self.claude_md.write_text("mine\n")
        code, out, _ = self.remove("claude", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(
            out, f"absent: no recall-first block in {self.claude_md}; --dry-run recorded nothing\n"
        )
        self.assertFalse(self.ledger.exists())

    def test_forced_replace_of_a_hand_edit_says_so(self):
        self.claude_md.write_text("mine\n")
        self.install()
        self.claude_md.write_text(self.claude_md.read_text().replace("this machine", "my laptop"))
        _, out, _ = self.install("claude", "--force")
        self.assertEqual(
            out,
            f"updated: hand edits in the recall-first block in {self.claude_md} replaced with v1\n",
        )

    def test_unversioned_begin_marker_is_upgraded(self):
        self.claude_md.write_text(
            "mine\n<!-- cairn:recall-first:begin -->\nold\n<!-- cairn:recall-first:end -->\n"
        )
        _, out, _ = self.install()
        self.assertEqual(
            out,
            f"updated: recall-first block in {self.claude_md} upgraded from an unversioned "
            "block to v1\n",
        )
        self.assertEqual(self.claude_md.read_text(), "mine\n" + render("claude"))

    def test_remove_keeps_a_symlink_even_when_cairn_created_the_file(self):
        self.install()  # created_file is true for CLAUDE.md
        real = self.root / "dotfiles-claude.md"
        real.write_text(render("claude"))
        self.claude_md.unlink()
        self.claude_md.symlink_to(real)
        self.assertEqual(self.remove()[0], 0)
        self.assertTrue(self.claude_md.is_symlink())
        self.assertEqual(real.read_text(), "")

    def test_concurrent_change_aborts_remove(self):
        self.install()  # cairn created the file, so remove would delete it
        real_scan = instructions.scan

        def scan_then_edit(path):
            found = real_scan(path)
            if path == self.claude_md:
                path.write_text("an editor saved this\n")
            return found

        with patch.object(instructions, "scan", scan_then_edit):
            code, _, err = self.remove()
        self.assertEqual(code, 1)
        self.assertIn("changed while cairn was updating it", err)
        self.assertEqual(self.claude_md.read_text(), "an editor saved this\n")
        self.assertEqual(self.ledger_record()["status"], "installed")

    def test_status_for_a_missing_home(self):
        self.codex_home.rmdir()
        code, _, err = self.cli("status", "--harness", "codex")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(f"Error: {self.codex_home} does not exist; "))
        self.claude_home.rmdir()
        code, out, err = self.cli("status")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "no Claude Code or Codex home found; nothing to report\n")
