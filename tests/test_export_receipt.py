"""Export receipts: written by ``cli.run`` after a collection-wide ``export`` only,
keyed by the output directory, and never failing an export."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cairn import cli, export_receipt
from cairn.sources.base import ConversationSource
from cairn.sources.claude import ClaudeSource
from cairn.sources.codex import CodexSource
from tests.test_shared_store import StoreFixture


class TestExportReceipt(StoreFixture):
    def run_source(self, source: ConversationSource, argv: list[str], stdin: str = "") -> int:
        with (
            patch("sys.stdin", io.StringIO(stdin)),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            cli.run(source, argv)
        return int(caught.exception.code or 0)

    def export(self, code: int) -> tuple[int, str]:
        err = io.StringIO()
        with (
            patch.object(CodexSource, "run_export", return_value=code),
            redirect_stdout(io.StringIO()),
            redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            cli.run(CodexSource(), ["export", "--all", "-q"])
        return int(caught.exception.code or 0), err.getvalue()

    def test_receipt_written_after_export_ok_and_failed(self):
        out = CodexSource().output_dir
        path = export_receipt.receipt_path("codex", out)
        self.assertEqual(path.parent, self.root / "state" / "cairn" / "exports")
        self.assertRegex(path.name, r"^codex-[0-9a-f]{16}\.json$")
        self.assertEqual(self.export(0), (0, ""))
        first = export_receipt.read("codex", out)
        assert first is not None
        self.assertEqual((first["source"], first["last_exit"]), ("codex", 0))
        self.assertEqual(first["output_dir"], str(out.resolve()))
        self.assertEqual(first["last_ok_at"], first["last_finished_at"])
        with patch("cairn.export_receipt.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2099-01-01T00:00:00+00:00"
            self.assertEqual(self.export(1)[0], 1)
        second = export_receipt.read("codex", out)
        assert second is not None
        self.assertEqual(second["last_exit"], 1)
        self.assertEqual(second["last_finished_at"], "2099-01-01T00:00:00+00:00")
        self.assertEqual(second["last_ok_at"], first["last_ok_at"])  # moves only on exit 0

    def test_noop_sync_and_targeted_export_write_no_receipt(self):
        source = ClaudeSource()
        # A hook payload with no session id: sync exits 0 and exports nothing.
        self.assertEqual(self.run_source(source, ["sync", "-q"], stdin="{}"), 0)
        with patch.object(ClaudeSource, "run_export", return_value=0):
            self.assertEqual(self.run_source(source, ["export", "-q", "one.jsonl"]), 0)
        self.assertIsNone(export_receipt.read("claude", source.output_dir))
        with patch.object(ClaudeSource, "run_export", return_value=0):
            self.assertEqual(self.run_source(source, ["export", "-q", "--all"]), 0)
        self.assertIsNotNone(export_receipt.read("claude", source.output_dir))

    def test_receipt_of_another_output_dir_is_not_read(self):
        here, there = self.root / "data-a" / "codex", self.root / "data-b" / "codex"
        export_receipt.write("codex", here, "export", 0)
        self.assertNotEqual(
            export_receipt.receipt_path("codex", here), export_receipt.receipt_path("codex", there)
        )
        self.assertIsNotNone(export_receipt.read("codex", here))
        self.assertIsNone(export_receipt.read("codex", there))
        # A receipt whose recorded directory does not match its key is not trusted either.
        export_receipt.receipt_path("codex", there).write_text(
            export_receipt.receipt_path("codex", here).read_text()
        )
        self.assertIsNone(export_receipt.read("codex", there))

    def test_receipt_write_error_is_warning(self):
        blocker = self.root / "state" / "cairn" / "exports"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the directory should be")
        code, err = self.export(0)
        self.assertEqual(code, 0)
        self.assertIn("warning: could not write the export receipt for codex: ", err)
        self.assertIsNone(export_receipt.read("codex", CodexSource().output_dir))
