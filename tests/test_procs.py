"""``procs.run_capped``: stdin input and the default ``/dev/null`` stdin."""

from __future__ import annotations

import sys
import unittest

from cairn.procs import run_capped

_ECHO_STDIN = "import sys; data = sys.stdin.read(); print(len(data)); print(data, end='')"


class TestRunCappedInput(unittest.TestCase):
    def test_run_capped_input_reaches_stdin(self):
        result = run_capped([sys.executable, "-c", _ECHO_STDIN], timeout=30, input='{"a": 1}\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '9\n{"a": 1}\n')

    def test_run_capped_without_input_reads_empty_stdin(self):
        result = run_capped([sys.executable, "-c", _ECHO_STDIN], timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, "0\n"))


if __name__ == "__main__":
    unittest.main()
