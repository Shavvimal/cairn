"""``cairn config show`` / ``cairn config set`` - read + write the config-as-data file."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cairn import admin, config


class _ConfigFixture(unittest.TestCase):
    """Fresh XDG_CONFIG_HOME with an initialised config (claude+codex enabled)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._prev = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "CAIRN_CONFIG")}
        os.environ.pop("CAIRN_CONFIG", None)
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
            set(data), {"config_path", "data_root", "qmd_binary", "cron_schedule", "collections"}
        )
        enabled = {c["name"]: c["enabled"] for c in data["collections"]}
        self.assertTrue(enabled["claude-code-sessions"])
        self.assertTrue(enabled["codex-sessions"])
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


if __name__ == "__main__":
    unittest.main()
