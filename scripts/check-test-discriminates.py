#!/usr/bin/env python3
"""Check that a unittest fails without its fix and passes with it.

Usage:
    uv run python scripts/check-test-discriminates.py TARGET [--since REF]
        [--files PATH [PATH ...]] [--timeout SECONDS] [--log-dir DIR] [--keep] [--json]

TARGET is a unittest name: ``tests.test_x``, ``tests.test_x.TestY`` or
``tests.test_x.TestY.test_z``. The script copies the working tree (tracked and untracked
files, minus ignored ones) to a temporary directory, reverts the source files to their
content at REF inside the copy, and runs TARGET there. The run must fail. Then it runs
TARGET in the real tree, where it must pass. The real tree and its ``.git`` are only read.

Stdout holds one evidence line (or a JSON object with ``--json``) for every verdict:

    reverted src/cairn/x.py (+12 -3) to f0ea1e1, ran TARGET -> FAIL (1), restored -> PASS

Exit codes: 0 discriminates, 1 does not discriminate, 2 usage or setup error, 3 vacuous
(the test did not run on one tree), 4 the test fails with the fix, 130 interrupted.

Standard library only. See CONTRIBUTING.md, "A new test must fail without its fix".
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import posixpath
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

PROG = "check-test-discriminates"
SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 600

EXIT_DISCRIMINATES = 0
EXIT_DOES_NOT = 1
EXIT_SETUP = 2
EXIT_VACUOUS = 3
EXIT_FIX_FAILS = 4
EXIT_INTERRUPTED = 130

# `git rev-parse --local-env-vars`: variables that point git at one repository. They are
# dropped from every git call in the copy and from both test runs, so a run started from
# a git hook (which sets GIT_INDEX_FILE) cannot write the copy's files into the real index.
GIT_LOCAL_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)

VERDICTS = {
    EXIT_DISCRIMINATES: "discriminates",
    EXIT_DOES_NOT: "does_not_discriminate",
    EXIT_VACUOUS: "vacuous",
    EXIT_FIX_FAILS: "fix_fails",
}

# Runs in the child interpreter: load TARGET, run it, write the counts to OUT as JSON.
RUNNER = r"""
import json
import sys
import unittest

target, out = sys.argv[1], sys.argv[2]


# Records, per test method, whether any part of it ran and whether any part skipped.
# result.skipped counts skipped subtests and testsRun counts methods, so the two cannot be
# compared. A method counts as skipped only when nothing in it ran.
class Result(unittest.TextTestResult):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ran_ids = set()
        self.skip_ids = set()

    @staticmethod
    def method_id(test):
        return getattr(test, "test_case", test).id()

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.skip_ids.add(self.method_id(test))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        self.ran_ids.add(test.id())

    def addSuccess(self, test):
        super().addSuccess(test)
        self.ran_ids.add(test.id())

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.ran_ids.add(test.id())

    def addError(self, test, err):
        super().addError(test, err)
        self.ran_ids.add(test.id())

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.ran_ids.add(test.id())

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.ran_ids.add(test.id())


loader = unittest.TestLoader()
load_errors = []
try:
    suite = loader.loadTestsFromName(target)
except Exception as exc:
    load_errors.append(f"{type(exc).__name__}: {exc}")
    suite = unittest.TestSuite()
load_errors.extend(loader.errors)


def walk(node):
    for item in node:
        if isinstance(item, unittest.TestSuite):
            yield from walk(item)
        else:
            yield item


failed = [t for t in walk(suite) if type(t).__name__ == "_FailedTest"]
if failed and not load_errors:
    load_errors.extend(f"failed to load {t.id()}" for t in failed)
runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Result)
result = runner.run(suite)
with open(out, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "run": result.testsRun,
            "failures": len(result.failures) + len(result.unexpectedSuccesses),
            "errors": len(result.errors),
            "skipped": len(result.skip_ids - result.ran_ids),
            "load_errors": load_errors,
        },
        handle,
    )
"""


class SetupError(Exception):
    """A usage or setup problem: exit 2 with the message on stderr."""


@dataclass
class FileChange:
    path: str
    state: str  # modified | added | deleted | untracked
    added: int | None  # None for a binary file
    deleted: int | None

    def label(self) -> str:
        if self.added is None or self.deleted is None:
            return f"{self.path} (binary)"
        return f"{self.path} (+{self.added} -{self.deleted})"


@dataclass
class RunResult:
    run: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    load_errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    log: str = ""

    @property
    def failed(self) -> int:
        return self.failures + self.errors

    def not_run_reason(self) -> str | None:
        """Why this run proves nothing, or None when at least one test executed."""
        if self.load_errors:
            return f"load error: {first_exception_line(self.load_errors[0])}"
        if self.run == 0:
            return "0 tests ran"
        if self.run <= self.skipped:
            return f"all {self.run} tests skipped"
        return None

    def as_json(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "failures": self.failures,
            "errors": self.errors,
            "skipped": self.skipped,
            "load_errors": self.load_errors,
            "seconds": round(self.seconds, 2),
            "log": self.log,
        }


def first_exception_line(text: str) -> str:
    """The exception line of a loader traceback (``ImportError: ...``), else its first line."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return "unknown"
    if len(lines) > 1 and lines[0].startswith("Failed to import"):
        return lines[-1]
    return lines[0]


def say(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, env=env)
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise SetupError(f"error: git {' '.join(args)} failed: {detail}")
    return proc


def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """BASE (default os.environ) without the variables that tie git to one repository."""
    env = dict(os.environ if base is None else base)
    for name in GIT_LOCAL_VARS:
        env.pop(name, None)
    return env


def git_text(root: Path, *args: str) -> str:
    return git(root, *args).stdout.decode(errors="replace").strip()


def repo_root() -> Path:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
    except FileNotFoundError:
        raise SetupError("error: not inside a git work tree") from None
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SetupError("error: not inside a git work tree")
    return Path(proc.stdout.strip())


def resolve_ref(root: Path, since: str | None) -> tuple[str, str, str]:
    """The name, full sha and short sha of the pre-fix commit."""
    if since is not None:
        proc = git(root, "rev-parse", "--verify", "--quiet", f"{since}^{{commit}}", check=False)
        if proc.returncode != 0:
            raise SetupError(f"error: unknown ref '{since}'")
        full = proc.stdout.decode().strip()
        name = since
    else:
        full = name = ""
        for base in ("main", "origin/main"):
            proc = git(root, "merge-base", "HEAD", base, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                full = proc.stdout.decode().strip()
                name = f"merge-base with {base}"
                break
        if not full:
            raise SetupError(
                "error: no ref 'main' or 'origin/main' to take the merge-base with; "
                "pass --since REF"
            )
    return name, full, git_text(root, "rev-parse", "--short=7", full)


def ref_entry(root: Path, ref: str, path: str) -> tuple[str, bytes] | None:
    """(mode, bytes) of PATH at REF, or None when REF has no blob there."""
    listing = git(root, "ls-tree", "-z", ref, "--", path).stdout.decode(errors="replace")
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, _, name = entry.partition("\t")
        mode, kind, sha = meta.split()
        if name == path and kind == "blob":
            return mode, git(root, "cat-file", "blob", sha).stdout
    return None


def work_entry(root: Path, path: str) -> tuple[str, bytes] | None:
    """(mode, bytes) of PATH in the working tree, in git's terms, or None when absent."""
    full = root / path
    if full.is_symlink():
        return "120000", os.readlink(full).encode()
    if not full.is_file():
        return None
    mode = "100755" if os.access(full, os.X_OK) else "100644"
    return mode, full.read_bytes()


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def tracked_paths(root: Path) -> set[str]:
    out = git(root, "ls-files", "-z", "--cached").stdout.decode(errors="replace")
    return {name for name in out.split("\0") if name}


def describe_change(root: Path, ref: str, path: str, tracked: set[str]) -> FileChange | None:
    """The change from REF to the working tree at PATH, or None when they are identical."""
    old = ref_entry(root, ref, path)
    new = work_entry(root, path)
    if old == new:
        return None
    if old is None:
        state = "added" if path in tracked else "untracked"
    elif new is None:
        state = "deleted"
    else:
        state = "modified"
    if path in tracked or old is not None:
        stat = git(root, "diff", "--no-renames", "--numstat", ref, "--", path).stdout.decode()
        fields = stat.split("\t")
        if len(fields) >= 3 and fields[0] == "-":
            return FileChange(path, state, None, None)
        if len(fields) >= 3:
            return FileChange(path, state, int(fields[0]), int(fields[1]))
    data = new[1] if new is not None else b""
    if is_binary(data):
        return FileChange(path, state, None, None)
    return FileChange(path, state, count_lines(data), 0)


def normalise(root: Path, raw: str) -> str:
    """RAW as a canonical repo-relative path (no ``.`` or ``..``), checked for escapes.

    A relative RAW is taken from the repository root. A path whose parent directory is a
    symlink is refused: reverting it in the copy would follow the link, and the link can
    point back into the real tree.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            parent = candidate.parent.resolve().relative_to(root.resolve())
        except ValueError:
            raise SetupError(f"error: {raw} is outside the repository") from None
        candidate = parent / candidate.name
    path = posixpath.normpath(PurePosixPath(*candidate.parts).as_posix())
    if path == ".." or path.startswith("../"):
        raise SetupError(f"error: {raw} is outside the repository")
    parts = PurePosixPath(path).parts
    for depth in range(1, len(parts)):
        prefix = "/".join(parts[:depth])
        if (root / prefix).is_symlink():
            raise SetupError(
                f"error: {path} has a symlinked parent directory ({prefix}); pass the real path"
            )
    return path


def inside(dest: Path, target: Path) -> Path:
    """TARGET, after a check that its parent directory resolves inside DEST."""
    real_dest = os.path.realpath(dest)
    real_parent = os.path.realpath(target.parent)
    if not (real_parent == real_dest or real_parent.startswith(real_dest + os.sep)):
        raise SetupError(f"error: {target} resolves outside the copy; nothing was written there")
    return target


def collect_changes(
    root: Path, ref: str, name: str, short: str, files: list[str] | None
) -> list[FileChange]:
    tracked = tracked_paths(root)
    if files:
        changes = []
        for raw in files:
            path = normalise(root, raw)
            if path == "tests" or path.startswith("tests/"):
                raise SetupError(f"error: {path} is under tests/; --files takes source files only")
            change = describe_change(root, ref, path, tracked)
            if change is None:
                raise SetupError(
                    f"error: {path} is identical at {name} ({short}); nothing to revert"
                )
            changes.append(change)
        return sorted(changes, key=lambda c: c.path)
    names = git(root, "diff", "--no-renames", "--name-only", "-z", ref, "--", "src/").stdout
    others = git(root, "ls-files", "-z", "--others", "--exclude-standard", "--", "src/").stdout
    candidates = {n for n in (names + b"\0" + others).decode(errors="replace").split("\0") if n}
    changes = [
        change
        for path in sorted(candidates)
        if (change := describe_change(root, ref, path, tracked)) is not None
    ]
    if not changes:
        raise SetupError(
            f"error: nothing to revert: no file under src/ differs from {name} ({short})"
        )
    return changes


def snapshot(root: Path, dest: Path) -> None:
    """Copy tracked and untracked files (minus ignored ones) from ROOT to DEST."""
    listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    for name in sorted({n for n in listing.decode(errors="replace").split("\0") if n}):
        source = root / name
        target = inside(dest, dest / name)
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(source), target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def revert(root: Path, ref: str, dest: Path, changes: list[FileChange]) -> None:
    """Give each changed path its REF content and mode inside DEST, or delete it there."""
    for change in changes:
        target = inside(dest, dest / change.path)
        if target.is_symlink() or target.exists():
            target.unlink()
        entry = ref_entry(root, ref, change.path)
        if entry is None:
            continue
        mode, data = entry
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            os.symlink(data.decode(), target)
            continue
        target.write_bytes(data)
        target.chmod(0o755 if mode == "100755" else 0o644)


def init_copy(dest: Path) -> None:
    """A fresh repository in the copy, so tests that call ``git ls-files`` see the same set."""
    env = {**isolated_env(), "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q"], ["add", "-A"]):
        proc = subprocess.run(["git", *args], cwd=dest, capture_output=True, env=env)
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            raise SetupError(f"error: git {' '.join(args)} failed in the copy: {detail}")


def run_env(tree: Path) -> dict[str, str]:
    env = isolated_env()
    parts = [str(tree / "src"), str(tree)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def top_packages(changes: list[FileChange]) -> list[str]:
    names = set()
    for change in changes:
        parts = PurePosixPath(change.path).parts
        if len(parts) >= 3 and parts[0] == "src":
            names.add(parts[1])
        elif len(parts) == 2 and parts[0] == "src" and parts[1].endswith(".py"):
            names.add(parts[1][: -len(".py")])
    return sorted(names)


def guard_imports(tree: Path, changes: list[FileChange], timeout: float) -> None:
    """Exit 2 when a reverted package would import from anywhere but the copy."""
    src = os.path.realpath(tree / "src")
    probe = (
        "import importlib, sys\n"
        "m = importlib.import_module(sys.argv[1])\n"
        "print(getattr(m, '__file__', None) or list(getattr(m, '__path__', []))[0])\n"
    )
    for pkg in top_packages(changes):
        proc = subprocess.Popen(
            [sys.executable, "-c", probe, pkg],
            cwd=tree,
            env=run_env(tree),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            raise SetupError(
                f"error: import of '{pkg}' timed out after {timeout:g} s on the reverted tree"
            ) from None
        except BaseException:
            kill_group(proc)
            raise
        if proc.returncode != 0 or not stdout.strip():
            continue  # the package does not import at REF; the run reports the load error
        where = os.path.realpath(stdout.strip().splitlines()[-1])
        if not (where == src or where.startswith(src + os.sep)):
            raise SetupError(
                f"error: package '{pkg}' imports from {where}, not from the reverted copy; "
                "the run would test the fix"
            )


def run_target(
    target: str, tree: Path, log: Path, timeout: float, phase: str, scrub: list[str]
) -> RunResult:
    out = log.with_suffix(".json")
    if out.exists():
        out.unlink()
    started = time.monotonic()
    with log.open("wb") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-c", RUNNER, target, str(out)],
            cwd=tree,
            env=run_env(tree),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            raise SetupError(
                f"error: test run timed out after {timeout:g} s on the {phase} tree; log: {log}"
            ) from None
        except BaseException:
            kill_group(proc)
            raise
    result = RunResult(seconds=time.monotonic() - started, log=str(log))
    if not out.exists():
        result.load_errors = [f"RunnerError: the test process exited {code} without a result"]
        return result
    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    result.run = int(data["run"])
    result.failures = int(data["failures"])
    result.errors = int(data["errors"])
    result.skipped = int(data["skipped"])
    result.load_errors = [scrub_paths(str(e), scrub) for e in data["load_errors"]]
    return result


def kill_group(proc: subprocess.Popen[Any]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def scrub_paths(text: str, prefixes: list[str]) -> str:
    """Make absolute paths under the copy or the repo relative, so the line is safe to paste."""
    for prefix in prefixes:
        text = text.replace(prefix + os.sep, "")
    return text


def decide(reverted: RunResult, restored: RunResult) -> int:
    if restored.load_errors or restored.failed:
        return EXIT_FIX_FAILS
    if reverted.not_run_reason() or restored.not_run_reason():
        return EXIT_VACUOUS
    if reverted.failed >= 1:
        return EXIT_DISCRIMINATES
    return EXIT_DOES_NOT


def outcome(result: RunResult) -> str:
    reason = result.not_run_reason()
    if result.load_errors and reason:
        return f"NOT RUN ({reason})"
    if result.failed:
        return f"FAIL ({result.failed})"
    if reason:
        return f"NOT RUN ({reason})"
    return "PASS"


def evidence(
    changes: list[FileChange], short: str, target: str, reverted: RunResult, restored: RunResult
) -> str:
    files = " ".join(change.label() for change in changes)
    return (
        f"reverted {files} to {short}, ran {target} -> {outcome(reverted)}, "
        f"restored -> {outcome(restored)}"
    )


def verdict_line(code: int, target: str, reverted: RunResult, restored: RunResult) -> str:
    if code == EXIT_DISCRIMINATES:
        return f"DISCRIMINATES: {target} fails with the fix reverted and passes with it"
    if code == EXIT_DOES_NOT:
        return (
            f"DOES NOT DISCRIMINATE: {target} passes with the fix reverted; tighten its assertions"
        )
    if code == EXIT_FIX_FAILS:
        return f"FIX FAILS: {target} fails on the current tree; fix the test or the code first"
    phase, result = ("reverted", reverted)
    if reverted.not_run_reason() is None:
        phase, result = ("current", restored)
    return (
        f"VACUOUS: {target} did not run on the {phase} tree ({result.not_run_reason()}); "
        "a load error or zero executed tests is not a failing assertion"
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Run TARGET with the fix reverted (must fail) and with it (must pass).",
    )
    parser.add_argument("target", metavar="TARGET", help="unittest name, e.g. tests.test_x.T.t")
    parser.add_argument("--since", metavar="REF", help="pre-fix commit (default: merge-base)")
    parser.add_argument("--files", nargs="+", metavar="PATH", help="files to revert")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    parser.add_argument("--log-dir", metavar="DIR", help="directory for the two run logs")
    parser.add_argument("--keep", action="store_true", help="keep the reverted copy")
    parser.add_argument("--json", action="store_true", help="print a JSON object")
    return parser.parse_args(argv)


def check(args: argparse.Namespace) -> int:
    root = repo_root()
    name, full, short = resolve_ref(root, args.since)
    changes = collect_changes(root, full, name, short, args.files)
    if args.log_dir:
        # Absolute, because the reverted run's working directory is the copy.
        log_dir = Path(os.path.abspath(args.log_dir))
        log_dir.mkdir(parents=True, exist_ok=True)
    else:
        log_dir = Path(tempfile.mkdtemp(prefix="cairn-discriminate-logs-"))
    work = Path(tempfile.mkdtemp(prefix="cairn-discriminate-"))
    tree = work / "tree"
    try:
        tree.mkdir()
        snapshot(root, tree)
        revert(root, full, tree, changes)
        init_copy(tree)
        guard_imports(tree, changes, args.timeout)
        scrub = sorted(
            {str(tree), os.path.realpath(tree), str(root), os.path.realpath(root)},
            key=len,
            reverse=True,
        )
        reverted = run_target(
            args.target, tree, log_dir / "reverted.log", args.timeout, "reverted", scrub
        )
        restored = run_target(
            args.target, root, log_dir / "restored.log", args.timeout, "current", scrub
        )
    finally:
        if args.keep:
            say(f"kept the reverted copy: {tree}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    code = decide(reverted, restored)
    line = evidence(changes, short, args.target, reverted, restored)
    if args.json:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "verdict": VERDICTS[code],
            "exit": code,
            "target": args.target,
            "ref": full,
            "ref_short": short,
            "files": [
                {"path": c.path, "added": c.added, "deleted": c.deleted, "state": c.state}
                for c in changes
            ],
            "reverted": reverted.as_json(),
            "restored": restored.as_json(),
            "evidence": line,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(line)
    say(verdict_line(code, args.target, reverted, restored))
    say(f"logs: {log_dir}")
    return code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return check(args)
    except SetupError as exc:
        say(str(exc))
        return EXIT_SETUP
    except OSError as exc:
        say(f"error: {exc}")
        return EXIT_SETUP
    except KeyboardInterrupt:
        say("interrupted; the working tree was not modified")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
