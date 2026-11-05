import os
import sys
from pathlib import Path

def get_os_platform() -> str:
    """
    Get OS platform: android / win / linux / unknown
    """
    kivy_build = os.environ.get('KIVY_BUILD', '')
    if kivy_build in {'android', 'ios'}:
        return kivy_build
    elif 'P4A_BOOTSTRAP' in os.environ:
        return 'android'
    elif 'ANDROID_ARGUMENT' in os.environ:
        return 'android'
    elif sys.platform in ('win32', 'cygwin'):
        return 'win'
    elif sys.platform == 'darwin':
        return 'macosx'
    elif sys.platform.startswith('linux'):
        return 'linux'
    elif sys.platform.startswith('freebsd'):
        return 'linux'
    return 'unknown'

def get_user_data_dir(app_name: str) -> Path:
    """
    Returns a safe, user-specific data directory for Linux, Windows & MAC.
    Creates the directory if it does not already exist.
    """
    platform = get_os_platform()
    home = Path.home()

    if platform == "win":
        # Windows standard: %LOCALAPPDATA% (or fallback to ~/AppData/Local)
        base_dir = os.environ.get("LOCALAPPDATA")
        if base_dir:
            data_dir = Path(base_dir) / app_name
        else:
            data_dir = home / "AppData" / "Local" / app_name

    elif platform == "macosx":
        # macOS standard: ~/Library/Application Support
        data_dir = home / "Library" / "Application Support" / app_name

    else:
        # Linux / Unix standard: XDG_DATA_HOME (or fallback to ~/.local/share)
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            data_dir = Path(xdg_data) / app_name
        else:
            data_dir = home / ".local" / "share" / app_name

    # Safely create the folder with parents
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Error while creating app dir: {e}")

    return data_dir
