""" Persisted Settings for OpenSMS.

This module is deliberately free of Kivy, jnius and Android imports so the
Settings contract can be tested on any platform. It takes an explicit path and
holds only a process-local write lock, never Settings themselves.
"""

import json
import os
import tempfile
import threading

from services.config import app_name
from services.webhookSvc import (
    BACKOFF_MAX_COUNT,
    BACKOFF_MAX_SECONDS,
    BACKOFF_MIN_COUNT,
    CAPACITY_MAX,
    CAPACITY_MIN,
    DEFAULT_BACKOFF,
    DEFAULT_CAPACITY,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT,
    MAX_ATTEMPTS_MAX,
    MAX_ATTEMPTS_MIN,
    TIMEOUT_MAX,
    TIMEOUT_MIN,
    has_target_credentials,
    is_valid_target,
    normalize_webhook_config,
)

DEFAULT_PORT = 9292
DEFAULT_BOOT = False

# Port range from the API contract in docs/remote-api.md. The Webhook retry
# ranges live with the delivery defaults in webhookSvc.
PORT_MIN = 1025
PORT_MAX = 65535
SECRET_MAX_LENGTH = 256

TOP_LEVEL_FIELDS = {"port", "boot", "webhook", "revision"}
REQUIRED_TOP_LEVEL_FIELDS = ("port", "boot", "webhook")
WRITABLE_WEBHOOK_FIELDS = (
    "enabled",
    "url",
    "timeout",
    "max_attempts",
    "backoff",
    "capacity",
)
WEBHOOK_FIELDS = set(WRITABLE_WEBHOOK_FIELDS) | {"secret"}


class SettingsValidationError(ValueError):
    """A malformed or out-of-range write (HTTP 422)."""


class SettingsRuleError(SettingsValidationError):
    """A schema-valid write that breaks a business rule (HTTP 400)."""


class SettingsRevisionConflict(Exception):
    """The write expected a Revision the stored Settings no longer have."""

    def __init__(self, expected: int, current: int):
        self.expected = expected
        self.current = current
        self.detail = (
            f"Revision conflict: expected {expected}, current revision is {current}"
        )
        super().__init__(self.detail)

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

## Validation

def _is_int(value) -> bool:
    """JSON integers only: `true` is not a port, and `1.0` is not an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_valid_port(value) -> bool:
    """Whether `value` is a JSON integer inside the API port range."""
    return _is_int(value) and PORT_MIN <= value <= PORT_MAX


def _unknown_field_error(prefix: str, unknown) -> SettingsValidationError:
    names = ", ".join(sorted(unknown))
    return SettingsValidationError(f"Unknown field(s) in {prefix}: {names}")


def validate_settings_payload(payload) -> dict:
    """Validate a PUT body and return the normalized writable subset.

    Only schema and range checks happen here, so a malformed field is reported
    before a stale Revision. Business rules live in `validate_business_rules`.
    """
    if not isinstance(payload, dict):
        raise SettingsValidationError("Request body must be a JSON object")

    unknown = set(payload) - TOP_LEVEL_FIELDS
    if unknown:
        raise _unknown_field_error("request", unknown)

    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in payload:
            raise SettingsValidationError(f"Missing required field: {field}")

    revision = payload.get("revision")
    if "revision" in payload and not _is_int(revision):
        raise SettingsValidationError("revision must be an integer")

    port = payload["port"]
    if not is_valid_port(port):
        raise SettingsValidationError(
            f"port must be an integer between {PORT_MIN} and {PORT_MAX}"
        )

    boot = payload["boot"]
    if not isinstance(boot, bool):
        raise SettingsValidationError("boot must be a boolean")

    webhook = payload["webhook"]
    if not isinstance(webhook, dict):
        raise SettingsValidationError("webhook must be an object")

    unknown = set(webhook) - WEBHOOK_FIELDS
    if unknown:
        raise _unknown_field_error("webhook", unknown)

    for field in WRITABLE_WEBHOOK_FIELDS:
        if field not in webhook:
            raise SettingsValidationError(
                f"Missing required webhook field: {field}"
            )

    enabled = webhook["enabled"]
    if not isinstance(enabled, bool):
        raise SettingsValidationError("webhook.enabled must be a boolean")

    url = webhook["url"]
    if not isinstance(url, str):
        raise SettingsValidationError("webhook.url must be a string")

    normalized_webhook = {
        "enabled": enabled,
        "url": url,
        "timeout": _validate_int_range(
            webhook["timeout"],
            "webhook.timeout",
            TIMEOUT_MIN,
            TIMEOUT_MAX,
        ),
        "max_attempts": _validate_int_range(
            webhook["max_attempts"],
            "webhook.max_attempts",
            MAX_ATTEMPTS_MIN,
            MAX_ATTEMPTS_MAX,
        ),
        "backoff": _validate_backoff(webhook["backoff"]),
        "capacity": _validate_int_range(
            webhook["capacity"],
            "webhook.capacity",
            CAPACITY_MIN,
            CAPACITY_MAX,
        ),
    }

    if "secret" in webhook:
        secret = webhook["secret"]
        if not isinstance(secret, str):
            raise SettingsValidationError("webhook.secret must be a string")
        if len(secret) > SECRET_MAX_LENGTH:
            raise SettingsValidationError(
                f"webhook.secret must be at most {SECRET_MAX_LENGTH} characters"
            )
        normalized_webhook["secret"] = secret

    normalized = {
        "port": port,
        "boot": boot,
        "webhook": normalized_webhook,
    }
    if "revision" in payload:
        normalized["revision"] = revision
    return normalized


def _validate_int_range(value, name: str, low: int, high: int) -> int:
    if not _is_int(value) or not low <= value <= high:
        raise SettingsValidationError(
            f"{name} must be an integer between {low} and {high}"
        )
    return value


def _validate_backoff(value) -> list:
    if not isinstance(value, list) or not (
        BACKOFF_MIN_COUNT <= len(value) <= BACKOFF_MAX_COUNT
    ):
        raise SettingsValidationError(
            "webhook.backoff must be a list of "
            f"{BACKOFF_MIN_COUNT}-{BACKOFF_MAX_COUNT} integers"
        )
    normalized = []
    for index, wait_for in enumerate(value):
        if (
            not _is_int(wait_for)
            or wait_for < 1
            or wait_for > BACKOFF_MAX_SECONDS
        ):
            raise SettingsValidationError(
                f"webhook.backoff[{index}] must be an integer between 1 and "
                f"{BACKOFF_MAX_SECONDS}"
            )
        normalized.append(wait_for)
    return normalized


def validate_business_rules(writable: dict):
    """Business rules that are not schema errors (HTTP 400)."""
    webhook = writable["webhook"]
    if has_target_credentials(webhook["url"]):
        raise SettingsRuleError("webhook.url must not contain credentials")
    if webhook["enabled"] and not is_valid_target(webhook["url"]):
        raise SettingsRuleError(
            "webhook.url must be a valid http(s) URL when webhook.enabled is true"
        )


def validate_merged_settings(settings: dict):
    """Re-validate a merged Settings object before persisting it.

    The stored secret is type-checked but not length-checked: the 256-character
    cap belongs to a secret being written, so an old over-long value must not
    block a write that leaves the secret alone.
    """
    webhook = settings.get("webhook")
    if not isinstance(webhook, dict):
        raise SettingsValidationError("webhook must be an object")
    if not isinstance(settings.get("permissions", False), bool):
        raise SettingsValidationError("permissions must be a boolean")
    if not isinstance(webhook.get("secret", ""), str):
        raise SettingsValidationError("webhook.secret must be a string")
    validate_settings_payload({
        "port": settings.get("port"),
        "boot": settings.get("boot"),
        "webhook": {
            field: webhook.get(field)
            for field in WRITABLE_WEBHOOK_FIELDS
        },
    })

## API shape

def writable_settings(settings: dict) -> dict:
    """Return only the writable subset, with the secret masked.

    Retry values are normalized the same way the dispatcher reads them, so an
    invalid value stored by hand is reported as the constant actually in use.
    """
    webhook = settings.get("webhook")
    if not isinstance(webhook, dict):
        webhook = {}
    retry = normalize_webhook_config(webhook)
    return {
        "port": settings.get("port", DEFAULT_PORT),
        "boot": settings.get("boot", DEFAULT_BOOT),
        "webhook": {
            "enabled": webhook.get("enabled", False),
            "url": webhook.get("url", ""),
            "secret_set": bool(webhook.get("secret")),
            "timeout": retry["timeout"],
            "max_attempts": retry["max_attempts"],
            "backoff": list(retry["backoff"]),
            "capacity": retry["capacity"],
        },
    }

## Writes

# Serialises read-modify-write inside this process (the API and the UI are
# separate processes; that cross-process window is accepted and documented).
_write_lock = threading.Lock()


def apply_writable_settings(settings: dict, writable: dict) -> dict:
    """Replace the writable subset, preserving internal and unknown fields.

    The secret follows sentinel semantics: absent from `writable` keeps the
    stored value, so this function only writes it when the payload carried it.
    """
    updated = dict(settings)
    updated["port"] = writable["port"]
    updated["boot"] = writable["boot"]
    existing_webhook = updated.get("webhook")
    webhook = dict(existing_webhook) if isinstance(existing_webhook, dict) else {}
    incoming = writable["webhook"]
    for field in WRITABLE_WEBHOOK_FIELDS:
        webhook[field] = incoming[field]
    webhook["backoff"] = list(incoming["backoff"])
    if "secret" in incoming:
        webhook["secret"] = incoming["secret"]
    updated["webhook"] = webhook
    return updated


def merge_local_settings(current: dict, form: dict) -> dict:
    """Overlay the fields the on-device form owns on freshly read Settings.

    Fields the form does not own keep the value that was just read: the
    Webhook retry settings, the secret when the form left it untouched, and
    every internal or unknown on-disk field. This is what lets a change made
    over the network survive a later local save.
    """
    if not isinstance(form, dict):
        raise SettingsValidationError("Request body must be a JSON object")

    unknown = set(form) - TOP_LEVEL_FIELDS
    if unknown:
        raise _unknown_field_error("request", unknown)

    fresh = writable_settings(current)
    merged = {
        "port": fresh["port"],
        "boot": fresh["boot"],
        "webhook": {
            field: fresh["webhook"][field]
            for field in WRITABLE_WEBHOOK_FIELDS
        },
    }
    for field in ("port", "boot"):
        if field in form:
            merged[field] = form[field]
    if "revision" in form:
        merged["revision"] = form["revision"]
    if "webhook" in form:
        form_webhook = form["webhook"]
        if not isinstance(form_webhook, dict):
            raise SettingsValidationError("webhook must be an object")
        unknown = set(form_webhook) - WEBHOOK_FIELDS
        if unknown:
            raise _unknown_field_error("webhook", unknown)
        merged["webhook"].update(form_webhook)
    return merged


def _store_writable(
    config_path: str,
    writable: dict,
    expected_revision: int | None,
    permissions: bool | None = None,
) -> dict:
    """Validate and persist a normalized writable subset.

    The caller must hold `_write_lock`, so the read-merge-write stays in one
    process-local critical section.
    """
    current = load_settings(config_path)
    if expected_revision is None:
        expected_revision = writable.get("revision")
    if (
        expected_revision is not None
        and expected_revision != get_revision(current)
    ):
        raise SettingsRevisionConflict(
            expected_revision, get_revision(current)
        )
    validate_business_rules(writable)
    merged = apply_writable_settings(current, writable)
    if permissions is not None:
        merged["permissions"] = permissions
    validate_merged_settings(merged)
    merged["revision"] = get_revision(current) + 1
    atomic_write_json(config_path, merged)
    return merged


def save_settings(config_path: str, payload, expected_revision: int | None = None) -> dict:
    """Validated read-merge-write of the writable Settings subset.

    `expected_revision` falls back to the payload's `revision` when omitted.
    Order: schema/range (422) -> Revision (409) -> business rule (400) -> write.
    On any error the persisted object is untouched and no temporary file is
    left behind.
    """
    writable = validate_settings_payload(payload)
    with _write_lock:
        return _store_writable(config_path, writable, expected_revision)


def save_local_settings(
    config_path: str, form: dict, expected_revision: int | None = None
) -> dict:
    """Save on-device fields over a fresh read of Settings.

    `form` carries only the fields the screen owns (port, boot, and some
    Webhook fields), plus internal fields owned by the permission flow.
    Everything else keeps its freshly read value, so a remote change is not
    undone and the Revision is bumped rather than reset. Validation is the
    same one the API uses.
    """
    permissions = None
    if isinstance(form, dict) and "permissions" in form:
        permissions = form["permissions"]
        if not isinstance(permissions, bool):
            raise SettingsValidationError("permissions must be a boolean")
        form = {
            field: value for field, value in form.items()
            if field != "permissions"
        }
    with _write_lock:
        current = load_settings(config_path)
        writable = validate_settings_payload(
            merge_local_settings(current, form)
        )
        return _store_writable(
            config_path, writable, expected_revision, permissions
        )


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
