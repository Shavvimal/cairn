"""``cairn doctor`` rg check - ripgrep is optional, so its absence only warns."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cairn import admin


def _run_check() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        admin._check_rg()
    return buf.getvalue()


class TestDoctorRg(unittest.TestCase):
    def test_rg_present_is_ok(self):
        with mock.patch.object(admin.shutil, "which", return_value="/usr/bin/rg"):
            out = _run_check()
        self.assertIn("ok   rg - /usr/bin/rg", out)

    def test_rg_missing_warns_and_never_fails(self):
        with mock.patch.object(admin.shutil, "which", return_value=None):
            out = _run_check()
        self.assertIn("warn rg", out)
        self.assertIn("ripgrep", out)
        self.assertNotIn("FAIL", out)


if __name__ == "__main__":
    unittest.main()
