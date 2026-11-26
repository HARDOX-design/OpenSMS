""" The control file: intents from the API and UI to the service loop.

The persisted Settings remain the single source of truth for the port. This
module only carries intents (start, stop, restart) and is deliberately free of
Kivy, jnius and Android imports, so Restart handling can be tested anywhere.
"""

import json
import os
import secrets

from services import settingsSvc

CONTROL_FILE = "request.json"
SETTINGS_FILE = "config.json"
INTENT_KEY = "server"
INTENT_START = "start"
INTENT_STOP = "stop"
INTENT_RESTART = "restart"
RESTART_ID_KEY = "restart_id"

## Read and write

def _control_path(config_dir: str) -> str:
    return os.path.join(config_dir, CONTROL_FILE)

def read_control(config_dir: str) -> dict:
    """Read the control file, treating a missing or malformed file as empty."""
    try:
        with open(_control_path(config_dir), "r", encoding="utf-8") as control_file:
            control = json.load(control_file)
    except Exception:
        return {}
    return control if isinstance(control, dict) else {}

def write_control(config_dir: str, control: dict):
    """Write the control file atomically; the service polls every 0.2 s."""
    settingsSvc.atomic_write_json(_control_path(config_dir), control)

## Intents

def intent(config_dir: str) -> str:
    """Return the requested server intent, or "" when none is usable."""
    value = read_control(config_dir).get(INTENT_KEY)
    return value if isinstance(value, str) else ""

## Effective port

def _settings_port(config_dir: str) -> int | None:
    """Read the port from Settings, or None when the file is unusable.

    Settings are read raw rather than through `load_settings` so an unreadable
    file is distinguishable from defaults; otherwise the control-file fallback
    could never apply.
    """
    try:
        with open(
            os.path.join(config_dir, SETTINGS_FILE), "r", encoding="utf-8"
        ) as settings_file:
            raw = json.load(settings_file)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    port = raw.get("port")
    return port if settingsSvc.is_valid_port(port) else None

def effective_port(config_dir: str) -> int:
    """Resolve the port to start on: Settings first, the control file second."""
    port = _settings_port(config_dir)
    if port is not None:
        return port
    port = read_control(config_dir).get("port")
    if settingsSvc.is_valid_port(port):
        return port
    return settingsSvc.DEFAULT_PORT

def request_restart(config_dir: str, port: int) -> dict:
    """Record a Restart without disturbing serial or unknown control keys."""
    control = read_control(config_dir)
    control["port"] = port
    control[INTENT_KEY] = INTENT_RESTART
    control[RESTART_ID_KEY] = secrets.token_hex(8)
    write_control(config_dir, control)
    return control

def restart_pending(config_dir: str) -> bool:
    return intent(config_dir) == INTENT_RESTART

def matches_restart(control: dict, restart_id) -> bool:
    """Whether `control` is the same Restart request as `restart_id`.

    The service uses this to suppress a stale intent that could not be
    normalised, while still accepting a later Restart with a new id.
    """
    if control.get(INTENT_KEY) != INTENT_RESTART:
        return False
    if restart_id is None:
        # A pre-upgrade intent has no id; treat it as the same stale request.
        return RESTART_ID_KEY not in control
    return control.get(RESTART_ID_KEY) == restart_id

def normalise_restart(config_dir: str, port: int | None = None) -> bool:
    """Turn a handled Restart back into a plain start, preserving other keys.

    Returns True only when a Restart was normalised, so a polling loop can
    handle each request exactly once.
    """
    control = read_control(config_dir)
    if control.get(INTENT_KEY) != INTENT_RESTART:
        return False
    control[INTENT_KEY] = INTENT_START
    control["port"] = effective_port(config_dir) if port is None else port
    write_control(config_dir, control)
    return True
