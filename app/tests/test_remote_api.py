import glob
import hashlib
import hmac
import json
import os
import threading
import time
from unittest import mock

from api_test_case import ApiTestCase
from test_webhook import MockTarget

from services import keySvc, postApi
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

    def test_mutating_key_and_settings_operations_declare_request_bodies(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        paths = openapi["paths"]
        for path, method in (
            ("/api/settings", "put"),
            ("/api/keys", "post"),
        ):
            with self.subTest(path=path):
                request_body = paths[path][method]["requestBody"]
                self.assertIsNotNone(request_body)
                self.assertIn(
                    "application/json",
                    request_body["content"],
                )
        settings_schema = openapi["components"]["schemas"]["SettingsRequest"]
        self.assertEqual(
            settings_schema["properties"]["revision"]["type"],
            "integer",
        )

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

    def test_health_ignores_malformed_stored_hashes(self):
        malformed_hashes = (
            123,
            None,
            {"digest": "a" * 64},
            ["a" * 64],
            "a" * 63,
            "g" * 64,
        )
        for index, malformed_hash in enumerate(malformed_hashes):
            with self.subTest(malformed_hash=malformed_hash):
                store = {
                    "version": 2,
                    "keys": [{
                        "id": f"k_{index:08x}",
                        "name": f"malformed-{index}",
                        "hash": malformed_hash,
                        "scopes": ["sms:write", "settings:write"],
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "owner": "proxy",
                    }],
                }
                with open(
                    os.path.join(self.config_dir, "keys.json"),
                    "w",
                    encoding="utf-8",
                ) as keys_file:
                    json.dump(store, keys_file)

                response = self.client.get(
                    "/api/health",
                    headers=self.auth("osms_attempt"),
                )

                self.assertEqual(response.status_code, 401)

    def test_health_accepts_a_valid_hash_after_malformed_entries(self):
        raw_key = "osms_valid_after_malformed_http"
        store = {
            "version": 2,
            "keys": [
                {
                    "id": "k_00000001",
                    "name": "malformed-type",
                    "hash": 123,
                    "scopes": ["sms:write", "settings:write"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "owner": "proxy",
                },
                {
                    "id": "k_00000002",
                    "name": "malformed-content",
                    "hash": "z" * 64,
                    "scopes": ["sms:write", "settings:write"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "owner": "proxy",
                },
                {
                    "id": "k_00000003",
                    "name": "valid",
                    "hash": keySvc.hash_api_key(raw_key),
                    "scopes": ["sms:write", "settings:write"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "owner": "proxy",
                },
            ],
        }
        with open(
            os.path.join(self.config_dir, "keys.json"),
            "w",
            encoding="utf-8",
        ) as keys_file:
            json.dump(store, keys_file)

        response = self.client.get(
            "/api/health",
            headers=self.auth(raw_key),
        )

        self.assertEqual(response.status_code, 200)

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


class StatusTests(ApiTestCase):

    def config(self, **overrides) -> dict:
        config = {
            "app": "opensms",
            "port": 9393,
            "boot": True,
            "permissions": True,
            "revision": 5,
            "webhook": {
                "enabled": False,
                "url": "",
                "secret": "",
                "timeout": 10,
                "max_attempts": 3,
                "backoff": [5, 30],
                "capacity": 100,
            },
        }
        webhook_overrides = overrides.pop("webhook", None)
        if webhook_overrides:
            config["webhook"].update(webhook_overrides)
        config.update(overrides)
        return config

    def write_json(self, name: str, value):
        with open(
            os.path.join(self.config_dir, name), "w", encoding="utf-8"
        ) as json_file:
            json.dump(value, json_file)

    def status(self, raw_key: str):
        return self.client.get("/api/status", headers=self.auth(raw_key))

    def warning_codes(self, body: dict) -> list:
        return [warning["code"] for warning in body["warnings"]]

    def test_status_refuses_a_missing_or_invalid_key(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 403)

        self.add_api_key()
        response = self.client.get(
            "/api/status", headers=self.auth("osms_not_a_real_key")
        )
        self.assertEqual(response.status_code, 401)

    def test_status_requires_the_settings_scope(self):
        raw_key = self.add_api_key(scopes=("sms:write",))

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 403)

    def test_status_separates_the_configured_and_bound_ports(self):
        self.write_config(self.config(port=9393))
        postApi.set_runtime_providers({
            "running": lambda: True,
            "port": lambda: 9292,
            "bound_port": lambda: 9292,
        })
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"], __version__)
        self.assertEqual(body["config"], {
            "port": 9393,
            "boot": True,
            "revision": 5,
        })
        self.assertTrue(body["api"]["running"])
        self.assertEqual(body["api"]["port"], 9292)
        self.assertEqual(body["api"]["bound_port"], 9292)

    def test_missing_settings_file_reports_the_documented_defaults(self):
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["config"], {
            "port": 9292,
            "boot": False,
            "revision": 0,
        })
        self.assertFalse(body["webhook"]["enabled"])
        self.assertEqual(body["webhook"]["outbox_capacity"], 100)
        self.assertIn("webhook_disabled", self.warning_codes(body))

    def test_an_unreadable_existing_settings_file_is_unknown(self):
        os.mkdir(os.path.join(self.config_dir, "config.json"))
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["config"])
        self.assertIn(
            "webhook_disabled", self.warning_codes(response.json())
        )

    def test_non_finite_settings_numbers_are_unknown(self):
        cases = {
            "overflowing port": '{"port": 1e999, "boot": false}',
            "infinite revision": '{"revision": Infinity, "boot": false}',
            "not-a-number capacity": (
                '{"boot": false, "webhook": {"capacity": NaN}}'
            ),
        }
        raw_key = self.add_api_key()
        for name, content in cases.items():
            with self.subTest(name=name):
                with open(
                    os.path.join(self.config_dir, "config.json"),
                    "w",
                    encoding="utf-8",
                ) as config_file:
                    config_file.write(content)

                response = self.status(raw_key)

                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(response.json()["config"])

    def test_corrupt_settings_are_unknown_but_other_status_is_preserved(self):
        with open(
            os.path.join(self.config_dir, "config.json"), "w", encoding="utf-8"
        ) as config_file:
            config_file.write("not json")
        self.write_json("resp.json", {"server": "started"})
        self.write_json("webhook_status.json", {
            "ok": True,
            "http_status": 204,
            "at": "2026-11-26T12:34:56+00:00",
        })
        postApi.set_runtime_providers({
            "network_ips": lambda: {
                "wifi": [{"interface": "wlan0", "ip": "192.168.1.50"}],
                "ethernet": [],
            },
            "outbox_pending": lambda: 2,
        })
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["config"])
        self.assertEqual(body["service"]["server"], "started")
        self.assertEqual(body["network"]["wifi"], [
            {"interface": "wlan0", "ip": "192.168.1.50"},
        ])
        self.assertEqual(body["webhook"], {
            "enabled": None,
            "url_host": None,
            "secret_set": None,
            "outbox_pending": 2,
            "outbox_capacity": None,
            "last_delivery": {
                "ok": True,
                "event": None,
                "delivery_id": None,
                "http_status": 204,
                "attempts": None,
                "error": None,
                "at": "2026-11-26T12:34:56+00:00",
            },
        })
        self.assertIn("webhook_disabled", self.warning_codes(body))
        self.assertNotIn("webhook_url_invalid", self.warning_codes(body))

    def test_missing_runtime_values_are_null_not_invented(self):
        postApi.set_runtime_providers({})
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()

        self.assertIsNone(body["api"]["bound_port"])
        self.assertIsNone(body["webhook"]["outbox_pending"])
        self.assertIsNone(body["webhook"]["last_delivery"])

    def test_status_reports_service_transitions_and_unknowns(self):
        self.write_json("resp.json", {
            "server": "started",
            "serial": "none",
            "provider": "test-network",
        })
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()
        self.assertEqual(body["service"], {
            "server": "started",
            "serial": "none",
            "provider": "test-network",
        })

        with open(
            os.path.join(self.config_dir, "resp.json"), "w", encoding="utf-8"
        ) as resp_file:
            resp_file.write("not json")

        body = self.status(raw_key).json()
        self.assertEqual(body["service"], {
            "server": "unknown",
            "serial": "unknown",
            "provider": "unknown",
        })

    def test_status_reports_network_interfaces(self):
        postApi.set_runtime_providers({
            "network_ips": lambda: {
                "wifi": [
                    {"interface": "wlan0", "ip": "192.168.1.50"},
                ],
                "ethernet": [
                    {"interface": "eth0", "ip": "10.0.0.2"},
                ],
            },
        })
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()

        self.assertEqual(body["network"], {
            "wifi": [{"interface": "wlan0", "ip": "192.168.1.50"}],
            "ethernet": [{"interface": "eth0", "ip": "10.0.0.2"}],
        })
        self.assertNotIn("network_unavailable", self.warning_codes(body))

    def test_network_failure_is_an_empty_list_and_a_warning(self):
        def fail():
            raise RuntimeError("no network facts")

        postApi.set_runtime_providers({"network_ips": fail})
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["network"], {"wifi": [], "ethernet": []})
        self.assertIn("network_unavailable", self.warning_codes(body))

    def test_default_network_discovery_failure_is_distinguishable(self):
        raw_key = self.add_api_key()

        with mock.patch.object(
            postApi.netSvc,
            "get_desktop_lan_ips",
            side_effect=OSError("network discovery failed"),
        ):
            response = self.status(raw_key)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["network"], {"wifi": [], "ethernet": []})
        self.assertIn("network_unavailable", self.warning_codes(body))

        postApi.set_runtime_providers({
            "network_ips": lambda: {"wifi": [], "ethernet": []},
        })
        body = self.status(raw_key).json()
        self.assertEqual(body["network"], {"wifi": [], "ethernet": []})
        self.assertNotIn("network_unavailable", self.warning_codes(body))

    def test_status_reports_the_last_delivery_attempt_and_outbox_depth(self):
        self.write_config(self.config(
            webhook={
                "enabled": True,
                "url": "https://proxy.example.lan/hooks/opensms",
                "capacity": 100,
            },
        ))
        self.write_json("webhook_status.json", {
            "ok": False,
            "event": "sms.received",
            "delivery_id": "d-1",
            "http_status": 500,
            "attempts": 3,
            "error": "HTTP 500",
            "at": "2026-11-26T12:34:56+00:00",
        })
        postApi.set_runtime_providers({"outbox_pending": lambda: 3})
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()

        self.assertEqual(body["webhook"]["outbox_pending"], 3)
        self.assertEqual(body["webhook"]["outbox_capacity"], 100)
        self.assertEqual(body["webhook"]["last_delivery"], {
            "ok": False,
            "event": "sms.received",
            "delivery_id": "d-1",
            "http_status": 500,
            "attempts": 3,
            "error": "HTTP 500",
            "at": "2026-11-26T12:34:56+00:00",
        })

    def test_an_unreadable_delivery_status_is_null(self):
        with open(
            os.path.join(self.config_dir, "webhook_status.json"),
            "w",
            encoding="utf-8",
        ) as status_file:
            status_file.write("not json")
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()

        self.assertIsNone(body["webhook"]["last_delivery"])

    def test_status_reports_only_the_webhook_host_and_secret_presence(self):
        credentialed_url = (
            "http://proxy-user:proxy-pass@proxy.example.lan:8080/"
            "hooks/SENSITIVE-PATH?token=SENSITIVE-TOKEN"
        )
        self.write_config(self.config(
            webhook={
                "enabled": True,
                "url": credentialed_url,
                "secret": "SENSITIVE-SECRET",
            },
        ))
        self.write_json("webhook_status.json", {
            "ok": False,
            "error": f"Could not deliver to {credentialed_url}",
            "at": "2026-11-26T12:34:56+00:00",
        })
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["webhook"]["url_host"], "proxy.example.lan")
        self.assertTrue(body["webhook"]["secret_set"])
        self.assertIn("webhook_url_invalid", self.warning_codes(body))
        for sensitive in (
            "SENSITIVE-PATH",
            "SENSITIVE-TOKEN",
            "SENSITIVE-SECRET",
            "proxy-user",
            "proxy-pass",
            "/hooks",
            "/hook",
        ):
            with self.subTest(sensitive=sensitive):
                self.assertNotIn(sensitive, response.text)

    def test_status_scrubs_credentials_from_a_previous_target_error(self):
        self.write_config(self.config(webhook={
            "enabled": True,
            "url": "https://current.example.lan/hook",
        }))
        self.write_json("webhook_status.json", {
            "ok": False,
            "error": (
                "Could not deliver to "
                "http://old-user:old-password@old.example.lan/hook"
            ),
        })
        raw_key = self.add_api_key()

        response = self.status(raw_key)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("old-user", response.text)
        self.assertNotIn("old-password", response.text)
        self.assertNotIn("http://", response.text)

    def test_forwarding_disabled_warns_that_new_inbound_sms_are_discarded(self):
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()

        self.assertFalse(body["webhook"]["enabled"])
        self.assertIn("webhook_disabled", self.warning_codes(body))
        warning = next(
            warning for warning in body["warnings"]
            if warning["code"] == "webhook_disabled"
        )
        self.assertIn("new Inbound SMS", warning["message"])
        self.assertIn("discarded", warning["message"])
        self.assertNotIn("webhook_url_invalid", self.warning_codes(body))

    def test_an_enabled_but_invalid_target_is_a_warning(self):
        self.write_config(self.config(
            webhook={"enabled": True, "url": "not-a-url"},
        ))
        raw_key = self.add_api_key()

        body = self.status(raw_key).json()

        self.assertIn("webhook_url_invalid", self.warning_codes(body))
        self.assertNotIn("webhook_disabled", self.warning_codes(body))

    def test_a_malformed_webhook_object_is_unknown_but_warns_if_disabled(self):
        raw_key = self.add_api_key()
        for webhook in ([], "broken", None, 42):
            with self.subTest(webhook=webhook):
                config = self.config()
                config["webhook"] = webhook
                self.write_config(config)
                postApi.set_runtime_providers({"outbox_pending": lambda: 2})

                response = self.status(raw_key)

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["config"], {
                    "port": 9393,
                    "boot": True,
                    "revision": 5,
                })
                self.assertEqual(body["webhook"], {
                    "enabled": None,
                    "url_host": None,
                    "secret_set": None,
                    "outbox_pending": 2,
                    "outbox_capacity": None,
                    "last_delivery": None,
                })
                self.assertIn(
                    "webhook_disabled", self.warning_codes(body)
                )
                self.assertNotIn(
                    "webhook_url_invalid", self.warning_codes(body)
                )

    def test_a_malformed_enabled_state_is_unknown_but_warns_effectively(self):
        valid_url = "https://proxy.example.lan/hooks/opensms"
        cases = (
            ("true", valid_url, None),
            (1, valid_url, None),
            ([], valid_url, "webhook_disabled"),
            ({}, valid_url, "webhook_disabled"),
            ("true", "not-a-url", "webhook_url_invalid"),
            (1, 42, "webhook_url_invalid"),
        )
        raw_key = self.add_api_key()
        for enabled, url, expected_warning in cases:
            with self.subTest(enabled=enabled, url=url):
                self.write_config(self.config(webhook={
                    "enabled": enabled,
                    "url": url,
                }))

                response = self.status(raw_key)

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertIsNone(body["webhook"]["enabled"])
                self.assertEqual(
                    body["webhook"]["url_host"],
                    "proxy.example.lan" if url == valid_url else None,
                )
                if expected_warning is None:
                    self.assertNotIn(
                        "webhook_disabled", self.warning_codes(body)
                    )
                    self.assertNotIn(
                        "webhook_url_invalid", self.warning_codes(body)
                    )
                else:
                    self.assertIn(
                        expected_warning, self.warning_codes(body)
                    )
                    other_warning = (
                        "webhook_url_invalid"
                        if expected_warning == "webhook_disabled"
                        else "webhook_disabled"
                    )
                    self.assertNotIn(
                        other_warning, self.warning_codes(body)
                    )

    def test_a_malformed_stored_target_type_is_safe_and_warns(self):
        cases = {
            "number": 42,
            "list": ["https://MALFORMED-TARGET.example.lan/hook"],
            "object": {"target": "MALFORMED-TARGET"},
            "null": None,
            "malformed authority": "http://proxy-user:proxy-pass@[invalid",
        }
        raw_key = self.add_api_key()
        for name, url in cases.items():
            with self.subTest(name=name):
                self.write_config(self.config(
                    webhook={"enabled": True, "url": url},
                ))
                self.write_json("webhook_status.json", {
                    "ok": False,
                    "error": f"Could not deliver to {url}",
                })

                response = self.status(raw_key)

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertIsNone(body["webhook"]["url_host"])
                if not isinstance(url, str):
                    self.assertEqual(
                        body["webhook"]["last_delivery"]["error"],
                        "Webhook delivery failed.",
                    )
                self.assertIn(
                    "webhook_url_invalid", self.warning_codes(body)
                )
                self.assertNotIn(
                    "webhook_disabled", self.warning_codes(body)
                )
                for sensitive in (
                    "MALFORMED-TARGET",
                    "old-user",
                    "old-password",
                    "proxy-user",
                    "proxy-pass",
                ):
                    self.assertNotIn(sensitive, response.text)

    def test_an_outbox_near_capacity_is_a_warning(self):
        self.write_config(self.config(
            webhook={
                "enabled": True,
                "url": "https://proxy.example.lan/hooks/opensms",
                "capacity": 10,
            },
        ))
        raw_key = self.add_api_key()

        postApi.set_runtime_providers({"outbox_pending": lambda: 7})
        body = self.status(raw_key).json()
        self.assertNotIn("outbox_near_capacity", self.warning_codes(body))

        postApi.set_runtime_providers({"outbox_pending": lambda: 8})
        body = self.status(raw_key).json()
        self.assertIn("outbox_near_capacity", self.warning_codes(body))
        self.assertEqual(body["webhook"]["outbox_pending"], 8)
        self.assertEqual(body["webhook"]["outbox_capacity"], 10)


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

    def test_settings_redacts_userinfo_from_a_legacy_target(self):
        credentialed_url = (
            "http://proxy-user:proxy-pass@proxy.example.lan:8080/"
            "hooks/opensms?token=SENSITIVE-TOKEN"
        )
        config = {
            "port": 9292,
            "boot": False,
            "revision": 4,
            "webhook": {
                "enabled": True,
                "url": credentialed_url,
                "secret": "topsecret",
            },
        }
        self.write_config(config)
        raw_key = self.add_api_key()

        response = self.client.get(
            "/api/settings", headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["settings"]["webhook"]["url"],
            (
                "http://proxy.example.lan:8080/hooks/opensms"
                "?token=SENSITIVE-TOKEN"
            ),
        )
        for sensitive in ("proxy-user", "proxy-pass", "topsecret"):
            with self.subTest(sensitive=sensitive):
                self.assertNotIn(sensitive, response.text)
        self.assertEqual(self.read_config(), config)

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
        self.assertEqual(entry["hash"], keySvc.hash_api_key(raw_key))
        self.assertEqual(entry["scopes"], ["settings:write", "sms:write"])
        self.assertTrue(entry["id"].startswith("k_"))
        self.assertTrue(entry["created_at"])
        self.assertEqual(entry["owner"], "phone")

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

    def test_put_replacing_a_legacy_credentialed_target_never_echoes_it(self):
        config = self.stored_config()
        config["webhook"]["enabled"] = True
        config["webhook"]["url"] = (
            "http://legacy-user:legacy-password@proxy.example.lan/hooks/old"
        )
        self.write_config(config)
        raw_key = self.add_api_key()

        response = self.put(self.valid_payload(webhook={
            "enabled": True,
            "url": "https://proxy.example.lan/hooks/new",
        }), raw_key)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["settings"]["webhook"]["url"],
            "https://proxy.example.lan/hooks/new",
        )
        self.assertNotIn("legacy-user", response.text)
        self.assertNotIn("legacy-password", response.text)
        self.assertEqual(
            self.read_config()["webhook"]["url"],
            "https://proxy.example.lan/hooks/new",
        )

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

    def test_a_null_secret_is_rejected_without_changing_settings(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        before = self.read_config()

        response = self.put(
            self.valid_payload(webhook={"secret": None}), raw_key
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "webhook.secret must be a string",
        )
        self.assertEqual(self.read_config(), before)
        self.assertEqual(self.tmp_files(), [])

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

    def test_put_rejects_coercible_integer_and_boolean_values(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        before = self.read_config()

        cases = {
            "revision string": {"revision": "7"},
            "revision float": {"revision": 7.0},
            "revision boolean": {"revision": True},
            "revision null": {"revision": None},
            "port string": {"port": "9292"},
            "port float": {"port": 9292.0},
            "port boolean": {"port": True},
            "boot string true": {"boot": "true"},
            "timeout string": {"webhook": {"timeout": "7"}},
            "timeout float": {"webhook": {"timeout": 7.0}},
            "timeout boolean": {"webhook": {"timeout": True}},
            "enabled string true": {"webhook": {"enabled": "true"}},
            "max attempts string": {"webhook": {"max_attempts": "7"}},
            "max attempts float": {"webhook": {"max_attempts": 7.0}},
            "max attempts boolean": {"webhook": {"max_attempts": True}},
            "backoff string": {"webhook": {"backoff": ["7"]}},
            "backoff float": {"webhook": {"backoff": [7.0]}},
            "backoff boolean": {"webhook": {"backoff": [True]}},
            "capacity string": {"webhook": {"capacity": "100"}},
            "capacity float": {"webhook": {"capacity": 100.0}},
            "capacity boolean": {"webhook": {"capacity": True}},
        }

        for name, overrides in cases.items():
            with self.subTest(name):
                response = self.put(
                    self.valid_payload(**overrides),
                    raw_key,
                )

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

    def test_put_rejects_a_credentialed_target_even_when_disabled(self):
        self.write_config(self.stored_config())
        raw_key = self.add_api_key()
        before = self.read_config()

        targets = {
            "valid authority": (
                "http://proxy-user:proxy-pass@proxy.example.lan/hooks/opensms"
            ),
            "malformed authority": "http://proxy-user:proxy-pass@[invalid",
        }
        for name, url in targets.items():
            for enabled in (True, False):
                with self.subTest(name=name, enabled=enabled):
                    response = self.put(self.valid_payload(webhook={
                        "enabled": enabled,
                        "url": url,
                    }), raw_key)

                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIsInstance(response.json()["detail"], str)
                    self.assertNotIn("proxy-user", response.text)
                    self.assertNotIn("proxy-pass", response.text)
                    self.assertEqual(self.read_config(), before)
                    self.assertEqual(self.tmp_files(), [])

    def test_put_rejects_a_non_object_body(self):
        raw_key = self.add_api_key()

        response = self.client.put(
            "/api/settings", json=["not", "an", "object"],
            headers=self.auth(raw_key),
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Request body must be a JSON object"
        )

    def test_put_rejects_malformed_json(self):
        raw_key = self.add_api_key()

        response = self.client.put(
            "/api/settings",
            content=b'{"port": ',
            headers={**self.auth(raw_key), "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Request body must be valid JSON"
        )


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


class WebhookTestTests(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.target = MockTarget()
        self.target.redirect_to(self.target.url + "-redirected")

    def tearDown(self):
        self.target.close()
        super().tearDown()

    def post_test(self, raw_key: str, payload=None):
        kwargs = {"headers": self.auth(raw_key)}
        if payload is not None:
            kwargs["json"] = payload
        return self.client.post("/api/webhook/test", **kwargs)

    def save_webhook(self, **overrides) -> dict:
        webhook = {
            "enabled": False,
            "url": self.target.url,
            "secret": "saved-secret",
            "timeout": 5,
            "max_attempts": 3,
            "backoff": [5, 30],
            "capacity": 100,
        }
        webhook.update(overrides)
        self.write_config({"webhook": webhook})
        return webhook

    def signature_for(self, secret: str, body: bytes) -> str:
        digest = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"

    def test_success_reports_the_status_and_a_null_error(self):
        raw_key = self.add_api_key()

        response = self.post_test(
            raw_key, {"url": self.target.url, "secret": "override-secret"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "http_status": 200,
            "error": None,
        })
        request = self.target.next_request()
        self.assertEqual(json.loads(request["body"])["event"], "webhook.test")
        self.assertEqual(
            request["headers"]["x-opensms-signature"],
            self.signature_for("override-secret", request["body"]),
        )

    def test_a_failed_delivery_reports_the_http_status_and_error(self):
        self.target.queue_statuses(500)
        raw_key = self.add_api_key()

        response = self.post_test(raw_key, {"url": self.target.url})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {
            "ok": False,
            "http_status": 500,
            "error": "HTTP 500",
        })

    def test_a_redirect_counts_as_a_failure(self):
        self.target.queue_statuses(302)
        raw_key = self.add_api_key()

        response = self.post_test(raw_key, {"url": self.target.url})

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["http_status"], 302)
        self.assertTrue(body["error"])
        # The redirect must not be followed, so only the original POST arrived.
        self.assertEqual(self.target.count, 1)

    def test_an_unreachable_target_reports_no_http_status(self):
        raw_key = self.add_api_key()

        response = self.post_test(
            raw_key, {"url": "http://127.0.0.1:1/unreachable"}
        )

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIsNone(body["http_status"])
        self.assertTrue(body["error"])

    def test_no_body_uses_the_saved_target_and_secret(self):
        self.save_webhook(enabled=False, secret="saved-secret")
        before = self.read_config()
        raw_key = self.add_api_key()

        response = self.post_test(raw_key)

        self.assertEqual(response.status_code, 200)
        request = self.target.next_request()
        self.assertEqual(
            request["headers"]["x-opensms-signature"],
            self.signature_for("saved-secret", request["body"]),
        )
        self.assertEqual(self.read_config(), before)

    def test_an_override_is_used_for_the_test_only(self):
        self.save_webhook(
            url="http://127.0.0.1:1/unreachable", secret="saved-secret"
        )
        before = self.read_config()
        raw_key = self.add_api_key()

        response = self.post_test(
            raw_key, {"url": self.target.url, "secret": "override-secret"}
        )

        self.assertEqual(response.status_code, 200)
        request = self.target.next_request()
        self.assertEqual(
            request["headers"]["x-opensms-signature"],
            self.signature_for("override-secret", request["body"]),
        )
        self.assertEqual(self.read_config(), before)

        # The next test without a body uses the saved target again.
        response = self.post_test(raw_key, {"url": self.target.url})
        self.assertEqual(response.status_code, 200)
        request = self.target.next_request()
        self.assertEqual(
            request["headers"]["x-opensms-signature"],
            self.signature_for("saved-secret", request["body"]),
        )

    def test_an_empty_secret_tests_unsigned_delivery(self):
        self.save_webhook(secret="saved-secret")
        raw_key = self.add_api_key()

        response = self.post_test(
            raw_key, {"url": self.target.url, "secret": ""}
        )

        self.assertEqual(response.status_code, 200)
        request = self.target.next_request()
        self.assertNotIn("x-opensms-signature", request["headers"])
        self.assertEqual(
            self.read_config()["webhook"]["secret"], "saved-secret"
        )

    def test_credentialed_targets_are_rejected_without_posting(self):
        raw_key = self.add_api_key()
        port = self.target.server.server_address[1]
        targets = {
            "password": f"http://proxy-user:proxy-pass@127.0.0.1:{port}/hook",
            "username only": f"http://proxy-user@127.0.0.1:{port}/hook",
            "empty password": f"http://proxy-user:@127.0.0.1:{port}/hook",
            "empty userinfo": f"http://:@127.0.0.1:{port}/hook",
            "https": "https://proxy-user:proxy-pass@proxy.example.lan/hook",
        }

        for name, url in targets.items():
            with self.subTest(name):
                response = self.post_test(raw_key, {"url": url})

                self.assertEqual(response.status_code, 400, response.text)
                self.assertIsInstance(response.json()["detail"], str)
                for sensitive in ("proxy-user", "proxy-pass", ":@", "@127"):
                    with self.subTest(sensitive=sensitive):
                        self.assertNotIn(sensitive, response.text)

        legacy_url = targets["password"]
        self.save_webhook(url=legacy_url)
        response = self.post_test(raw_key)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn("proxy-user", response.text)
        self.assertNotIn("proxy-pass", response.text)
        self.assertEqual(self.target.count, 0)

    def test_a_test_needs_a_valid_target(self):
        raw_key = self.add_api_key()

        cases = {
            "no saved target": None,
            "empty override": {"url": ""},
            "not a url": {"url": "not-a-url"},
            "not http": {"url": "ftp://proxy.example.lan/hooks"},
            "no host": {"url": "http:///hooks"},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                response = self.post_test(raw_key, payload)

                self.assertEqual(response.status_code, 400, response.text)
                self.assertIsInstance(response.json()["detail"], str)

        self.assertEqual(self.target.count, 0)

    def test_the_saved_timeout_is_used(self):
        self.save_webhook(timeout=7)
        raw_key = self.add_api_key()
        calls = []

        def record(config, timeout):
            calls.append((config, timeout))
            return True, 204, None

        with mock.patch.object(
            postApi.webhookSvc, "send_test_webhook", side_effect=record
        ):
            response = self.post_test(raw_key, {"url": self.target.url})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls[0][1], 7)

    def test_body_shape_errors_keep_their_messages(self):
        raw_key = self.add_api_key()

        response = self.client.post(
            "/api/webhook/test",
            content=b'{"url": ',
            headers={**self.auth(raw_key), "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Request body must be valid JSON"
        )

        response = self.post_test(raw_key, ["not", "an", "object"])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Request body must be a JSON object"
        )

        response = self.post_test(raw_key, {"z": 1, "a": 2})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Unknown field(s) in request: a, z",
        )

    def test_unknown_fields_and_wrong_types_are_rejected(self):
        raw_key = self.add_api_key()

        cases = {
            "unknown field": {"url": self.target.url, "unknown": 1},
            "url not a string": {"url": 42},
            "secret not a string": {"secret": 42},
            "secret too long": {"secret": "x" * 257},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                response = self.post_test(raw_key, payload)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], str)

        self.assertEqual(self.target.count, 0)

    def test_a_webhook_test_requires_authentication_and_the_settings_scope(self):
        response = self.client.post(
            "/api/webhook/test", json={"url": self.target.url}
        )
        self.assertEqual(response.status_code, 403)

        raw_key = self.add_api_key(scopes=("sms:write",))
        response = self.post_test(raw_key, {"url": self.target.url})
        self.assertEqual(response.status_code, 403)

        self.assertEqual(self.target.count, 0)


class FakeTimer:
    """A threading.Timer that records its callback instead of firing."""

    instances = []

    def __init__(self, interval, target=None, args=(), kwargs=None):
        self.interval = interval
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.started = False
        self.daemon = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def fire(self):
        self.target(*self.args, **self.kwargs)


class RestartTestCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        FakeTimer.instances = []
        postApi.set_runtime_providers({"running": lambda: True})
        self.timer_patch = mock.patch.object(postApi, "Timer", FakeTimer)
        self.timer_patch.start()

    def tearDown(self):
        self.timer_patch.stop()
        super().tearDown()

    def request_path(self) -> str:
        return os.path.join(self.config_dir, postApi.controlSvc.CONTROL_FILE)

    def read_request(self):
        with open(self.request_path(), encoding="utf-8") as request_file:
            return json.load(request_file)

    def write_request(self, value: dict):
        with open(self.request_path(), "w", encoding="utf-8") as request_file:
            json.dump(value, request_file)

    def restart(self, raw_key: str):
        return self.client.post(
            "/api/server/restart", headers=self.auth(raw_key)
        )


class RestartTests(RestartTestCase):

    def test_restart_is_acknowledged_before_the_intent_is_written(self):
        self.write_config({"port": 9393, "boot": False, "webhook": {}})
        self.write_request({
            "port": 9292,
            "server": "start",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "unknown_key": "keep me",
        })
        raw_key = self.add_api_key()

        with mock.patch.object(postApi.PosiApiServer, "stop") as stop_server:
            response = self.restart(raw_key)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {
            "status": "restarting",
            "port": 9393,
        })
        stop_server.assert_not_called()
        # The response is already back while the control file still says start.
        self.assertEqual(self.read_request()["server"], "start")
        self.assertEqual(len(FakeTimer.instances), 1)
        timer = FakeTimer.instances[0]
        self.assertTrue(timer.started)
        self.assertTrue(timer.daemon)
        self.assertEqual(timer.interval, postApi.RESTART_DELAY_SECONDS)

        timer.fire()

        self.assertEqual(self.read_request(), {
            "port": 9393,
            "server": "restart",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "unknown_key": "keep me",
        })

    def test_the_restart_uses_the_effective_port_over_the_control_port(self):
        self.write_config({"port": 9393, "boot": False, "webhook": {}})
        self.write_request({"port": 9292, "server": "start"})
        raw_key = self.add_api_key()

        response = self.restart(raw_key)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["port"], 9393)

    def test_a_second_restart_within_five_seconds_is_refused(self):
        raw_key = self.add_api_key()

        first = self.restart(raw_key)
        second = self.restart(raw_key)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertIsInstance(second.json()["detail"], str)
        self.assertEqual(len(FakeTimer.instances), 1)

    def test_the_debounce_lifts_after_five_seconds(self):
        raw_key = self.add_api_key()
        self.restart(raw_key)

        postApi._last_restart_at = (
            time.monotonic() - postApi.RESTART_DEBOUNCE_SECONDS - 1
        )
        second = self.restart(raw_key)

        self.assertEqual(second.status_code, 202)
        self.assertEqual(len(FakeTimer.instances), 2)

    def test_a_failed_intent_write_releases_the_debounce(self):
        raw_key = self.add_api_key()
        self.assertEqual(self.restart(raw_key).status_code, 202)

        with mock.patch.object(
            postApi.controlSvc,
            "request_restart",
            side_effect=OSError("disk full"),
        ):
            FakeTimer.instances[-1].fire()

        self.assertFalse(os.path.exists(self.request_path()))
        retry = self.restart(raw_key)
        self.assertEqual(retry.status_code, 202)

    def test_a_restart_is_refused_while_the_server_is_not_running(self):
        postApi.set_runtime_providers({"running": lambda: False})
        raw_key = self.add_api_key()

        response = self.restart(raw_key)

        self.assertEqual(response.status_code, 409)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertFalse(os.path.exists(self.request_path()))
        self.assertEqual(FakeTimer.instances, [])

    def test_restart_requires_authentication_and_the_settings_scope(self):
        response = self.client.post("/api/server/restart")
        self.assertEqual(response.status_code, 403)

        raw_key = self.add_api_key(scopes=("sms:write",))
        response = self.restart(raw_key)
        self.assertEqual(response.status_code, 403)

        self.assertEqual(FakeTimer.instances, [])


class ControlRouteTests(ApiTestCase):

    def test_the_superseded_control_route_is_gone(self):
        raw_key = self.add_api_key()

        response = self.client.post(
            "/control/", json={"api": "start"}, headers=self.auth(raw_key)
        )

        self.assertEqual(response.status_code, 404)


class KeyCreateTests(ApiTestCase):

    def create_key(self, raw_key: str, payload: dict):
        return self.client.post(
            "/api/keys", json=payload, headers=self.auth(raw_key)
        )

    def test_create_returns_the_key_once_and_it_authenticates_immediately(self):
        admin = self.add_api_key(name="admin")

        response = self.create_key(admin, {"name": "  sms-proxy  "})

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["name"], "sms-proxy")
        self.assertRegex(body["id"], r"^k_[0-9a-f]{8}$")
        self.assertEqual(body["scopes"], ["settings:write", "sms:write"])
        self.assertTrue(body["created_at"])
        self.assertTrue(body["key"].startswith("osms_"))
        self.assertNotIn("hash", body)

        health = self.client.get("/api/health", headers=self.auth(body["key"]))
        self.assertEqual(health.status_code, 200)

    def test_scopes_are_an_optional_subset(self):
        admin = self.add_api_key(name="admin")

        response = self.create_key(
            admin, {"name": "sender", "scopes": ["sms:write"]}
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["scopes"], ["sms:write"])
        key = response.json()["key"]
        self.assertEqual(
            self.client.post(
                "/sms/",
                json={"number": "+14155552671", "text": "hello"},
                headers=self.auth(key),
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/api/settings", headers=self.auth(key)
            ).status_code,
            403,
        )

    def test_an_empty_or_unknown_scope_set_is_rejected(self):
        admin = self.add_api_key(name="admin")

        cases = {
            "empty list": [],
            "null": None,
            "unknown scope": ["sms:read"],
            "mixed unknown": ["sms:write", "admin"],
            "not a list": "sms:write",
        }
        for name, scopes in cases.items():
            with self.subTest(name):
                response = self.create_key(
                    admin, {"name": "bad-scopes", "scopes": scopes}
                )

                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], str)

    def test_a_duplicate_name_differing_only_by_case_is_refused(self):
        admin = self.add_api_key(name="admin")
        first = self.create_key(admin, {"name": "sms-proxy"})
        self.assertEqual(first.status_code, 201)

        response = self.create_key(admin, {"name": "SMS-Proxy"})

        self.assertEqual(response.status_code, 409)
        self.assertIsInstance(response.json()["detail"], str)

    def test_the_phone_key_name_is_reserved_case_insensitively(self):
        admin = self.add_api_key(name="admin")

        for name in ("opensms", "OpenSMS", "  OPENSMS  "):
            with self.subTest(name=name):
                response = self.create_key(admin, {"name": name})
                self.assertEqual(response.status_code, 422)
                self.assertIn("reserved", response.json()["detail"].lower())

    def test_names_are_trimmed_and_length_checked(self):
        admin = self.add_api_key(name="admin")

        cases = {
            "too short": "a",
            "blank": "   ",
            "too long": "x" * 65,
            "control character": "bad\nname",
            "C1 control character": "bad\u0085name",
            "line separator": "bad\u2028name",
            "not a string": 42,
        }
        for name, value in cases.items():
            with self.subTest(name):
                response = self.create_key(admin, {"name": value})

                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], str)

    def test_create_rejects_unknown_fields_and_a_non_object_body(self):
        admin = self.add_api_key(name="admin")

        response = self.create_key(
            admin, {"name": "proxy", "unexpected": True, "another": 1}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Unknown field(s) in request: another, unexpected",
        )

        response = self.client.post(
            "/api/keys", json=["not", "an", "object"],
            headers=self.auth(admin),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Request body must be a JSON object"
        )

    def test_create_rejects_malformed_json_and_a_missing_name(self):
        admin = self.add_api_key(name="admin")

        response = self.client.post(
            "/api/keys",
            content=b'{"name": ',
            headers={**self.auth(admin), "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Request body must be valid JSON"
        )

        response = self.create_key(admin, {})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "Missing required field: name"
        )

    def test_create_requires_authentication_and_the_settings_scope(self):
        response = self.client.post(
            "/api/keys", json={"name": "proxy"}
        )
        self.assertEqual(response.status_code, 403)

        sender = self.add_api_key(name="sender", scopes=("sms:write",))
        response = self.create_key(sender, {"name": "proxy"})
        self.assertEqual(response.status_code, 403)

    def test_the_stored_key_is_hashed_and_never_returned_again(self):
        admin = self.add_api_key(name="admin")
        created = self.create_key(admin, {"name": "proxy"}).json()

        store = self.read_key_store()
        entry = next(
            key for key in store["keys"] if key["id"] == created["id"]
        )
        self.assertEqual(entry["hash"], keySvc.hash_api_key(created["key"]))
        self.assertEqual(entry["scopes"], ["settings:write", "sms:write"])
        self.assertNotIn(created["key"], json.dumps(store))


class KeyListTests(ApiTestCase):

    def test_list_never_returns_hashes(self):
        admin = self.add_api_key(name="admin")
        created = self.client.post(
            "/api/keys", json={"name": "proxy"}, headers=self.auth(admin)
        ).json()

        response = self.client.get("/api/keys", headers=self.auth(admin))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["keys"]), 2)
        entry = next(
            key for key in body["keys"] if key["id"] == created["id"]
        )
        self.assertEqual(entry, {
            "id": created["id"],
            "name": "proxy",
            "scopes": ["settings:write", "sms:write"],
            "created_at": created["created_at"],
        })
        self.assertNotIn("owner", entry)
        self.assertNotIn("hash", entry)
        self.assertNotIn("key", entry)
        self.assertNotIn("hash", response.text)
        self.assertNotIn(created["key"], response.text)

    def test_list_requires_authentication_and_the_settings_scope(self):
        response = self.client.get("/api/keys")
        self.assertEqual(response.status_code, 403)

        sender = self.add_api_key(name="sender", scopes=("sms:write",))
        response = self.client.get("/api/keys", headers=self.auth(sender))
        self.assertEqual(response.status_code, 403)


class KeyDeleteTests(ApiTestCase):

    def delete(self, raw_key: str, key_id: str):
        return self.client.delete(
            f"/api/keys/{key_id}", headers=self.auth(raw_key)
        )

    def test_delete_takes_effect_immediately(self):
        admin = self.add_api_key(name="admin")
        created = self.client.post(
            "/api/keys", json={"name": "proxy"}, headers=self.auth(admin)
        ).json()
        key = created["key"]

        response = self.delete(admin, created["id"])

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            self.client.get("/api/health", headers=self.auth(key)).status_code,
            401,
        )

    def test_an_unknown_or_malformed_id_is_not_found(self):
        admin = self.add_api_key(name="admin")
        self.add_api_key(name="other")

        for key_id in ("k_deadbeef", "not-an-id", "k_xyz"):
            with self.subTest(key_id):
                response = self.delete(admin, key_id)

                self.assertEqual(response.status_code, 404)

    def test_deleting_the_last_remaining_key_is_refused(self):
        only_key = self.add_api_key(name="only")
        key_id = self.read_key_store()["keys"][0]["id"]

        response = self.delete(only_key, key_id)

        self.assertEqual(response.status_code, 409)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(only_key)
            ).status_code,
            200,
        )

    def test_the_caller_can_delete_its_own_key_while_another_remains(self):
        first = self.add_api_key(name="first")
        second = self.add_api_key(name="second")
        first_id = next(
            key["id"] for key in self.read_key_store()["keys"]
            if key["name"] == "first"
        )

        response = self.delete(first, first_id)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(first)
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(second)
            ).status_code,
            200,
        )

    def test_delete_requires_authentication_and_the_settings_scope(self):
        admin = self.add_api_key(name="admin")
        other = self.add_api_key(name="other")
        other_id = next(
            key["id"] for key in self.read_key_store()["keys"]
            if key["name"] == "other"
        )

        self.assertEqual(
            self.client.delete(f"/api/keys/{other_id}").status_code, 403
        )

        sender = self.add_api_key(name="sender", scopes=("sms:write",))
        self.assertEqual(
            self.delete(sender, other_id).status_code, 403
        )
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(other)
            ).status_code,
            200,
        )


class PhoneKeyRotationTests(ApiTestCase):

    def test_rotation_replaces_only_the_phone_key(self):
        old_phone = self.add_api_key(name="opensms", owner="phone")
        proxy = self.add_api_key(name="sms-proxy")

        new_phone = postApi.generate_new_api_key()

        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(old_phone)
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(new_phone)
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(proxy)
            ).status_code,
            200,
        )
        names = [key["name"] for key in self.read_key_store()["keys"]]
        self.assertEqual(names.count("opensms"), 1)
        self.assertIn("sms-proxy", names)

    def test_rotation_preserves_an_existing_proxy_named_opensms(self):
        proxy = self.add_api_key(name="OpenSMS", owner="proxy")

        new_phone = postApi.generate_new_api_key()

        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(proxy)
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(new_phone)
            ).status_code,
            200,
        )
        entries = self.read_key_store()["keys"]
        proxies = [
            entry for entry in entries
            if entry.get("owner") == "proxy"
        ]
        phones = [
            entry for entry in entries
            if entry.get("owner") == "phone"
        ]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0]["name"], "OpenSMS")
        self.assertEqual(len(phones), 1)

    def test_rotation_adds_a_phone_key_without_touching_proxy_keys(self):
        proxy = self.add_api_key(name="sms-proxy")

        new_phone = postApi.generate_new_api_key()

        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(new_phone)
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/api/health", headers=self.auth(proxy)
            ).status_code,
            200,
        )
        self.assertEqual(len(self.read_key_store()["keys"]), 2)
