""" Inbound SMS webhook delivery.

This module is deliberately free of Kivy, jnius and Android imports so the
payload contract, signing and outbox behaviour can be tested on any platform.
"""

import hashlib
import hmac
import json
import os
import queue
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from services.config import __version__

# Public contract values
EVENT_INBOUND = "sms.received"
EVENT_TEST = "webhook.test"

# Delivery semantics (see docs/adr/0001-inbound-sms-webhook.md)
DEFAULT_TIMEOUT = 10
DEFAULT_BACKOFF = (5, 30)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CAPACITY = 100

## Config

def default_webhook_config() -> dict:
    return {
        "enabled": False,
        "url": "",
        "secret": "",
    }

def normalize_webhook_config(raw: dict | None) -> dict:
    config = default_webhook_config()
    if isinstance(raw, dict):
        config["enabled"] = bool(raw.get("enabled", False))
        config["url"] = str(raw.get("url") or "").strip()
        config["secret"] = str(raw.get("secret") or "")
    return config

def get_webhook_config(config_path: str) -> dict:
    """Read the current webhook config from the app config file (hot reload)."""
    try:
        with open(config_path, "r", encoding="utf-8") as cf:
            app_config = json.load(cf)
    except Exception:
        return default_webhook_config()
    return normalize_webhook_config(app_config.get("webhook"))

def is_valid_target(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

## Payload

def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def iso_from_millis(timestamp_ms) -> str | None:
    if not isinstance(timestamp_ms, (int, float)) or timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).isoformat(timespec="seconds")

def iso_from_millis_local(timestamp_ms) -> str | None:
    if not isinstance(timestamp_ms, (int, float)) or timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(
        timestamp_ms / 1000
    ).astimezone().isoformat(timespec="seconds")

def build_inbound_payload(message: dict) -> dict:
    """Turn a stored inbound message into the public webhook payload.

    `message` is what the Android receiver wrote to the inbox spool. The sender
    is kept verbatim because it may be an alphanumeric sender ID.
    """
    payload = {
        "event": EVENT_INBOUND,
        "delivery_id": str(uuid.uuid4()),
        "received_at": (
            iso_from_millis_local(message.get("received_at_ms")) or iso_now()
        ),
        "sent_at": iso_from_millis(message.get("sent_at_ms")),
        "from": message.get("from") or "unknown",
        "text": message.get("text") or "",
        "parts": int(message.get("parts") or 1),
    }
    sim = {}
    subscription_id = message.get("subscription_id")
    if isinstance(subscription_id, int) and subscription_id >= 0:
        sim["subscription_id"] = subscription_id
    slot = message.get("slot")
    if isinstance(slot, int) and slot >= 0:
        sim["slot"] = slot
    if sim:
        payload["sim"] = sim
    return payload

def build_test_payload() -> dict:
    return {
        "event": EVENT_TEST,
        "delivery_id": str(uuid.uuid4()),
        "sent_at": iso_now(),
    }

def encode_payload(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

## Signing / HTTP

def sign_body(secret: str, body: bytes) -> str | None:
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"

def build_headers(payload: dict, body: bytes, secret: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"OpenSMS/{__version__}",
        "X-OpenSMS-Event": payload["event"],
        "X-OpenSMS-Delivery": payload["delivery_id"],
    }
    signature = sign_body(secret, body)
    if signature:
        headers["X-OpenSMS-Signature"] = signature
    return headers

class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Treat 3xx as failures: a redirected POST must not count as delivered."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urlrequest.build_opener(_NoRedirect())

def post_json(url: str, body: bytes, headers: dict, timeout: float) -> tuple:
    """POST once. Returns (ok, http_status, error_message)."""
    try:
        req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    except Exception as exc:
        return False, None, str(exc) or exc.__class__.__name__
    try:
        with _opener.open(req, timeout=timeout) as resp:
            resp.read()
            status = getattr(resp, "status", resp.getcode())
        return (200 <= int(status) < 300), int(status), None
    except urlerror.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        return False, int(exc.code), f"HTTP {exc.code}"
    except Exception as exc:
        return False, None, str(exc) or exc.__class__.__name__

## Files

def _atomic_write(path: str, text: str):
    tmp_path = f"{path}.tmp"
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as tf:
        tf.write(text)
    os.replace(tmp_path, path)

def read_webhook_status(status_path: str) -> dict | None:
    try:
        with open(status_path, "r", encoding="utf-8") as sf:
            status = json.load(sf)
        return status if isinstance(status, dict) else None
    except Exception:
        return None

def format_webhook_status(status: dict | None) -> str:
    if not status:
        return "No deliveries yet"
    when = status.get("at", "")
    if status.get("ok"):
        return f"Last delivery: delivered (HTTP {status.get('http_status')}) at {when}"
    error = status.get("error") or "failed"
    if error == "webhook disabled":
        return f"Last delivery: not sent (webhook disabled) at {when}"
    attempts = status.get("attempts", 0)
    return f"Last delivery: failed after {attempts} attempt(s) at {when} ({error})"

def _remove_quietly(path: str):
    try:
        os.remove(path)
    except OSError:
        pass

def claim_inbox_messages(inbox_dir: str) -> list:
    """Claim spooled inbound messages so they can be forwarded durably.

    The Android receiver writes one JSON file per SMS, then renames it into
    place. Each returned item has a `path` (renamed to `.processing`) and the
    parsed `message`. Acknowledge with `ack_inbox_message` only after the
    message is durably in the Outbox, so a crash leaves it to be claimed again.
    """
    if not os.path.isdir(inbox_dir):
        return []
    claimed = []
    for name in sorted(os.listdir(inbox_dir)):
        if not (name.endswith(".json") or name.endswith(".processing")):
            continue
        path = os.path.join(inbox_dir, name)
        if name.endswith(".json"):
            processing_path = f"{path}.processing"
            try:
                os.replace(path, processing_path)
            except OSError:
                continue
            path = processing_path
        try:
            with open(path, "r", encoding="utf-8") as message_file:
                message = json.load(message_file)
        except Exception:
            _remove_quietly(path)
            continue
        if isinstance(message, dict):
            claimed.append({"path": path, "message": message})
        else:
            _remove_quietly(path)
    return claimed

def ack_inbox_message(path: str):
    _remove_quietly(path)

## Dispatcher

class WebhookDispatcher:
    """Single ordered worker that delivers Enqueued webhooks from an outbox."""

    def __init__(
        self,
        config_path: str,
        outbox_path: str,
        status_path: str,
        log=None,
        timeout: float = DEFAULT_TIMEOUT,
        backoff: tuple = DEFAULT_BACKOFF,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        capacity: int = DEFAULT_CAPACITY,
    ):
        self.config_path = config_path
        self.outbox_path = outbox_path
        self.status_path = status_path
        self.timeout = timeout
        self.backoff = tuple(backoff)
        self.max_attempts = max(1, int(max_attempts))
        self.capacity = max(1, int(capacity))
        self.log = log or (lambda message: None)
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._outbox: OrderedDict = OrderedDict()
        self._load_outbox()
        self._thread = threading.Thread(
            target=self._run,
            name="webhook-outbox",
            daemon=True,
        )
        self._thread.start()

    ## Outbox

    def _load_outbox(self):
        try:
            with open(self.outbox_path, "r", encoding="utf-8") as of:
                records = json.load(of)
        except Exception:
            records = []
        if not isinstance(records, list):
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            delivery_id = record.get("delivery_id")
            payload = record.get("payload")
            url = record.get("url")
            if not delivery_id or not isinstance(payload, dict) or not url:
                continue
            self._outbox[delivery_id] = record
            self._queue.put(record)

    def _persist_outbox(self):
        _atomic_write(
            self.outbox_path,
            json.dumps(list(self._outbox.values()), ensure_ascii=False),
        )

    ## Public API

    def enqueue(self, payload: dict) -> bool:
        config = get_webhook_config(self.config_path)
        if not config["enabled"] or not is_valid_target(config["url"]):
            return False
        record = {
            "delivery_id": payload["delivery_id"],
            "event": payload["event"],
            "url": config["url"],
            "payload": payload,
            "attempts": 0,
        }
        with self._lock:
            while len(self._outbox) >= self.capacity:
                dropped_id, dropped = self._outbox.popitem(last=False)
                self.log(
                    f"Webhook outbox full, dropped {dropped.get('event')} "
                    f"{dropped_id}"
                )
            self._outbox[record["delivery_id"]] = record
            self._persist_outbox()
        self._queue.put(record)
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._outbox)

    def stop(self):
        self._stop.set()
        self._queue.put(None)
        self._thread.join(timeout=5)

    ## Worker

    def _run(self):
        while not self._stop.is_set():
            try:
                record = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if record is None:
                break
            delivery_id = record.get("delivery_id")
            with self._lock:
                current = self._outbox.get(delivery_id)
            if not current:
                continue
            ok, status, error, interrupted = self._deliver(current)
            if interrupted:
                # Leave the record in the Outbox so it is delivered after the
                # next service start instead of being reported as failed.
                break
            with self._lock:
                self._outbox.pop(delivery_id, None)
                self._persist_outbox()
            self._write_status(current, ok, status, error)
            if ok:
                self.log(
                    f"Webhook delivered: {current.get('event')} "
                    f"({delivery_id})"
                )
            else:
                self.log(
                    f"Webhook failed: {current.get('event')} "
                    f"({delivery_id}) after "
                    f"{current.get('attempts', 0)} attempt(s): {error}"
                )

    def _deliver(self, record: dict) -> tuple:
        """Attempt delivery. Returns (ok, status, error, interrupted)."""
        status = None
        error = None
        attempts_done = int(record.get("attempts") or 0)
        if attempts_done >= self.max_attempts:
            return False, None, "retry limit reached", False
        for attempt in range(attempts_done + 1, self.max_attempts + 1):
            if self._stop.is_set():
                return False, status, "dispatcher stopped", True
            config = get_webhook_config(self.config_path)
            if not config["enabled"]:
                return False, status, "webhook disabled", False
            record["attempts"] = attempt
            with self._lock:
                self._persist_outbox()
            body = encode_payload(record["payload"])
            headers = build_headers(record["payload"], body, config["secret"])
            ok, status, error = post_json(
                record["url"], body, headers, self.timeout
            )
            if ok:
                return True, status, None, False
            if self._stop.is_set():
                return False, status, "dispatcher stopped", True
            if attempt < self.max_attempts:
                wait_for = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                if self._stop.wait(wait_for):
                    return False, status, "dispatcher stopped", True
        return False, status, error, False

    def _write_status(self, record: dict, ok: bool, status, error):
        payload = {
            "ok": bool(ok),
            "event": record.get("event"),
            "delivery_id": record.get("delivery_id"),
            "http_status": status,
            "attempts": record.get("attempts", 0),
            "error": error,
            "at": iso_now(),
        }
        try:
            _atomic_write(self.status_path, json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            self.log(f"Webhook status write failed: {exc}")

## Test delivery (Settings "Send test" runs this in the UI process)

def send_test_webhook(config: dict, timeout: float = DEFAULT_TIMEOUT) -> tuple:
    payload = build_test_payload()
    body = encode_payload(payload)
    headers = build_headers(payload, body, config.get("secret", ""))
    return post_json(config["url"], body, headers, timeout)
