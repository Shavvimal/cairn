"""``cairn doctor`` QMD runtime and index checks, default and ``--strict-index``.

A ``#!python`` stub stands in for qmd and records every subcommand, which proves
doctor only probes (``--version``, ``status``, ``collection list``) and never
embeds, installs or upgrades.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest import mock

from cairn import __version__, admin
from tests.test_shared_store import StoreFixture

COMPLETE = "Documents\n  Total:    4 files indexed\n  Vectors:  9 embedded\n"
PENDING = COMPLETE + "  Pending:  2 need embedding (run 'qmd embed')\n"
EMPTY = "Documents\n  Total:    0 files indexed\n  Vectors:  0 embedded\n"
NO_VECTORS = "Documents\n  Total:    4 files indexed\n  Vectors:  0 embedded\n"

STUB = """
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
spec = json.loads((here / "doctor.json").read_text())
with open(here / "doctor-calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "--version":
    print(spec["version"])
elif sys.argv[1] == "status":
    if spec.get("crash"):
        os.kill(os.getpid(), 11)
    print(spec["status"])
"""


class TestDoctorIndex(StoreFixture):
    def stub(self, status: str = COMPLETE, version: str = "qmd 2.5.3 (179d450)", **extra):
        self.fake_qmd(STUB)
        (self.root / "doctor.json").write_text(
            json.dumps({"status": status, "version": version, **extra})
        )

    def doctor(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with (
            mock.patch.object(admin, "_current_crontab", return_value=[]),
            redirect_stdout(out),
        ):
            code = admin.doctor_main(list(args))
        return code, out.getvalue()

    def calls(self) -> set[str]:
        path = self.root / "doctor-calls.log"
        return (
            {line.split()[0] for line in path.read_text().splitlines()} if path.exists() else set()
        )

    def test_missing_executable_is_critical(self):
        code, out = self.doctor()  # StoreFixture's qmd_binary is /missing/qmd
        self.assertEqual(code, 1)
        self.assertIn("FAIL qmd - '/missing/qmd' not found", out)
        self.assertIn("(e.g. npm install -g @tobilu/qmd) [QMD_MISSING]\n", out)

    def test_version_succeeds_but_runtime_crashes(self):
        self.stub(crash=True)
        for args in ((), ("--strict-index",)):
            code, out = self.doctor(*args)
            self.assertEqual(code, 1)
            self.assertIn("ok   qmd version - qmd 2.5.3", out)
            self.assertIn("FAIL qmd runtime", out)
            self.assertIn("killed by signal 11", out)

    def test_old_version_warns_but_is_judged_by_the_index(self):
        self.stub(version="qmd 1.1.5")
        code, out = self.doctor("--strict-index")
        self.assertEqual(code, 0, out)
        self.assertIn("warn qmd version - qmd 1.1.5: older than 2.5.3", out)

    def test_unknown_version_warns(self):
        self.stub(version="qmd 2.6.0-beta.1")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("is not a release version", out)

    def test_unrecognised_status(self):
        self.stub(status="Some new format")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn qmd runtime - ran, but", out)
        self.assertIn("the semantic index is not verified: could not read 'qmd status'", out)
        self.assertEqual(self.doctor("--strict-index")[0], 1)

    def test_empty_index_passes_both_modes_without_claiming_search_works(self):
        self.stub(status=EMPTY)
        for args in ((), ("--strict-index",)):
            code, out = self.doctor(*args)
            self.assertEqual(code, 0, out)
            self.assertIn("the index is empty, so no search can be verified yet", out)
            self.assertIn("All critical checks passed, but the semantic index is not verified", out)

    def test_no_vectors_warns_by_default_and_fails_strict(self):
        self.stub(status=NO_VECTORS)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn embeddings - 4 documents, 0 vectors, 0 pending: keyword search", out)
        code, out = self.doctor("--strict-index")
        self.assertEqual(code, 1)
        self.assertIn("FAIL embeddings", out)

    def test_pending_warns_by_default_and_fails_strict(self):
        self.stub(status=PENDING)
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("warn embeddings - 4 documents, 9 vectors, 2 pending", out)
        self.assertIn("not verified", out)
        self.assertEqual(self.doctor("--strict-index")[0], 1)

    def test_malformed_pending_line_fails_strict(self):
        self.stub(status=COMPLETE + "  Pending:  unknown need embedding\n")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("could not read 'qmd status'", out)
        self.assertEqual(self.doctor("--strict-index")[0], 1)

    def test_complete_index_passes_and_composes_with_other_options(self):
        self.stub()
        manifest = self.root / "plugin.json"
        manifest.write_text(json.dumps({"name": "cairn", "version": __version__}))
        code, out = self.doctor(
            "--strict-index", "--harness", "claude", "--plugin-manifest", str(manifest)
        )
        self.assertEqual(code, 0, out)
        self.assertIn("ok   qmd runtime", out)
        self.assertIn("ok   embeddings - 4 documents, 9 vectors, 0 pending", out)
        self.assertTrue(out.rstrip().endswith("All critical checks passed."), out)
        self.assertLessEqual(self.calls(), {"--version", "status", "collection"})


if __name__ == "__main__":
    import unittest

    unittest.main()
