import hashlib
import hmac
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import webhookSvc


class MockTarget:
    """Local HTTP server standing in for a user's Webhook receiver."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.server.count += 1
            self.server.requests.put({
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })
            if self.server.delay:
                time.sleep(self.server.delay)
            status = self.server.statuses.get_nowait() \
                if not self.server.statuses.empty() else 200
            if status in (301, 302, 303, 307, 308):
                self.send_response(status)
                self.send_header("Location", self.server.redirect_location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.server.requests = queue.Queue()
        self.server.statuses = queue.Queue()
        self.server.count = 0
        self.server.delay = 0
        self.server.redirect_location = None
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    @property
    def count(self):
        return self.server.count

    def queue_statuses(self, *codes):
        for code in codes:
            self.server.statuses.put(code)

    def redirect_to(self, url):
        self.server.redirect_location = url

    def set_delay(self, seconds):
        self.server.delay = seconds

    def next_request(self, timeout=5):
        return self.server.requests.get(timeout=timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class WebhookSvcTests(unittest.TestCase):

    def setUp(self):
        self.target = MockTarget()
        self.target.redirect_to(self.target.url + "-redirected")
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.outbox_path = os.path.join(self.tmp.name, "outbox.json")
        self.status_path = os.path.join(self.tmp.name, "webhook_status.json")
        self.inbox_dir = os.path.join(self.tmp.name, "inbox")
        os.makedirs(self.inbox_dir)
        self.write_config(enabled=True, url=self.target.url, secret="s3cret")
        self.dispatchers = []

    def tearDown(self):
        for dispatcher in self.dispatchers:
            dispatcher.stop()
        self.target.close()
        self.tmp.cleanup()

    def write_config(self, enabled=True, url=None, secret="", **retry):
        webhook = {
            "enabled": enabled,
            "url": url if url is not None else self.target.url,
            "secret": secret,
        }
        webhook.update(retry)
        with open(self.config_path, "w", encoding="utf-8") as cf:
            json.dump({
                "port": 9292,
                "boot": False,
                "webhook": webhook,
            }, cf)

    def write_live_webhook(self, **overrides):
        webhook = {
            "enabled": True,
            "url": self.target.url,
            "secret": "",
            "timeout": webhookSvc.DEFAULT_TIMEOUT,
            "max_attempts": webhookSvc.DEFAULT_MAX_ATTEMPTS,
            "backoff": list(webhookSvc.DEFAULT_BACKOFF),
            "capacity": webhookSvc.DEFAULT_CAPACITY,
        }
        webhook.update(overrides)
        self.write_config(**webhook)

    def make_dispatcher(self, **kwargs):
        kwargs.setdefault("backoff", (0.01, 0.01))
        kwargs.setdefault("timeout", 1)
        dispatcher = webhookSvc.WebhookDispatcher(
            config_path=self.config_path,
            outbox_path=self.outbox_path,
            status_path=self.status_path,
            log=lambda message: None,
            **kwargs,
        )
        self.dispatchers.append(dispatcher)
        return dispatcher

    def write_spooled(self, filename, message):
        with open(os.path.join(self.inbox_dir, filename), "w",
                  encoding="utf-8") as sf:
            if isinstance(message, str):
                sf.write(message)
            else:
                json.dump(message, sf)

    def wait_for_status(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(self.status_path, encoding="utf-8") as sf:
                    return json.load(sf)
            except Exception:
                time.sleep(0.02)
        self.fail("webhook status was not written")

    ## Contract

    def test_valid_target(self):
        self.assertTrue(webhookSvc.is_valid_target("http://192.168.1.5:8000/hook"))
        self.assertTrue(webhookSvc.is_valid_target("https://example.com/hook"))
        self.assertFalse(webhookSvc.is_valid_target(""))
        self.assertFalse(webhookSvc.is_valid_target("192.168.1.5/hook"))
        self.assertFalse(webhookSvc.is_valid_target("ftp://example.com"))
        self.assertFalse(webhookSvc.is_valid_target("http://["))
        self.assertFalse(webhookSvc.is_valid_target(None))

    def test_payload_shape(self):
        payload = webhookSvc.build_inbound_payload({
            "from": "BANKALERT",
            "text": "Your OTP is 123456",
            "received_at_ms": 1700000005000,
            "sent_at_ms": 1700000000000,
            "parts": 2,
            "subscription_id": 3,
            "slot": 1,
        })
        self.assertEqual(payload["event"], "sms.received")
        self.assertEqual(payload["from"], "BANKALERT")
        self.assertEqual(payload["text"], "Your OTP is 123456")
        self.assertEqual(payload["parts"], 2)
        self.assertEqual(payload["sim"], {"subscription_id": 3, "slot": 1})
        self.assertTrue(payload["sent_at"].startswith("2023-11-14T"))
        received = datetime.fromisoformat(payload["received_at"])
        self.assertEqual(int(received.timestamp() * 1000), 1700000005000)
        self.assertEqual(len(payload["delivery_id"]), 36)

    def test_payload_omits_unknown_sim(self):
        payload = webhookSvc.build_inbound_payload({
            "from": "+14155552671",
            "text": "hi",
            "sent_at_ms": 0,
            "parts": 1,
        })
        self.assertNotIn("sim", payload)
        self.assertIsNone(payload["sent_at"])
        # Falls back to now when the receiver did not store a receipt time.
        datetime.fromisoformat(payload["received_at"])

    def test_signature(self):
        body = webhookSvc.encode_payload(webhookSvc.build_test_payload())
        signed = webhookSvc.build_headers({"event": "x", "delivery_id": "y"}, body, "secret")
        unsigned = webhookSvc.build_headers({"event": "x", "delivery_id": "y"}, body, "")
        expected = "sha256=" + hmac.new(
            b"secret", body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(signed["X-OpenSMS-Signature"], expected)
        self.assertNotIn("X-OpenSMS-Signature", unsigned)
        self.assertEqual(signed["Content-Type"], "application/json")

    def test_test_payload_event(self):
        payload = webhookSvc.build_test_payload()
        self.assertEqual(payload["event"], "webhook.test")
        self.assertNotIn("text", payload)

    def test_test_without_url_fails_cleanly(self):
        ok, status, error = webhookSvc.send_test_webhook({"url": "", "secret": ""})
        self.assertFalse(ok)
        self.assertIsNone(status)
        self.assertTrue(error)

    def test_credentialed_target_is_not_posted_by_test_delivery(self):
        url = self.target.url.replace("http://", "http://user:password@")

        ok, status, error = webhookSvc.send_test_webhook(
            {"url": url, "secret": ""}
        )

        self.assertFalse(ok)
        self.assertIsNone(status)
        self.assertIn("credentials", error)
        self.assertEqual(self.target.count, 0)

    ## Delivery

    def test_delivers_signed_payload(self):
        dispatcher = self.make_dispatcher()
        payload = webhookSvc.build_inbound_payload({
            "from": "+14155552671",
            "text": "hello",
            "sent_at_ms": 1700000000000,
            "parts": 1,
        })
        self.assertTrue(dispatcher.enqueue(payload))
        request = self.target.next_request()
        body = json.loads(request["body"])
        self.assertEqual(body["event"], "sms.received")
        self.assertEqual(body["from"], "+14155552671")
        self.assertEqual(
            request["headers"]["x-opensms-delivery"],
            payload["delivery_id"],
        )
        self.assertTrue(
            request["headers"]["x-opensms-signature"].startswith("sha256=")
        )
        status = self.wait_for_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["http_status"], 200)

    def test_disabled_does_not_enqueue(self):
        self.write_config(enabled=False)
        dispatcher = self.make_dispatcher()
        payload = webhookSvc.build_inbound_payload({"from": "x", "text": "y"})
        self.assertFalse(dispatcher.enqueue(payload))
        self.assertEqual(dispatcher.pending_count(), 0)
        with self.assertRaises(queue.Empty):
            self.target.next_request(timeout=0.5)

    def test_retries_then_succeeds(self):
        self.target.queue_statuses(500)
        dispatcher = self.make_dispatcher()
        payload = webhookSvc.build_inbound_payload({"from": "x", "text": "y"})
        dispatcher.enqueue(payload)
        first = self.target.next_request()
        second = self.target.next_request()
        self.assertEqual(json.loads(first["body"])["delivery_id"],
                         json.loads(second["body"])["delivery_id"])
        status = self.wait_for_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["attempts"], 2)

    def test_redirect_is_not_delivered(self):
        self.target.queue_statuses(302, 302, 302)
        dispatcher = self.make_dispatcher()
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        self.assertEqual(self.target.count, 3)
        status = self.wait_for_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["http_status"], 302)

    def test_fails_after_max_attempts(self):
        self.target.queue_statuses(500, 500, 500)
        dispatcher = self.make_dispatcher(max_attempts=3)
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        status = self.wait_for_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["attempts"], 3)
        self.assertEqual(status["http_status"], 500)

    def test_shutdown_keeps_in_flight_delivery(self):
        self.target.queue_statuses(500, 500, 500)
        dispatcher = self.make_dispatcher(backoff=(5, 5), max_attempts=3)
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        self.target.next_request()
        dispatcher.stop()
        with open(self.outbox_path, encoding="utf-8") as of:
            outbox = json.load(of)
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["attempts"], 1)

    def test_resume_honours_persisted_attempts(self):
        payload = webhookSvc.build_inbound_payload({"from": "x", "text": "y"})
        with open(self.outbox_path, "w", encoding="utf-8") as of:
            json.dump([{
                "delivery_id": payload["delivery_id"],
                "event": payload["event"],
                "url": self.target.url,
                "payload": payload,
                "attempts": 2,
            }], of)
        self.target.queue_statuses(500)
        dispatcher = self.make_dispatcher(max_attempts=3)
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        self.assertEqual(self.target.count, 1)
        with open(self.status_path, encoding="utf-8") as sf:
            status = json.load(sf)
        self.assertFalse(status["ok"])
        self.assertEqual(status["attempts"], 3)

    def test_legacy_credentialed_outbox_target_is_not_posted(self):
        payload = webhookSvc.build_inbound_payload({
            "from": "+14155552671", "text": "legacy",
        })
        credentialed_url = self.target.url.replace(
            "http://", "http://legacy-user:legacy-password@"
        )
        with open(self.outbox_path, "w", encoding="utf-8") as of:
            json.dump([{
                "delivery_id": payload["delivery_id"],
                "event": payload["event"],
                "url": credentialed_url,
                "payload": payload,
                "attempts": 0,
            }], of)

        dispatcher = self.make_dispatcher(max_attempts=1)

        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        status = self.wait_for_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["attempts"], 0)
        self.assertIn("credentials", status["error"])
        self.assertEqual(self.target.count, 0)

    def test_outbox_is_delivered_after_restart(self):
        payload = webhookSvc.build_inbound_payload({
            "from": "x", "text": "y", "parts": 1,
        })
        with open(self.outbox_path, "w", encoding="utf-8") as of:
            json.dump([{
                "delivery_id": payload["delivery_id"],
                "event": payload["event"],
                "url": self.target.url,
                "payload": payload,
                "attempts": 0,
            }], of)
        dispatcher = self.make_dispatcher()
        request = self.target.next_request()
        self.assertEqual(
            json.loads(request["body"])["delivery_id"],
            payload["delivery_id"],
        )
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))

    def test_capacity_drops_oldest(self):
        class BlockingDispatcher(webhookSvc.WebhookDispatcher):
            def __init__(self, *args, **kwargs):
                self.release = threading.Event()
                super().__init__(*args, **kwargs)

            def _deliver(self, record):
                self.release.wait(5)
                return True, 200, None, False

        dispatcher = self.make_dispatcher(capacity=2)
        dispatcher.stop()
        blocking = BlockingDispatcher(
            config_path=self.config_path,
            outbox_path=self.outbox_path,
            status_path=self.status_path,
            log=lambda message: None,
            capacity=2,
        )
        self.dispatchers.append(blocking)
        first = webhookSvc.build_inbound_payload({"from": "a", "text": "1"})
        blocking.enqueue(first)
        self.assertTrue(wait_for(lambda: blocking.pending_count() == 1))
        blocking.enqueue(webhookSvc.build_inbound_payload({"from": "b", "text": "2"}))
        blocking.enqueue(webhookSvc.build_inbound_payload({"from": "c", "text": "3"}))
        self.assertEqual(blocking.pending_count(), 2)
        blocking.release.set()

    ## Live Settings

    def test_missing_retry_settings_use_constants(self):
        self.write_config(enabled=True)
        config = webhookSvc.get_webhook_config(self.config_path)
        self.assertEqual(config["timeout"], webhookSvc.DEFAULT_TIMEOUT)
        self.assertEqual(
            config["max_attempts"], webhookSvc.DEFAULT_MAX_ATTEMPTS
        )
        self.assertEqual(config["backoff"], list(webhookSvc.DEFAULT_BACKOFF))
        self.assertEqual(config["capacity"], webhookSvc.DEFAULT_CAPACITY)

    def test_invalid_retry_settings_fall_back_to_constants(self):
        config = webhookSvc.normalize_webhook_config({
            "enabled": True,
            "url": self.target.url,
            "timeout": 0,
            "max_attempts": "many",
            "backoff": [0, -1],
            "capacity": 99999,
        })
        self.assertEqual(config["timeout"], webhookSvc.DEFAULT_TIMEOUT)
        self.assertEqual(
            config["max_attempts"], webhookSvc.DEFAULT_MAX_ATTEMPTS
        )
        self.assertEqual(config["backoff"], list(webhookSvc.DEFAULT_BACKOFF))
        self.assertEqual(config["capacity"], webhookSvc.DEFAULT_CAPACITY)

    def test_malformed_settings_never_crash_the_dispatcher(self):
        with open(self.config_path, "w", encoding="utf-8") as cf:
            cf.write("{not json")
        dispatcher = self.make_dispatcher()
        self.assertFalse(dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        })))
        self.assertEqual(dispatcher.pending_count(), 0)

    def test_malformed_settings_do_not_crash_a_queued_delivery(self):
        payload = webhookSvc.build_inbound_payload({"from": "x", "text": "y"})
        with open(self.outbox_path, "w", encoding="utf-8") as of:
            json.dump([{
                "delivery_id": payload["delivery_id"],
                "event": payload["event"],
                "url": self.target.url,
                "payload": payload,
                "attempts": 0,
            }], of)
        with open(self.config_path, "w", encoding="utf-8") as cf:
            cf.write("{not json")
        dispatcher = self.make_dispatcher()
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        status = self.wait_for_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "webhook disabled")

    def test_live_timeout_is_read_per_attempt(self):
        dispatcher = self.make_dispatcher(timeout=None)
        self.write_live_webhook(timeout=1, max_attempts=1)
        self.target.set_delay(2)
        started = time.time()
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        status = self.wait_for_status(timeout=5)
        elapsed = time.time() - started
        self.assertFalse(status["ok"])
        self.assertEqual(status["attempts"], 1)
        self.assertLess(elapsed, 1.8)

    def test_live_max_attempts_changes_without_restart(self):
        dispatcher = self.make_dispatcher()
        self.write_live_webhook(max_attempts=1)
        self.target.queue_statuses(500)
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        status = self.wait_for_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(self.target.count, 1)

    def test_live_backoff_is_read_per_attempt(self):
        dispatcher = self.make_dispatcher(backoff=None)
        self.write_live_webhook(max_attempts=2, backoff=[7])
        self.target.queue_statuses(500)
        waits = []

        def fake_wait(event, timeout=None):
            waits.append(timeout)
            return False

        with mock.patch.object(threading.Event, "wait", fake_wait):
            dispatcher.enqueue(webhookSvc.build_inbound_payload({
                "from": "x", "text": "y",
            }))
            status = self.wait_for_status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["attempts"], 2)
        self.assertEqual([wait for wait in waits if wait is not None], [7])

    def test_live_capacity_drops_oldest(self):
        class BlockingDispatcher(webhookSvc.WebhookDispatcher):
            def __init__(self, *args, **kwargs):
                self.release = threading.Event()
                super().__init__(*args, **kwargs)

            def _deliver(self, record):
                self.release.wait(5)
                return True, 200, None, False

        dispatcher = BlockingDispatcher(
            config_path=self.config_path,
            outbox_path=self.outbox_path,
            status_path=self.status_path,
            log=lambda message: None,
        )
        self.dispatchers.append(dispatcher)
        self.write_live_webhook(capacity=10)
        first = webhookSvc.build_inbound_payload({"from": "a", "text": "1"})
        dispatcher.enqueue(first)
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 1))
        for index in range(2, 12):
            dispatcher.enqueue(webhookSvc.build_inbound_payload({
                "from": "x", "text": str(index),
            }))
        self.assertEqual(dispatcher.pending_count(), 10)
        with open(self.outbox_path, encoding="utf-8") as of:
            records = json.load(of)
        delivery_ids = [record["delivery_id"] for record in records]
        self.assertEqual(len(delivery_ids), 10)
        self.assertNotIn(first["delivery_id"], delivery_ids)
        dispatcher.release.set()

    def test_explicit_max_attempts_wins_over_live_settings(self):
        dispatcher = self.make_dispatcher(max_attempts=1)
        self.write_live_webhook(max_attempts=5)
        self.target.queue_statuses(500)
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        status = self.wait_for_status()
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(self.target.count, 1)

    ## Inbox spool

    def test_claim_and_ack_spool(self):
        self.write_spooled("sms-1.json", {"from": "a", "text": "1"})
        self.write_spooled("sms-2.json", {"from": "b", "text": "2"})
        self.write_spooled("inbound-1.part", {"from": "partial", "text": "x"})
        self.write_spooled("sms-0.json.processing", {"from": "c", "text": "0"})
        claimed = webhookSvc.claim_inbox_messages(self.inbox_dir)
        self.assertEqual(
            [item["message"]["from"] for item in claimed], ["c", "a", "b"]
        )
        for item in claimed:
            self.assertTrue(item["path"].endswith(".processing"))
            self.assertTrue(os.path.exists(item["path"]))
            webhookSvc.ack_inbox_message(item["path"])
            self.assertFalse(os.path.exists(item["path"]))
        self.assertTrue(os.path.exists(
            os.path.join(self.inbox_dir, "inbound-1.part")
        ))
        self.assertEqual(webhookSvc.claim_inbox_messages(self.inbox_dir), [])

    def test_processing_leftovers_are_recovered(self):
        self.write_spooled("sms-1.json.processing", {"from": "a", "text": "1"})
        claimed = webhookSvc.claim_inbox_messages(self.inbox_dir)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["message"]["from"], "a")

    def test_corrupt_spool_file_is_discarded(self):
        self.write_spooled("sms-bad.json", "not json")
        self.assertEqual(webhookSvc.claim_inbox_messages(self.inbox_dir), [])
        self.assertEqual(os.listdir(self.inbox_dir), [])

    def test_missing_inbox_dir(self):
        self.assertEqual(
            webhookSvc.claim_inbox_messages(os.path.join(self.tmp.name, "nope")),
            [],
        )

    def test_format_status(self):
        self.assertEqual(
            webhookSvc.format_webhook_status(None), "No deliveries yet"
        )
        delivered = webhookSvc.format_webhook_status({
            "ok": True, "http_status": 204, "at": "now",
        })
        self.assertIn("delivered (HTTP 204)", delivered)
        failed = webhookSvc.format_webhook_status({
            "ok": False, "http_status": None, "attempts": 3,
            "error": "timed out", "at": "now",
        })
        self.assertIn("failed after 3 attempt(s)", failed)
        credentialed = webhookSvc.format_webhook_status({
            "ok": False,
            "attempts": 1,
            "error": (
                "Could not deliver to "
                "http://status-user:status-password@old.example.lan/hook"
            ),
            "at": "now",
        })
        self.assertNotIn("status-user", credentialed)
        self.assertNotIn("status-password", credentialed)
        self.assertNotIn("http://", credentialed)
        ordinary_url = "http://ordinary.example.lan/hook"
        ordinary = webhookSvc.format_webhook_status({
            "ok": False,
            "attempts": 1,
            "error": f"Could not deliver to {ordinary_url}",
            "at": "now",
        })
        self.assertIn(ordinary_url, ordinary)


if __name__ == "__main__":
    unittest.main()
