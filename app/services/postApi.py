import hashlib
import re
import secrets
import asyncio
import time
from datetime import datetime, timezone
from threading import Thread, Lock, Timer
from os.path import join
from urllib.parse import urlparse
import json
#from functools import partial

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import uvicorn

# local imports
from services.pathMgr import get_os_platform
from services.smsApi import SmsCrossPlatform, clean_and_validate_phone
from services.logSvc import LogService
from services.config import __version__, app_name
from services import netSvc, settingsSvc, controlSvc, webhookSvc

# OS specific
#try:
#    from kivy.utils import platform
#except ImportError:
#    if sys.platform.startswith("win"):
#        platform = "win"
#    elif sys.platform.startswith("linux"):
#        platform = "linux"
#    else:
#        platform = "unknown"

## global vars / obj
platform = get_os_platform()
logger = None
sms_api = SmsCrossPlatform()

# API key scopes
SCOPE_SEND = "sms:write"
SCOPE_SETTINGS = "settings:write"

VALID_SCOPES = frozenset({SCOPE_SEND, SCOPE_SETTINGS})
PHONE_KEY_NAME = "opensms"
KEY_NAME_MIN = 2
KEY_NAME_MAX = 64
_KEY_ID_PATTERN = re.compile(r"^k_[0-9a-f]{8}$")
_SCOPE_ERROR = (
    "scopes must be a non-empty subset of: "
    + ", ".join(sorted(VALID_SCOPES))
)
_DEFAULT_SCOPES = object()


class KeyValidationError(ValueError):
    """A malformed key request (HTTP 422)."""


class KeyConflict(Exception):
    """A key request that conflicts with the stored keys (HTTP 409)."""

# Test seam: tests point the API at a temporary config directory so they never
# read or write the real app data directory.
_config_dir: str | None = None
_runtime: dict = {}
_last_restart_at: float | None = None
_restart_lock = Lock()
_started_at: float | None = None
_key_lock = Lock()

# Restart orchestration. The handler acknowledges before anything stops, and
# the control intent is written only after the response has had time to leave.
RESTART_DELAY_SECONDS = 0.4
RESTART_DEBOUNCE_SECONDS = 5.0

## OS specific setups
# for android
if platform == "android":
    from android.storage import app_storage_path
    from jnius import autoclass
#for other platforms like windows or linux
else:
    from services.serialMgr import SerialConDesktop
    from services.pathMgr import get_user_data_dir
    serial_mgr = SerialConDesktop()

def _default_config_dir() -> str:
    if platform == "android":
        base_path = app_storage_path()
    else:
        base_path = get_user_data_dir(app_name)
    return join(base_path, "config")

def get_config_dir() -> str:
    """Return the config directory, resolving the platform default lazily."""
    if _config_dir:
        return _config_dir
    return _default_config_dir()

def set_config_dir(path: str | None):
    """Point config reads and writes at `path`; None restores the default."""
    global _config_dir
    _config_dir = str(path) if path else None

def set_runtime_providers(providers: dict | None = None):
    """Replace the live runtime value providers (callables keyed by name)."""
    _runtime.clear()
    if providers:
        _runtime.update(providers)

def get_runtime_provider(name: str, default=None):
    provider = _runtime.get(name)
    if provider is None:
        return default
    try:
        return provider()
    except Exception:
        return default

def reset_runtime_state():
    """Test seam: forget runtime providers and restart/uptime bookkeeping."""
    global _last_restart_at, _started_at
    set_runtime_providers({})
    _last_restart_at = None
    _started_at = None

## Functions

def set_serial_port(
    port:str, 
    baud_rate: int | None = 115200,
    timeout: int = 3,
    write_timeout: int = 5
):
    set_ok = False
    network_provider = "none"
    if platform != "android":
        set_ok = serial_mgr.initialize_gsm_module(
            port,
            baud_rate,
            timeout,
            write_timeout
        )
        network_provider = serial_mgr.get_net_operator()
        if set_ok:
            sms_api.set_serial_connection(serial_mgr)
    return set_ok, network_provider

def set_api_logger(log_obj:LogService):
    global logger
    logger = log_obj

def log_activities(message:str):
    if logger:
        Thread(
            target=logger.write_log,
            kwargs={
                "message": message
            },
            daemon=True
        ).start()

def hash_api_key(raw_key: str) -> str:
    """Hash the raw key using SHA-256."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

def _keys_path() -> str:
    return join(get_config_dir(), "keys.json")

def _settings_path() -> str:
    return join(get_config_dir(), "config.json")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        keys.append({
            "id": f"k_{secrets.token_hex(4)}",
            "name": str(info.get("name") or PHONE_KEY_NAME),
            "hash": str(key_hash),
            "scopes": sorted(scopes),
            "created_at": _now_iso(),
        })
    return keys

def load_key_store() -> tuple[dict, bool]:
    """Return the key store, migrating a version-one file in memory.

    The second item is True when a migration is pending persistence.
    """
    try:
        with open(_keys_path(), "r", encoding="utf-8") as keys_file:
            raw = json.load(keys_file)
    except Exception:
        return {"version": 2, "keys": []}, False
    if not isinstance(raw, dict):
        return {"version": 2, "keys": []}, False
    if raw.get("version") == 2 and isinstance(raw.get("keys"), list):
        keys = [entry for entry in raw["keys"] if isinstance(entry, dict)]
        return {"version": 2, "keys": keys}, False
    return {"version": 2, "keys": _migrate_v1_keys(raw)}, True

def save_key_store(store: dict):
    settingsSvc.atomic_write_json(_keys_path(), store)

def authenticate_key(raw_key: str) -> dict | None:
    hashed = hash_api_key(raw_key)
    with _key_lock:
        store, migrated = load_key_store()
        match = None
        for entry in store["keys"]:
            if secrets.compare_digest(entry.get("hash") or "", hashed):
                match = entry
        if match is not None and migrated:
            save_key_store(store)
    return match

def has_api_keys() -> bool:
    store, _ = load_key_store()
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

def _new_key_entry(name: str, scopes, raw_key: str) -> dict:
    return {
        "id": f"k_{secrets.token_hex(4)}",
        "name": name,
        "hash": hash_api_key(raw_key),
        "scopes": sorted(scopes),
        "created_at": _now_iso(),
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

def create_api_key(name, scopes=_DEFAULT_SCOPES) -> dict:
    """Create a named key; the raw key is returned only in this result."""
    name = _normalize_key_name(name)
    normalized_scopes = (
        sorted(VALID_SCOPES)
        if scopes is _DEFAULT_SCOPES
        else _normalize_key_scopes(scopes)
    )
    raw_key = _new_raw_key()
    with _key_lock:
        store, _ = load_key_store()
        for entry in store["keys"]:
            existing_name = entry.get("name")
            if (
                isinstance(existing_name, str)
                and existing_name.casefold() == name.casefold()
            ):
                raise KeyConflict(f"An API key named {name} already exists")
        entry = _new_key_entry(name, normalized_scopes, raw_key)
        store["keys"].append(entry)
        save_key_store(store)
    log_activities(f"API key created: {name}")
    return {**public_key(entry), "key": raw_key}

def list_api_keys() -> list:
    """Every key without its hash or raw value."""
    store, _ = load_key_store()
    return [public_key(entry) for entry in store["keys"]]

def delete_api_key(key_id: str) -> bool:
    """Delete a key by id, returning False when it does not exist.

    Raises KeyConflict when deleting it would leave no key at all.
    """
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
        return False
    with _key_lock:
        store, _ = load_key_store()
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
        save_key_store(store)
    log_activities(f"API key deleted: {removed.get('name')}")
    return True

def rotate_phone_key() -> str:
    """Replace only the phone-owned key, leaving proxy-owned keys intact."""
    raw_key = _new_raw_key()
    with _key_lock:
        store, _ = load_key_store()
        store["keys"] = [
            entry for entry in store["keys"]
            if str(entry.get("name") or "").casefold() != PHONE_KEY_NAME
        ]
        store["keys"].append(
            _new_key_entry(PHONE_KEY_NAME, VALID_SCOPES, raw_key)
        )
        save_key_store(store)
    print("Replaced the phone API key")
    log_activities("Replaced the phone API key")
    return raw_key

def generate_new_api_key() -> str:
    """Backwards-compatible entry point for rotating the phone key."""
    return rotate_phone_key()



# data formatting for request
class SmsData(BaseModel):
    number: str
    text: str
    #sub_text: str | None = None

# FastAPI app
app = FastAPI()
security = HTTPBearer(auto_error=False)

# api server class with callback function
class PosiApiServer:

    def __init__(self, host="0.0.0.0", port=9292):
        self.host = host
        self.port = port
        self.bound_port: int | None = None
        self.kivyCallback = None
        self.server: uvicorn.Server | None = None
        self.thread: Thread | None = None
        self.started_at: float | None = None

    # ----------------------------
    # Control callback
    # ----------------------------
    def set_control_caller(self, callback=None):
        self.controlCallback = callback

    # ----------------------------
    # Kivy callback
    # ----------------------------
    def set_kivy_caller(self, callback=None):
        self.kivyCallback = callback

    # ----------------------------
    # Internal server runner
    # ----------------------------
    def _run(self, port=None):
        final_port = port if port else self.port
        self.port = final_port
        self.bound_port = final_port
        config = uvicorn.Config(
            app,
            host=self.host,
            port=final_port,
            log_level="info",
            loop="asyncio"
        )
        global _started_at
        try:
            self.server = uvicorn.Server(config)
            app.state.server = self  # attach instance
            _started_at = self.started_at = time.time()
            asyncio.run(self.server.serve())
        except OSError as e:
            err_msg = f"Error while port binding {final_port}: {e}"
            print(f"Error while port binding {final_port}: {e}")
            log_activities(f"Server Error: {err_msg}")
            if self.controlCallback:
                self.controlCallback("bind_error", str(e))
        except Exception as e:
            err_msg = f"Error while starting the server: {e}"
            print(f"Server error: {e}")
            log_activities(err_msg)
            if self.controlCallback:
                self.controlCallback("error", str(e))
        finally:
            self.bound_port = None
            self.server = None

    # ----------------------------
    # Public start
    # ----------------------------
    def start(self, port=None):
        if self.thread and self.thread.is_alive():
            print("Server already running")
            return
        self.thread = Thread(
            target=self._run,
            kwargs={
                "port": port
            },
            daemon=True
        )
        self.thread.start()
        print("FastAPI started")

    # ----------------------------
    # Public stop
    # ----------------------------
    def stop(self) -> bool:
        """Stop the server; False when its thread outlived the join timeout.

        Leaving `thread` in place on a timeout keeps `start()` from adding a
        second listener while the first is still shutting down.
        """
        if self.server:
            print("Stopping FastAPI...")
            self.server.should_exit = True
        stopped = True
        if self.thread:
            self.thread.join(timeout=5)
            stopped = not self.thread.is_alive()
        self.server = None
        if stopped:
            self.thread = None
            print("FastAPI stopped")
        else:
            print("FastAPI did not stop within 5 seconds")
        return stopped

# handle post requests

async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if credentials is None:
        raise HTTPException(
            status_code=403,
            detail="Not authenticated",
        )
    client_info = authenticate_key(credentials.credentials)
    if not client_info:
        print(f"Authentication failed from IP: {client_ip}")
        log_activities(f"Authentication failed from IP: {client_ip}")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key",
        )
    return client_info

def require_scope(scope: str):
    async def scope_guard(
        client: dict = Depends(verify_api_key)
    ) -> dict:
        if scope not in (client.get("scopes") or []):
            raise HTTPException(
                status_code=403,
                detail=f"Missing required scope: {scope}",
            )
        return client
    return scope_guard

@app.get("/api/health")
async def api_health(
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    server = getattr(app.state, "server", None)
    started_at = getattr(server, "started_at", None) or _started_at
    uptime_s = max(0, int(time.time() - started_at)) if started_at else 0
    return {
        "status": "ok",
        "version": __version__,
        "platform": platform,
        "uptime_s": uptime_s,
    }

def _status_runtime(name: str, fallback=None):
    """A runtime provider value, or `fallback` when none is registered."""
    if name not in _runtime:
        return fallback
    return get_runtime_provider(name, fallback)


def _live_server():
    return getattr(app.state, "server", None)


def _live_attr(name: str):
    return getattr(_live_server(), name, None)


def _status_uptime_s() -> int:
    started_at = _live_attr("started_at") or _started_at
    if not started_at:
        return 0
    return max(0, int(time.time() - started_at))


def _service_status() -> dict:
    """Read the service transition log, reporting unknown fields honestly."""
    try:
        with open(
            join(get_config_dir(), "resp.json"), "r", encoding="utf-8"
        ) as response_file:
            resp = json.load(response_file)
    except Exception:
        resp = None
    if not isinstance(resp, dict):
        resp = {}
    return {
        field: (
            resp.get(field)
            if isinstance(resp.get(field), str) and resp.get(field)
            else "unknown"
        )
        for field in ("server", "serial", "provider")
    }


def _network_status(warnings: list) -> dict:
    """Return the network interface lists, or empty lists on any failure."""
    try:
        provider = _runtime.get("network_ips")
        network = provider() if provider is not None else netSvc.get_network_ips()
    except Exception:
        network = None
    if not isinstance(network, dict):
        warnings.append({
            "code": "network_unavailable",
            "message": "Network interfaces could not be discovered.",
        })
        return {"wifi": [], "ethernet": []}

    def entries(name: str) -> list:
        values = network.get(name)
        if not isinstance(values, list):
            return []
        return [
            entry for entry in values
            if isinstance(entry, dict)
            and isinstance(entry.get("interface"), str)
            and isinstance(entry.get("ip"), str)
        ]

    return {"wifi": entries("wifi"), "ethernet": entries("ethernet")}


def _last_delivery(config_dir: str) -> dict | None:
    status = webhookSvc.read_webhook_status(
        join(config_dir, "webhook_status.json")
    )
    if not status:
        return None
    return {
        field: status.get(field)
        for field in (
            "ok",
            "event",
            "delivery_id",
            "http_status",
            "attempts",
            "error",
            "at",
        )
    }


def _webhook_status(settings: dict, config_dir: str, warnings: list) -> dict:
    webhook = settingsSvc.writable_settings(settings)["webhook"]
    url = webhook["url"]
    url_host = None
    if url:
        try:
            url_host = urlparse(url).hostname
        except ValueError:
            url_host = None

    pending = _status_runtime("outbox_pending")
    capacity = webhook["capacity"]
    if not isinstance(capacity, int) or isinstance(capacity, bool):
        capacity = None

    if not webhook["enabled"]:
        warnings.append({
            "code": "webhook_disabled",
            "message": (
                "Webhook forwarding is disabled: new Inbound SMS are "
                "discarded."
            ),
        })
    elif not webhookSvc.is_valid_target(url):
        warnings.append({
            "code": "webhook_url_invalid",
            "message": (
                "Webhook forwarding is enabled with an invalid Webhook target."
            ),
        })

    if (
        isinstance(pending, int)
        and not isinstance(pending, bool)
        and isinstance(capacity, int)
        and not isinstance(capacity, bool)
        and capacity > 0
        and pending >= 0.8 * capacity
    ):
        warnings.append({
            "code": "outbox_near_capacity",
            "message": (
                f"The Outbox holds {pending} of {capacity} Webhooks; the "
                "oldest will be dropped when it is full."
            ),
        })

    return {
        "enabled": bool(webhook["enabled"]),
        "url_host": url_host,
        "secret_set": bool(webhook["secret_set"]),
        "outbox_pending": pending,
        "outbox_capacity": capacity,
        "last_delivery": _last_delivery(config_dir),
    }


@app.get("/api/status")
async def api_status(
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    config_dir = get_config_dir()
    settings = settingsSvc.load_settings(_settings_path())
    warnings: list = []

    network = _network_status(warnings)
    webhook = _webhook_status(settings, config_dir, warnings)

    return {
        "version": __version__,
        "api": {
            "running": bool(_status_runtime("running", True)),
            "port": _status_runtime("port", _live_attr("port")),
            "bound_port": _status_runtime(
                "bound_port", _live_attr("bound_port")
            ),
            "uptime_s": _status_uptime_s(),
        },
        "service": _service_status(),
        "config": {
            "port": settings.get("port"),
            "boot": settings.get("boot"),
            "revision": settingsSvc.get_revision(settings),
        },
        "network": network,
        "webhook": webhook,
        "warnings": warnings,
    }

@app.get("/api/settings")
async def api_get_settings(
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    settings = settingsSvc.load_settings(_settings_path())
    return _settings_body(settings)

def _settings_body(settings: dict) -> dict:
    return {
        "revision": settingsSvc.get_revision(settings),
        "settings": settingsSvc.writable_settings(settings),
    }

def _settings_log_message(payload: dict) -> str:
    """Field names only: values and request bodies must never reach the log."""
    fields = ["port", "boot"]
    webhook = payload.get("webhook")
    if isinstance(webhook, dict):
        fields.extend(f"webhook.{field}" for field in webhook)
    return "Settings updated: " + ", ".join(fields)

def _restart_required(settings: dict) -> bool:
    """True only when the saved port differs from a known live bound port."""
    bound_port = get_runtime_provider("bound_port")
    if bound_port is None:
        return False
    try:
        return int(settings.get("port")) != int(bound_port)
    except (TypeError, ValueError):
        return False

def _write_restart_intent(config_dir: str, port: int):
    global _last_restart_at
    try:
        controlSvc.request_restart(config_dir, port)
    except Exception as exc:
        # The 202 has already promised nothing is written yet; let a retry
        # through instead of debouncing a Restart that never happened.
        with _restart_lock:
            _last_restart_at = None
        print(f"Could not write the Restart intent: {exc}")
        log_activities(f"Could not write the Restart intent: {exc}")


def _schedule_restart(config_dir: str, port: int):
    """Write the control intent after the 202 has had time to reach the client."""
    timer = Timer(
        RESTART_DELAY_SECONDS,
        _write_restart_intent,
        args=(config_dir, port),
    )
    timer.daemon = True
    timer.start()


@app.put("/api/settings")
async def api_put_settings(
    request: Request,
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Request body must be valid JSON",
        )
    try:
        saved = settingsSvc.save_settings(_settings_path(), payload)
    except settingsSvc.SettingsRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail)
    except settingsSvc.SettingsRuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except settingsSvc.SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    log_activities(_settings_log_message(payload))
    return {
        **_settings_body(saved),
        "restart_required": _restart_required(saved),
    }

KEY_REQUEST_FIELDS = {"name", "scopes"}


async def _key_request_body(request: Request) -> dict:
    """Validate the shape of a key-creation body, before touching the store."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Request body must be valid JSON",
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail="Request body must be a JSON object",
        )
    unknown = set(payload) - KEY_REQUEST_FIELDS
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown field(s) in request: {', '.join(sorted(unknown))}",
        )
    if "name" not in payload:
        raise HTTPException(
            status_code=422,
            detail="Missing required field: name",
        )
    return payload


@app.post("/api/keys", status_code=201)
async def api_create_key(
    request: Request,
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    payload = await _key_request_body(request)
    try:
        return create_api_key(
            payload["name"], payload.get("scopes", _DEFAULT_SCOPES)
        )
    except KeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except KeyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/keys")
async def api_list_keys(
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    return {"keys": list_api_keys()}


@app.delete("/api/keys/{key_id}", status_code=204)
async def api_delete_key(
    key_id: str,
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    try:
        deleted = delete_api_key(key_id)
    except KeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="API key not found")

WEBHOOK_TEST_FIELDS = {"url", "secret"}


def _webhook_test_overrides(payload) -> dict:
    """Validate a Webhook test body; an absent body means "use saved Settings"."""
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail="Request body must be a JSON object",
        )
    unknown = set(payload) - WEBHOOK_TEST_FIELDS
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown field(s) in request: {', '.join(sorted(unknown))}",
        )
    overrides = {}
    if "url" in payload:
        if not isinstance(payload["url"], str):
            raise HTTPException(
                status_code=422, detail="url must be a string"
            )
        overrides["url"] = payload["url"]
    if "secret" in payload:
        secret = payload["secret"]
        if not isinstance(secret, str):
            raise HTTPException(
                status_code=422, detail="secret must be a string"
            )
        if len(secret) > settingsSvc.SECRET_MAX_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=(
                    "secret must be at most "
                    f"{settingsSvc.SECRET_MAX_LENGTH} characters"
                ),
            )
        overrides["secret"] = secret
    return overrides


def _resolve_webhook_test_config(settings: dict, overrides: dict) -> tuple:
    """Resolve the target, secret and timeout for one test delivery.

    Overrides apply to this test only; every absent field comes from the saved
    Settings. An empty secret means unsigned delivery, whether saved or passed.
    """
    webhook = settings.get("webhook")
    if not isinstance(webhook, dict):
        webhook = {}
    url = overrides.get("url", webhook.get("url", ""))
    if not webhookSvc.is_valid_target(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "A valid http(s) Webhook target is required to send a test"
            ),
        )
    secret = webhook.get("secret")
    if not isinstance(secret, str):
        secret = ""
    secret = overrides.get("secret", secret)
    try:
        timeout = float(webhook.get("timeout", webhookSvc.DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = webhookSvc.DEFAULT_TIMEOUT
    return url, secret, timeout


def _scrub_webhook_target(url: str, error: str) -> str:
    """Remove URL parts from an error so credentials never reach the caller."""
    if not error:
        return error
    candidates = {url, url.strip()}
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if parsed is not None:
        candidates.update({
            parsed.username,
            parsed.password,
            parsed.path if parsed.path not in ("", "/") else None,
            parsed.query or None,
            parsed.fragment or None,
        })
    for sensitive in candidates:
        if sensitive:
            error = error.replace(sensitive, "<webhook target>")
    return error


@app.post("/api/webhook/test")
async def api_webhook_test(
    request: Request,
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    raw_body = await request.body()
    payload = {}
    if raw_body:
        try:
            payload = json.loads(raw_body)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Request body must be valid JSON",
            )
    overrides = _webhook_test_overrides(payload)
    settings = settingsSvc.load_settings(_settings_path())
    url, secret, timeout = _resolve_webhook_test_config(settings, overrides)

    # The delivery is blocking; keep the event loop free while it runs.
    ok, status, error = await run_in_threadpool(
        webhookSvc.send_test_webhook,
        {"url": url, "secret": secret},
        timeout=timeout,
    )
    if ok:
        log_activities(f"Webhook test delivered (HTTP {status})")
        return {"ok": True, "http_status": status, "error": None}
    if error:
        error = _scrub_webhook_target(url, error)
    if status is None:
        log_activities("Webhook test failed")
    else:
        log_activities(f"Webhook test failed (HTTP {status})")
    return JSONResponse(
        status_code=502,
        content={"ok": False, "http_status": status, "error": error},
    )


@app.post("/api/server/restart", status_code=202)
async def api_server_restart(
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    """Acknowledge a Restart, then hand the intent to the service loop.

    The handler never stops its own server: stopping uvicorn from inside a
    request risks answering into a dead socket.
    """
    global _last_restart_at

    if not _status_runtime("running", True):
        raise HTTPException(
            status_code=409,
            detail="The API server is not running",
        )

    now = time.monotonic()
    with _restart_lock:
        if (
            _last_restart_at is not None
            and now - _last_restart_at < RESTART_DEBOUNCE_SECONDS
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A Restart was requested less than "
                    f"{RESTART_DEBOUNCE_SECONDS:g} seconds ago"
                ),
            )
        _last_restart_at = now

    config_dir = get_config_dir()
    port = controlSvc.effective_port(config_dir)
    _schedule_restart(config_dir, port)
    log_activities(f"Restart requested on port {port}")
    return {"status": "restarting", "port": port}


@app.post("/sms/")
async def process_sms_req(
    item: SmsData,
    request: Request,
    client: dict = Depends(require_scope(SCOPE_SEND))
):
    server: PosiApiServer = request.app.state.server

    client_ip = request.client.host if request.client else "unknown"
    print(f"Authenticated request from: {client_ip}")
    log_activities(f"Authenticated request from: {client_ip}")

    valid_phone = clean_and_validate_phone(item.number)
    if not valid_phone:
        raise HTTPException(
            status_code=400,
            detail="Invalid phone number",
        )

    stat = sms_api.send_sms(valid_phone, item.text)
    if stat:
        print(f"SMS dispatched to: {valid_phone}")
        log_activities(f"SMS dispatched to: {valid_phone}")
    else:
        print(f"SMS failed to: {valid_phone}")
        log_activities(f"SMS failed to: {valid_phone}")

    if server.kivyCallback:
        Thread(
            target=server.kivyCallback,
            kwargs={
                "stat": stat
            },
            daemon=True
        ).start()

    return {"stat": stat}

# test locally
if __name__ == "__main__":
    apiServ = PosiApiServer()
    #apiServ.set_kivy_caller(kivy_func)
    apiServ.start()
