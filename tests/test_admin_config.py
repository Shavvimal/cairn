"""``cairn config show`` / ``cairn config set`` - read + write the config-as-data file."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cairn import admin, config, instructions
from tests.test_shared_store import StoreFixture


def _capture(fn, *args) -> tuple[int, str]:
    """Run an admin command, returning (exit_code, stdout)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


class _ConfigFixture(unittest.TestCase):
    """Fresh XDG_CONFIG_HOME with an initialised config (claude+codex enabled)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        # The developer's shell may route agy to GCP; the billing checks must not see it.
        scrubbed = ("AGY_ADC_AUTH", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_CLOUD_LOCATION")
        self._prev = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "CAIRN_CONFIG", *scrubbed)}
        for key in ("CAIRN_CONFIG", *scrubbed):
            os.environ.pop(key, None)
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        config.get_config.cache_clear()
        admin._config_init(data_root="/tmp/ctx", enable=["claude", "codex"])
        config.get_config.cache_clear()
        self.cfg_path = config.user_config_path()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.get_config.cache_clear()
        self._tmp.cleanup()

    def _show_json(self) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(admin._config_show(as_json=True), 0)
        return json.loads(buf.getvalue())


class TestConfigShow(_ConfigFixture):
    def test_show_json_shape_and_enabled_state(self):
        data = self._show_json()
        self.assertEqual(data["data_root"], "/tmp/ctx")
        self.assertEqual(
            set(data),
            {
                "config_path",
                "data_root",
                "qmd_binary",
                "cron_schedule",
                "collections",
                "invoke",
                "routines",
            },
        )
        self.assertEqual(data["invoke"]["codex"]["model"], "gpt-6-astra")
        self.assertIsNone(data["invoke"]["claude"]["effort"])
        self.assertEqual(data["invoke"]["claude"]["profile"], "review")
        enabled = {c["name"]: c["enabled"] for c in data["collections"]}
        self.assertTrue(enabled["claude-code-sessions"])
        self.assertTrue(enabled["codex-sessions"])
        self.assertFalse(enabled["antigravity-sessions"])
        self.assertFalse(enabled["cursor-sessions"])
        sd = next(c for c in data["collections"] if c["name"] == "service-docs")
        self.assertEqual(sd["type"], "service-docs")


class TestConfigSet(_ConfigFixture):
    def test_set_collection_enabled_round_trips(self):
        self.assertEqual(admin._config_set("cursor.enabled", "true"), 0)
        config.get_config.cache_clear()
        enabled = {c["name"]: c["enabled"] for c in self._show_json()["collections"]}
        self.assertTrue(enabled["cursor-sessions"])

    def test_set_cron_schedule_and_qmd_binary(self):
        self.assertEqual(admin._config_set("cron.schedule", "0 */4 * * *"), 0)
        self.assertEqual(admin._config_set("qmd_binary", "/opt/qmd"), 0)
        config.get_config.cache_clear()
        data = self._show_json()
        self.assertEqual(data["cron_schedule"], "0 */4 * * *")
        self.assertEqual(data["qmd_binary"], "/opt/qmd")

    def test_unknown_collection_is_rejected(self):
        self.assertEqual(admin._config_set("bogus.store", "/x"), 1)

    def test_non_bool_enabled_is_rejected(self):
        self.assertEqual(admin._config_set("codex.enabled", "maybe"), 1)

    def test_invalid_result_is_restored(self):
        # If the written config fails validation, the prior file must be put back (§8).
        before = self.cfg_path.read_text(encoding="utf-8")
        with mock.patch.object(admin, "load_config_from", side_effect=config.ConfigError("boom")):
            self.assertEqual(admin._config_set("qmd_binary", "/opt/qmd"), 1)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before)


class TestConfigSetInvoke(_ConfigFixture):
    def test_set_invoke_effort_round_trips(self):
        self.assertEqual(admin._config_set("invoke.codex.effort", "medium"), 0)
        config.get_config.cache_clear()
        self.assertEqual(self._show_json()["invoke"]["codex"]["effort"], "medium")

    def test_set_invoke_int_and_null(self):
        self.assertEqual(admin._config_set("invoke.claude.timeout_s", "600"), 0)
        self.assertEqual(admin._config_set("invoke.codex.model", "null"), 0)
        config.get_config.cache_clear()
        invoke = self._show_json()["invoke"]
        self.assertEqual(invoke["claude"]["timeout_s"], 600)
        self.assertIsNone(invoke["codex"]["model"])

    def test_bad_invoke_keys_are_rejected(self):
        before = self.cfg_path.read_text(encoding="utf-8")
        self.assertEqual(admin._config_set("invoke.bogus.x", "1"), 1)
        self.assertEqual(admin._config_set("invoke.codex.bogus", "1"), 1)
        self.assertEqual(admin._config_set("invoke.codex.timeout_s", "soon"), 1)
        self.assertEqual(admin._config_set("invoke.codex.timeout_s", "-5"), 1)
        self.assertEqual(admin._config_set("invoke.codex.profile", "yolo"), 1)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before)

    def test_show_text_lists_invoke(self):
        rc, out = _capture(admin._config_show, False)
        self.assertEqual(rc, 0)
        self.assertIn("invoke      :", out)
        self.assertIn("model=gpt-6-astra effort=high profile=review", out)
        self.assertIn("model=(peer default)", out)
        self.assertIn("billing=inherit", out)

    def test_set_antigravity_billing(self):
        before = self.cfg_path.read_text(encoding="utf-8")
        self.assertEqual(admin._config_set("invoke.antigravity.billing.route", "gcp"), 0)
        self.assertEqual(admin._config_set("invoke.antigravity.billing.project", "my-proj"), 0)
        config.get_config.cache_clear()
        billing = self._show_json()["invoke"]["antigravity"]["billing"]
        self.assertEqual(billing, {"route": "gcp", "project": "my-proj", "location": None})
        self.assertIsNone(self._show_json()["invoke"]["codex"]["billing"])
        self.assertEqual(admin._config_set("invoke.antigravity.billing.project", "null"), 0)
        config.get_config.cache_clear()
        self.assertIsNone(self._show_json()["invoke"]["antigravity"]["billing"]["project"])
        _, out = _capture(admin._config_show, False)
        self.assertIn("billing=gcp", out)
        after = self.cfg_path.read_text(encoding="utf-8")
        for key, value in (
            ("invoke.codex.billing.route", "gcp"),
            ("invoke.antigravity.billing", "gcp"),
            ("invoke.antigravity.billing.route", "free"),
            ("invoke.antigravity.billing.bogus", "1"),
        ):
            with self.subTest(key=key):
                self.assertEqual(admin._config_set(key, value), 1)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), after)
        self.assertNotEqual(after, before)


class TestDoctorInvoke(_ConfigFixture):
    def test_reports_antigravity_permissions(self):
        with TemporaryDirectory() as tmp:
            store = Path(tmp) / "agy"
            store.mkdir()
            self.assertEqual(admin._config_set("antigravity.store", str(store)), 0)
            config.get_config.cache_clear()
            env = {"CODEX_HOME": tmp}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(admin.shutil, "which", return_value="/bin/agy"),
            ):
                _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn("warn antigravity permissions - agy has no command allow rules", out)
                self.assertIn("--grant-commands", out)
                (store / "settings.json").write_text(
                    json.dumps(
                        {
                            "permissions": {
                                "allow": ["command(rg)", "command(git diff)"],
                                "deny": ["command(curl)"],
                            }
                        }
                    )
                )
                _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn("ok   antigravity permissions - 2 allow and 1 deny rules in", out)
                self.assertIn("allow: command(rg), command(git diff)", out)
                self.assertIn("deny:  command(curl)", out)
            with mock.patch.object(admin.shutil, "which", return_value=None):
                _, out = _capture(admin._check_invoke, config.get_config())
                self.assertNotIn("antigravity permissions", out)  # no agy, nothing to check

    def test_reports_antigravity_billing_readiness(self):
        with TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "adc.json")
            env = {"GOOGLE_APPLICATION_CREDENTIALS": missing, "CODEX_HOME": tmp}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(admin.shutil, "which", return_value=None),
            ):
                _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn("ok   antigravity billing - route inherit", out)
                self.assertEqual(admin._config_set("invoke.antigravity.billing.route", "gcp"), 0)
                config.get_config.cache_clear()
                _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn("warn antigravity billing - route gcp is not ready", out)
                self.assertIn("adc.json does not exist", out)
                Path(missing).write_text('{"type":"authorized_user","quota_project_id":"q-p"}')
                probe = "cairn.harnesses.antigravity.adc_token_check"
                with mock.patch(probe, return_value=("ok", None)):
                    _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn(
                    "ok   antigravity billing - route gcp via ADC, quota project q-p, token ok",
                    out,
                )
                with mock.patch(probe, return_value=("refused", "Google rejected the token")):
                    _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn("warn antigravity billing - route gcp is not ready: Google", out)
                with mock.patch(probe, return_value=("unverified", "gcloud is not installed")):
                    _, out = _capture(admin._check_invoke, config.get_config())
                self.assertIn("quota project q-p; token not verified: gcloud is not", out)

    def test_reports_defaults_and_missing_codex_cache(self):
        # Only codex is "installed": with agy on PATH the permissions line would read the
        # developer's real agy settings (the fixture scrubs config, not HOME).
        def only_codex(name):
            return "/bin/codex" if name == "codex" else None

        with (
            TemporaryDirectory() as codex_home,
            mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}),
        ):
            with mock.patch.object(admin.shutil, "which", side_effect=only_codex):
                _, out = _capture(admin._check_invoke, config.get_config())
            self.assertIn("ok   invoke defaults - codex: model=gpt-6-astra", out)
            self.assertIn("warn codex model cache", out)
            self.assertNotIn("antigravity permissions", out)
            Path(codex_home, "models_cache.json").write_text("{}")
            with mock.patch.object(admin.shutil, "which", side_effect=only_codex):
                _, out = _capture(admin._check_invoke, config.get_config())
            self.assertNotIn("warn", out)


class TestServiceDocs(_ConfigFixture):
    """`cairn config add-service-doc / remove-service-doc` - manage service_sources."""

    def _service_sources(self) -> dict:
        config.get_config.cache_clear()
        coll = config.get_config().collections["service-docs"]
        return {s.name: str(s.path) for s in coll.service_sources}

    def test_add_validates_path_exists(self):
        rc, _ = _capture(admin._config_add_service_doc, "api", "/no/such/dir", None)
        self.assertEqual(rc, 1)

    def test_add_clears_placeholder_and_registers_real_path(self):
        with TemporaryDirectory() as docs:
            rc, _ = _capture(admin._config_add_service_doc, "api", docs, "API docs")
            self.assertEqual(rc, 0)
            sources = self._service_sources()
            # the bundled placeholder (~/Code/your-product/api/docs) is gone, replaced
            # by exactly the real path we added.
            self.assertEqual(set(sources), {"api"})
            self.assertEqual(sources["api"], str(Path(docs)))

    def test_add_second_source_keeps_first(self):
        with TemporaryDirectory() as d1, TemporaryDirectory() as d2:
            self.assertEqual(admin._config_add_service_doc("api", d1, None), 0)
            self.assertEqual(admin._config_add_service_doc("web", d2, None), 0)
            self.assertEqual(set(self._service_sources()), {"api", "web"})

    def test_add_hints_when_disabled(self):
        # service-docs is disabled in this fixture (only claude+codex enabled).
        with TemporaryDirectory() as docs:
            rc, out = _capture(admin._config_add_service_doc, "api", docs, None)
        self.assertEqual(rc, 0)
        self.assertIn("service-docs is disabled", out)

    def test_remove_unknown_is_rejected(self):
        self.assertEqual(admin._config_remove_service_doc("nope"), 1)

    def test_add_then_remove_round_trips(self):
        with TemporaryDirectory() as docs:
            self.assertEqual(admin._config_add_service_doc("api", docs, None), 0)
            self.assertEqual(admin._config_remove_service_doc("api"), 0)
        self.assertEqual(self._service_sources(), {})

    def test_show_lists_service_sources(self):
        with TemporaryDirectory() as docs:
            self.assertEqual(admin._config_add_service_doc("api", docs, None), 0)
            config.get_config.cache_clear()
            data = self._show_json()
        sd = next(c for c in data["collections"] if c["name"] == "service-docs")
        self.assertEqual(sd["type"], "service-docs")
        self.assertEqual(set(sd["service_sources"]), {"api"})


if __name__ == "__main__":
    unittest.main()


ROUTINE = {
    "peer": "claude",
    "prompt_file": "/tmp/site/prompts/daily.md",
    "cd": "/tmp/site",
    "profile": "scheduled",
    "at": "05:20",
    "validate": ["true"],
}
PROFILE = {"base": "edit", "extra_args": ["--permission-mode", "dontAsk"]}


def _quiet(fn, *args) -> tuple[int, str, str]:
    """Run an admin command, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args)
    return rc, out.getvalue(), err.getvalue()


class TestConfigSetRoutines(_ConfigFixture):
    def setUp(self):
        super().setUp()
        self.assertEqual(
            _quiet(admin._config_set, "invoke.claude.profiles.scheduled", json.dumps(PROFILE))[0],
            0,
        )

    def test_set_whole_routine_then_show_round_trip(self):
        rc, _, err = _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        self.assertEqual(rc, 0, err)
        shown = self._show_json()["routines"]["daily"]
        resolved = shown.pop("resolved")
        self.assertEqual(shown, ROUTINE)
        self.assertEqual(resolved["prompt_file"], "/tmp/site/prompts/daily.md")
        self.assertEqual(resolved["outputs"], {"journal_dir": "/tmp/ctx/journal"})
        self.assertEqual(resolved["log"], "/tmp/ctx/logs/routine-daily.log")
        self.assertEqual(resolved["label"], "dev.cairn.routine.daily")
        self.assertEqual((resolved["terminal_hook"], resolved["stale_sources"]), (None, "warn"))
        hook = ["~/bin/notify", "--quiet"]
        rc, _, err = _quiet(admin._config_set, "routines.daily.terminal_hook", json.dumps(hook))
        self.assertEqual(rc, 0, err)
        resolved = self._show_json()["routines"]["daily"]["resolved"]
        home = str(Path.home())
        self.assertEqual(resolved["terminal_hook"], [f"{home}/bin/notify", "--quiet"])
        self.assertTrue(resolved["plist"].endswith("dev.cairn.routine.daily.plist"))
        self.assertTrue(resolved["state_dir"].endswith("cairn/routines/daily"))
        # The template's example routine is listed too.
        self.assertIn("example-daily", self._show_json()["routines"])
        rc, text = _capture(admin._config_show, False)
        self.assertIn("claude profile scheduled = base edit + --permission-mode dontAsk", text)
        self.assertIn("daily                  claude/scheduled at 05:20 local, catch-up 3d", text)
        self.assertIn("outputs.journal_dir = /tmp/ctx/journal", text)

    def test_set_routine_scalars_coerce(self):
        _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        for key, value in (
            ("catch_up_days", "5"),
            ("timeout_s", "null"),
            ("budget_usd_per_day", "2.5"),
            ("model", "opus"),
            ("prepare", '["cairn", "recall", "digest", "--day", "{{DAY}}"]'),
        ):
            rc, _, err = _quiet(admin._config_set, f"routines.daily.{key}", value)
            self.assertEqual(rc, 0, err)
        spec = config.get_config().routines["daily"]
        self.assertEqual(spec.catch_up_days, 5)
        self.assertIsNone(spec.timeout_s)
        self.assertEqual(spec.budget_usd_per_day, 2.5)
        self.assertEqual(spec.model, "opus")
        self.assertEqual(spec.prepare, ("cairn", "recall", "digest", "--day", "{{DAY}}"))
        rc, _, err = _quiet(admin._config_set, "routines.daily.catch_up_days", "99")
        self.assertEqual(rc, 1)
        self.assertIn("catch_up_days must be an integer from 1 to 30", err)

    def test_field_on_missing_routine_refused(self):
        rc, _, err = _quiet(admin._config_set, "routines.nightly.at", "06:00")
        self.assertEqual(rc, 1)
        self.assertIn(
            "routine 'nightly' does not exist; create it with: cairn config set "
            "routines.nightly '{\"peer\": ..., ...}'",
            err,
        )

    def test_sources_comma_list_and_json(self):
        _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        self.assertEqual(_quiet(admin._config_set, "routines.daily.sources", "claude,codex")[0], 0)
        self.assertEqual(config.get_config().routines["daily"].sources, ("claude", "codex"))
        self.assertEqual(_quiet(admin._config_set, "routines.daily.sources", '["claude"]')[0], 0)
        self.assertEqual(config.get_config().routines["daily"].sources, ("claude",))
        rc, _, err = _quiet(admin._config_set, "routines.daily.sources", "cursor")
        self.assertEqual(rc, 1)
        self.assertIn("disabled (sync.enabled=false)", err)

    def test_validate_requires_json_array(self):
        _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        rc, _, err = _quiet(admin._config_set, "routines.daily.validate", "pnpm,validate")
        self.assertEqual(rc, 1)
        self.assertIn("takes a JSON array of strings (an argv list)", err)
        rc, _, err = _quiet(
            admin._config_set, "routines.daily.validate", '["pnpm", "validate", "{{DAY}}"]'
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            config.get_config().routines["daily"].validate, ("pnpm", "validate", "{{DAY}}")
        )

    def test_terminal_hook_and_stale_sources_set(self):
        _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        rc, _, err = _quiet(admin._config_set, "routines.daily.terminal_hook", '["/bin/echo"]')
        self.assertEqual(rc, 0, err)
        self.assertEqual(config.get_config().routines["daily"].terminal_hook, ("/bin/echo",))
        self.assertEqual(_quiet(admin._config_set, "routines.daily.terminal_hook", "null")[0], 0)
        self.assertIsNone(config.get_config().routines["daily"].terminal_hook)
        rc, _, err = _quiet(admin._config_set, "routines.daily.terminal_hook", '["notify"]')
        self.assertEqual(rc, 1)
        self.assertIn("terminal_hook[0] must be an absolute path after expanding ~", err)
        rc, _, err = _quiet(admin._config_set, "routines.daily.stale_sources", "refuse")
        self.assertEqual(rc, 0, err)
        self.assertEqual(config.get_config().routines["daily"].stale_sources, "refuse")

    def test_outputs_key_set_and_null_removes(self):
        _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        rc, _, err = _quiet(admin._config_set, "routines.daily.outputs.inbox_dir", "/tmp/inbox")
        self.assertEqual(rc, 0, err)
        outputs = config.get_config().routines["daily"].outputs
        self.assertEqual(outputs["inbox_dir"], Path("/tmp/inbox"))
        self.assertEqual(
            _quiet(admin._config_set, "routines.daily.outputs.inbox_dir", "null")[0], 0
        )
        self.assertNotIn("inbox_dir", config.get_config().routines["daily"].outputs)
        rc, _, err = _quiet(admin._config_set, "routines.daily.outputs.day", "/tmp/x")
        self.assertEqual(rc, 1)
        self.assertIn("which is a built-in variable", err)

    def test_unset_profile_in_use_restored(self):
        _quiet(admin._config_set, "routines.daily", json.dumps(ROUTINE))
        before = self.cfg_path.read_text()
        rc, _, err = _quiet(admin._config_unset, "invoke.claude.profiles.scheduled")
        self.assertEqual(rc, 1)
        self.assertIn(".profile 'scheduled' is not a profile of claude", err)
        self.assertEqual(self.cfg_path.read_text(), before)

    def test_unset_routine_field_output_and_routine(self):
        _quiet(admin._config_set, "routines.daily", json.dumps({**ROUTINE, "model": "opus"}))
        self.assertEqual(_quiet(admin._config_unset, "routines.daily.model")[0], 0)
        self.assertIsNone(config.get_config().routines["daily"].model)
        rc, _, err = _quiet(admin._config_unset, "routines.daily.at")
        self.assertEqual(rc, 1)
        self.assertIn("routines.daily.at is required; set it instead", err)
        rc, out, _ = _quiet(admin._config_unset, "routines.daily")
        self.assertEqual(rc, 0)
        self.assertIn(
            "Removed routines.daily; its launchd job stays until 'cairn routine uninstall daily'",
            out,
        )
        self.assertNotIn("daily", config.get_config().routines)


class TestConfigSetProfiles(_ConfigFixture):
    def test_set_profile_object_round_trip(self):
        rc, _, err = _quiet(admin._config_set, "invoke.codex.profiles.nightly", '{"base": "edit"}')
        self.assertEqual(rc, 0, err)
        rc, _, err = _quiet(
            admin._config_set,
            "invoke.codex.profiles.nightly.extra_args",
            '["--add-dir", "/tmp/journal"]',
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            _quiet(admin._config_set, "invoke.codex.profiles.nightly.base", "review")[0], 0
        )
        shown = self._show_json()["invoke"]["codex"]["profiles"]["nightly"]
        self.assertEqual(
            shown,
            {"name": "nightly", "base": "review", "extra_args": ["--add-dir", "/tmp/journal"]},
        )
        rc, _, err = _quiet(admin._config_set, "invoke.codex.profile", "nightly")
        self.assertEqual(rc, 0, err)
        rc, _, err = _quiet(admin._config_set, "invoke.antigravity.profiles.x", '{"base": "edit"}')
        self.assertEqual(rc, 1)
        rc, _, err = _quiet(admin._config_set, "invoke.codex.profiles.other.base", "edit")
        self.assertEqual(rc, 1)
        self.assertIn("profile 'other' does not exist", err)

    def test_profiles_scalar_key_refused(self):
        rc, _, err = _quiet(admin._config_set, "invoke.claude.profiles", "x")
        self.assertEqual(rc, 1)
        self.assertIn("profiles is an object of named profiles", err)
        rc, _, err = _quiet(admin._config_set, "invoke.claude.profiles.x", "edit")
        self.assertEqual(rc, 1)
        self.assertIn("takes a JSON object", err)


class TestConfigShowNotes(_ConfigFixture):
    def test_notes_type_for_storeless_collection(self):
        types = {c["name"]: c["type"] for c in self._show_json()["collections"]}
        self.assertEqual(types["journal"], "notes")
        self.assertEqual(types["service-docs"], "service-docs")
        self.assertEqual(types["claude-code-sessions"], "sessions")


class TestDisableSourceInRoutine(_ConfigFixture):
    """The shipped example names claude and codex in ``example-daily``: disabling one of
    them must drop it from the routine, not reject the edit."""

    def setUp(self):
        super().setUp()
        self.example = Path(self._tmp.name) / "example.json"
        self.example.write_text(config.example_template_text(), encoding="utf-8")
        os.environ["CAIRN_CONFIG"] = str(self.example)
        config.get_config.cache_clear()

    def test_disable_codex_on_example_config(self):
        self.assertEqual(config.get_config().routines["example-daily"].sources, ("claude", "codex"))
        rc, out, err = _quiet(admin._config_set, "codex.enabled", "false")
        self.assertEqual(rc, 0, err)
        self.assertIn("Set codex.enabled = false", out)
        self.assertIn("note: removed codex from routines.example-daily.sources", out)
        loaded = config.load_config_from(self.example)
        self.assertFalse(loaded.collections["codex-sessions"].sync.enabled)
        self.assertEqual(loaded.routines["example-daily"].sources, ("claude",))

    def test_disable_unused_source_prints_no_note(self):
        rc, out, err = _quiet(admin._config_set, "cursor.enabled", "false")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("note:", out)
        self.assertEqual(config.get_config().routines["example-daily"].sources, ("claude", "codex"))


class TestDoctorRecallFirst(StoreFixture):
    def setUp(self):
        super().setUp()
        self.claude_home = Path(os.environ["CLAUDE_CONFIG_DIR"])
        self.claude_home.mkdir()
        self.claude_md = self.claude_home / "CLAUDE.md"

    def check(self, harness: str | None = "claude") -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            admin._check_recall_first(harness)
        return buf.getvalue()

    def test_doctor_reports_recall_first_states(self):
        md = self.claude_md
        self.assertEqual(
            self.check(),
            f"  warn recall-first [claude] - not in {md}; run 'cairn instructions install "
            "--harness claude' (or 'remove' to silence this)\n",
        )
        # no ledger record yet: an older version on disk is stale
        md.write_text(
            "mine\n<!-- cairn:recall-first:begin v0 -->\nold\n<!-- cairn:recall-first:end -->\n"
        )
        self.assertEqual(
            self.check(),
            "  warn recall-first [claude] - stale (v0 on disk, v1 shipped); run 'cairn "
            "instructions install --harness claude'\n",
        )
        md.write_text("mine\n")
        with redirect_stdout(io.StringIO()):
            instructions.main(["install", "--harness", "claude"])
        self.assertEqual(self.check(), f"  ok   recall-first [claude] - v1 current in {md}\n")
        md.write_text(md.read_text().replace("this machine", "my laptop"))
        self.assertEqual(
            self.check(),
            "  warn recall-first [claude] - edited by hand inside the markers; edits there "
            "are replaced by 'install --force'\n",
        )
        with redirect_stdout(io.StringIO()):
            instructions.main(["remove", "--harness", "claude"])
        date = self.check().split("opted out on ")[1][:10]
        self.assertEqual(
            self.check(),
            f"  ok   recall-first [claude] - opted out on {date}; 'cairn instructions install "
            "--harness claude --force' adds it\n",
        )
        md.write_text("<!-- cairn:recall-first:end -->\n")
        self.assertEqual(
            self.check(),
            f"  warn recall-first [claude] - damaged markers in {md}; fix them by hand\n",
        )
        codex_home = Path(os.environ["CODEX_HOME"])
        self.assertEqual(
            self.check("codex"), f"  warn recall-first [codex] - {codex_home} not found\n"
        )
        self.assertEqual(self.check("antigravity"), "")
        out = self.check(None)  # without --harness, a missing home is skipped
        self.assertIn("[claude]", out)
        self.assertNotIn("[codex]", out)
        codex_home.mkdir()
        (codex_home / "AGENTS.override.md").write_text("rules\n")
        self.assertIn("(AGENTS.override.md is the active file)", self.check("codex"))

    def test_doctor_recall_first_runs_without_config(self):
        os.environ["CAIRN_CONFIG"] = str(self.root / "missing.json")
        config.get_config.cache_clear()
        buf = io.StringIO()
        with (
            redirect_stdout(buf),
            mock.patch.object(admin, "_current_crontab", return_value=[]),
        ):
            rc = admin.doctor_main([])
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("  FAIL config", out)
        self.assertIn("  warn recall-first [claude] - not in", out)
        self.assertNotIn("[codex]", out)
        self.assertFalse(self.claude_md.exists())
