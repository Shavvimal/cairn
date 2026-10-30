"""The error registry (``cairn.errors``), ``cairn errors`` and the generated reference."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cairn import __main__ as dispatcher
from cairn import errors, instructions, invoke

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "error-codes.md"
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
FILL = {"name": "daily", "thread_id": "0198a0c1-0000-7000-8000-000000000000", "peer": "codex"}
FILL["harness"] = "claude"

# A verify command must only read. Each rule is (argv prefix, a token the argv must hold).
READ_ONLY = (
    (("cairn", "help"), None),
    (("cairn", "config", "path"), None),
    (("cairn", "config", "show"), None),
    (("cairn", "doctor"), None),  # creates a missing data_root and nothing else
    (("cairn", "routine", "list"), None),
    (("cairn", "routine", "status"), None),
    (("cairn", "routine", "run"), "--dry-run"),
    (("cairn", "invoke", "status"), None),
    (("cairn", "invoke", "tail"), "--no-follow"),
    (("cairn", "invoke", "{peer}"), "--help"),
    (("cairn", "hook", "status"), None),
    (("cairn", "recall", "list"), None),
    (("cairn", "recall", "gaps"), None),
    (("cairn", "recall", "decisions"), None),
    (("cairn", "instructions", "status"), None),
)


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = errors.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class _Parsed(Exception):
    """Raised right after the top-level parser accepted an argv: nothing else runs."""


class TestRegistry(unittest.TestCase):
    def test_codes_are_upper_snake_and_unique(self):
        codes = [e.code for e in errors._ENTRIES]
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertRegex(code, CODE_RE)
        self.assertEqual(set(errors.REGISTRY), set(codes))

    def test_every_entry_has_all_fields(self):
        for row in errors.REGISTRY.values():
            with self.subTest(row.code):
                for text in (row.meaning, row.why, row.next_step):
                    self.assertTrue(text.strip())
                    self.assertTrue(text.endswith("."), text)
                self.assertIsInstance(row.exit, int)
                self.assertIsInstance(row.retryable, bool)
                self.assertTrue(row.raised_at)
                for command, value in row.exit_by_command.items():
                    self.assertIn(command, dispatcher._ADMIN)
                    self.assertIsInstance(value, int)

    def test_raised_at_names_a_real_function(self):
        for row in errors.REGISTRY.values():
            for site in row.raised_at:
                with self.subTest(row.code, site=site):
                    path, name = site.split(":")
                    text = (REPO / path).read_text(encoding="utf-8")
                    self.assertRegex(text, rf"(?m)^\s*(def|class) {re.escape(name)}\b")

    def test_verify_is_read_only(self):
        for row in errors.REGISTRY.values():
            if row.verify is None:
                continue
            with self.subTest(row.code):
                rules = [
                    needed
                    for prefix, needed in READ_ONLY
                    if tuple(row.verify[: len(prefix)]) == prefix
                ]
                self.assertTrue(rules, f"{row.verify} is not on the read-only allowlist")
                needed = rules[0]
                if needed is not None:
                    self.assertIn(needed, row.verify)

    def test_verify_placeholders_are_from_the_closed_set(self):
        for row in errors.REGISTRY.values():
            for part in row.verify or ():
                for name in re.findall(r"\{([a-z_]+)\}", part):
                    self.assertIn(name, errors.PLACEHOLDERS, row.code)

    def test_verify_parses(self):
        """Each verify argv, placeholders filled, is accepted by the real parser."""
        original = argparse.ArgumentParser.parse_args

        def parse_then_stop(parser, args=None, namespace=None):
            raise _Parsed(original(parser, args, namespace))

        with (
            TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ, {"HOME": tmp, "XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}
            ),
        ):
            for row in errors.REGISTRY.values():
                argv = errors.fix(row.code, **FILL).get("verify")
                if argv is None:
                    continue
                with self.subTest(row.code, argv=argv):
                    self.assertEqual(argv[0], "cairn")
                    command, rest = argv[1], argv[2:]
                    if command == "help":
                        self.assertEqual(rest, [])
                        continue
                    target = dispatcher._resolve(dispatcher._ADMIN[command])
                    out = io.StringIO()
                    with (
                        mock.patch.object(argparse.ArgumentParser, "parse_args", parse_then_stop),
                        redirect_stdout(out),
                        redirect_stderr(io.StringIO()),
                    ):
                        try:
                            target(rest)
                        except _Parsed:
                            pass
                        except SystemExit as stop:  # --help prints and exits 0
                            self.assertEqual(stop.code, 0)
                            self.assertIn("usage:", out.getvalue())
                        else:
                            self.fail(f"{argv} returned without parsing")

    def test_outcome_codes_cover_exit_table(self):
        for status, exit_code in invoke.EXIT_BY_OUTCOME.items():
            code = errors.outcome_code(status)
            if status == "succeeded":
                self.assertIsNone(code)
                continue
            assert code is not None
            self.assertEqual(errors.exit_for(code), exit_code, status)
        self.assertEqual(errors.exit_for("INVOKE_BUSY"), invoke.EXIT_BUSY)
        self.assertEqual(errors.exit_for("INVOKE_FAILED"), invoke.EXIT_FATAL)
        self.assertEqual(errors.exit_for("INVOKE_USAGE"), invoke.EXIT_USAGE)
        self.assertIsNone(errors.outcome_code("running"))
        self.assertIsNone(errors.outcome_code(None))


class TestHelpers(unittest.TestCase):
    def test_placeholder_unfilled_drops_verify(self):
        self.assertEqual(
            errors.fix("INVOKE_BUSY"), {"next_step": errors.entry("INVOKE_BUSY").next_step}
        )
        self.assertEqual(
            errors.fix("INVOKE_BUSY", thread_id="t1")["verify"],
            ["cairn", "invoke", "status", "t1"],
        )
        self.assertNotIn("verify", errors.fix("INVOKE_BUSY", thread_id=None))
        self.assertNotIn("verify", errors.fix("INVOKE_NESTED"))

    def test_unknown_code_raises(self):
        for call in (
            lambda: errors.fix("NOPE"),
            lambda: errors.tag({}, "NOPE"),
            lambda: errors.code_line("NOPE"),
            lambda: errors.finding("NOPE"),
        ):
            with self.assertRaises(KeyError):
                call()

    def test_fail_keeps_first_line_and_adds_code_line(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()) as out:
            code = errors.fail("RECALL_FAILED", "bad date 'x'", exit=1, prefix="Error: ")
        self.assertEqual(code, 1)
        self.assertEqual(
            err.getvalue(),
            "Error: bad date 'x'\ncode: RECALL_FAILED (run 'cairn errors RECALL_FAILED')\n",
        )
        self.assertEqual(out.getvalue(), "")

    def test_fail_json_object_shape(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = errors.fail("ROUTINE_NESTED", "refused", exit=2, as_json=True, name="daily")
        self.assertEqual(code, 2)
        data = json.loads(out.getvalue())
        self.assertEqual(
            data,
            {
                "ok": False,
                "code": "ROUTINE_NESTED",
                "error": "refused",
                "exit": 2,
                "retryable": False,
                "fix": {
                    "next_step": errors.entry("ROUTINE_NESTED").next_step,
                    "verify": ["cairn", "routine", "status", "daily"],
                },
            },
        )

    def test_tag_keeps_existing_keys(self):
        payload = {"error": "e", "retryable": True}
        tagged = errors.tag(payload, "INVOKE_FAILED", thread_id="t")
        self.assertEqual(
            (tagged["error"], tagged["retryable"], tagged["code"]), ("e", True, "INVOKE_FAILED")
        )
        self.assertEqual(tagged["fix"]["verify"], ["cairn", "invoke", "status", "t"])
        self.assertEqual(payload, {"error": "e", "retryable": True})  # not mutated
        self.assertEqual(errors.tag({"code": "OWN"}, "INVOKE_FAILED")["code"], "OWN")

    def test_exit_by_command(self):
        for code in ("CONFIG_UNKNOWN_KEY", "CONFIG_INVALID", "CONFIG_NOT_FOUND"):
            self.assertEqual(errors.exit_for(code, "routine"), 2)
            self.assertEqual(errors.exit_for(code, "recall"), 1)
            self.assertEqual(errors.exit_for(code), 1)

    def test_finding_suffix(self):
        self.assertEqual(errors.finding("QMD_MISSING"), " [QMD_MISSING]")
        self.assertEqual(errors.finding(None), "")

    def test_code_of(self):
        self.assertEqual(errors.code_of(ValueError("x"), "RECALL_FAILED"), "RECALL_FAILED")
        exc = instructions.InstructionsError("x", code="INSTRUCTIONS_BLOCK_EDITED")
        self.assertEqual(errors.code_of(exc, "RECALL_FAILED"), "INSTRUCTIONS_BLOCK_EDITED")

    def test_exception_defaults_are_registered(self):
        from cairn.config import ConfigError
        from cairn.routine_plan import RoutineError

        for exc in (
            ConfigError("x"),
            RoutineError("x"),
            invoke.UsageError("x"),
            instructions.InstructionsError("x"),
        ):
            self.assertIn(exc.code, errors.REGISTRY)
            self.assertEqual(str(exc), "x")


class TestRender(unittest.TestCase):
    def test_render_is_fresh(self):
        self.assertEqual(
            DOC.read_text(encoding="utf-8"),
            errors.render_markdown(),
            f"docs/error-codes.md is stale; run: {errors.RENDER_COMMAND}",
        )

    def test_render_flag_prints_the_document(self):
        code, out, _ = run_cli("--render")
        self.assertEqual((code, out), (0, errors.render_markdown()))

    def test_render_lists_every_code_and_the_contract(self):
        text = errors.render_markdown()
        self.assertTrue(text.startswith("<!-- GENERATED by python -m cairn.errors --render"))
        for code in errors.REGISTRY:
            self.assertIn(f"\n## {code}\n", text)


class TestCli(unittest.TestCase):
    def test_cli_lists_codes(self):
        code, out, err = run_cli()
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), len(errors.REGISTRY))
        self.assertEqual([line.split()[0] for line in lines], sorted(errors.REGISTRY))
        self.assertIn("exit 75 ", next(line for line in lines if line.startswith("INVOKE_BUSY")))

    def test_cli_list_json(self):
        code, out, _ = run_cli("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([e["code"] for e in data["codes"]], sorted(errors.REGISTRY))

    def test_cli_lookup_json(self):
        code, out, _ = run_cli("routine_template_unknown_var", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["code"], "ROUTINE_TEMPLATE_UNKNOWN_VAR")
        self.assertEqual(data["verify"], ["cairn", "routine", "run", "{name}", "--dry-run"])
        self.assertEqual((data["exit"], data["retryable"]), (2, False))
        self.assertEqual(
            sorted(data),
            sorted(
                [
                    "code",
                    "meaning",
                    "why",
                    "next_step",
                    "verify",
                    "exit",
                    "exit_by_command",
                    "exit_note",
                    "retryable",
                    "shown_in",
                    "raised_at",
                ]
            ),
        )

    def test_cli_lookup_text(self):
        code, out, _ = run_cli("CONFIG_UNKNOWN_KEY")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("CONFIG_UNKNOWN_KEY\nMeaning:    "))
        self.assertIn("Verify:     cairn config show --json\n", out)
        self.assertIn("Exit:       1 (cairn routine: 2)\n", out)

    def test_cli_unknown_exits_2(self):
        code, out, err = run_cli("NOPE")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(
            err, "cairn errors: unknown code 'NOPE'; run 'cairn errors' for the list\n"
        )

    def test_dispatcher_routes_errors_and_unknown_command(self):
        with TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}
            env["XDG_CACHE_HOME"] = tmp
            ok = subprocess.run(
                [sys.executable, "-m", "cairn", "errors", "INVOKE_BUSY"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue(ok.stdout.startswith("INVOKE_BUSY\n"))
            bad = subprocess.run(
                [sys.executable, "-m", "cairn", "nope"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(bad.returncode, 2)
        lines = bad.stderr.splitlines()
        self.assertEqual(lines[0], "cairn: unknown command 'nope'.")
        self.assertIn("errors", lines[1])
        self.assertEqual(lines[2], "run 'cairn help' for usage.")
        self.assertEqual(
            lines[3], "code: CLI_UNKNOWN_COMMAND (run 'cairn errors CLI_UNKNOWN_COMMAND')"
        )


class TestInstructionsSite(unittest.TestCase):
    def test_json_failure_carries_code_and_fix(self):
        with (
            TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "XDG_STATE_HOME": tmp,
                    "CLAUDE_CONFIG_DIR": str(Path(tmp) / "missing-claude"),
                },
            ),
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = instructions.main(["install", "--harness", "claude", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(
            err.getvalue().splitlines()[1],
            "code: INSTRUCTIONS_HOME_MISSING (run 'cairn errors INSTRUCTIONS_HOME_MISSING')",
        )
        data = json.loads(out.getvalue())
        self.assertEqual(data["code"], "INSTRUCTIONS_HOME_MISSING")
        self.assertEqual(data["harness"], "claude")
        self.assertEqual(data["fix"]["verify"], ["cairn", "instructions", "status", "--json"])


if __name__ == "__main__":
    unittest.main()
