"""``cairn routine``: templates, day set, refresh window, the run loop, stamps, the lock,
the launchd plist, install / uninstall / kickstart, status and the doctor lines.

No real launchctl, peer or export runs: :func:`cairn.routine_run.run_command` and
:func:`cairn.routine.launchctl` are replaced by recorders.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from cairn import config, routine, routine_plan, routine_run
from cairn.procs import ProcessCleanupError
from cairn.storage import lock_path
from tests.test_shared_store import StoreFixture

TODAY = date(2026, 11, 12)  # the worked example of spec section 7.4
LOG_LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 routine=daily run=\d{8}T\d{6}Z-[0-9a-f]{8} "
    r"event=[a-z-]+( [a-z_]+=(\"[^\"]*\"|\S+))*$|^      \[stderr\] "
)


class Children:
    """Records every child argv and answers with scripted exit codes.

    ``codes[kind]`` is an int or a list consumed one call at a time. ``kind`` is
    ``refresh``, ``prepare``, ``invoke`` or ``validate``.
    """

    def __init__(self, cost: float | None = 0.84, **codes):
        self.codes = {"refresh": 0, "prepare": 0, "invoke": 0, "validate": 0, **codes}
        self.cost = cost
        self.calls: list[tuple[str, list[str], dict, str | None]] = []
        self.on_validate: Callable[[list[str]], None] | None = None

    @staticmethod
    def kind(argv: list[str]) -> str:
        if "export" in argv:
            return "refresh"
        if "invoke" in argv:
            return "invoke"
        return "prepare" if argv[0] == "prep" else "validate"

    def argvs(self, kind: str) -> list[list[str]]:
        return [argv for k, argv, _env, _cwd in self.calls if k == kind]

    def __call__(self, argv, *, timeout, env, cwd):
        kind = self.kind(argv)
        self.calls.append((kind, list(argv), env, cwd))
        code = self.codes[kind]
        if isinstance(code, list):
            code = code.pop(0)
        if code == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout or 0, output="", stderr="slow\n")
        stdout = ""
        if kind == "invoke":
            usage = None if self.cost is None else {"total_cost_usd": self.cost}
            outcome = "succeeded" if code == 0 else "timeout"
            stdout = "noise\n" + json.dumps(
                {"thread_id": "t-1", "outcome": outcome, "usage": usage}
            )
        if kind == "prepare":
            stdout = json.dumps({"sessions": [{"id": "s1"}], "skipped": []})
        if kind == "validate" and self.on_validate:
            self.on_validate(argv)
        return subprocess.CompletedProcess(argv, code, stdout, "boom\n" if code else "")


class RoutineFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.site = self.root / "site"
        (self.site / "prompts").mkdir(parents=True)
        self.prompt = self.site / "prompts" / "daily.md"
        self.prompt.write_text("Write the journal for {{DAY}} into {{JOURNAL_DIR}}.\n")
        self.write_config()
        self.today = patch("cairn.routine_plan.utc_today", return_value=TODAY)
        self.today.start()
        self.addCleanup(self.today.stop)

    def write_config(self, **overrides) -> None:
        data = json.loads(self.config.read_text())
        data["collections"]["journal"] = {"sync": {"enabled": True}}
        data["invoke"] = {
            "claude": {
                "profiles": {
                    "scheduled": {
                        "base": "edit",
                        "extra_args": [
                            "--permission-mode",
                            "dontAsk",
                            "--add-dir",
                            "{{JOURNAL_DIR}}",
                        ],
                    }
                }
            }
        }
        spec = {
            "peer": "claude",
            "prompt_file": str(self.prompt),
            "cd": str(self.site),
            "profile": "scheduled",
            "at": "05:20",
            "sources": ["claude", "codex"],
            "prepare": ["prep", "--day", "{{DAY}}"],
            "validate": ["check", "{{DAY}}", "{{ATTEMPT_DIR}}"],
            # The fixture enables antigravity outside sources, so the default "warn" would
            # flag it on every run; TestStaleSources drops this key to test the default.
            "stale_sources": "ignore",
        }
        spec.update(overrides)
        data["routines"] = {"daily": {k: v for k, v in spec.items() if v is not ...}}
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()

    @property
    def cfg(self):
        return config.get_config()

    @property
    def state(self) -> Path:
        return routine_run.state_dir(self.cfg, "daily")

    @property
    def log(self) -> Path:
        return routine_run.log_path(self.cfg, "daily")

    def attempt(self, day: str, number: int) -> Path:
        return self.cfg.data_root / "routines" / "daily" / day / f"attempt-{number}"

    def stamp(self, kind: str, day: str) -> dict | None:
        return routine_plan.read_json(self.state / f"{kind}-{day}.json")

    def events(self, name: str) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if f"event={name}" in line]

    def cli(self, *argv: str, children: Children | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        children = children if children is not None else Children()
        with (
            patch("cairn.routine_run.run_command", children),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = routine.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class TestTemplate(RoutineFixture):
    def test_renders_all_variables(self):
        self.prompt.write_text(
            "{{DAY}} {{DATA_ROOT}} {{ATTEMPT_DIR}} {{DIGEST_DIR}} {{JOURNAL_DIR}}"
        )
        where = self.attempt("2026-11-11", 1)
        rendered = routine_plan.render_day(
            self.cfg, self.cfg.routines["daily"], date(2026, 11, 11), where
        )
        data_root = str(self.cfg.data_root)
        self.assertEqual(
            rendered.prompt,
            f"2026-11-11 {data_root} {where} {where}/digest {data_root}/journal",
        )
        self.assertEqual(rendered.validate, ["check", "2026-11-11", str(where)])
        self.assertEqual(
            rendered.profile_args,
            ["--permission-mode", "dontAsk", "--add-dir", f"{data_root}/journal"],
        )

    def test_single_pass_spaced_braces_and_tilde(self):
        self.prompt.write_text("{{ DAY }} {{#each}} {{DAY}}")
        self.write_config(validate=["~/bin/check", "x~/y", "~", "{{DAY}}"])
        spec = self.cfg.routines["daily"]
        variables = {"DAY": "{{DAY}}"}
        self.assertEqual(routine_plan.render_text("{{DAY}}", variables, None), "{{DAY}}")
        rendered = routine_plan.render_day(
            self.cfg, spec, date(2026, 11, 11), self.attempt("2026-11-11", 1)
        )
        self.assertEqual(rendered.prompt, "{{ DAY }} {{#each}} 2026-11-11")
        home = str(Path.home())
        self.assertEqual(rendered.validate, [f"{home}/bin/check", "x~/y", home, "2026-11-11"])

    def test_unknown_variable_errors_with_location(self):
        self.prompt.write_text("line one\nsee {{JORNAL_DIR}}\n")
        children = Children()
        code, _out, err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 2)
        self.assertIn(
            f"routine daily: unknown template variable {{{{JORNAL_DIR}}}} in {self.prompt} line 2; "
            "known: ATTEMPT_DIR, DATA_ROOT, DAY, DIGEST_DIR, JOURNAL_DIR",
            err,
        )
        self.assertEqual(children.calls, [])  # no refresh, no invoke
        self.assertFalse((self.cfg.data_root / "routines").exists())
        self.assertEqual(list(self.state.glob("*-2026-*.json")), [])
        last = routine_plan.read_json(self.state / "last-run.json")
        assert last is not None
        self.assertEqual(last["exit"], 2)
        self.assertIn("JORNAL_DIR", last["error"])

    def test_unknown_variable_in_argv_names_the_element(self):
        self.write_config(validate=["check", "{{NOPE}}"])
        code, _out, err = self.cli("run", "daily")
        self.assertEqual(code, 2)
        self.assertIn("{{NOPE}} in validate[1]", err)

    def test_relative_path_rejected_at_load(self):
        self.write_config(prompt_file="prompts/daily.md")
        code, _out, err = self.cli("run", "daily")
        self.assertEqual(code, 2)
        self.assertIn("routines.daily.prompt_file must be absolute", err)


class TestDays(RoutineFixture):
    def test_window_oldest_first_skips_done(self):
        self.state.mkdir(parents=True)
        (self.state / "done-2026-11-10.json").write_text("{}")
        spec = self.cfg.routines["daily"]
        days, skipped = routine_plan.compute_days(spec, self.state, TODAY)
        self.assertEqual(days, [date(2026, 11, 9), date(2026, 11, 11)])
        self.assertEqual(skipped, [])

    def test_attempt_cap_skips_day_and_uncounted_do_not(self):
        self.state.mkdir(parents=True)
        (self.state / "failed-2026-11-09.json").write_text(json.dumps({"attempts": 3}))
        (self.state / "failed-2026-11-10.json").write_text(json.dumps({"attempts": 0}))
        days, skipped = routine_plan.compute_days(self.cfg.routines["daily"], self.state, TODAY)
        self.assertEqual(days, [date(2026, 11, 10), date(2026, 11, 11)])
        self.assertEqual(skipped, [(date(2026, 11, 9), "attempts-exhausted")])

    def test_day_flag_rejects_today_and_future(self):
        code, _out, err = self.cli("run", "daily", "--day", "2026-11-12")
        self.assertEqual(code, 2)
        self.assertIn("day 2026-11-12 has not ended in UTC", err)

    def test_force_rerun_that_fails_supersedes_the_done_stamp(self):
        self.state.mkdir(parents=True)
        (self.state / "done-2026-11-11.json").write_text(
            json.dumps({"status": "done", "attempt": 1, "cost_usd": 0.5, "history": []})
        )
        code, _out, _err = self.cli(
            "run", "daily", "--day", "2026-11-11", "--force", children=Children(validate=1)
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(self.events("day-superseded")), 1)
        self.assertIsNone(self.stamp("done", "2026-11-11"))
        failed = self.stamp("failed", "2026-11-11")
        assert failed is not None
        self.assertEqual((failed["reason"], failed["attempts"]), ("validate-exit-1", 1))
        self.assertEqual([h["reason"] for h in failed["history"]], ["done", "validate-exit-1"])
        self.assertAlmostEqual(failed["cost_usd_total"], 0.5 + 0.84)
        # The next scheduled run sees the day as pending again.
        code, out, _err = self.cli("run", "daily", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("day 2026-11-11 attempt 2", out)

    def test_corrupt_failed_stamp_fails_closed(self):
        self.state.mkdir(parents=True)
        stamp = self.state / "failed-2026-11-11.json"
        stamp.write_text('{"attempts": 3, "history": [')
        children = Children()
        code, _out, _err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 0)
        self.assertEqual(
            [e.split("day=")[1] for e in self.events("day-skipped")],
            ["2026-11-11 reason=corrupt-stamp"],
        )
        self.assertNotIn("2026-11-11", [a[-2] for a in children.argvs("validate")])
        self.assertEqual(stamp.read_text(), '{"attempts": 3, "history": [')

    def test_force_new_attempt_numbering(self):
        for number in (1, 2):
            self.attempt("2026-11-11", number).mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.state / "done-2026-11-11.json").write_text(
            json.dumps(
                {
                    "status": "done",
                    "attempt": 2,
                    "cost_usd": 0.5,
                    "finished_at": "2026-11-12T05:30:00+00:00",
                    "history": [{"attempt": 1, "reason": "validate-exit-1", "cost_usd": 0.47}],
                }
            )
        )
        code, _out, _err = self.cli("run", "daily", "--day", "2026-11-11")
        self.assertEqual(code, 0)
        self.assertEqual(self.events("day-skipped")[0].split("reason=")[1], "done")
        code, _out, _err = self.cli("run", "daily", "--day", "2026-11-11", "--force")
        self.assertEqual(code, 0)
        self.assertTrue(self.attempt("2026-11-11", 3).is_dir())
        done = self.stamp("done", "2026-11-11")
        assert done is not None
        self.assertEqual(done["attempt"], 3)
        self.assertEqual([h["attempt"] for h in done["history"]], [1, 2])
        self.assertAlmostEqual(done["cost_usd_day_total"], 0.47 + 0.5 + 0.84)


class TestRefresh(RoutineFixture):
    def test_since_is_earliest_day_minus_one_worked_example(self):
        children = Children(invoke=[0, 0, 124], validate=[0, 3])
        code, _out, _err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 1)
        refresh = children.argvs("refresh")
        self.assertEqual(
            [argv[3:] for argv in refresh],
            [
                ["claude", "export", "--since", "2026-11-08"],
                ["codex", "export", "--since", "2026-11-08"],
            ],
        )
        self.assertFalse(any("sync" in argv for _k, argv, _e, _c in children.calls))
        self.assertEqual(self.stamp("done", "2026-11-09")["status"], "done")  # type: ignore[index]
        self.assertEqual(self.stamp("done", "2026-11-10")["status"], "empty")  # type: ignore[index]
        failed = self.stamp("failed", "2026-11-11")
        assert failed is not None
        self.assertEqual((failed["reason"], failed["attempts"]), ("invoke-exit-124", 1))

        # Friday: 11-10 is done, 11-11 runs as attempt 2, then 11-12.
        with patch("cairn.routine_plan.utc_today", return_value=date(2026, 11, 13)):
            children = Children()
            code, _out, _err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 0)
        self.assertEqual(children.argvs("refresh")[0][-1], "2026-11-10")
        self.assertTrue(self.attempt("2026-11-11", 2).is_dir())
        self.assertIsNone(self.stamp("failed", "2026-11-11"))
        done = self.stamp("done", "2026-11-11")
        assert done is not None
        self.assertEqual(done["attempt"], 2)
        self.assertEqual(done["history"][0]["reason"], "invoke-exit-124")

    def test_failed_refresh_marks_incomplete_input_and_skips_invoke(self):
        children = Children(refresh=[0, 1])
        code, _out, _err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 1)
        self.assertEqual(children.argvs("invoke"), [])
        for day in ("2026-11-09", "2026-11-10", "2026-11-11"):
            failed = self.stamp("failed", day)
            assert failed is not None
            self.assertEqual((failed["reason"], failed["attempts"]), ("incomplete-input", 0))
            self.assertIs(failed["history"][0]["counted"], False)
        self.assertEqual(len(self.events("refresh")), 2)


class TestRun(RoutineFixture):
    def run_one(self, children: Children, *extra: str) -> int:
        code, _out, _err = self.cli(
            "run", "daily", "--day", "2026-11-11", *extra, children=children
        )
        return code

    def test_stamp_written_only_after_validate(self):
        children = Children()
        seen = []
        children.on_validate = lambda argv: seen.append(self.stamp("done", "2026-11-11"))
        self.assertEqual(self.run_one(children), 0)
        self.assertEqual(seen, [None])
        done = self.stamp("done", "2026-11-11")
        assert done is not None
        self.assertEqual(
            (done["status"], done["validate_exit"], done["thread_id"]), ("done", 0, "t-1")
        )
        attempt = json.loads((self.attempt("2026-11-11", 1) / "attempt.json").read_text())
        self.assertEqual(attempt["reason"], "done")

    def test_validate_exit_3_is_empty_day(self):
        self.assertEqual(self.run_one(Children(validate=3)), 0)
        done = self.stamp("done", "2026-11-11")
        assert done is not None
        self.assertEqual((done["status"], done["validate_exit"]), ("empty", 3))
        self.assertIn("result=empty", self.events("validate")[0])

    def test_validate_failure_failed_stamp(self):
        self.assertEqual(self.run_one(Children(validate=1)), 1)
        failed = self.stamp("failed", "2026-11-11")
        assert failed is not None
        self.assertEqual((failed["reason"], failed["attempts"]), ("validate-exit-1", 1))
        self.assertIsNone(self.stamp("done", "2026-11-11"))
        self.assertIn("      [stderr] boom", self.log.read_text())

    def test_invoke_nonzero_skips_validate(self):
        children = Children(invoke=124)
        self.assertEqual(self.run_one(children), 1)
        self.assertEqual(children.argvs("validate"), [])
        failed = self.stamp("failed", "2026-11-11")
        assert failed is not None
        self.assertEqual(failed["reason"], "invoke-exit-124")
        self.assertEqual(failed["invoke_exit"], 124)
        self.assertEqual(failed["cost_usd_total"], 0.84)

    def test_prepare_exit_3_skips_invoke(self):
        children = Children(prepare=3)
        self.assertEqual(self.run_one(children), 0)
        self.assertEqual(children.argvs("invoke"), [])
        self.assertEqual(children.argvs("validate"), [])
        done = self.stamp("done", "2026-11-11")
        assert done is not None
        self.assertEqual(done["status"], "empty")
        self.assertIsNone(done["invoke_exit"])
        self.assertEqual(done["prepare_exit"], 3)
        # prepare's stdout always lands in <ATTEMPT_DIR>/prepare.out for the peer.
        out = json.loads((self.attempt("2026-11-11", 1) / "prepare.out").read_text())
        self.assertEqual(out["sessions"], [{"id": "s1"}])

    def test_prepare_failure_counts_and_logs_stderr(self):
        self.assertEqual(self.run_one(Children(prepare=1)), 1)
        self.assertEqual(self.stamp("failed", "2026-11-11")["reason"], "prepare-exit-1")  # type: ignore[index]
        self.assertTrue((self.attempt("2026-11-11", 1) / "prepare.out").is_file())
        self.assertIn("      [stderr] boom", self.log.read_text())

    def test_first_failure_defers_remaining_days(self):
        children = Children(validate=[1])
        code, _out, _err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 1)
        self.assertEqual(len(children.argvs("invoke")), 1)
        deferred = self.events("day-deferred")
        self.assertEqual([line.split("day=")[1] for line in deferred], ["2026-11-10", "2026-11-11"])
        last = routine_plan.read_json(self.state / "last-run.json")
        assert last is not None
        self.assertEqual((last["failed"], last["deferred"], last["exit"]), (1, 2, 1))
        self.assertEqual(last["failures"], [{"day": "2026-11-09", "reason": "validate-exit-1"}])

    def test_cost_recorded_in_log_and_stamp(self):
        self.run_one(Children())
        self.assertIn("cost_usd=0.84", self.events("invoke")[0])
        self.assertEqual(self.stamp("done", "2026-11-11")["cost_usd"], 0.84)  # type: ignore[index]
        self.assertIn("cost_usd=0.84", self.events("end")[0])

    def test_codex_cost_is_null(self):
        self.write_config(peer="codex", profile="edit")
        self.run_one(Children(cost=None))
        self.assertIn("cost_usd=null", self.events("invoke")[0])
        self.assertIsNone(self.stamp("done", "2026-11-11")["cost_usd"])  # type: ignore[index]

    def test_budget_passes_remaining_via_extra_last(self):
        self.write_config(budget_usd_per_day=4.0)
        self.state.mkdir(parents=True)
        (self.state / "failed-2026-11-11.json").write_text(
            json.dumps({"attempts": 1, "history": [{"attempt": 1, "cost_usd": 0.47}]})
        )
        children = Children()
        self.run_one(children)
        self.assertEqual(children.argvs("invoke")[0][-3:], ["--extra", "--max-budget-usd", "3.53"])

    def test_budget_exhausted_no_invoke(self):
        self.write_config(budget_usd_per_day=1.0)
        self.state.mkdir(parents=True)
        (self.state / "failed-2026-11-11.json").write_text(
            json.dumps({"attempts": 1, "history": [{"attempt": 1, "cost_usd": 1.2}]})
        )
        children = Children()
        self.run_one(children)
        self.assertEqual(children.argvs("invoke"), [])
        failed = self.stamp("failed", "2026-11-11")
        assert failed is not None
        self.assertEqual((failed["reason"], failed["attempts"]), ("budget-exhausted", 1))

    def test_child_env_scrubbed_and_argv_shape(self):
        children = Children()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "host", "CAIRN_INVOKE_CALLER": "x"}):
            self.run_one(children)
        (_kind, argv, env, cwd) = next(c for c in children.calls if c[0] == "invoke")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("CAIRN_INVOKE_DEPTH", env)
        self.assertNotIn("CAIRN_INVOKE_CALLER", env)
        self.assertEqual(env["CAIRN_ROUTINE"], "daily")
        self.assertEqual(env["CAIRN_ROUTINE_DAY"], "2026-11-11")
        self.assertEqual(cwd, str(self.site))
        where = self.attempt("2026-11-11", 1)
        self.assertEqual(
            argv[3:],
            [
                "invoke",
                "claude",
                "--prompt-file",
                str(where / "prompt.md"),
                "--cd",
                str(self.site),
                "--profile",
                "scheduled",
                "--profile-args-file",
                str(where / "profile-args.json"),
                "--owner",
                "routine:daily",
                "--json",
            ],
        )
        args = json.loads((where / "profile-args.json").read_text())
        self.assertEqual(args[-1], str(self.cfg.data_root / "journal"))
        refresh_env = children.calls[0][2]
        self.assertNotIn("CAIRN_ROUTINE", refresh_env)

    def test_builtin_profile_has_no_args_file(self):
        self.write_config(profile="edit", model="opus", timeout_s=600)
        children = Children()
        self.run_one(children)
        argv = children.argvs("invoke")[0]
        self.assertNotIn("--profile-args-file", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--timeout-s") + 1], "600")
        self.assertNotIn("--extra", argv)

    def test_inside_peer_refused(self):
        children = Children()
        with patch.dict(os.environ, {"CAIRN_INVOKE_DEPTH": "1"}):
            code, _out, err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 2)
        self.assertIn("refusing to run inside a peer (CAIRN_INVOKE_DEPTH=1)", err)
        self.assertEqual(children.calls, [])

    def test_run_command_keeps_non_utf8_child_output(self):
        argv = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'ok\\xff\\n'); sys.stderr.buffer.write(b'\\xfe')",
        ]
        for timeout in (None, 30):
            with self.subTest(timeout=timeout):
                result = routine_run.run_command(
                    argv, timeout=timeout, env=dict(os.environ), cwd=None
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "ok\ufffd\n")
                self.assertEqual(result.stderr, "\ufffd")

    def test_dry_run_writes_nothing(self):
        children = Children()
        code, out, _err = self.cli("run", "daily", "--dry-run", children=children)
        self.assertEqual(code, 0)
        self.assertEqual(children.calls, [])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.cfg.data_root / "routines").exists())
        self.assertIn("refresh: ", out)
        self.assertIn("--since 2026-11-08", out)
        self.assertIn("day 2026-11-09 attempt 1 (would create", out)
        self.assertIn("Write the journal for 2026-11-11", out)
        self.assertIn("--- end ---", out)

    def test_log_line_format(self):
        self.cli("run", "daily", children=Children(validate=[0, 1]))
        lines = self.log.read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, LOG_LINE)
        # Not a TTY: each event is written once, to the log only.
        self.assertEqual(len(self.events("start")), 1)


class TestLock(RoutineFixture):
    def test_second_run_exits_zero_with_skip_line(self):
        self.state.mkdir(parents=True)
        path = lock_path(self.state, "routine")
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time\nfrom pathlib import Path\nfrom cairn.storage import locked\n"
                f"with locked(Path({str(path)!r})):\n print('held', flush=True)\n time.sleep(30)\n",
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        assert holder.stdout is not None
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        children = Children()
        code, _out, _err = self.cli("run", "daily", children=children)
        self.assertEqual(code, 0)
        self.assertEqual(children.calls, [])
        self.assertEqual(len(self.events("skipped-lock")), 1)
        self.assertEqual(self.events("start"), [])


class TestPlist(RoutineFixture):
    def render(self) -> dict:
        spec = self.cfg.routines["daily"]
        environment = routine.environment_block(self.cfg)
        return plistlib.loads(routine.render_plist(self.cfg, spec, environment, ["/x/cairn"]))

    def test_render_matches_template(self):
        plist = self.render()
        self.assertEqual(plist["Label"], "dev.cairn.routine.daily")
        self.assertEqual(plist["ProgramArguments"], ["/x/cairn", "routine", "run", "daily"])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 5, "Minute": 20})
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertIs(plist["RunAtLoad"], False)
        log = str(self.cfg.data_root / "logs" / "routine-daily.log")
        self.assertEqual((plist["StandardOutPath"], plist["StandardErrorPath"]), (log, log))
        self.assertEqual(plist["EnvironmentVariables"]["CAIRN_CONFIG"], str(self.config.resolve()))

    def test_env_block_omits_agent_homes_unless_set(self):
        with patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ.pop("CODEX_HOME", None)
            os.environ.pop("XDG_STATE_HOME", None)
            env = self.render()["EnvironmentVariables"]
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_STATE_HOME"):
            self.assertNotIn(name, env)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            env = self.render()["EnvironmentVariables"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str((self.root / "claude-home").resolve()))
        self.assertEqual(env["XDG_STATE_HOME"], str((self.root / "state").resolve()))

    def test_xml_escaping(self):
        data = json.loads(self.config.read_text())
        data["data_root"] = str(self.root / "a & <b>")
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        plist = self.render()
        self.assertIn("a & <b>", plist["StandardOutPath"])


class Launchctl:
    def __init__(self, loaded: bool = True):
        self.calls: list[tuple[str, ...]] = []
        self.loaded = loaded

    def __call__(self, *args, timeout=None):
        self.calls.append(args)
        code = 0 if (args[0] != "print" or self.loaded) else 113
        return subprocess.CompletedProcess(["launchctl", *args], code, "", "")


class TestLaunchd(RoutineFixture):
    def cli_launchd(self, *argv, fake: Launchctl, macos: bool = True):
        with (
            patch("cairn.routine.launchctl", fake),
            patch("cairn.routine.is_macos", return_value=macos),
        ):
            return self.cli(*argv)

    def test_install_boots_out_then_bootstraps(self):
        fake = Launchctl()
        code, out, _err = self.cli_launchd("install", "daily", fake=fake)
        self.assertEqual(code, 0, _err)
        uid = os.getuid()
        plist = Path.home() / "Library/LaunchAgents/dev.cairn.routine.daily.plist"
        self.assertEqual(
            fake.calls,
            [
                ("bootout", f"gui/{uid}/dev.cairn.routine.daily"),
                ("bootstrap", f"gui/{uid}", str(plist)),
            ],
        )
        self.assertEqual(plistlib.loads(plist.read_bytes())["Label"], "dev.cairn.routine.daily")
        self.assertIn("Installed dev.cairn.routine.daily: daily at 05:20 local", out)

    def test_non_macos_prints_crontab_line_exit_2(self):
        fake = Launchctl()
        code, out, err = self.cli_launchd("install", "daily", fake=fake, macos=False)
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertIn("launchd-only in this version", err)
        self.assertTrue(out.startswith("20 5 * * * /usr/bin/env "))
        self.assertIn("routine run daily", out)
        self.assertTrue(out.strip().endswith("# cairn routine daily"))

    def test_preflight_missing_prompt_exit_2(self):
        self.prompt.unlink()
        fake = Launchctl()
        code, _out, err = self.cli_launchd("install", "daily", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn(f"prompt_file not found: {self.prompt}", err)
        self.assertEqual(fake.calls, [])

    def test_uninstall_keeps_stamps(self):
        self.cli_launchd("install", "daily", fake=Launchctl())
        self.cli("run", "daily", "--day", "2026-11-11")
        fake = Launchctl()
        code, out, _err = self.cli_launchd("uninstall", "daily", fake=fake)
        self.assertEqual(code, 0)
        self.assertIn("Removed dev.cairn.routine.daily (stamps kept in", out)
        self.assertFalse(
            (Path.home() / "Library/LaunchAgents/dev.cairn.routine.daily.plist").exists()
        )
        self.assertIsNotNone(self.stamp("done", "2026-11-11"))

    def test_kickstart_default_without_k_and_force_restart_adds_k(self):
        target = f"gui/{os.getuid()}/dev.cairn.routine.daily"
        fake = Launchctl()
        self.assertEqual(self.cli_launchd("kickstart", "daily", fake=fake)[0], 0)
        self.assertEqual(fake.calls[-1], ("kickstart", target))
        fake = Launchctl()
        self.cli_launchd("kickstart", "daily", "--force-restart", fake=fake)
        self.assertEqual(fake.calls[-1], ("kickstart", "-k", target))
        fake = Launchctl(loaded=False)
        code, _out, err = self.cli_launchd("kickstart", "daily", fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("is not loaded; run 'cairn routine install daily'", err)

    def test_status_json_sections(self):
        self.cli("run", "daily", children=Children(validate=[0, 1]))
        code, out, _err = self.cli_launchd("status", "daily", "--json", fake=Launchctl())
        # A failed day needs attention: exit 1 (it was 0 before the hardening spec).
        self.assertEqual(code, 1)
        report = json.loads(out)
        for key in (
            "config",
            "paths",
            "installed",
            "last_run",
            "days",
            "last_cost_usd",
            "log_tail",
            "schema_version",
            "state",
            "exit",
            "attention",
            "paused",
            "disabled",
            "last_run_age_s",
            "stale_after_s",
        ):
            self.assertIn(key, report)
        self.assertEqual(
            (report["schema_version"], report["state"], report["exit"]), (1, "fresh", 1)
        )
        self.assertEqual(report["attention"], ["failed-day", "last-run-exit-1", "launchd-missing"])
        self.assertEqual(report["stale_after_s"], 172800)
        states = {row["day"]: row["state"] for row in report["days"]}
        self.assertEqual(
            states, {"2026-11-11": "pending", "2026-11-10": "failed", "2026-11-09": "done"}
        )
        self.assertEqual(report["installed"], "no")
        self.assertEqual(report["last_cost_usd"], 0.84)
        self.assertTrue(report["paths"]["latest_prepare_out"].endswith("attempt-1/prepare.out"))
        code, out, _err = self.cli_launchd("list", fake=Launchctl())
        self.assertIn("daily  peer=claude profile=scheduled at=05:20 installed=no", out)
        self.assertIn("pending=2 failed=1 state=fresh", out)


class TestDoctor(RoutineFixture):
    def checks(self, fake: Launchctl, macos: bool = True):
        with (
            patch("cairn.routine.launchctl", fake),
            patch("cairn.routine.is_macos", return_value=macos),
        ):
            return routine.doctor_checks(self.cfg)

    def test_routines_section_lines(self):
        lines = self.checks(Launchctl())
        self.assertIn(
            ("warn", "routine [daily] launchd", "not installed; run 'cairn routine install daily'"),
            lines,
        )
        self.assertIn(("warn", "routine [daily] last run", "never ran"), lines)
        self.assertIn(
            ("warn", "routine [daily] stamps", "pending 2026-11-09, 2026-11-10, 2026-11-11"), lines
        )
        self.cli("run", "daily", children=Children(invoke=[0, 0, 124]))
        plist = Path.home() / "Library/LaunchAgents/dev.cairn.routine.daily.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("x")
        lines = dict(
            ((label, (level, detail)) for level, label, detail in self.checks(Launchctl()))
        )
        self.assertEqual(
            lines["routine [daily] launchd"],
            ("ok", "dev.cairn.routine.daily loaded, daily at 05:20 local"),
        )
        level, detail = lines["routine [daily] last run"]
        self.assertEqual(level, "warn")
        self.assertIn("exit 1 (failed 1: 2026-11-11 invoke-exit-124)", detail)
        self.assertEqual(
            lines["routine [daily] stamps"],
            ("warn", "failed 2026-11-11 (invoke-exit-124, 1/3 attempts)"),
        )
        lines = dict(
            (
                (label, (level, detail))
                for level, label, detail in self.checks(Launchctl(loaded=False))
            )
        )
        self.assertEqual(lines["routine [daily] launchd"][0], "warn")
        self.assertIn("plist present but not loaded", lines["routine [daily] launchd"][1])
        lines = dict(
            (
                (label, (level, detail))
                for level, label, detail in self.checks(Launchctl(), macos=False)
            )
        )
        self.assertEqual(
            lines["routine [daily] launchd"], ("warn", "launchd only; not installed on this OS")
        )

    def test_journal_line_variants(self):
        journal = self.cfg.data_root / "journal"
        lines = {label: (level, detail) for level, label, detail in self.checks(Launchctl())}
        self.assertEqual(
            lines["routine [daily] journal"], ("ok", f"{journal} is the 'journal' collection")
        )
        data = json.loads(self.config.read_text())
        del data["collections"]["journal"]
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        lines = {label: (level, detail) for level, label, detail in self.checks(Launchctl())}
        self.assertIn("has no 'journal' collection in config", lines["routine [daily] journal"][1])
        self.write_config(outputs={"journal_dir": str(self.root / "notes")})
        lines = {label: (level, detail) for level, label, detail in self.checks(Launchctl())}
        self.assertIn("is outside", lines["routine [daily] journal"][1])

    def test_doctor_prints_routine_lines(self):
        from cairn import admin

        out = io.StringIO()
        with (
            patch("cairn.routine.launchctl", Launchctl()),
            patch("cairn.routine.is_macos", return_value=True),
            redirect_stdout(out),
        ):
            admin._check_routines(self.cfg)
        text = out.getvalue()
        self.assertIn("  warn routine [daily] launchd - not installed", text)
        self.assertIn("  ok   routine [daily] journal - ", text)

    def test_no_routines_prints_nothing(self):
        data = json.loads(self.config.read_text())
        del data["routines"]
        self.config.write_text(json.dumps(data))
        config.get_config.cache_clear()
        self.assertEqual(self.checks(Launchctl()), [])


# ---------------------------------------------------------------------------
# Hardening (docs/routine-hardening-spec.md)
# ---------------------------------------------------------------------------

HOOK_SCRIPT = """\
import json, os, sys
record = {
    "env": {k: v for k, v in os.environ.items() if k.startswith("CAIRN_ROUTINE")},
    "stdin": sys.stdin.read(),
}
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
sys.stderr.write("hook said no\\n")
sys.exit(int(sys.argv[2]))
"""


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class HardeningFixture(RoutineFixture):
    def setUp(self):
        super().setUp()
        macos = patch("cairn.routine.is_macos", return_value=False)
        macos.start()
        self.addCleanup(macos.stop)

    def last_run(self) -> dict:
        last = routine_plan.read_json(self.state / "last-run.json")
        assert last is not None
        return last

    def hook_config(self, exit_code: int = 0, **overrides) -> Path:
        script = self.root / "hook.py"
        script.write_text(HOOK_SCRIPT)
        calls = self.root / "hook-calls.jsonl"
        self.write_config(
            terminal_hook=[sys.executable, str(script), str(calls), str(exit_code)], **overrides
        )
        return calls

    @staticmethod
    def hook_calls(calls: Path) -> list[dict]:
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    def json_run(self, *argv: str, children: Children | None = None) -> tuple[int, dict, str]:
        code, out, err = self.cli("run", "daily", "--json", *argv, children=children)
        return code, json.loads(out), err


class TestPauseResume(HardeningFixture):
    def test_pause_writes_marker_and_run_skips_without_stamp(self):
        code, out, _err = self.cli("pause", "daily", "--reason", "  moving house ")
        self.assertEqual(code, 0)
        self.assertEqual(
            out, "Paused daily: moving house (runs skip until 'cairn routine resume daily')\n"
        )
        marker = routine_plan.read_json(self.state / "paused.json")
        assert marker is not None
        self.assertEqual(marker["reason"], "moving house")
        children = Children()
        for extra in ((), ("--day", "2026-11-11", "--force")):
            with self.subTest(extra=extra):
                code, out, err = self.cli("run", "daily", *extra, children=children)
                self.assertEqual(code, 0)
                self.assertEqual(out, "")
                self.assertEqual(
                    err.splitlines()[-1],
                    f"routine daily: paused since {marker['paused_at']} (moving house); "
                    "run 'cairn routine resume daily' to resume",
                )
        self.assertEqual(children.calls, [])
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["paused.json"])
        self.assertEqual(len(self.events("skipped-paused")), 2)
        self.assertIn('reason="moving house"', self.events("skipped-paused")[0])
        code, out, _err = self.cli("pause", "daily", "--reason", "still moving")
        self.assertEqual(out, "daily was already paused; reason updated: still moving\n")
        again = routine_plan.read_json(self.state / "paused.json")
        assert again is not None
        self.assertEqual(
            (again["reason"], again["paused_at"]), ("still moving", marker["paused_at"])
        )
        code, out, _err = self.cli("run", "daily", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("routine daily: paused since "))
        self.assertIn("day 2026-11-09 attempt 1 (would create", out)

    def test_pause_requires_reason_and_rejects_newline(self):
        message = "cairn routine: --reason must be non-empty, one line, at most 200 characters\n"
        for reason in ("", "   ", "one\ntwo", "x" * 201):
            with self.subTest(reason=reason):
                code, _out, err = self.cli("pause", "daily", "--reason", reason)
                self.assertEqual((code, err), (2, message))
        self.assertFalse((self.state / "paused.json").exists())
        self.assertEqual(self.cli("pause", "daily", "--reason", "x" * 200)[0], 0)
        code, _out, err = self.cli("pause", "nightly", "--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("routine 'nightly' is not in routines", err)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            routine.main(["pause", "daily"])
        self.assertEqual(caught.exception.code, 2)

    def test_resume_removes_markers_and_breaker(self):
        self.state.mkdir(parents=True)
        for name in ("paused.json", "disabled.json", "breaker.json"):
            (self.state / name).write_text("{}")
        code, out, _err = self.cli("resume", "daily")
        self.assertEqual(code, 0)
        self.assertEqual(out, "Resumed daily (removed: paused.json, disabled.json, breaker.json)\n")
        self.assertEqual(list(self.state.iterdir()), [])
        code, out, _err = self.cli("resume", "daily")
        self.assertEqual((code, out), (0, "daily is not paused or disabled; nothing to resume\n"))
        # A routine already removed from config can still be resumed.
        gone = routine_run.state_dir(self.cfg, "gone")
        gone.mkdir(parents=True)
        (gone / "paused.json").write_text("{}")
        code, out, _err = self.cli("resume", "gone")
        self.assertEqual((code, out), (0, "Resumed gone (removed: paused.json)\n"))

    def test_install_keeps_pause_marker_and_notes_it(self):
        self.cli("pause", "daily", "--reason", "moving house")
        note = (
            "note: routine daily is paused (moving house); the job is installed but each run "
            "skips until 'cairn routine resume daily'\n"
        )
        with (
            patch("cairn.routine.launchctl", Launchctl()),
            patch("cairn.routine.is_macos", return_value=True),
        ):
            code, _out, err = self.cli("install", "daily")
            self.assertEqual(code, 0)
            self.assertTrue(err.endswith(note), err)  # after the PATH warning for 'check'
            code, _out, err = self.cli("kickstart", "daily")
            self.assertEqual((code, err), (0, note))
        self.assertTrue((self.state / "paused.json").is_file())

    def test_list_appends_state(self):
        code, out, _err = self.cli("list")
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("pending=3 failed=0 state=never_run"), out)
        self.cli("run", "daily")
        self.cli("pause", "daily", "--reason", "away")
        code, out, _err = self.cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["state"], "paused")

    def test_doctor_reports_paused_and_disabled(self):
        self.cli("pause", "daily", "--reason", "moving house")
        lines = {label: (level, detail) for level, label, detail in routine.doctor_checks(self.cfg)}
        level, detail = lines["routine [daily] paused"]
        self.assertEqual(level, "warn")
        self.assertRegex(
            detail,
            r"^paused since \S+ \(moving house\); run 'cairn routine resume daily' to resume$",
        )
        (self.state / "disabled.json").write_text("{not json")
        lines = {label: (level, detail) for level, label, detail in routine.doctor_checks(self.cfg)}
        self.assertNotIn("routine [daily] paused", lines)
        self.assertEqual(
            lines["routine [daily] disabled"],
            (
                "warn",
                "disabled since unknown ((unreadable marker)); fix the cause, then run "
                "'cairn routine resume daily'",
            ),
        )


class TestBreaker(HardeningFixture):
    def test_breaker_disables_after_three_counted_failed_runs(self):
        for number in (1, 2, 3):
            code, report, err = self.json_run(children=Children(validate=1))
            self.assertEqual((code, report["status"]), (1, "failed"))
            self.assertEqual(report["breaker"]["consecutive_failed_runs"], number)
        self.assertIs(report["breaker"]["disabled"], True)
        reason = "3 consecutive failed runs: validate-exit-1, validate-exit-1, validate-exit-1"
        marker = routine_plan.read_json(self.state / "disabled.json")
        assert marker is not None
        self.assertEqual(marker["reason"], reason)
        self.assertEqual(len(marker["runs"]), 3)
        self.assertIn("routine daily: disabled since ", err)
        self.assertEqual(len(self.events("disabled")), 1)
        children = Children()
        code, report, err = self.json_run("--day", "2026-11-10", "--force", children=children)
        self.assertEqual((code, report["status"], report["reason"]), (0, "skipped", "disabled"))
        self.assertEqual(children.calls, [])
        self.assertIn(f"({reason}); fix the cause, then run 'cairn routine resume daily'", err)
        self.assertEqual(len(self.events("skipped-disabled")), 1)
        code, _out, _err = self.cli("status", "daily")
        self.assertEqual(code, 2)
        self.assertEqual(self.cli("resume", "daily")[0], 0)
        code, report, _err = self.json_run(children=Children())
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.assertEqual(report["breaker"], {"consecutive_failed_runs": 0, "disabled": False})

    def test_breaker_ignores_uncounted_failures_and_resets_on_done(self):
        for _ in range(3):
            code, report, _err = self.json_run(children=Children(refresh=1))
            self.assertEqual((code, report["status"]), (1, "failed"))
        self.assertFalse((self.state / "breaker.json").exists())
        self.assertFalse((self.state / "disabled.json").exists())
        for _ in range(2):
            self.json_run(children=Children(validate=1))
        breaker = routine_plan.read_json(self.state / "breaker.json")
        assert breaker is not None
        self.assertEqual(breaker["consecutive_failed_runs"], 2)
        self.assertEqual(breaker["runs"][-1]["reason"], "validate-exit-1")
        code, report, _err = self.json_run(children=Children(validate=[0, 1]))
        self.assertEqual((code, report["status"]), (1, "partial"))
        self.assertFalse((self.state / "breaker.json").exists())
        self.assertEqual(report["breaker"]["consecutive_failed_runs"], 0)

    def test_marker_that_lands_before_the_lock_still_skips(self):
        # A run that held the lock tripped the breaker (or a pause landed) between this
        # run's first marker check and its lock: the check under the lock catches it.
        self.json_run(children=Children())
        before = self.last_run()
        real_locked = routine_run.locked
        state = self.state

        @contextmanager
        def tripping_lock(*args, **kwargs):
            with real_locked(*args, **kwargs):
                (state / "disabled.json").write_text(json.dumps({"reason": "tripped"}))
                yield

        children = Children()
        with patch("cairn.routine_run.locked", tripping_lock):
            code, report, err = self.json_run("--day", "2026-11-10", "--force", children=children)
        self.assertEqual((code, report["status"], report["reason"]), (0, "skipped", "disabled"))
        self.assertEqual(children.calls, [])
        self.assertIn("(tripped); fix the cause, then run 'cairn routine resume daily'", err)
        self.assertFalse((self.state / "running.json").exists())
        self.assertEqual(self.last_run(), before)


class TestTerminalHook(HardeningFixture):
    def test_hook_runs_once_per_stamp_with_env_and_stdin(self):
        calls = self.hook_config()
        code, report, _err = self.json_run(children=Children(validate=[0, 1]))
        self.assertEqual(code, 1)
        records = self.hook_calls(calls)
        self.assertEqual(len(records), 2)  # done 11-09, failed 11-10; 11-11 deferred
        done, failed = records
        where = str(self.attempt("2026-11-09", 1))
        self.assertEqual(
            done["env"],
            {
                "CAIRN_ROUTINE": "daily",
                "CAIRN_ROUTINE_DAY": "2026-11-09",
                "CAIRN_ROUTINE_ATTEMPT_DIR": where,
                "CAIRN_ROUTINE_OUTCOME": "done",
                "CAIRN_ROUTINE_REASON": "",
                "CAIRN_ROUTINE_ATTEMPT": "1",
                "CAIRN_ROUTINE_RUN_ID": report["run_id"],
            },
        )
        self.assertEqual(
            (failed["env"]["CAIRN_ROUTINE_OUTCOME"], failed["env"]["CAIRN_ROUTINE_REASON"]),
            ("failed", "validate-exit-1"),
        )
        row = json.loads(done["stdin"])
        self.assertTrue(done["stdin"].endswith("}\n"))
        self.assertEqual((row["routine"], row["run_id"]), ("daily", report["run_id"]))
        self.assertEqual(
            (row["day"], row["outcome"], row["attempt_dir"]), ("2026-11-09", "done", where)
        )
        self.assertEqual(len(self.events("hook")), 2)
        self.assertIn("exit=0", self.events("hook")[0])
        outcomes = [
            (r["day"], r["outcome"], r["hook"] and r["hook"]["exit"]) for r in report["rows"]
        ]
        self.assertEqual(
            outcomes,
            [
                ("2026-11-09", "done", 0),
                ("2026-11-10", "failed", 0),
                ("2026-11-11", "deferred", None),
            ],
        )

    def test_hook_failure_and_timeout_never_change_stamp_or_exit(self):
        calls = self.hook_config(exit_code=5)
        code, report, _err = self.json_run("--day", "2026-11-11")
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.assertEqual(len(self.hook_calls(calls)), 1)
        self.assertIsNotNone(self.stamp("done", "2026-11-11"))
        self.assertIn("exit=5", self.events("hook")[0])
        self.assertIn("      [stderr] hook said no", self.log.read_text())
        self.assertEqual(report["rows"][0]["hook"]["exit"], 5)

        timeout = subprocess.TimeoutExpired(["hook"], 10, stderr=b"slow hook\n")
        with patch("cairn.routine_run.run_hook", side_effect=timeout):
            code, report, _err = self.json_run("--day", "2026-11-10")
        self.assertEqual(code, 0)
        self.assertEqual(report["rows"][0]["hook"]["exit"], "timeout")
        self.assertIn("exit=timeout", self.events("hook")[-1])
        self.assertIsNotNone(self.stamp("done", "2026-11-10"))

        self.write_config(terminal_hook=[str(self.root / "no-such-hook")])
        code, report, _err = self.json_run("--day", "2026-11-09")
        self.assertEqual(code, 0)
        self.assertEqual(report["rows"][0]["hook"]["exit"], "missing")
        self.assertIsNotNone(self.stamp("done", "2026-11-09"))
        for line in self.log.read_text().splitlines():
            self.assertRegex(line, LOG_LINE)

    def test_hook_refused_or_broken_never_changes_stamp_or_exit(self):
        script = self.root / "not-executable"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)
        self.write_config(terminal_hook=[str(script)])
        code, report, _err = self.json_run("--day", "2026-11-11")
        self.assertEqual((code, report["rows"][0]["hook"]["exit"]), (0, 126))
        self.assertIsNotNone(self.stamp("done", "2026-11-11"))
        broken = ProcessCleanupError("cleanup incomplete: pid 1 was not reaped 2s after SIGKILL")
        with patch("cairn.routine_run.run_hook", side_effect=broken):
            code, report, _err = self.json_run("--day", "2026-11-10")
        self.assertEqual((code, report["rows"][0]["hook"]["exit"]), (0, "error"))
        self.assertIn("exit=error", self.events("hook")[-1])
        self.assertIn("ProcessCleanupError: cleanup incomplete", self.log.read_text())
        self.assertIsNotNone(self.stamp("done", "2026-11-10"))

    def test_hook_runs_per_day_on_incomplete_input(self):
        calls = self.hook_config()
        code, _report, _err = self.json_run(children=Children(refresh=1))
        self.assertEqual(code, 1)
        records = self.hook_calls(calls)
        self.assertEqual(
            [r["env"]["CAIRN_ROUTINE_DAY"] for r in records],
            ["2026-11-09", "2026-11-10", "2026-11-11"],
        )
        for record in records:
            env = record["env"]
            self.assertEqual(env["CAIRN_ROUTINE_REASON"], "incomplete-input")
            self.assertEqual(
                (env["CAIRN_ROUTINE_ATTEMPT"], env["CAIRN_ROUTINE_ATTEMPT_DIR"]), ("", "")
            )
            self.assertIsNone(json.loads(record["stdin"])["attempt"])


class TestStaleSources(HardeningFixture):
    def setUp(self):
        super().setUp()
        self.write_config(stale_sources=...)  # the default: warn

    def receipt(self, source: str, hours_ago: float) -> str:
        from datetime import UTC, datetime, timedelta

        from cairn import export_receipt

        when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        output_dir = self.cfg.output_dir(f"{source}-sessions")
        path = export_receipt.receipt_path(source, output_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"source": source, "output_dir": str(output_dir.resolve()), "last_ok_at": when}
            )
        )
        return when

    def test_stale_source_warns_and_reports(self):
        code, report, err = self.json_run("--day", "2026-11-11")
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.assertEqual(
            err,
            "warning: routine daily: source antigravity has no export record; add it to "
            "routines.daily.sources or run 'cairn sync'\n",
        )
        self.assertEqual(
            report["stale_sources"],
            [{"source": "antigravity", "last_export_at": None, "age_h": None, "basis": "none"}],
        )
        self.assertIn("source=antigravity age_h=null basis=none", self.events("stale-source")[0])
        code, out, _err = self.cli("status", "daily", "--json")
        self.assertIn("stale-source", json.loads(out)["attention"])

        when = self.receipt("antigravity", 71.2)
        code, report, err = self.json_run("--day", "2026-11-10")
        self.assertEqual(
            err,
            f"warning: routine daily: source antigravity was last exported {when} (71.2 h ago); "
            "add it to routines.daily.sources or run 'cairn sync'\n",
        )
        self.assertEqual(report["stale_sources"][0]["basis"], "receipt")
        self.receipt("antigravity", 1)
        code, report, err = self.json_run("--day", "2026-11-09")
        self.assertEqual((err, report["stale_sources"]), ("", []))

    def test_stale_source_refuse_fails_days_uncounted_without_invoke(self):
        self.write_config(stale_sources="refuse")
        children = Children()
        code, report, _err = self.json_run(children=children)
        self.assertEqual((code, report["status"]), (1, "failed"))
        self.assertEqual([k for k, *_rest in children.calls], ["refresh", "refresh"])
        for day in ("2026-11-09", "2026-11-10", "2026-11-11"):
            failed = self.stamp("failed", day)
            assert failed is not None
            self.assertEqual((failed["reason"], failed["attempts"]), ("stale-source", 0))
        self.assertFalse((self.state / "breaker.json").exists())
        code, report, _err = self.json_run("--stale-sources", "warn", children=Children())
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.write_config(stale_sources="warn")
        code, report, err = self.json_run(
            "--day", "2026-11-11", "--force", "--stale-sources", "ignore"
        )
        self.assertEqual((code, err, report["stale_sources"]), (0, "", []))

    def test_stale_source_ignores_routine_sources_and_disabled_collections(self):
        self.write_config(sources=["claude", "codex", "antigravity"], stale_sources=...)
        code, report, err = self.json_run("--day", "2026-11-11")
        self.assertEqual((code, err, report["stale_sources"]), (0, "", []))
        data = json.loads(self.config.read_text())
        data["collections"]["antigravity-sessions"]["sync"] = {"enabled": False}
        self.config.write_text(json.dumps(data))
        self.write_config(stale_sources=...)
        code, report, err = self.json_run("--day", "2026-11-10")
        self.assertEqual((code, err, report["stale_sources"]), (0, "", []))

    def test_receipt_without_a_successful_export_falls_back_to_catalog_mtime(self):
        from cairn import export_receipt
        from cairn.catalog import catalog_path

        output_dir = self.cfg.output_dir("antigravity-sessions")
        export_receipt.write("antigravity", output_dir, "export", 1)  # last_ok_at: null
        catalog = catalog_path(output_dir)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text("{}")
        code, report, err = self.json_run("--day", "2026-11-11")
        self.assertEqual((code, err, report["stale_sources"]), (0, "", []))
        old = catalog.stat().st_mtime - 3 * 86_400
        os.utime(catalog, (old, old))
        code, report, err = self.json_run("--day", "2026-11-10")
        self.assertEqual(report["stale_sources"][0]["basis"], "catalog-mtime")

    def test_clean_run_reports_stale_sources_too(self):
        self.receipt("antigravity", 1)
        code, report, _err = self.json_run(children=Children())
        self.assertEqual((code, report["status"], report["stale_sources"]), (0, "ok", []))
        self.receipt("antigravity", 30)
        code, report, err = self.json_run()
        self.assertEqual((code, report["status"]), (0, "clean"))
        self.assertEqual(report["stale_sources"][0]["source"], "antigravity")
        self.assertIn("source antigravity was last exported ", err)
        code, out, _err = self.cli("status", "daily", "--json")
        self.assertIn("stale-source", json.loads(out)["attention"])
        self.receipt("antigravity", 1)  # the user ran 'cairn sync'
        code, report, err = self.json_run()
        self.assertEqual((report["status"], report["stale_sources"], err), ("clean", [], ""))
        code, out, _err = self.cli("status", "daily", "--json")
        self.assertEqual((code, json.loads(out)["attention"]), (0, []))

    def test_stale_source_falls_back_to_catalog_mtime(self):
        from cairn.catalog import catalog_path

        catalog = catalog_path(self.cfg.output_dir("antigravity-sessions"))
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text("{}")
        code, report, err = self.json_run("--day", "2026-11-11")
        self.assertEqual((err, report["stale_sources"]), ("", []))
        old = catalog.stat().st_mtime - 3 * 86_400
        os.utime(catalog, (old, old))
        code, report, err = self.json_run("--day", "2026-11-10")
        self.assertEqual(code, 0)
        entry = report["stale_sources"][0]
        self.assertEqual((entry["basis"], entry["age_h"]), ("catalog-mtime", 72.0))
        self.assertIn("source antigravity was last exported ", err)


class TestRunReport(HardeningFixture):
    def test_run_json_report_status_values(self):
        code, report, _err = self.json_run(children=Children(validate=[0, 1]))
        self.assertEqual((code, report["status"], report["schema_version"]), (1, "partial", 1))
        code, report, _err = self.json_run(children=Children(validate=1))
        self.assertEqual((code, report["status"]), (1, "failed"))
        code, report, _err = self.json_run(children=Children())
        self.assertEqual((code, report["status"]), (0, "ok"))
        code, report, _err = self.json_run()
        self.assertEqual((code, report["status"], report["rows"]), (0, "clean", []))
        self.cli("pause", "daily", "--reason", "away")
        code, report, _err = self.json_run()
        self.assertEqual((code, report["status"], report["reason"]), (0, "skipped", "paused"))
        self.assertEqual(self.last_run()["status"], "clean")  # a skip keeps the last real run
        code, _out, err = self.cli("run", "daily", "--json", "--dry-run")
        self.assertEqual(
            (code, err), (2, "cairn routine: --json does not combine with --dry-run\n")
        )

    def test_run_json_stdout_is_one_object_on_tty(self):
        out = TtyStringIO()
        with (
            patch("cairn.routine_run.run_command", Children(validate=[0, 1])),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = routine.main(["run", "daily", "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue())
        self.assertEqual(report, self.last_run())
        out = TtyStringIO()
        with (
            patch("cairn.routine_run.run_command", Children()),
            redirect_stdout(out),
        ):
            routine.main(["run", "daily", "--day", "2026-11-10", "--force"])
        self.assertIn(" event=start ", out.getvalue())  # without --json a TTY still echoes

    def test_lock_held_json_report_keeps_last_run(self):
        self.json_run(children=Children())
        before = self.last_run()

        def held(*_args, **_kwargs):
            raise BlockingIOError

        out = TtyStringIO()
        children = Children()
        with (
            patch("cairn.routine_run.locked", held),
            patch("cairn.routine_run.run_command", children),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = routine.main(["run", "daily", "--json", "--day", "2026-11-10", "--force"])
        report = json.loads(out.getvalue())  # one object, no TTY skip line
        self.assertEqual(code, 0)
        self.assertEqual(
            (report["status"], report["reason"], report["exit"], report["rows"]),
            ("skipped", "lock-held", 0, []),
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(children.calls, [])
        self.assertEqual(self.last_run(), before)

    def test_running_marker_names_this_process_while_body_runs(self):
        seen: list[dict] = []

        def body(run, today, day, force):
            seen.append(json.loads((run.sdir / "running.json").read_text()))
            return run.finish(0)

        with patch.object(routine_run.Run, "body", autospec=True, side_effect=body):
            code, _out, _err = self.cli("run", "daily")
        self.assertEqual(code, 0)
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertEqual(seen[0]["run_id"], self.last_run()["run_id"])
        self.assertFalse((self.state / "running.json").exists())

    def test_last_run_json_keeps_legacy_keys(self):
        self.cli("run", "daily", children=Children(validate=[0, 1]))
        last = self.last_run()
        legacy = ("run_id", "started_at", "finished_at", "exit", "days", "done", "empty")
        for key in (*legacy, "failed", "deferred", "cost_usd", "error", "failures"):
            self.assertIn(key, last)
        self.assertEqual(last["days"], ["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertEqual(
            (last["done"], last["failed"], last["deferred"], last["exit"]), (1, 1, 1, 1)
        )
        self.assertEqual(
            (last["schema_version"], last["routine"], last["status"]), (1, "daily", "partial")
        )
        self.assertFalse((self.state / "running.json").exists())
