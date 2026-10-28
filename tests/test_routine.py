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
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from cairn import config, routine, routine_plan, routine_run
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

    def __call__(self, *args):
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
        self.assertEqual(code, 0)
        report = json.loads(out)
        for key in (
            "config",
            "paths",
            "installed",
            "last_run",
            "days",
            "last_cost_usd",
            "log_tail",
        ):
            self.assertIn(key, report)
        states = {row["day"]: row["state"] for row in report["days"]}
        self.assertEqual(
            states, {"2026-11-11": "pending", "2026-11-10": "failed", "2026-11-09": "done"}
        )
        self.assertEqual(report["installed"], "no")
        self.assertEqual(report["last_cost_usd"], 0.84)
        self.assertTrue(report["paths"]["latest_prepare_out"].endswith("attempt-1/prepare.out"))
        code, out, _err = self.cli_launchd("list", fake=Launchctl())
        self.assertIn("daily  peer=claude profile=scheduled at=05:20 installed=no", out)
        self.assertIn("pending=2 failed=1", out)


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
