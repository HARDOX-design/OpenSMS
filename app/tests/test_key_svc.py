import json
import multiprocessing
import os
import queue
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import keySvc


def create_key_worker(config_dir, name, result_queue):
    try:
        created = keySvc.create_api_key(config_dir, name)
        result_queue.put(("ok", created["name"]))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def authenticate_key_worker(config_dir, raw_key, result_queue):
    try:
        entry = keySvc.authenticate_key(config_dir, raw_key)
        result_queue.put(("ok", entry is not None))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def delete_key_worker(config_dir, key_id, result_queue):
    try:
        deleted = keySvc.delete_api_key(config_dir, key_id)
        result_queue.put(("ok", deleted))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def rotate_phone_key_worker(config_dir, result_queue):
    try:
        raw_key = keySvc.rotate_phone_key(config_dir)
        result_queue.put(("ok", raw_key))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def read_key_store_worker(config_dir, iterations, result_queue):
    try:
        for _ in range(iterations):
            with open(
                os.path.join(config_dir, "keys.json"),
                "r",
                encoding="utf-8",
            ) as keys_file:
                store = json.load(keys_file)
            if not isinstance(store, dict) or not isinstance(
                store.get("keys"), list
            ):
                raise AssertionError("Malformed readable key store")
        result_queue.put(("ok", None))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


class KeyStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.first_dir = os.path.join(self.tmp.name, "first")
        self.second_dir = os.path.join(self.tmp.name, "second")

    def tearDown(self):
        self.tmp.cleanup()

    def test_persistence_is_scoped_to_the_explicit_config_directory(self):
        created = keySvc.create_api_key(self.first_dir, "proxy")

        self.assertTrue(os.path.isfile(os.path.join(self.first_dir, "keys.json")))
        self.assertFalse(os.path.exists(os.path.join(self.second_dir, "keys.json")))
        self.assertFalse(keySvc.has_api_keys(self.second_dir))
        self.assertIsNotNone(
            keySvc.authenticate_key(self.first_dir, created["key"])
        )
        self.assertIsNone(
            keySvc.authenticate_key(self.second_dir, created["key"])
        )

        with open(
            os.path.join(self.first_dir, "keys.json"), encoding="utf-8"
        ) as keys_file:
            stored = json.load(keys_file)
        self.assertNotIn(created["key"], json.dumps(stored))
        self.assertEqual(
            keySvc.list_api_keys(self.first_dir),
            [{field: created[field] for field in (
                "id", "name", "scopes", "created_at"
            )}],
        )

    def test_malformed_v2_hashes_are_authentication_failures(self):
        malformed_hashes = (
            123,
            None,
            {"digest": "a" * 64},
            ["a" * 64],
            "a" * 63,
            "g" * 64,
        )
        os.makedirs(self.first_dir, exist_ok=True)

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
                    os.path.join(self.first_dir, "keys.json"),
                    "w",
                    encoding="utf-8",
                ) as keys_file:
                    json.dump(store, keys_file)

                with mock.patch.object(
                    keySvc.secrets,
                    "compare_digest",
                    wraps=keySvc.secrets.compare_digest,
                ) as compare_digest:
                    match = keySvc.authenticate_key(
                        self.first_dir,
                        "osms_attempt",
                    )

                self.assertIsNone(match)
                compare_digest.assert_not_called()

    def test_malformed_v2_hashes_do_not_hide_a_later_valid_hash(self):
        raw_key = "osms_valid_after_malformed"
        valid_hash = keySvc.hash_api_key(raw_key)
        os.makedirs(self.first_dir, exist_ok=True)
        store = {
            "version": 2,
            "keys": [
                {
                    "id": "k_00000001",
                    "name": "malformed",
                    "hash": ["not", "a", "hash"],
                    "scopes": ["sms:write", "settings:write"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "owner": "proxy",
                },
                {
                    "id": "k_00000002",
                    "name": "valid",
                    "hash": valid_hash,
                    "scopes": ["sms:write", "settings:write"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "owner": "proxy",
                },
            ],
        }
        with open(
            os.path.join(self.first_dir, "keys.json"),
            "w",
            encoding="utf-8",
        ) as keys_file:
            json.dump(store, keys_file)

        match = keySvc.authenticate_key(self.first_dir, raw_key)

        self.assertIsNotNone(match)
        self.assertEqual(match["name"], "valid")

    def test_concurrent_processes_can_create_keys_without_lost_updates(self):
        keySvc.create_api_key(self.first_dir, "seed")
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()

        creators = [
            context.Process(
                target=create_key_worker,
                args=(self.first_dir, f"proxy-{index}", result_queue),
            )
            for index in range(8)
        ]
        readers = [
            context.Process(
                target=read_key_store_worker,
                args=(self.first_dir, 100, result_queue),
            )
            for _ in range(2)
        ]

        for process in creators + readers:
            process.start()
        for process in creators + readers:
            process.join(20)

        for process in creators + readers:
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)

        outcomes = []
        for _ in range(len(creators) + len(readers)):
            try:
                outcomes.append(result_queue.get(timeout=5))
            except queue.Empty:
                self.fail("A cross-process key-store worker did not report")

        failures = [
            outcome for outcome in outcomes
            if outcome[0] != "ok"
        ]
        self.assertEqual(failures, [])

        store = keySvc.load_key_store(self.first_dir)[0]
        names = [entry["name"] for entry in store["keys"]]
        self.assertEqual(len(names), 9)
        self.assertEqual(
            set(names),
            {"seed", *(f"proxy-{index}" for index in range(8))},
        )
        self.assertEqual(len(set(names)), len(names))

    def test_concurrent_authentication_migrates_a_legacy_store_once(self):
        raw_keys = ("legacy-one", "legacy-two")
        legacy_store = {
            keySvc.hash_api_key(raw_keys[0]): {
                "name": "first",
                "scopes": ["sms:write"],
            },
            keySvc.hash_api_key(raw_keys[1]): {
                "name": "second",
                "scopes": [],
            },
        }
        os.makedirs(self.first_dir, exist_ok=True)
        with open(
            os.path.join(self.first_dir, "keys.json"),
            "w",
            encoding="utf-8",
        ) as keys_file:
            json.dump(legacy_store, keys_file)

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        authenticators = [
            context.Process(
                target=authenticate_key_worker,
                args=(self.first_dir, raw_key, result_queue),
            )
            for raw_key in raw_keys
        ]
        for process in authenticators:
            process.start()
        for process in authenticators:
            process.join(20)

        for process in authenticators:
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)

        outcomes = []
        for _ in authenticators:
            try:
                outcomes.append(result_queue.get(timeout=5))
            except queue.Empty:
                self.fail("An authentication worker did not report")
        self.assertEqual(outcomes, [("ok", True), ("ok", True)])

        store = keySvc.load_key_store(self.first_dir)[0]
        self.assertEqual(store["version"], 2)
        self.assertEqual(len(store["keys"]), 2)
        hashes = {entry["hash"] for entry in store["keys"]}
        self.assertEqual(
            hashes,
            {keySvc.hash_api_key(raw_key) for raw_key in raw_keys},
        )
        for entry in store["keys"]:
            self.assertEqual(
                set(entry["scopes"]),
                {"sms:write", "settings:write"},
            )

    def test_unmarked_v2_phone_aliases_do_not_mark_a_key_as_phone(self):
        raw_key = "osms_unmarked_phone_name"
        store = {
            "version": 2,
            "keys": [{
                "id": "k_00000001",
                "name": "opensms",
                "hash": keySvc.hash_api_key(raw_key),
                "scopes": ["sms:write", "settings:write"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "phone_owned": True,
            }],
        }
        os.makedirs(self.first_dir, exist_ok=True)
        with open(
            os.path.join(self.first_dir, "keys.json"),
            "w",
            encoding="utf-8",
        ) as keys_file:
            json.dump(store, keys_file)

        keySvc.rotate_phone_key(self.first_dir)

        entries = keySvc.load_key_store(self.first_dir)[0]["keys"]
        self.assertEqual(len(entries), 2)
        old = next(entry for entry in entries if entry["id"] == "k_00000001")
        self.assertEqual(old["owner"], "proxy")
        self.assertIsNotNone(keySvc.authenticate_key(self.first_dir, raw_key))
        self.assertEqual(
            len([entry for entry in entries if entry["owner"] == "phone"]),
            1,
        )

    def test_has_api_keys_does_not_persist_a_legacy_migration(self):
        raw_key = "osms_legacy_read_only"
        legacy_store = {
            keySvc.hash_api_key(raw_key): {
                "name": "legacy",
                "scopes": ["sms:write"],
            },
        }
        os.makedirs(self.first_dir, exist_ok=True)
        with open(
            os.path.join(self.first_dir, "keys.json"),
            "w",
            encoding="utf-8",
        ) as keys_file:
            json.dump(legacy_store, keys_file)

        self.assertTrue(keySvc.has_api_keys(self.first_dir))

        with open(
            os.path.join(self.first_dir, "keys.json"), encoding="utf-8"
        ) as keys_file:
            self.assertEqual(json.load(keys_file), legacy_store)

        self.assertIsNotNone(keySvc.authenticate_key(self.first_dir, raw_key))
        with open(
            os.path.join(self.first_dir, "keys.json"), encoding="utf-8"
        ) as keys_file:
            self.assertEqual(json.load(keys_file)["version"], 2)

    def test_concurrent_processes_can_delete_different_keys(self):
        keySvc.create_api_key(self.first_dir, "keep")
        first = keySvc.create_api_key(self.first_dir, "delete-first")
        second = keySvc.create_api_key(self.first_dir, "delete-second")

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        deleters = [
            context.Process(
                target=delete_key_worker,
                args=(self.first_dir, first["id"], result_queue),
            ),
            context.Process(
                target=delete_key_worker,
                args=(self.first_dir, second["id"], result_queue),
            ),
        ]
        for process in deleters:
            process.start()
        for process in deleters:
            process.join(20)

        for process in deleters:
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)

        outcomes = []
        for _ in deleters:
            try:
                outcomes.append(result_queue.get(timeout=5))
            except queue.Empty:
                self.fail("A deletion worker did not report")
        self.assertEqual(outcomes, [("ok", True), ("ok", True)])

        names = [
            entry["name"]
            for entry in keySvc.load_key_store(self.first_dir)[0]["keys"]
        ]
        self.assertEqual(names, ["keep"])

    def test_concurrent_processes_can_rotate_the_phone_key(self):
        keySvc.rotate_phone_key(self.first_dir)
        keySvc.create_api_key(self.first_dir, "sms-proxy")

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        rotators = [
            context.Process(
                target=rotate_phone_key_worker,
                args=(self.first_dir, result_queue),
            )
            for _ in range(4)
        ]
        for process in rotators:
            process.start()
        for process in rotators:
            process.join(20)

        for process in rotators:
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)

        outcomes = []
        for _ in rotators:
            try:
                outcomes.append(result_queue.get(timeout=5))
            except queue.Empty:
                self.fail("A rotation worker did not report")
        failures = [
            outcome for outcome in outcomes
            if outcome[0] != "ok"
        ]
        self.assertEqual(failures, [])

        names = [
            entry["name"]
            for entry in keySvc.load_key_store(self.first_dir)[0]["keys"]
        ]
        self.assertEqual(names.count("opensms"), 1)
        self.assertEqual(names.count("sms-proxy"), 1)
        self.assertEqual(len(names), 2)


if __name__ == "__main__":
    unittest.main()
