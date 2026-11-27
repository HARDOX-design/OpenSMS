import asyncio
import time
from threading import Thread, Lock, Timer
from os.path import join
from urllib.parse import urlparse
import json
#from functools import partial
from typing import Annotated

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr
import uvicorn

# local imports
from services.pathMgr import get_os_platform
from services.smsApi import SmsCrossPlatform, clean_and_validate_phone
from services.logSvc import LogService
from services.config import __version__, app_name
from services import netSvc, settingsSvc, controlSvc, webhookSvc, keySvc
from services.keySvc import KeyConflict, KeyValidationError

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

# API key scopes and request sentinel come from the persistence owner.
SCOPE_SEND = keySvc.SCOPE_SEND
SCOPE_SETTINGS = keySvc.SCOPE_SETTINGS
VALID_SCOPES = keySvc.VALID_SCOPES
_DEFAULT_SCOPES = keySvc.DEFAULT_SCOPES

# Test seam: tests point the API at a temporary config directory so they never
# read or write the real app data directory.
_config_dir: str | None = None
_runtime: dict = {}
_last_restart_at: float | None = None
_restart_lock = Lock()
_started_at: float | None = None

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

def _settings_path() -> str:
    return join(get_config_dir(), "config.json")


# Key persistence lives in keySvc. These adapters keep existing server, UI, and
# CLI call sites working while binding them to this module's config-directory
# seam and activity logger.
def authenticate_key(raw_key: str) -> dict | None:
    return keySvc.authenticate_key(get_config_dir(), raw_key)


def has_api_keys() -> bool:
    return keySvc.has_api_keys(get_config_dir())


def create_api_key(name, scopes=_DEFAULT_SCOPES) -> dict:
    return keySvc.create_api_key(
        get_config_dir(), name, scopes, log=log_activities
    )


def list_api_keys() -> list:
    return keySvc.list_api_keys(get_config_dir())


def delete_api_key(key_id: str) -> bool:
    return keySvc.delete_api_key(
        get_config_dir(), key_id, log=log_activities
    )


def rotate_phone_key() -> str:
    return keySvc.rotate_phone_key(get_config_dir(), log=log_activities)


def generate_new_api_key() -> str:
    """Backwards-compatible entry point for rotating the phone key."""
    return rotate_phone_key()


# data formatting for request
class SmsData(BaseModel):
    number: str
    text: str
    #sub_text: str | None = None


BackoffWait = Annotated[
    StrictInt,
    Field(ge=1, le=settingsSvc.BACKOFF_MAX_SECONDS),
]


class WebhookSettingsRequest(BaseModel):
    enabled: StrictBool
    url: StrictStr
    timeout: StrictInt = Field(
        ge=settingsSvc.TIMEOUT_MIN,
        le=settingsSvc.TIMEOUT_MAX,
    )
    max_attempts: StrictInt = Field(
        ge=settingsSvc.MAX_ATTEMPTS_MIN,
        le=settingsSvc.MAX_ATTEMPTS_MAX,
    )
    backoff: list[BackoffWait] = Field(
        min_length=settingsSvc.BACKOFF_MIN_COUNT,
        max_length=settingsSvc.BACKOFF_MAX_COUNT,
        min_items=settingsSvc.BACKOFF_MIN_COUNT,
        max_items=settingsSvc.BACKOFF_MAX_COUNT,
    )
    capacity: StrictInt = Field(
        ge=settingsSvc.CAPACITY_MIN,
        le=settingsSvc.CAPACITY_MAX,
    )
    secret: StrictStr = Field(
        default=None,
        max_length=settingsSvc.SECRET_MAX_LENGTH,
    )

    class Config:
        extra = "forbid"


class SettingsRequest(BaseModel):
    revision: StrictInt = None
    port: StrictInt = Field(
        ge=settingsSvc.PORT_MIN,
        le=settingsSvc.PORT_MAX,
    )
    boot: StrictBool
    webhook: WebhookSettingsRequest

    def __init__(self, **data):
        if "revision" in data and data["revision"] is None:
            raise ValueError("revision must be an integer")
        webhook = data.get("webhook")
        if (
            isinstance(webhook, dict)
            and "secret" in webhook
            and webhook["secret"] is None
        ):
            raise ValueError("webhook.secret must be a string")
        super().__init__(**data)

    class Config:
        extra = "forbid"


class KeyCreateRequest(BaseModel):
    name: StrictStr
    scopes: list[StrictStr] = None

    def __init__(self, **data):
        if "scopes" in data and data["scopes"] is None:
            raise ValueError("scopes must be a list")
        super().__init__(**data)

    class Config:
        extra = "forbid"


# FastAPI app
app = FastAPI()
security = HTTPBearer(auto_error=False)


def _model_dump(model):
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _request_validation_detail(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Request body must be valid JSON"

    error = errors[0]
    error_type = str(error.get("type", ""))
    loc = tuple(error.get("loc", ()))
    field_parts = [
        str(part)
        for part in loc
        if str(part) not in ("body", "__root__")
    ]
    field = ".".join(field_parts)
    message = str(error.get("msg", ""))

    if error_type in {"json_invalid", "value_error.jsondecode"}:
        return "Request body must be valid JSON"

    if "webhook.secret must be a string" in message:
        return "webhook.secret must be a string"
    if "scopes must be a list" in message:
        return "scopes must be a list"
    if "revision must be an integer" in message:
        return "revision must be an integer"

    if error_type in {"extra_forbidden", "value_error.extra"}:
        extra_errors = [
            item
            for item in errors
            if str(item.get("type", "")) in {
                "extra_forbidden",
                "value_error.extra",
            }
        ]
        names = sorted({
            str(item["loc"][-1])
            for item in extra_errors
            if item.get("loc")
        })
        prefix = "webhook" if "webhook" in field_parts else "request"
        return f"Unknown field(s) in {prefix}: {', '.join(names)}"

    if error_type in {"missing", "value_error.missing"}:
        if not field_parts:
            return "Request body must be valid JSON"
        if "webhook" in field_parts:
            return f"Missing required webhook field: {field_parts[-1]}"
        return f"Missing required field: {field_parts[-1]}"

    if not field_parts and error_type in {
        "model_attributes_type",
        "model_type",
        "dict_type",
        "list_type",
        "type_error.dict",
        "type_error.list",
    }:
        return "Request body must be a JSON object"

    if field == "revision":
        return "revision must be an integer"
    if field == "port":
        return (
            f"port must be an integer between "
            f"{settingsSvc.PORT_MIN} and {settingsSvc.PORT_MAX}"
        )
    if field == "boot":
        return "boot must be a boolean"
    if field == "webhook":
        return "webhook must be an object"
    if field == "webhook.enabled":
        return "webhook.enabled must be a boolean"
    if field == "webhook.url":
        return "webhook.url must be a string"
    if field == "webhook.timeout":
        return (
            f"webhook.timeout must be an integer between "
            f"{settingsSvc.TIMEOUT_MIN} and {settingsSvc.TIMEOUT_MAX}"
        )
    if field == "webhook.max_attempts":
        return (
            f"webhook.max_attempts must be an integer between "
            f"{settingsSvc.MAX_ATTEMPTS_MIN} and "
            f"{settingsSvc.MAX_ATTEMPTS_MAX}"
        )
    if field == "webhook.backoff":
        return (
            f"webhook.backoff must be a list of "
            f"{settingsSvc.BACKOFF_MIN_COUNT}-"
            f"{settingsSvc.BACKOFF_MAX_COUNT} integers"
        )
    if field.startswith("webhook.backoff."):
        index = field.rsplit(".", 1)[-1]
        return (
            f"webhook.backoff[{index}] must be an integer between 1 and "
            f"{settingsSvc.BACKOFF_MAX_SECONDS}"
        )
    if field == "webhook.capacity":
        return (
            f"webhook.capacity must be an integer between "
            f"{settingsSvc.CAPACITY_MIN} and {settingsSvc.CAPACITY_MAX}"
        )
    if field == "webhook.secret":
        if error_type in {
            "string_too_long",
            "value_error.any_str.max_length",
        }:
            return (
                f"webhook.secret must be at most "
                f"{settingsSvc.SECRET_MAX_LENGTH} characters"
            )
        return "webhook.secret must be a string"
    if field == "name":
        return "name must be a string"
    if field == "scopes":
        return "scopes must be a list"
    if field.startswith("scopes."):
        return (
            "scopes must be a non-empty subset of: "
            + ", ".join(sorted(keySvc.VALID_SCOPES))
        )

    return message.removeprefix("Value error, ").replace("Value error, ", "")


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    return JSONResponse(
        status_code=422,
        content={"detail": _request_validation_detail(exc)},
    )


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
    return {
        "status": "ok",
        "version": __version__,
        "platform": platform,
        "uptime_s": _status_uptime_s(),
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
        network = (
            provider()
            if provider is not None
            else netSvc.get_network_ips(strict=True)
        )
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


def _webhook_status(
    settings: dict | None, config_dir: str, warnings: list
) -> dict:
    pending = _status_runtime("outbox_pending")
    stored_webhook = settings.get("webhook") if settings is not None else None
    effective_webhook = webhookSvc.normalize_webhook_config(
        stored_webhook if isinstance(stored_webhook, dict) else None
    )
    if settings is None or not isinstance(stored_webhook, dict):
        enabled = url = secret_set = capacity = None
        url_host = None
    else:
        webhook = settingsSvc.writable_settings(settings)["webhook"]
        stored_enabled = webhook["enabled"]
        enabled = stored_enabled if isinstance(stored_enabled, bool) else None
        url = webhook["url"]
        url_host = webhookSvc.target_host(url)
        secret_set = webhook["secret_set"]
        capacity = webhook["capacity"]
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            capacity = None

    # Warnings describe what the dispatcher will do with malformed Settings,
    # even when the persisted value itself is unknown.
    if not effective_webhook["enabled"]:
        warnings.append({
            "code": "webhook_disabled",
            "message": (
                "Webhook forwarding is disabled: new Inbound SMS are "
                "discarded."
            ),
        })
    elif not webhookSvc.is_valid_target(effective_webhook["url"]):
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

    last_delivery = _last_delivery(config_dir)
    if last_delivery and isinstance(last_delivery.get("error"), str):
        last_delivery["error"] = _scrub_webhook_target(
            url, last_delivery["error"]
        )

    return {
        "enabled": enabled,
        "url_host": url_host,
        "secret_set": secret_set,
        "outbox_pending": pending,
        "outbox_capacity": capacity,
        "last_delivery": last_delivery,
    }


@app.get("/api/status")
async def api_status(
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    config_dir = get_config_dir()
    try:
        settings = settingsSvc.load_settings(_settings_path(), strict=True)
    except settingsSvc.SettingsLoadError:
        settings = None
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
        "config": (
            {
                "port": settings.get("port"),
                "boot": settings.get("boot"),
                "revision": settingsSvc.get_revision(settings),
            }
            if settings is not None
            else None
        ),
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
    writable = settingsSvc.writable_settings(settings)
    writable["webhook"]["url"] = webhookSvc.redact_target_credentials(
        writable["webhook"]["url"]
    )
    return {
        "revision": settingsSvc.get_revision(settings),
        "settings": writable,
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
        bound_port = _live_attr("bound_port")
    port = settings.get("port")
    if bound_port is None or port is None:
        return False
    try:
        return int(port) != int(bound_port)
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


async def _request_json(request: Request, *, allow_empty: bool = False):
    """Parse a JSON body, optionally treating no body as an empty object."""
    raw_body = await request.body()
    if allow_empty and not raw_body:
        return {}
    try:
        return json.loads(raw_body)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Request body must be valid JSON",
        )


def _require_json_object(payload):
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail="Request body must be a JSON object",
        )
    return payload


async def _request_json_object(
    request: Request, *, allow_empty: bool = False
) -> dict:
    payload = await _request_json(request, allow_empty=allow_empty)
    return _require_json_object(payload)


def _reject_unknown_fields(payload: dict, allowed_fields):
    unknown = set(payload) - allowed_fields
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown field(s) in request: {', '.join(sorted(unknown))}",
        )


@app.put("/api/settings")
async def api_put_settings(
    payload: SettingsRequest,
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    payload_dict = _model_dump(payload)
    try:
        saved = settingsSvc.save_settings(_settings_path(), payload_dict)
    except settingsSvc.SettingsRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail)
    except settingsSvc.SettingsRuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except settingsSvc.SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    log_activities(_settings_log_message(payload_dict))
    return {
        **_settings_body(saved),
        "restart_required": _restart_required(saved),
    }

@app.post("/api/keys", status_code=201)
async def api_create_key(
    payload: KeyCreateRequest,
    client: dict = Depends(require_scope(SCOPE_SETTINGS))
):
    payload_dict = _model_dump(payload)
    try:
        return create_api_key(
            payload_dict["name"],
            payload_dict.get("scopes", _DEFAULT_SCOPES),
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


def _webhook_test_overrides(payload: dict) -> dict:
    """Validate Webhook test overrides; an absent body means saved Settings."""
    _reject_unknown_fields(payload, WEBHOOK_TEST_FIELDS)
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
    timeout = webhookSvc.normalize_webhook_config(webhook)["timeout"]
    return url, secret, timeout


def _scrub_webhook_target(url: object, error: str) -> str:
    """Remove URL parts from an error so credentials never reach the caller."""
    if not error:
        return error
    if not isinstance(url, str):
        return "Webhook delivery failed."
    error = webhookSvc.scrub_webhook_error(error)
    candidates: set[str | None] = {url, url.strip()}
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
    payload = await _request_json_object(request, allow_empty=True)
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

    if not _status_runtime("restart_consumer", True):
        raise HTTPException(
            status_code=409,
            detail="Restart is not available on this platform",
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
