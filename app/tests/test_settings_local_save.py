"""Shared-seam tests for a local (on-device) Settings save.

The Kivy screen has no automated seam, so the merge that keeps a remote change
alive is exercised through `settingsSvc.save_local_settings`: the caller passes
only the fields the form owns, exactly as the screen does, and the module fills
the rest from a fresh read.
"""

import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import settingsSvc


class LocalSaveTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.write_config(self.stored_config())

    def tearDown(self):
        self.tmp.cleanup()

    def stored_config(self) -> dict:
        return {
            "app": "opensms",
            "port": 9292,
            "boot": False,
            "permissions": True,
            "revision": 3,
            "unknown_top_level": "internal",
            "webhook": {
                "enabled": True,
                "url": "https://remote.example.lan/hooks/opensms",
                "secret": "remote-secret",
                "timeout": 7,
                "max_attempts": 4,
                "backoff": [1, 2, 3],
                "capacity": 250,
                "unknown_webhook": "internal",
            },
        }

    def write_config(self, config: dict):
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file)

    def read_config(self) -> dict:
        with open(self.config_path, encoding="utf-8") as config_file:
            return json.load(config_file)

    def tmp_files(self) -> list:
        return glob.glob(os.path.join(self.tmp.name, "*.tmp"))

    def screen_form(self, current: dict, **overrides) -> dict:
        """Mimic the screen showing freshly read Settings, then editing them.

        Only the fields the screen owns are carried; the secret is omitted
        unless the operator typed a new one.
        """
        writable = settingsSvc.writable_settings(current)
        form = {
            "port": writable["port"],
            "boot": writable["boot"],
            "webhook": {
                "enabled": writable["webhook"]["enabled"],
                "url": writable["webhook"]["url"],
            },
        }
        webhook_overrides = overrides.pop("webhook", None)
        if webhook_overrides:
            form["webhook"].update(webhook_overrides)
        form.update(overrides)
        return form


class LocalSaveMergeTests(LocalSaveTestCase):

    def test_entering_then_saving_keeps_a_remote_webhook_target(self):
        # The screen re-reads on entry, so it shows the remote target.
        current = settingsSvc.load_settings(self.config_path)
        form = self.screen_form(current, port=9393)

        saved = settingsSvc.save_local_settings(self.config_path, form)

        self.assertEqual(saved["revision"], 4)
        self.assertEqual(saved["port"], 9393)
        webhook = saved["webhook"]
        self.assertEqual(
            webhook["url"], "https://remote.example.lan/hooks/opensms"
        )
        self.assertEqual(webhook["secret"], "remote-secret")
        on_disk = self.read_config()
        self.assertEqual(on_disk["revision"], 4)
        self.assertEqual(on_disk["webhook"]["url"], webhook["url"])
        self.assertEqual(on_disk["webhook"]["secret"], "remote-secret")

    def test_fields_the_form_does_not_own_keep_their_freshly_read_values(self):
        # A port-only local save must not touch the Webhook at all.
        form = {"port": 9393}

        saved = settingsSvc.save_local_settings(self.config_path, form)

        webhook = saved["webhook"]
        self.assertEqual(
            webhook["url"], "https://remote.example.lan/hooks/opensms"
        )
        self.assertEqual(webhook["secret"], "remote-secret")
        self.assertEqual(webhook["timeout"], 7)
        self.assertEqual(webhook["max_attempts"], 4)
        self.assertEqual(webhook["backoff"], [1, 2, 3])
        self.assertEqual(webhook["capacity"], 250)
        self.assertEqual(saved["unknown_top_level"], "internal")
        self.assertEqual(webhook["unknown_webhook"], "internal")
        self.assertEqual(saved["permissions"], True)
        self.assertEqual(self.read_config(), saved)

    def test_an_untouched_secret_keeps_the_stored_value(self):
        form = {
            "webhook": {
                "enabled": True,
                "url": "https://new.example.lan/hooks/opensms",
            }
        }

        saved = settingsSvc.save_local_settings(self.config_path, form)

        self.assertEqual(
            saved["webhook"]["url"],
            "https://new.example.lan/hooks/opensms",
        )
        self.assertEqual(saved["webhook"]["secret"], "remote-secret")

    def test_an_edited_secret_replaces_the_stored_value(self):
        form = {"webhook": {"secret": "local-secret"}}

        saved = settingsSvc.save_local_settings(self.config_path, form)

        self.assertEqual(saved["webhook"]["secret"], "local-secret")
        self.assertEqual(self.read_config()["webhook"]["secret"], "local-secret")

    def test_clearing_the_secret_field_clears_the_stored_secret(self):
        form = {"webhook": {"secret": ""}}

        saved = settingsSvc.save_local_settings(self.config_path, form)

        self.assertEqual(saved["webhook"]["secret"], "")
        self.assertEqual(self.read_config()["webhook"]["secret"], "")

    def test_a_legacy_config_gains_defaults_and_keeps_a_saved_port(self):
        self.write_config({
            "app": "opensms",
            "port": 9292,
            "boot": False,
            "webhook": {
                "enabled": False,
                "url": "",
                "secret": "",
            },
        })

        saved = settingsSvc.save_local_settings(self.config_path, {"port": 9393})

        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["port"], 9393)
        self.assertEqual(saved["webhook"]["timeout"], 10)
        self.assertEqual(saved["webhook"]["max_attempts"], 3)
        self.assertEqual(saved["webhook"]["backoff"], [5, 30])
        self.assertEqual(saved["webhook"]["capacity"], 100)


class LocalSaveValidationTests(LocalSaveTestCase):

    def assert_rejected(self, form: dict, error_class):
        before = self.read_config()

        with self.assertRaises(error_class):
            settingsSvc.save_local_settings(self.config_path, form)

        self.assertEqual(self.read_config(), before)
        self.assertEqual(self.tmp_files(), [])

    def test_an_out_of_range_port_rejects_the_whole_save(self):
        for port in (
            settingsSvc.PORT_MIN - 1,
            settingsSvc.PORT_MAX + 1,
        ):
            with self.subTest(port=port):
                self.assert_rejected(
                    {"port": port}, settingsSvc.SettingsValidationError
                )

    def test_a_non_integer_port_rejects_the_whole_save(self):
        self.assert_rejected(
            {"port": "9393"}, settingsSvc.SettingsValidationError
        )

    def test_a_boolean_port_rejects_the_whole_save(self):
        self.assert_rejected({"port": True}, settingsSvc.SettingsValidationError)

    def test_enabling_forwarding_without_a_valid_target_rejects_the_save(self):
        for url in ("", "not-a-url", "ftp://proxy.example.lan/hooks", "http:///x"):
            with self.subTest(url=url):
                self.assert_rejected(
                    {
                        "webhook": {
                            "enabled": True,
                            "url": url,
                        }
                    },
                    settingsSvc.SettingsRuleError,
                )

    def test_both_port_bounds_are_accepted(self):
        for port in (settingsSvc.PORT_MIN, settingsSvc.PORT_MAX):
            with self.subTest(port=port):
                saved = settingsSvc.save_local_settings(
                    self.config_path, {"port": port}
                )
                self.assertEqual(saved["port"], port)


if __name__ == "__main__":
    unittest.main()
