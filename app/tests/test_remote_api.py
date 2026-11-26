import glob
import os
import threading
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


class RecordingLogger:

    def __init__(self):
        self.messages = []
        self.written = threading.Event()
        self._lock = threading.Lock()

    def write_log(self, message: str):
        with self._lock:
            self.messages.append(message)
        self.written.set()

    def text(self) -> str:
        with self._lock:
            return "\n".join(self.messages)


class SettingsWriteTestCase(ApiTestCase):

    def valid_payload(self, **overrides) -> dict:
        payload = {
            "port": 9292,
            "boot": False,
            "webhook": {
                "enabled": False,
                "url": "",
                "timeout": 10,
                "max_attempts": 3,
                "backoff": [5, 30],
                "capacity": 100,
            },
        }
        webhook_overrides = overrides.pop("webhook", None)
        if webhook_overrides:
            payload["webhook"].update(webhook_overrides)
        payload.update(overrides)
        return payload

    def stored_config(self) -> dict:
        return {
            "app": "opensms",
            "port": 9292,
            "boot": False,
            "permissions": True,
            "revision": 3,
            "unknown_top_level": "internal",
            "webhook": {
                "enabled": False,
                "url": "",
                "secret": "old-secret",
                "timeout": 10,
                "max_attempts": 3,
                "backoff": [5, 30],
                "capacity": 100,
                "unknown_webhook": "internal",
            },
        }

    def put(self, payload: dict, raw_key: str):
        return self.client.put(
            "/api/settings", json=payload, headers=self.auth(raw_key)
        )

    def tmp_files(self) -> list:
        return glob.glob(os.path.join(self.config_dir, "*.tmp"))


class SettingsWriteTests(SettingsWriteTestCase):

    def test_put_replaces_the_writable_subset_and_bumps_the_revision(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(
            port=9393,
            boot=True,
            webhook={
                "enabled": True,
                "url": "https://proxy.example.lan/hooks/opensms",
                "timeout": 7,
                "max_attempts": 4,
                "backoff": [1, 2, 3],
                "capacity": 250,
            },
        ), raw_key)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["revision"], 4)
        self.assertEqual(body["settings"]["port"], 9393)
        self.assertTrue(body["settings"]["boot"])
        self.assertEqual(body["settings"]["webhook"], {
            "enabled": True,
            "url": "https://proxy.example.lan/hooks/opensms",
            "secret_set": True,
            "timeout": 7,
            "max_attempts": 4,
            "backoff": [1, 2, 3],
            "capacity": 250,
        })
        self.assertNotIn("old-secret", response.text)

        on_disk = self.read_config()
        self.assertEqual(on_disk["revision"], 4)
        self.assertEqual(on_disk["port"], 9393)
        self.assertTrue(on_disk["boot"])
        self.assertEqual(on_disk["app"], "opensms")
        self.assertTrue(on_disk["permissions"])
        self.assertEqual(on_disk["unknown_top_level"], "internal")
        self.assertEqual(on_disk["webhook"]["unknown_webhook"], "internal")
        self.assertEqual(on_disk["webhook"]["secret"], "old-secret")
        self.assertEqual(self.tmp_files(), [])

    def test_omitting_the_secret_keeps_the_stored_secret(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["settings"]["webhook"]["secret_set"])
        self.assertEqual(self.read_config()["webhook"]["secret"], "old-secret")

    def test_an_empty_secret_clears_the_stored_secret(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(
            self.valid_payload(webhook={"secret": ""}), raw_key
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["settings"]["webhook"]["secret_set"])
        self.assertEqual(self.read_config()["webhook"]["secret"], "")

    def test_a_non_empty_secret_replaces_the_stored_secret(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(
            self.valid_payload(webhook={"secret": "new-shared-secret"}), raw_key
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["settings"]["webhook"]["secret_set"])
        self.assertEqual(
            self.read_config()["webhook"]["secret"], "new-shared-secret"
        )
        self.assertNotIn("new-shared-secret", response.text)

    def test_put_rejects_a_secret_longer_than_256_characters(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(
            self.valid_payload(webhook={"secret": "x" * 257}), raw_key
        )

        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(self.read_config()["webhook"]["secret"], "old-secret")


class SettingsValidationTests(SettingsWriteTestCase):

    def test_put_rejects_invalid_values_and_leaves_settings_unchanged(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        before = self.read_config()

        cases = {
            "port below range": {"port": 1024},
            "port above range": {"port": 65536},
            "port not an integer": {"port": "9292"},
            "port is a boolean": {"port": True},
            "boot is not a boolean": {"boot": "yes"},
            "enabled is not a boolean": {"webhook": {"enabled": "yes"}},
            "url is not a string": {"webhook": {"url": 42}},
            "timeout below range": {"webhook": {"timeout": 0}},
            "timeout above range": {"webhook": {"timeout": 61}},
            "max attempts below range": {"webhook": {"max_attempts": 0}},
            "max attempts above range": {"webhook": {"max_attempts": 51}},
            "backoff empty": {"webhook": {"backoff": []}},
            "backoff has a zero": {"webhook": {"backoff": [0]}},
            "backoff has a negative value": {"webhook": {"backoff": [-1]}},
            "backoff above range": {"webhook": {"backoff": [3601]}},
            "backoff too long": {"webhook": {"backoff": [1] * 9}},
            "backoff is not a list": {"webhook": {"backoff": "5"}},
            "capacity below range": {"webhook": {"capacity": 9}},
            "capacity above range": {"webhook": {"capacity": 5001}},
            "unknown top-level field": {"unknown_top_level": 1},
            "unknown webhook field": {"webhook": {"unknown_webhook": 1}},
        }

        for name, overrides in cases.items():
            with self.subTest(name):
                response = self.put(self.valid_payload(**overrides), raw_key)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], str)
                self.assertTrue(response.json()["detail"])
                self.assertEqual(self.read_config(), before)
                self.assertEqual(self.tmp_files(), [])

    def test_put_rejects_missing_required_fields(self):
        raw_key = self.add_api_key()
        cases = {
            "port": "port",
            "boot": "boot",
            "webhook": "webhook",
            "webhook.enabled": ("webhook", "enabled"),
            "webhook.url": ("webhook", "url"),
            "webhook.timeout": ("webhook", "timeout"),
            "webhook.max_attempts": ("webhook", "max_attempts"),
            "webhook.backoff": ("webhook", "backoff"),
            "webhook.capacity": ("webhook", "capacity"),
        }

        for name, path in cases.items():
            with self.subTest(name):
                payload = self.valid_payload()
                if isinstance(path, tuple):
                    del payload[path[0]][path[1]]
                else:
                    del payload[path]

                response = self.put(payload, raw_key)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(name.split(".")[-1], response.json()["detail"])

    def test_put_rejects_enabling_forwarding_without_a_valid_target(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        before = self.read_config()

        for name, url in {
            "empty target": "",
            "not a url": "not-a-url",
            "not http": "ftp://proxy.example.lan/hooks",
            "no host": "http:///hooks",
        }.items():
            with self.subTest(name):
                response = self.put(
                    self.valid_payload(webhook={"enabled": True, "url": url}),
                    raw_key,
                )

                self.assertEqual(response.status_code, 400, response.text)
                self.assertIsInstance(response.json()["detail"], str)
                self.assertEqual(self.read_config(), before)
                self.assertEqual(self.tmp_files(), [])

    def test_put_rejects_a_non_object_body(self):
        raw_key = self.add_api_key()

        response = self.client.put(
            "/api/settings", json=["not", "an", "object"],
            headers=self.auth(raw_key),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(response.json()["detail"], str)

    def test_put_rejects_malformed_json(self):
        raw_key = self.add_api_key()

        response = self.client.put(
            "/api/settings",
            content=b'{"port": ',
            headers={**self.auth(raw_key), "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(response.json()["detail"], str)


class SettingsRevisionTests(SettingsWriteTestCase):

    def test_a_stale_revision_is_a_conflict(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        before = self.read_config()

        response = self.put(self.valid_payload(revision=2), raw_key)

        self.assertEqual(response.status_code, 409)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(self.read_config(), before)
        self.assertEqual(self.tmp_files(), [])

    def test_a_matching_revision_succeeds(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(revision=3), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 4)

    def test_an_absent_revision_is_last_writer_wins(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 4)

    def test_a_non_integer_revision_is_rejected(self):
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(revision="3"), raw_key)

        self.assertEqual(response.status_code, 422)

    def test_schema_errors_are_reported_before_a_stale_revision(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(
            self.valid_payload(revision=1, port=80), raw_key
        )

        self.assertEqual(response.status_code, 422)

    def test_a_stale_revision_is_reported_before_a_business_rule(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()

        response = self.put(
            self.valid_payload(
                revision=1, webhook={"enabled": True, "url": ""}
            ),
            raw_key,
        )

        self.assertEqual(response.status_code, 409)


class SettingsRestartRequiredTests(SettingsWriteTestCase):

    def test_restart_is_not_required_when_the_saved_port_is_bound(self):
        postApi.set_runtime_providers({"bound_port": lambda: 9292})
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(port=9292), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["restart_required"])

    def test_restart_is_required_when_the_saved_port_differs(self):
        postApi.set_runtime_providers({"bound_port": lambda: 9292})
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(port=9393), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["restart_required"])

    def test_a_pending_port_change_keeps_reporting_restart_required(self):
        config = self.stored_config()
        config["port"] = 9393
        self.write_config(config)
        postApi.set_runtime_providers({"bound_port": lambda: 9292})
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(port=9393), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["restart_required"])

    def test_restart_is_not_required_when_the_bound_port_is_unknown(self):
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(port=9393), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["restart_required"])


class SettingsWriteAuthTests(SettingsWriteTestCase):

    def test_put_refuses_a_missing_key(self):
        response = self.client.put("/api/settings", json=self.valid_payload())

        self.assertEqual(response.status_code, 403)

    def test_put_refuses_an_invalid_key(self):
        self.add_api_key()

        response = self.put(self.valid_payload(), "osms_not_a_real_key")

        self.assertEqual(response.status_code, 401)

    def test_put_requires_the_settings_scope(self):
        raw_key = self.add_api_key(scopes=("sms:write",))

        response = self.put(self.valid_payload(), raw_key)

        self.assertEqual(response.status_code, 403)


class SettingsWriteLoggingTests(SettingsWriteTestCase):

    def test_logs_name_fields_without_the_secret_or_the_body(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        logger = RecordingLogger()
        postApi.set_api_logger(logger)

        response = self.put(self.valid_payload(
            webhook={
                "enabled": True,
                "url": "https://proxy.example.lan/hooks/SENSITIVE-PATH",
                "secret": "super-duper-secret",
            },
        ), raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(logger.written.wait(timeout=2))
        logged = logger.text()
        self.assertIn("Settings updated", logged)
        self.assertIn("port", logged)
        self.assertIn("webhook", logged)
        self.assertNotIn("super-duper-secret", logged)
        self.assertNotIn("SENSITIVE-PATH", logged)
        self.assertNotIn("old-secret", logged)
