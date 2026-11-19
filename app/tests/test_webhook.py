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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import webhookSvc


class MockTarget:
    """Local HTTP server standing in for a user's Webhook receiver."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.server.requests.put({
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })
            status = self.server.statuses.get_nowait() \
                if not self.server.statuses.empty() else 200
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.server.requests = queue.Queue()
        self.server.statuses = queue.Queue()
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def queue_statuses(self, *codes):
        for code in codes:
            self.server.statuses.put(code)

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
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.outbox_path = os.path.join(self.tmp.name, "outbox.json")
        self.status_path = os.path.join(self.tmp.name, "webhook_status.json")
        self.write_config(enabled=True, url=self.target.url, secret="s3cret")
        self.dispatchers = []

    def tearDown(self):
        for dispatcher in self.dispatchers:
            dispatcher.stop()
        self.target.close()
        self.tmp.cleanup()

    def write_config(self, enabled=True, url=None, secret=""):
        with open(self.config_path, "w", encoding="utf-8") as cf:
            json.dump({
                "port": 9292,
                "boot": False,
                "webhook": {
                    "enabled": enabled,
                    "url": url if url is not None else self.target.url,
                    "secret": secret,
                },
            }, cf)

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

    ## Contract

    def test_valid_target(self):
        self.assertTrue(webhookSvc.is_valid_target("http://192.168.1.5:8000/hook"))
        self.assertTrue(webhookSvc.is_valid_target("https://example.com/hook"))
        self.assertFalse(webhookSvc.is_valid_target(""))
        self.assertFalse(webhookSvc.is_valid_target("192.168.1.5/hook"))
        self.assertFalse(webhookSvc.is_valid_target("ftp://example.com"))
        self.assertFalse(webhookSvc.is_valid_target(None))

    def test_payload_shape(self):
        payload = webhookSvc.build_inbound_payload({
            "from": "BANKALERT",
            "text": "Your OTP is 123456",
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
        datetime.fromisoformat(payload["received_at"])
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
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        with open(self.status_path, encoding="utf-8") as sf:
            status = json.load(sf)
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
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        with open(self.status_path, encoding="utf-8") as sf:
            status = json.load(sf)
        self.assertTrue(status["ok"])
        self.assertEqual(status["attempts"], 2)

    def test_fails_after_max_attempts(self):
        self.target.queue_statuses(500, 500, 500)
        dispatcher = self.make_dispatcher(max_attempts=3)
        dispatcher.enqueue(webhookSvc.build_inbound_payload({
            "from": "x", "text": "y",
        }))
        self.assertTrue(wait_for(lambda: dispatcher.pending_count() == 0))
        with open(self.status_path, encoding="utf-8") as sf:
            status = json.load(sf)
        self.assertFalse(status["ok"])
        self.assertEqual(status["attempts"], 3)
        self.assertEqual(status["http_status"], 500)

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
                return True, 200, None

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

    ## Inbox plumbing

    def test_drain_inbox_file(self):
        inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        with open(inbox, "w", encoding="utf-8") as inf:
            inf.write(json.dumps({"from": "a", "text": "1"}) + "\n")
            inf.write("not json\n")
            inf.write(json.dumps({"from": "b", "text": "2"}) + "\n")
        messages = webhookSvc.drain_inbox_file(inbox)
        self.assertEqual([m["from"] for m in messages], ["a", "b"])
        self.assertFalse(os.path.exists(inbox))
        self.assertEqual(webhookSvc.drain_inbox_file(inbox), [])

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


if __name__ == "__main__":
    unittest.main()
