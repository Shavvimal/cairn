"""``routines`` and ``invoke.<peer>.profiles``: parsing, defaults and the exact errors."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cairn import config
from cairn.admin import _apply_choices

DATA_ROOT = "/tmp/cairn-routines"


def _build(routines: object = None, invoke: dict | None = None, collections=None):
    data: dict = {
        "data_root": DATA_ROOT,
        "collections": (
            collections
            if collections is not None
            else {
                "claude-code-sessions": {"store": "/tmp/claude"},
                "codex-sessions": {"store": "/tmp/codex"},
                "cursor-sessions": {"store": "/tmp/cursor", "sync": {"enabled": False}},
                "service-docs": {},
            }
        ),
        "project_groups": {},
        "repo_catalog": {},
        "project_descriptions": {},
    }
    if routines is not None:
        data["routines"] = routines
    if invoke is not None:
        data["invoke"] = invoke
    return config._build(data, Path(f"{DATA_ROOT}/config.json"), None)


def _routine(**overrides) -> dict:
    spec = {
        "peer": "claude",
        "prompt_file": "/tmp/site/prompts/daily.md",
        "cd": "/tmp/site",
        "at": "05:20",
        "validate": ["true"],
    }
    spec.update(overrides)
    return {key: value for key, value in spec.items() if value is not ...}


SCHEDULED = {
    "claude": {
        "profiles": {
            "scheduled": {
                "base": "edit",
                "extra_args": ["--permission-mode", "dontAsk", "--add-dir", "{{JOURNAL_DIR}}"],
            }
        }
    }
}


class TestRoutineParsing(unittest.TestCase):
    def assertConfigError(self, message: str, *args, **kwargs):
        with self.assertRaises(config.ConfigError) as caught:
            _build(*args, **kwargs)
        self.assertEqual(str(caught.exception), message)

    def test_routine_minimal_parses_with_defaults(self):
        cfg = _build({"daily": _routine()}, {"claude": {"profile": "edit"}})
        spec = cfg.routines["daily"]
        self.assertEqual(spec.profile, "edit")
        self.assertEqual(spec.day, "yesterday-utc")
        self.assertEqual(spec.catch_up_days, 3)
        self.assertEqual(spec.sources, ())
        self.assertIsNone(spec.model)
        self.assertIsNone(spec.timeout_s)
        self.assertIsNone(spec.prepare)
        self.assertEqual(spec.validate, ("true",))
        self.assertEqual(spec.outputs, {"journal_dir": Path(DATA_ROOT) / "journal"})
        # An explicit outputs object without journal_dir still gets the default key.
        cfg = _build({"daily": _routine(outputs={"inbox_dir": "/tmp/inbox"})})
        self.assertEqual(
            cfg.routines["daily"].outputs,
            {"inbox_dir": Path("/tmp/inbox"), "journal_dir": Path(DATA_ROOT) / "journal"},
        )
        self.assertEqual(_build().routines, {})
        self.assertEqual(_build({"//": "comment"}).routines, {})

    def test_routine_name_regex_rejects_bad_names(self):
        for name in ("Daily", "a/b", "..", "-x", "x" * 65, ""):
            with self.subTest(name=name):
                self.assertConfigError(
                    f"routine name {name!r} is invalid: use 1 to 64 characters from a-z, "
                    "0-9 and '-', starting with a letter or digit",
                    {name: _routine()},
                )
        self.assertIn("x" * 64, _build({"x" * 64: _routine()}).routines)

    def test_routine_block_and_entry_must_be_objects(self):
        self.assertConfigError("'routines' must be an object of named routines, got list", [])
        self.assertConfigError("routines.daily must be an object, got str", {"daily": "x"})

    def test_routine_unknown_key_rejected(self):
        self.assertConfigError(
            "routines.daily has unknown keys: schedule. Allowed keys: at, budget_usd_per_day, "
            "catch_up_days, cd, day, model, outputs, peer, prepare, profile, prompt_file, "
            "sources, stale_sources, terminal_hook, timeout_s, validate.",
            {"daily": _routine(schedule="daily")},
        )

    def test_routine_missing_required_keys(self):
        self.assertConfigError(
            "routines.daily is missing required keys: cd, validate",
            {"daily": _routine(cd=..., validate=...)},
        )

    def test_routine_peer_must_be_claude_or_codex(self):
        self.assertConfigError(
            "routines.daily.peer must be one of claude, codex, got 'antigravity'",
            {"daily": _routine(peer="antigravity")},
        )

    def test_routine_relative_path_rejected(self):
        self.assertConfigError(
            "routines.daily.prompt_file must be absolute after expanding ~ and {{DATA_ROOT}}, "
            "got 'prompts/a.md'",
            {"daily": _routine(prompt_file="prompts/a.md")},
        )
        self.assertConfigError(
            "routines.daily.cd must be a non-empty string, got ''", {"daily": _routine(cd="")}
        )

    def test_routine_path_with_spaces_allowed(self):
        spec = _build({"daily": _routine(prompt_file="~/My Notes/p.md")}).routines["daily"]
        self.assertEqual(spec.prompt_file, Path.home() / "My Notes" / "p.md")

    def test_routine_dollar_var_not_expanded(self):
        self.assertConfigError(
            "routines.daily.prompt_file must be absolute after expanding ~ and {{DATA_ROOT}}, "
            "got '$HOME/p.md'",
            {"daily": _routine(prompt_file="$HOME/p.md")},
        )

    def test_routine_path_rejects_day_variable(self):
        self.assertConfigError(
            "routines.daily.outputs.journal_dir may only use {{DATA_ROOT}}, found {{DAY}}",
            {"daily": _routine(outputs={"journal_dir": "{{DATA_ROOT}}/journal/{{DAY}}"})},
        )

    def test_routine_outputs_key_collision_rejected(self):
        for key, token in (("day", "DAY"), ("attempt_dir", "ATTEMPT_DIR")):
            with self.subTest(key=key):
                self.assertConfigError(
                    f"routines.daily.outputs key {key!r} renders as {{{{{token}}}}}, which is "
                    "a built-in variable",
                    {"daily": _routine(outputs={key: "/tmp/x"})},
                )
        self.assertConfigError(
            "routines.daily.outputs key 'Inbox' is invalid: use a-z, 0-9 and '_', starting "
            "with a letter",
            {"daily": _routine(outputs={"Inbox": "/tmp/x"})},
        )
        self.assertConfigError(
            "routines.daily.outputs must be an object of name -> directory, got list",
            {"daily": _routine(outputs=["/tmp/x"])},
        )

    def test_routine_sources_must_be_enabled_windowed(self):
        self.assertConfigError(
            "routines.daily.sources names 'service-docs', which is not a windowed session "
            "source; choose antigravity, claude, codex, cursor, granola",
            {"daily": _routine(sources=["service-docs"])},
        )
        self.assertConfigError(
            "routines.daily.sources names 'cursor', whose collection 'cursor-sessions' is "
            "disabled (sync.enabled=false)",
            {"daily": _routine(sources=["cursor"])},
        )
        self.assertConfigError(
            "routines.daily.sources names 'granola', but collections has no 'granola-sessions'",
            {"daily": _routine(sources=["granola"])},
        )
        self.assertConfigError(
            "routines.daily.sources must be an array of source names, got str",
            {"daily": _routine(sources="claude")},
        )
        spec = _build({"daily": _routine(sources=["claude", "codex"])}).routines["daily"]
        self.assertEqual(spec.sources, ("claude", "codex"))

    def test_routine_scalar_rules(self):
        cases = (
            ({"at": "5:20"}, "routines.daily.at must be HH:MM in 24-hour local time, got '5:20'"),
            ({"at": "24:00"}, "routines.daily.at must be HH:MM in 24-hour local time, got '24:00'"),
            (
                {"day": "today"},
                "routines.daily.day must be \"yesterday-utc\", got 'today'",
            ),
            (
                {"catch_up_days": 31},
                "routines.daily.catch_up_days must be an integer from 1 to 30, got 31",
            ),
            (
                {"catch_up_days": True},
                "routines.daily.catch_up_days must be an integer from 1 to 30, got True",
            ),
            (
                {"timeout_s": 0},
                "routines.daily.timeout_s must be a positive integer or null, got 0",
            ),
            (
                {"budget_usd_per_day": -1},
                "routines.daily.budget_usd_per_day must be a positive number or null, got -1",
            ),
            ({"model": ""}, "routines.daily.model must be a non-empty string, got ''"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                self.assertConfigError(message, {"daily": _routine(**overrides)})

    def test_routine_budget_is_claude_only(self):
        self.assertConfigError(
            "routines.daily.budget_usd_per_day is Claude-only (codex reports no cost); remove "
            "it or set it to null",
            {"daily": _routine(peer="codex", budget_usd_per_day=4)},
        )
        spec = _build({"daily": _routine(budget_usd_per_day=4)}).routines["daily"]
        self.assertEqual(spec.budget_usd_per_day, 4.0)
        codex = _build({"daily": _routine(peer="codex", budget_usd_per_day=None)})
        self.assertIsNone(codex.routines["daily"].budget_usd_per_day)

    def test_routine_validate_must_be_argv_list(self):
        for field in ("validate", "prepare"):
            for value, shown in (("pnpm validate", "str"), ([], "an empty array")):
                with self.subTest(field=field, value=value):
                    self.assertConfigError(
                        f"routines.daily.{field} must be a non-empty array of strings (an argv "
                        f"list, never a shell string), got {shown}",
                        {"daily": _routine(**{field: value})},
                    )
        spec = _build({"daily": _routine(prepare=["cairn", "recall", "digest"])})
        self.assertEqual(spec.routines["daily"].prepare, ("cairn", "recall", "digest"))

    def test_terminal_hook_argv_rules(self):
        where = "routines.daily.terminal_hook"
        cases: tuple[tuple[object, str], ...] = (
            (
                "/usr/bin/notify",
                f"{where} must be a non-empty array of strings (an argv list, never a shell "
                "string), got str",
            ),
            (
                [],
                f"{where} must be a non-empty array of strings (an argv list, never a shell "
                "string), got an empty array",
            ),
            (
                ["/usr/bin/notify", "{{DAY}}"],
                f"{where} takes no template variables (the hook reads CAIRN_ROUTINE_* from its "
                "environment), found {{DAY}}",
            ),
            (["notify"], f"{where}[0] must be an absolute path after expanding ~, got 'notify'"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                self.assertConfigError(message, {"daily": _routine(terminal_hook=value)})
        spec = _build({"daily": _routine(terminal_hook=None)}).routines["daily"]
        self.assertIsNone(spec.terminal_hook)
        self.assertIsNone(_build({"daily": _routine()}).routines["daily"].terminal_hook)
        spec = _build({"daily": _routine(terminal_hook=["~/bin/notify", "--quiet"])})
        self.assertEqual(
            spec.routines["daily"].terminal_hook, (str(Path.home() / "bin/notify"), "--quiet")
        )

    def test_stale_sources_enum(self):
        self.assertConfigError(
            "routines.daily.stale_sources must be one of warn, refuse, ignore, got 'loud'",
            {"daily": _routine(stale_sources="loud")},
        )
        self.assertEqual(_build({"daily": _routine()}).routines["daily"].stale_sources, "warn")
        for mode in ("warn", "refuse", "ignore"):
            spec = _build({"daily": _routine(stale_sources=mode)}).routines["daily"]
            self.assertEqual(spec.stale_sources, mode)

    def test_routine_profile_must_resolve_for_peer(self):
        self.assertConfigError(
            "routines.daily.profile 'scheduled' is not a profile of codex; choose review, "
            "edit, full",
            {"daily": _routine(peer="codex", profile="scheduled")},
            SCHEDULED,
        )
        spec = _build({"daily": _routine(profile="scheduled")}, SCHEDULED).routines["daily"]
        self.assertEqual(spec.profile, "scheduled")


class TestProfileParsing(unittest.TestCase):
    def assertConfigError(self, message: str, invoke: dict):
        with self.assertRaises(config.ConfigError) as caught:
            _build(invoke=invoke)
        self.assertEqual(str(caught.exception), message)

    def test_profiles_parse(self):
        policy = _build(invoke=SCHEDULED).invoke.claude
        self.assertEqual(
            policy.profiles["scheduled"],
            config.ProfileSpec(
                "scheduled",
                "edit",
                ("--permission-mode", "dontAsk", "--add-dir", "{{JOURNAL_DIR}}"),
            ),
        )
        self.assertEqual(
            config.resolve_profile(policy, "scheduled", peer="claude"),
            ("edit", ("--permission-mode", "dontAsk", "--add-dir", "{{JOURNAL_DIR}}")),
        )
        self.assertEqual(config.resolve_profile(policy, "review", peer="claude"), ("review", ()))
        with self.assertRaises(config.ConfigError) as caught:
            config.resolve_profile(policy, "nightly", peer="claude")
        self.assertEqual(
            str(caught.exception),
            "unknown profile 'nightly' for claude; choose review, edit, full, scheduled",
        )
        empty = _build(invoke={"codex": {"profiles": {"plain": {"base": "edit"}}}})
        self.assertEqual(empty.invoke.codex.profiles["plain"].extra_args, ())

    def test_profiles_parse_and_shadow_rejected(self):
        self.assertConfigError(
            "invoke.claude.profiles.review shadows the built-in profile 'review'; pick another "
            "name",
            {"claude": {"profiles": {"review": {"base": "review"}}}},
        )
        self.assertConfigError(
            "invoke.claude.profiles name 'Nightly' is invalid: use 1 to 64 characters from "
            "a-z, 0-9 and '-', starting with a letter or digit",
            {"claude": {"profiles": {"Nightly": {"base": "edit"}}}},
        )
        self.assertConfigError(
            "invoke.claude.profiles must be an object of named profiles, got list",
            {"claude": {"profiles": []}},
        )
        self.assertConfigError(
            "invoke.claude.profiles.x has unknown keys: mode. Allowed keys: base, extra_args.",
            {"claude": {"profiles": {"x": {"base": "edit", "mode": "plan"}}}},
        )
        self.assertConfigError(
            "invoke.claude.profiles.x.base must be one of review, edit, full, got 'yolo'",
            {"claude": {"profiles": {"x": {"base": "yolo"}}}},
        )

    def test_profiles_extra_args_must_be_list(self):
        self.assertConfigError(
            "invoke.claude.profiles.x.extra_args must be an array of strings (an argv list, "
            "never a string), got str",
            {"claude": {"profiles": {"x": {"base": "edit", "extra_args": "--add-dir /tmp"}}}},
        )
        self.assertConfigError(
            "invoke.claude.profiles.x.extra_args: --permission-mode 'auto' would loosen base "
            "'edit'; allowed: plan, dontAsk, acceptEdits",
            {
                "claude": {
                    "profiles": {"x": {"base": "edit", "extra_args": ["--permission-mode", "auto"]}}
                }
            },
        )
        self.assertConfigError(
            "invoke.codex.profiles.x.extra_args: codex extra_args allow only --add-dir DIR and "
            "-c KEY=VALUE; got '-s'",
            {"codex": {"profiles": {"x": {"base": "edit", "extra_args": ["-s", "read-only"]}}}},
        )

    def test_profiles_antigravity_rejected(self):
        self.assertConfigError(
            "invoke.antigravity.profiles is not supported; only claude, codex take config profiles",
            {"antigravity": {"profiles": {"x": {"base": "edit"}}}},
        )

    def test_peer_profile_may_name_config_profile(self):
        invoke = {"claude": {**SCHEDULED["claude"], "profile": "scheduled"}}
        self.assertEqual(_build(invoke=invoke).invoke.claude.profile, "scheduled")
        self.assertConfigError(
            "invoke.claude.profile must be one of review, edit, full, scheduled, got 'nightly'",
            {"claude": {**SCHEDULED["claude"], "profile": "nightly"}},
        )
        self.assertConfigError(
            "invoke.codex.profile must be one of review, edit, full, got 'scheduled'",
            {"codex": {"profile": "scheduled"}},
        )


class TestTemplateChoices(unittest.TestCase):
    def test_disabled_sources_leave_the_example_routine(self):
        out = json.loads(_apply_choices(config.example_template_text(), "/tmp/ctx", ["claude"]))
        self.assertEqual(out["routines"]["example-daily"]["sources"], ["claude"])
        built = config._build(out, Path("/tmp/ctx/config.json"), None)
        self.assertEqual(built.routines["example-daily"].sources, ("claude",))


if __name__ == "__main__":
    unittest.main()
