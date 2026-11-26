""" Persisted Settings for OpenSMS.

This module is deliberately free of Kivy, jnius and Android imports so the
Settings contract can be tested on any platform. It takes an explicit path and
holds no module state.
"""

import json
import os
import tempfile

from services.config import app_name
from services.webhookSvc import (
    DEFAULT_BACKOFF,
    DEFAULT_CAPACITY,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT,
)

DEFAULT_PORT = 9292
DEFAULT_BOOT = False

## Defaults

def default_webhook_settings() -> dict:
    return {
        "enabled": False,
        "url": "",
        "secret": "",
        "timeout": DEFAULT_TIMEOUT,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "backoff": list(DEFAULT_BACKOFF),
        "capacity": DEFAULT_CAPACITY,
    }

def default_settings() -> dict:
    return {
        "app": app_name,
        "port": DEFAULT_PORT,
        "boot": DEFAULT_BOOT,
        "permissions": False,
        "revision": 0,
        "webhook": default_webhook_settings(),
    }

## Load

def load_settings(config_path: str) -> dict:
    """Load Settings, filling missing fields from the defaults.

    Unknown fields, at the top level and inside the Webhook object, are kept so
    a later write can preserve them.
    """
    settings = default_settings()
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except Exception:
        return settings
    if not isinstance(raw, dict):
        return settings
    for key, value in raw.items():
        if key == "webhook":
            continue
        settings[key] = value
    raw_webhook = raw.get("webhook")
    if isinstance(raw_webhook, dict):
        settings["webhook"].update(raw_webhook)
    return settings

def get_revision(settings: dict) -> int:
    try:
        return int(settings.get("revision") or 0)
    except (TypeError, ValueError):
        return 0

## API shape

def writable_settings(settings: dict) -> dict:
    """Return only the writable subset, with the secret masked."""
    webhook = settings.get("webhook")
    if not isinstance(webhook, dict):
        webhook = {}
    backoff = webhook.get("backoff")
    if not isinstance(backoff, list):
        backoff = list(DEFAULT_BACKOFF)
    return {
        "port": settings.get("port", DEFAULT_PORT),
        "boot": settings.get("boot", DEFAULT_BOOT),
        "webhook": {
            "enabled": webhook.get("enabled", False),
            "url": webhook.get("url", ""),
            "secret_set": bool(webhook.get("secret")),
            "timeout": webhook.get("timeout", DEFAULT_TIMEOUT),
            "max_attempts": webhook.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            "backoff": list(backoff),
            "capacity": webhook.get("capacity", DEFAULT_CAPACITY),
        },
    }

## Writes

def atomic_write_json(path: str, data):
    """Write JSON via a temporary file in the same directory, then replace."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    file_descriptor, tmp_path = tempfile.mkstemp(
        prefix="config.",
        suffix=".tmp",
        dir=directory or ".",
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
