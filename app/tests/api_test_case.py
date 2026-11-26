"""Shared setup for HTTP-seam API tests.

Every test points the API at a temporary config directory and resets the
module-level runtime providers, restart debounce state and logger, so no test
reads or writes the real app data directory.
"""

import os
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
