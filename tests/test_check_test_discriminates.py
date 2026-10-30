"""scripts/check-test-discriminates.py against a tiny invented repository.

Each test builds a git repo in a temp dir: ``src/demo/__init__.py`` (the code),
``tests/test_demo.py`` (a test that needs the fix) and a ``feature`` branch off ``main``
that holds the fix. The script runs as a subprocess with the same interpreter, so the
assertions cover its real exit codes, stdout and stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-test-discriminates.py"
TARGET = "tests.test_demo.DemoTest.test_value"

BASE_CODE = "def value():\n    return 1\n"
FIX_CODE = "def value():\n    return 2\n"
TEST_CODE = textwrap.dedent(
    """\
    import unittest

    from demo import value


    class DemoTest(unittest.TestCase):
        def test_value(self):
            self.assertEqual(value(), 2)
    """
)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class DiscriminationScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cairn-discriminate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        home = self.tmp / "home"
        home.mkdir()
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "x",
            "GIT_AUTHOR_EMAIL": "x@y.z",
            "GIT_COMMITTER_NAME": "x",
            "GIT_COMMITTER_EMAIL": "x@y.z",
        }
        self.env.pop("PYTHONPATH", None)
        # The script's temp copy and default log dir land here, so a leak is visible.
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.env["TMPDIR"] = str(self.scratch)
        self.git("init", "-q", "-b", "main")
        self.write("src/demo/__init__.py", BASE_CODE)
        self.write("tests/__init__.py", "")
        self.write(".gitignore", "__pycache__/\n")
        self.commit("base")
        self.base = self.git("rev-parse", "--short=7", "HEAD")
        self.git("checkout", "-q", "-b", "feature")

    # -- helpers ---------------------------------------------------------------

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def write(self, rel: str, text: str, mode: int | None = None) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def fix_and_test(self, commit: bool = True, test: str = TEST_CODE) -> None:
        self.write("src/demo/__init__.py", FIX_CODE)
        self.write("tests/test_demo.py", test)
        if commit:
            self.commit("fix")

    def run_script(self, *args: str, target: str = TARGET) -> subprocess.CompletedProcess[str]:
        if "--log-dir" not in args:
            args = (*args, "--log-dir", str(self.tmp / "logs"))
        return subprocess.run(
            [sys.executable, str(SCRIPT), target, *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def line(self, files: str, reverted: str, restored: str = "PASS") -> str:
        return (
            f"reverted {files} to {self.base}, ran {TARGET} -> {reverted}, restored -> {restored}"
        )

    def tree_digest(self) -> dict[str, str]:
        digest = {}
        for path in sorted(self.repo.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                rel = path.relative_to(self.repo).as_posix()
                digest[rel] = hashlib.sha256(path.read_bytes()).hexdigest() + oct(
                    path.stat().st_mode
                )
        return digest

    # -- verdicts --------------------------------------------------------------

    def test_discriminating_test_exits_0_with_evidence_line(self):
        self.fix_and_test()
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), self.line("src/demo/__init__.py (+1 -1)", "FAIL (1)"))
        self.assertIn("DISCRIMINATES: " + TARGET, proc.stderr)

    def test_test_passing_both_ways_exits_1(self):
        self.fix_and_test(test=TEST_CODE.replace("value(), 2", "value() > 0, True"))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout.strip(), self.line("src/demo/__init__.py (+1 -1)", "PASS"))
        self.assertIn("DOES NOT DISCRIMINATE", proc.stderr)

    def test_import_error_on_reverted_tree_exits_3(self):
        self.write("src/demo/__init__.py", FIX_CODE + "\n\ndef added():\n    return 3\n")
        self.write("tests/test_demo.py", TEST_CODE.replace("import value", "import added, value"))
        self.commit("fix")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("-> NOT RUN (load error: ImportError", proc.stdout)
        self.assertTrue(proc.stdout.strip().endswith("restored -> PASS"), proc.stdout)
        self.assertNotIn(str(self.tmp), proc.stdout)
        self.assertIn("VACUOUS: " + TARGET + " did not run on the reverted tree", proc.stderr)

    def test_all_skipped_exits_3(self):
        skipped = TEST_CODE.replace(
            "    def test_value", "    @unittest.skip('x')\n    def test_value"
        )
        self.fix_and_test(test=skipped)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("NOT RUN (all 1 tests skipped)", proc.stdout)

    def test_failing_on_current_tree_exits_4(self):
        self.fix_and_test(test=TEST_CODE.replace("value(), 2", "value(), 3"))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertTrue(proc.stdout.strip().endswith("restored -> FAIL (1)"), proc.stdout)
        self.assertIn("FIX FAILS", proc.stderr)

    # -- the real tree ---------------------------------------------------------

    def test_real_tree_bytes_unchanged(self):
        self.fix_and_test()
        self.write("src/demo/__init__.py", FIX_CODE + "# uncommitted\n")
        self.write("src/demo/extra.py", "X = 1\n")
        before = self.tree_digest()
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tree_digest(), before)

    def test_uncommitted_fix_is_reverted(self):
        self.fix_and_test(commit=False)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), self.line("src/demo/__init__.py (+1 -1)", "FAIL (1)"))

    def test_untracked_source_file_is_removed_in_copy(self):
        self.fix_and_test()
        self.write("src/demo/extra.py", "X = 1\n")
        proc = self.run_script("--keep", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        kept = Path(proc.stderr.split("kept the reverted copy: ", 1)[1].splitlines()[0])
        self.addCleanup(shutil.rmtree, kept.parent, True)
        self.assertTrue(kept.is_dir())
        self.assertFalse((kept / "src/demo/extra.py").exists())
        self.assertEqual((kept / "src/demo/__init__.py").read_text(), BASE_CODE)
        states = {f["path"]: f["state"] for f in json.loads(proc.stdout)["files"]}
        self.assertEqual(states["src/demo/extra.py"], "untracked")

    def test_exec_bit_restored_from_ref(self):
        self.git("checkout", "-q", "main")
        self.write("hooks/launch.sh", "#!/bin/sh\necho base\n", mode=0o755)
        self.commit("hook")
        self.base = self.git("rev-parse", "--short=7", "HEAD")
        self.git("checkout", "-q", "-B", "feature")
        self.fix_and_test()
        self.write("hooks/launch.sh", "#!/bin/sh\necho fix\n", mode=0o644)
        self.commit("hook mode")
        proc = self.run_script("--keep", "--files", "hooks/launch.sh", "src/demo/__init__.py")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        kept = Path(proc.stderr.split("kept the reverted copy: ", 1)[1].splitlines()[0])
        self.addCleanup(shutil.rmtree, kept.parent, True)
        hook = kept / "hooks/launch.sh"
        self.assertEqual(hook.read_text(), "#!/bin/sh\necho base\n")
        self.assertTrue(os.access(hook, os.X_OK))

    # -- setup errors ----------------------------------------------------------

    def test_files_under_tests_rejected(self):
        self.fix_and_test()
        proc = self.run_script("--files", "tests/test_demo.py")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.strip(),
            "check-test-discriminates: error: tests/test_demo.py is under tests/; "
            "--files takes source files only",
        )
        self.assertEqual(proc.stdout, "")

    def test_nothing_to_revert_exits_2(self):
        self.write("tests/test_demo.py", TEST_CODE)
        self.commit("test only")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.strip(),
            "check-test-discriminates: error: nothing to revert: no file under src/ "
            f"differs from merge-base with main ({self.base})",
        )

    def test_identical_file_exits_2(self):
        self.fix_and_test()
        proc = self.run_script("--since", "main", "--files", ".gitignore")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.strip(),
            f"check-test-discriminates: error: .gitignore is identical at main ({self.base}); "
            "nothing to revert",
        )

    def test_unknown_ref_exits_2(self):
        self.fix_and_test()
        proc = self.run_script("--since", "no-such-ref")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.strip(), "check-test-discriminates: error: unknown ref 'no-such-ref'"
        )

    def test_no_main_and_no_since_exits_2(self):
        self.fix_and_test()
        self.git("branch", "-q", "-m", "main", "trunk")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.strip(),
            "check-test-discriminates: error: no ref 'main' or 'origin/main' to take the "
            "merge-base with; pass --since REF",
        )

    def test_not_a_git_tree_exits_2(self):
        outside = self.tmp / "plain"
        outside.mkdir()
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), TARGET],
            cwd=outside,
            env={**self.env, "GIT_CEILING_DIRECTORIES": str(self.tmp)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.strip(), "check-test-discriminates: error: not inside a git work tree"
        )

    def test_shadowed_import_exits_2(self):
        self.fix_and_test()
        shadow = self.tmp / "shadow"
        shadow.mkdir()
        (shadow / "sitecustomize.py").write_text(
            f"import sys\nsys.path.insert(0, {str(self.repo / 'src')!r})\n", encoding="utf-8"
        )
        self.env["PYTHONPATH"] = str(shadow)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("error: package 'demo' imports from ", proc.stderr)
        self.assertIn("not from the reverted copy; the run would test the fix", proc.stderr)

    # -- output ----------------------------------------------------------------

    def test_json_shape(self):
        self.fix_and_test()
        proc = self.run_script("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(
            set(data),
            {
                "schema_version",
                "verdict",
                "exit",
                "target",
                "ref",
                "ref_short",
                "files",
                "reverted",
                "restored",
                "evidence",
            },
        )
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["verdict"], "discriminates")
        self.assertEqual(data["exit"], 0)
        self.assertEqual(data["ref_short"], self.base)
        self.assertTrue(data["ref"].startswith(self.base))
        self.assertEqual(
            data["files"],
            [{"path": "src/demo/__init__.py", "added": 1, "deleted": 1, "state": "modified"}],
        )
        for phase, failures in (("reverted", 1), ("restored", 0)):
            run = data[phase]
            self.assertEqual(
                set(run),
                {"run", "failures", "errors", "skipped", "load_errors", "seconds", "log"},
            )
            self.assertEqual((run["run"], run["failures"], run["errors"]), (1, failures, 0))
            self.assertIsInstance(run["seconds"], float)
        self.assertEqual(data["evidence"], self.line("src/demo/__init__.py (+1 -1)", "FAIL (1)"))

    def test_temp_copy_removed_and_logs_kept(self):
        self.fix_and_test()
        logs = self.tmp / "run-logs"
        proc = self.run_script("--log-dir", str(logs))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertIn("FAILED", (logs / "reverted.log").read_text())
        self.assertIn("OK", (logs / "restored.log").read_text())
        self.assertIn(f"logs: {logs}", proc.stderr)

    def test_multiple_files_in_line(self):
        self.fix_and_test()
        self.write("src/demo/b.py", "A = 1\nB = 2\n")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            self.line("src/demo/__init__.py (+1 -1) src/demo/b.py (+2 -0)", "FAIL (1)"),
        )


if __name__ == "__main__":
    unittest.main()
