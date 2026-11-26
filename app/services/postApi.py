import hashlib
import secrets
import asyncio
import time
from datetime import datetime, timezone
from threading import Thread, Lock
from os.path import join
import json
#from functools import partial

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import uvicorn

# local imports
from services.pathMgr import get_os_platform
from services.smsApi import SmsCrossPlatform, clean_and_validate_phone
from services.logSvc import LogService
from services.config import __version__, app_name
from services import settingsSvc

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

# Test seam: tests point the API at a temporary config directory so they never
# read or write the real app data directory.
_config_dir: str | None = None
_runtime: dict = {}
_last_restart_at: float | None = None
_started_at: float | None = None
_key_lock = Lock()

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
            "name": str(info.get("name") or "opensms"),
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

def generate_new_api_key() -> str:
    # Generate a high-entropy secret token
    new_raw_key = f"osms_{secrets.token_urlsafe(32)}"
    save_key_store({
        "version": 2,
        "keys": [{
            "id": f"k_{secrets.token_hex(4)}",
            "name": "opensms",
            "hash": hash_api_key(new_raw_key),
            "scopes": sorted({SCOPE_SEND, SCOPE_SETTINGS}),
            "created_at": _now_iso(),
        }],
    })
    print("Created/updated the API key")
    log_activities("Created/updated the API key")
    return new_raw_key



# data formatting for request
class SmsData(BaseModel):
    number: str
    text: str
    #sub_text: str | None = None

class ControlData(BaseModel):
    """
    This is not yet implemented. Placeholder only.
    """
    api: str

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
    def stop(self):
        if self.server:
            print("Stopping FastAPI...")
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)
        self.server = None
        self.thread = None
        print("FastAPI stopped")

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

@app.post("/control/")
async def internal_control(
    item: ControlData,
    request: Request,
    client: dict = Depends(verify_api_key)
):
    server: PosiApiServer = request.app.state.server
    return item

# test locally
if __name__ == "__main__":
    apiServ = PosiApiServer()
    #apiServ.set_kivy_caller(kivy_func)
    apiServ.start()
