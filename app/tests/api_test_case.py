"""Shared setup for HTTP-seam API tests.

Every test points the API at a temporary config directory and resets the
module-level runtime providers, restart debounce state and logger, so no test
reads or writes the real app data directory.
"""

import json
import os
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from services import postApi


class ApiTestCase(unittest.TestCase):

    def _reset_module_state(self):
        postApi.reset_runtime_state()
        postApi.set_api_logger(None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = self.tmp.name
        postApi.set_config_dir(self.config_dir)
        self._reset_module_state()
        # /sms/ reads the server off app state; tests never start a real one.
        postApi.app.state.server = postApi.PosiApiServer()
        self.client = TestClient(postApi.app)

    def tearDown(self):
        self.client.close()
        postApi.app.state.server = None
        self._reset_module_state()
        postApi.set_config_dir(None)
        self.tmp.cleanup()

    def auth(self, raw_key: str) -> dict:
        return {"Authorization": f"Bearer {raw_key}"}

    def write_config(self, config: dict):
        with open(
            os.path.join(self.config_dir, "config.json"), "w", encoding="utf-8"
        ) as config_file:
            json.dump(config, config_file)

    def read_key_store(self) -> dict:
        with open(
            os.path.join(self.config_dir, "keys.json"), encoding="utf-8"
        ) as keys_file:
            return json.load(keys_file)

    def add_api_key(self, scopes=("sms:write", "settings:write"),
                    name: str = "test") -> str:
        """Write a v2 key store entry and return the raw key."""
        raw_key = f"osms_{secrets.token_urlsafe(16)}"
        keys_path = os.path.join(self.config_dir, "keys.json")
        if os.path.exists(keys_path):
            store = self.read_key_store()
        else:
            store = {"version": 2, "keys": []}
        store["keys"].append({
            "id": f"k_{secrets.token_hex(4)}",
            "name": name,
            "hash": postApi.hash_api_key(raw_key),
            "scopes": sorted(scopes),
            "created_at": "2026-01-01T00:00:00+00:00",
        })
        with open(keys_path, "w", encoding="utf-8") as keys_file:
            json.dump(store, keys_file)
        return raw_key

    def add_legacy_api_key(self, scopes=("sms:write",),
                           name: str = "opensms") -> str:
        """Write a version-one key store entry and return the raw key."""
        raw_key = f"osms_{secrets.token_urlsafe(16)}"
        with open(
            os.path.join(self.config_dir, "keys.json"), "w", encoding="utf-8"
        ) as keys_file:
            json.dump({
                postApi.hash_api_key(raw_key): {
                    "name": name,
                    "scopes": list(scopes),
                },
            }, keys_file)
        return raw_key
