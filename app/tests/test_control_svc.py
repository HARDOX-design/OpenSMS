"""Direct tests for the control file, the API-to-service intent channel.

The control module is the seam where Restart normalisation, effective-port
resolution and the no-loop guarantee are proven without Android, Kivy or a
running server.
"""

import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import controlSvc


class ControlTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def control_path(self) -> str:
        return os.path.join(self.config_dir, controlSvc.CONTROL_FILE)

    def settings_path(self) -> str:
        return os.path.join(self.config_dir, controlSvc.SETTINGS_FILE)

    def write_control(self, control):
        with open(self.control_path(), "w", encoding="utf-8") as control_file:
            json.dump(control, control_file)

    def read_control(self) -> dict:
        with open(self.control_path(), encoding="utf-8") as control_file:
            return json.load(control_file)

    def write_settings(self, port):
        with open(self.settings_path(), "w", encoding="utf-8") as settings_file:
            json.dump({"app": "opensms", "port": port}, settings_file)

    def tmp_files(self) -> list:
        return glob.glob(os.path.join(self.config_dir, "*.tmp"))


class ControlFileTests(ControlTestCase):

    def test_a_missing_control_file_reads_as_empty(self):
        self.assertEqual(controlSvc.read_control(self.config_dir), {})

    def test_an_unreadable_control_file_reads_as_empty(self):
        with open(self.control_path(), "w", encoding="utf-8") as control_file:
            control_file.write("not json")

        self.assertEqual(controlSvc.read_control(self.config_dir), {})

    def test_writing_the_control_file_is_atomic_json(self):
        controlSvc.write_control(self.config_dir, {"server": "start", "port": 9292})

        self.assertEqual(
            self.read_control(), {"server": "start", "port": 9292}
        )
        self.assertEqual(self.tmp_files(), [])


class EffectivePortTests(ControlTestCase):

    def test_the_settings_port_wins(self):
        self.write_settings(9393)
        self.write_control({"port": 9292, "server": "start"})

        self.assertEqual(controlSvc.effective_port(self.config_dir), 9393)

    def test_the_control_port_is_the_fallback_without_settings(self):
        self.write_control({"port": 9393, "server": "start"})

        self.assertEqual(controlSvc.effective_port(self.config_dir), 9393)

    def test_an_unreadable_settings_file_falls_back_to_the_control_port(self):
        with open(self.settings_path(), "w", encoding="utf-8") as settings_file:
            settings_file.write("not json")
        self.write_control({"port": 9393, "server": "start"})

        self.assertEqual(controlSvc.effective_port(self.config_dir), 9393)

    def test_an_invalid_settings_port_falls_back_to_the_control_port(self):
        self.write_settings(80)
        self.write_control({"port": 9393, "server": "start"})

        self.assertEqual(controlSvc.effective_port(self.config_dir), 9393)

    def test_the_default_port_is_the_last_resort(self):
        self.assertEqual(
            controlSvc.effective_port(self.config_dir),
            controlSvc.settingsSvc.DEFAULT_PORT,
        )


class ControlPreservationTests(ControlTestCase):

    def test_requesting_a_restart_preserves_unrelated_keys(self):
        self.write_control({
            "port": 9292,
            "server": "start",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "unknown_key": "keep me",
        })

        controlSvc.request_restart(self.config_dir, 9393)

        self.assertEqual(self.read_control(), {
            "port": 9393,
            "server": "restart",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "unknown_key": "keep me",
        })
        self.assertEqual(self.tmp_files(), [])

    def test_normalising_a_restart_returns_start_and_keeps_other_keys(self):
        self.write_control({
            "port": 9393,
            "server": "restart",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "unknown_key": "keep me",
        })

        changed = controlSvc.normalise_restart(self.config_dir, 9393)

        self.assertTrue(changed)
        self.assertEqual(self.read_control(), {
            "port": 9393,
            "server": "start",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "unknown_key": "keep me",
        })
        self.assertEqual(self.tmp_files(), [])

    def test_normalising_without_a_port_uses_the_effective_port(self):
        self.write_settings(9393)
        self.write_control({"port": 9292, "server": "restart"})

        controlSvc.normalise_restart(self.config_dir)

        self.assertEqual(self.read_control()["port"], 9393)

    def test_normalising_a_non_restart_intent_does_nothing(self):
        self.write_control({"port": 9292, "server": "start"})

        self.assertFalse(controlSvc.normalise_restart(self.config_dir, 9393))
        self.assertEqual(self.read_control(), {"port": 9292, "server": "start"})

    def test_repeated_polling_does_not_restart_again(self):
        self.write_control({"port": 9292, "server": "start"})
        controlSvc.request_restart(self.config_dir, 9393)

        self.assertTrue(controlSvc.restart_pending(self.config_dir))
        self.assertTrue(controlSvc.normalise_restart(self.config_dir, 9393))

        self.assertFalse(controlSvc.restart_pending(self.config_dir))
        self.assertFalse(controlSvc.normalise_restart(self.config_dir, 9393))
        self.assertEqual(self.read_control()["server"], "start")
