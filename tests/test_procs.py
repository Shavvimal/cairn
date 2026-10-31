"""``procs.run_capped``: stdin input and the default ``/dev/null`` stdin."""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from cairn.procs import run_capped

_ECHO_STDIN = "import sys; data = sys.stdin.read(); print(len(data)); print(data, end='')"
_SLEEP = "import time; time.sleep(30)"


class TestRunCappedInput(unittest.TestCase):
    def test_run_capped_input_reaches_stdin(self):
        result = run_capped([sys.executable, "-c", _ECHO_STDIN], timeout=30, input='{"a": 1}\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '9\n{"a": 1}\n')

    def test_run_capped_input_child_that_never_reads_times_out(self):
        started: list[subprocess.Popen] = []
        real = subprocess.Popen

        def recording(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        begin = time.monotonic()
        with (
            patch("cairn.procs.subprocess.Popen", recording),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            # 1 MiB does not fit a pipe buffer, so the write blocks until the timeout.
            run_capped([sys.executable, "-c", _SLEEP], timeout=0.5, input="x" * (1 << 20))
        self.assertLess(time.monotonic() - begin, 10)
        stdin = started[0].stdin
        assert stdin is not None
        self.assertTrue(stdin.closed)

    def test_run_capped_interrupt_closes_the_stdin_pipe(self):
        started: list[subprocess.Popen] = []
        real = subprocess.Popen

        def recording(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        def interrupted(_self, input=None, timeout=None):
            raise KeyboardInterrupt

        with (
            patch.object(real, "communicate", interrupted),
            patch("cairn.procs.subprocess.Popen", recording),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_capped([sys.executable, "-c", _SLEEP], timeout=30, input="x")
        stdin = started[0].stdin
        assert stdin is not None
        self.assertTrue(stdin.closed)

    def test_run_capped_without_input_reads_empty_stdin(self):
        result = run_capped([sys.executable, "-c", _ECHO_STDIN], timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, "0\n"))


if __name__ == "__main__":
    unittest.main()
