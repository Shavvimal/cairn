"""Export receipts: written by ``cli.run`` after every ``export``, never failing one."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cairn import cli, export_receipt
from cairn.sources.codex import CodexSource
from tests.test_shared_store import StoreFixture


class TestExportReceipt(StoreFixture):
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
        path = self.root / "state" / "cairn" / "exports" / "codex.json"
        self.assertEqual(export_receipt.receipt_path("codex"), path)
        self.assertEqual(self.export(0), (0, ""))
        first = export_receipt.read("codex")
        assert first is not None
        self.assertEqual((first["source"], first["last_exit"]), ("codex", 0))
        self.assertEqual(first["last_ok_at"], first["last_finished_at"])
        with patch("cairn.export_receipt.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2099-01-01T00:00:00+00:00"
            self.assertEqual(self.export(1)[0], 1)
        second = export_receipt.read("codex")
        assert second is not None
        self.assertEqual(second["last_exit"], 1)
        self.assertEqual(second["last_finished_at"], "2099-01-01T00:00:00+00:00")
        self.assertEqual(second["last_ok_at"], first["last_ok_at"])  # moves only on exit 0

    def test_receipt_write_error_is_warning(self):
        blocker = self.root / "state" / "cairn" / "exports"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the directory should be")
        code, err = self.export(0)
        self.assertEqual(code, 0)
        self.assertIn("warning: could not write the export receipt for codex: ", err)
        self.assertIsNone(export_receipt.read("codex"))
