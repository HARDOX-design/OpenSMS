import os
import time

from api_test_case import ApiTestCase

from services import postApi
from services.config import __version__
from services.pathMgr import get_os_platform


class ApiHarnessTests(ApiTestCase):

    def test_request_without_a_key_is_refused(self):
        response = self.client.post(
            "/sms/",
            json={"number": "+14155552671", "text": "hello"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("stat", response.json())

    def test_api_reads_keys_from_the_temporary_config_directory(self):
        raw_key = postApi.generate_new_api_key()
        self.assertTrue(
            os.path.exists(os.path.join(self.config_dir, "keys.json"))
        )

        response = self.client.post(
            "/sms/",
            json={"number": "+14155552671", "text": "hello"},
            headers=self.auth(raw_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("stat", response.json())
        self.assertEqual(postApi.get_config_dir(), self.config_dir)


class HealthTests(ApiTestCase):

    def test_health_refuses_a_missing_key(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 403)

    def test_health_refuses_an_invalid_key(self):
        self.add_api_key()
        response = self.client.get(
            "/api/health", headers=self.auth("osms_not_a_real_key")
        )
        self.assertEqual(response.status_code, 401)

    def test_health_reports_status_version_platform_and_uptime(self):
        raw_key = self.add_api_key()
        postApi.app.state.server.started_at = time.time() - 10

        response = self.client.get("/api/health", headers=self.auth(raw_key))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], __version__)
        self.assertEqual(body["platform"], get_os_platform())
        self.assertGreaterEqual(body["uptime_s"], 9)


class SettingsReadTests(ApiTestCase):

    def test_settings_defaults_when_no_config_exists(self):
        raw_key = self.add_api_key()

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["revision"], 0)
        self.assertEqual(body["settings"], {
            "port": 9292,
            "boot": False,
            "webhook": {
                "enabled": False,
                "url": "",
                "secret_set": False,
                "timeout": 10,
                "max_attempts": 3,
                "backoff": [5, 30],
                "capacity": 100,
            },
        })

    def test_settings_masks_the_secret_and_hides_internal_fields(self):
        self.write_config({
            "app": "opensms",
            "port": 9393,
            "boot": True,
            "permissions": True,
            "revision": 7,
            "unknown_top_level": "internal",
            "webhook": {
                "enabled": True,
                "url": "https://proxy.example.lan/hooks/opensms",
                "secret": "topsecret",
                "timeout": 5,
                "max_attempts": 2,
                "backoff": [1, 2],
                "capacity": 50,
                "unknown_webhook": "internal",
            },
        })
        raw_key = self.add_api_key()

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("topsecret", response.text)
        body = response.json()
        self.assertEqual(body["revision"], 7)
        settings = body["settings"]
        self.assertEqual(settings["port"], 9393)
        self.assertTrue(settings["boot"])
        self.assertNotIn("app", settings)
        self.assertNotIn("permissions", settings)
        self.assertNotIn("unknown_top_level", settings)
        self.assertTrue(settings["webhook"]["secret_set"])
        self.assertNotIn("secret", settings["webhook"])
        self.assertNotIn("unknown_webhook", settings["webhook"])

    def test_settings_fill_defaults_for_a_legacy_config(self):
        self.write_config({
            "app": "opensms",
            "port": 9393,
            "boot": True,
            "webhook": {
                "enabled": False,
                "url": "",
                "secret": "",
            },
        })
        raw_key = self.add_api_key()

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["revision"], 0)
        webhook = body["settings"]["webhook"]
        self.assertEqual(webhook["timeout"], 10)
        self.assertEqual(webhook["max_attempts"], 3)
        self.assertEqual(webhook["backoff"], [5, 30])
        self.assertEqual(webhook["capacity"], 100)


class KeyMigrationTests(ApiTestCase):

    def test_legacy_key_authenticates_and_is_migrated_on_first_use(self):
        raw_key = self.add_legacy_api_key()

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 200)
        store = self.read_key_store()
        self.assertEqual(store["version"], 2)
        self.assertEqual(len(store["keys"]), 1)
        entry = store["keys"][0]
        self.assertEqual(entry["hash"], postApi.hash_api_key(raw_key))
        self.assertEqual(entry["scopes"], ["settings:write", "sms:write"])
        self.assertTrue(entry["id"].startswith("k_"))
        self.assertTrue(entry["created_at"])

        again = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )
        self.assertEqual(again.status_code, 200)

    def test_legacy_key_without_scopes_is_granted_both(self):
        raw_key = self.add_legacy_api_key(scopes=())

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )
        self.assertEqual(response.status_code, 200)
        entry = self.read_key_store()["keys"][0]
        self.assertEqual(entry["scopes"], ["settings:write", "sms:write"])

        send = self.client.post(
            "/sms/",
            json={"number": "+14155552671", "text": "hello"},
            headers=self.auth(raw_key),
        )
        self.assertEqual(send.status_code, 200)


class ScopeTests(ApiTestCase):

    def test_send_only_key_cannot_read_settings(self):
        raw_key = self.add_api_key(scopes=("sms:write",))

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 403)

    def test_send_only_key_cannot_read_health(self):
        raw_key = self.add_api_key(scopes=("sms:write",))

        response = self.client.get(
            "/api/health", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 403)

    def test_settings_only_key_cannot_send_sms(self):
        raw_key = self.add_api_key(scopes=("settings:write",))

        response = self.client.post(
            "/sms/",
            json={"number": "+14155552671", "text": "hello"},
            headers=self.auth(raw_key),
        )

        self.assertEqual(response.status_code, 403)
