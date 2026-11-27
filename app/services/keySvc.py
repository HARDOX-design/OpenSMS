"""Named API key persistence.

This module owns the versioned ``keys.json`` store, hashing, migration, scopes,
and key lifecycle. Every operation takes an explicit config directory so the
HTTP server, phone UI, and CLI share one implementation without a hidden path
or import-time filesystem dependency.
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from services.settingsSvc import atomic_write_json

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


SCOPE_SEND = "sms:write"
SCOPE_SETTINGS = "settings:write"

VALID_SCOPES = frozenset({SCOPE_SEND, SCOPE_SETTINGS})
PHONE_KEY_NAME = "opensms"
PHONE_KEY_OWNER = "phone"
PROXY_KEY_OWNER = "proxy"
KEY_OWNER_FIELD = "owner"
KEY_NAME_MIN = 2
KEY_NAME_MAX = 64
KEYS_FILE = "keys.json"
KEY_LOCK_FILE = "keys.json.lock"
_KEY_ID_PATTERN = re.compile(r"^k_[0-9a-f]{8}$")
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
_SCOPE_ERROR = (
    "scopes must be a non-empty subset of: "
    + ", ".join(sorted(VALID_SCOPES))
)
DEFAULT_SCOPES = object()
_key_lock = threading.RLock()


class KeyValidationError(ValueError):
    """A malformed key request (HTTP 422)."""


class KeyConflict(Exception):
    """A key request that conflicts with the stored keys (HTTP 409)."""


def hash_api_key(raw_key: str) -> str:
    """Hash the raw key using SHA-256."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _is_canonical_sha256_hash(value) -> bool:
    return (
        isinstance(value, str)
        and _SHA256_HEX_PATTERN.fullmatch(value) is not None
    )


def _keys_path(config_dir: str) -> str:
    return os.path.join(config_dir, KEYS_FILE)


def _key_lock_path(config_dir: str) -> str:
    return os.path.join(config_dir, KEY_LOCK_FILE)


@contextmanager
def _key_store_lock(config_dir: str):
    """Serialize key-store reads and writes across threads and processes."""
    os.makedirs(config_dir, exist_ok=True)
    with _key_lock:
        with open(_key_lock_path(config_dir), "a+b") as lock_file:
            locked = False
            try:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    locked = True
                elif msvcrt is not None:
                    lock_file.seek(0, os.SEEK_END)
                    if lock_file.tell() == 0:
                        lock_file.write(b"\0")
                        lock_file.flush()
                    lock_file.seek(0)
                    while True:
                        try:
                            msvcrt.locking(
                                lock_file.fileno(),
                                msvcrt.LK_NBLCK,
                                1,
                            )
                            locked = True
                            break
                        except OSError:
                            time.sleep(0.01)
                yield
            finally:
                if locked:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    else:
                        lock_file.seek(0)
                        msvcrt.locking(
                            lock_file.fileno(),
                            msvcrt.LK_UNLCK,
                            1,
                        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_owner_value(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if normalized in {PHONE_KEY_OWNER, PROXY_KEY_OWNER}:
        return normalized
    return None


def _entry_owner(entry: dict) -> str | None:
    """Read the documented persisted ownership marker."""
    return _normalize_owner_value(entry.get(KEY_OWNER_FIELD))


def _is_phone_owned(entry: dict) -> bool:
    """Whether this entry carries an explicit phone ownership marker."""
    return _entry_owner(entry) == PHONE_KEY_OWNER


def _normalize_v2_keys(entries: list) -> tuple[list, bool]:
    """Add the documented proxy marker to unmarked v2 entries."""
    normalized = []
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            changed = True
            continue
        current = dict(entry)
        owner = _entry_owner(current) or PROXY_KEY_OWNER
        if current.get(KEY_OWNER_FIELD) != owner:
            current[KEY_OWNER_FIELD] = owner
            changed = True
        normalized.append(current)
    return normalized, changed


def _migrate_v1_keys(raw: dict) -> list:
    """Convert a version-one {hash: {name, scopes}} store to version two.

    Version-one scopes were stored but never enforced, so migration grants both
    scopes once; otherwise every existing install would lock itself out.
    """
    keys = []
    for key_hash, info in raw.items():
        if not isinstance(info, dict):
            continue
        scopes = info.get("scopes")
        if not isinstance(scopes, list):
            scopes = []
        scopes = {str(scope) for scope in scopes if isinstance(scope, str)}
        scopes.update({SCOPE_SEND, SCOPE_SETTINGS})
        name = str(info.get("name") or PHONE_KEY_NAME).strip()
        keys.append({
            "id": f"k_{secrets.token_hex(4)}",
            "name": name,
            "hash": str(key_hash),
            "scopes": sorted(scopes),
            "created_at": _now_iso(),
            "owner": (
                PHONE_KEY_OWNER
                if name.casefold() == PHONE_KEY_NAME
                else PROXY_KEY_OWNER
            ),
        })
    return keys


def load_key_store(config_dir: str) -> tuple[dict, bool]:
    """Return the key store, migrating a version-one file in memory.

    The second item is True when a migration is pending persistence.
    """
    try:
        with open(_keys_path(config_dir), "r", encoding="utf-8") as keys_file:
            raw = json.load(keys_file)
    except Exception:
        return {"version": 2, "keys": []}, False
    if not isinstance(raw, dict):
        return {"version": 2, "keys": []}, False
    if raw.get("version") == 2 and isinstance(raw.get("keys"), list):
        keys, normalized = _normalize_v2_keys(raw["keys"])
        return {"version": 2, "keys": keys}, normalized
    return {"version": 2, "keys": _migrate_v1_keys(raw)}, True


def save_key_store(config_dir: str, store: dict):
    """Persist a version-two key store atomically."""
    with _key_store_lock(config_dir):
        _save_key_store(config_dir, store)


def _save_key_store(config_dir: str, store: dict):
    """Persist a key store while the caller holds the key-store lock."""
    atomic_write_json(_keys_path(config_dir), store)


def authenticate_key(config_dir: str, raw_key: str) -> dict | None:
    """Return the matching stored entry, persisting a pending migration."""
    hashed = hash_api_key(raw_key)
    with _key_store_lock(config_dir):
        store, migrated = load_key_store(config_dir)
        match = None
        for entry in store["keys"]:
            if not isinstance(entry, dict):
                continue
            stored_hash = entry.get("hash")
            if not _is_canonical_sha256_hash(stored_hash):
                continue
            if secrets.compare_digest(stored_hash, hashed):
                match = entry
        if match is not None and migrated:
            _save_key_store(config_dir, store)
    return match


def has_api_keys(config_dir: str) -> bool:
    with _key_store_lock(config_dir):
        store, _ = load_key_store(config_dir)
    return bool(store["keys"])


def _normalize_key_name(value) -> str:
    if not isinstance(value, str):
        raise KeyValidationError("name must be a string")
    name = value.strip()
    if not KEY_NAME_MIN <= len(name) <= KEY_NAME_MAX:
        raise KeyValidationError(
            f"name must be {KEY_NAME_MIN}-{KEY_NAME_MAX} characters"
        )
    if not name.isprintable():
        raise KeyValidationError("name must not contain control characters")
    if name.casefold() == PHONE_KEY_NAME:
        raise KeyValidationError(
            f"name {PHONE_KEY_NAME} is reserved for the phone key"
        )
    return name


def _normalize_key_scopes(value) -> list:
    if not isinstance(value, list):
        raise KeyValidationError("scopes must be a list")
    scopes = set()
    for scope in value:
        if not isinstance(scope, str) or scope not in VALID_SCOPES:
            raise KeyValidationError(_SCOPE_ERROR)
        scopes.add(scope)
    if not scopes:
        raise KeyValidationError(_SCOPE_ERROR)
    return sorted(scopes)


def _new_key_entry(
    name: str,
    scopes,
    raw_key: str,
    *,
    owner: str = PROXY_KEY_OWNER,
) -> dict:
    return {
        "id": f"k_{secrets.token_hex(4)}",
        "name": name,
        "hash": hash_api_key(raw_key),
        "scopes": sorted(scopes),
        "created_at": _now_iso(),
        "owner": owner,
    }


def _new_raw_key() -> str:
    return f"osms_{secrets.token_urlsafe(32)}"


def public_key(entry: dict) -> dict:
    """The listable shape of a key: never the hash or the raw key."""
    scopes = entry.get("scopes")
    if not isinstance(scopes, list):
        scopes = []
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "scopes": list(scopes),
        "created_at": entry.get("created_at"),
    }


def create_api_key(
    config_dir: str,
    name,
    scopes=DEFAULT_SCOPES,
    *,
    log=None,
) -> dict:
    """Create a named key; the raw key is returned only in this result."""
    name = _normalize_key_name(name)
    normalized_scopes = (
        sorted(VALID_SCOPES)
        if scopes is DEFAULT_SCOPES
        else _normalize_key_scopes(scopes)
    )
    raw_key = _new_raw_key()
    with _key_store_lock(config_dir):
        store, _ = load_key_store(config_dir)
        for entry in store["keys"]:
            existing_name = entry.get("name")
            if (
                isinstance(existing_name, str)
                and existing_name.casefold() == name.casefold()
            ):
                raise KeyConflict(f"An API key named {name} already exists")
        entry = _new_key_entry(name, normalized_scopes, raw_key)
        store["keys"].append(entry)
        _save_key_store(config_dir, store)
    if log is not None:
        log(f"API key created: {name}")
    return {**public_key(entry), "key": raw_key}


def list_api_keys(config_dir: str) -> list:
    """Every key without its hash or raw value."""
    with _key_store_lock(config_dir):
        store, migrated = load_key_store(config_dir)
        if migrated:
            _save_key_store(config_dir, store)
    return [public_key(entry) for entry in store["keys"]]


def delete_api_key(config_dir: str, key_id: str, *, log=None) -> bool:
    """Delete a key by id, returning False when it does not exist.

    Raises KeyConflict when deleting it would leave no key at all.
    """
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
        return False
    removed = None
    with _key_store_lock(config_dir):
        store, _ = load_key_store(config_dir)
        keys = store["keys"]
        index = next(
            (
                position
                for position, entry in enumerate(keys)
                if entry.get("id") == key_id
            ),
            None,
        )
        if index is None:
            return False
        if len(keys) <= 1:
            raise KeyConflict("The last remaining API key cannot be deleted")
        removed = keys.pop(index)
        _save_key_store(config_dir, store)
    if log is not None:
        log(f"API key deleted: {removed.get('name')}")
    return True


def rotate_phone_key(config_dir: str, *, log=None) -> str:
    """Replace only the phone-owned key, leaving proxy-owned keys intact."""
    raw_key = _new_raw_key()
    with _key_store_lock(config_dir):
        store, _ = load_key_store(config_dir)
        store["keys"] = [
            entry for entry in store["keys"]
            if not _is_phone_owned(entry)
        ]
        store["keys"].append(
            _new_key_entry(
                PHONE_KEY_NAME,
                VALID_SCOPES,
                raw_key,
                owner=PHONE_KEY_OWNER,
            )
        )
        _save_key_store(config_dir, store)
    print("Replaced the phone API key")
    if log is not None:
        log("Replaced the phone API key")
    return raw_key
