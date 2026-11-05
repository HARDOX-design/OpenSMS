# -*- mode: python ; coding: utf-8 -*-
#from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import os
from importlib.util import find_spec

# 1. Dynamically locate the kivymd installation directory
kivymd_path = os.path.dirname(find_spec("kivymd").origin)

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        (os.path.join(kivymd_path, "data"), "kivymd/data"),
        ('data', 'data'),
        ('screens', 'screens'),
        ('main_layout.kv', '.')
    ],
    hiddenimports=[
        "kivymd.uix.screenmanager",
        "kivymd.uix.appbar",
        "kivymd.uix.floatlayout",
        "kivymd.uix.navigationdrawer",
        "kivymd.icon_definitions",
        "kivymd.uix.divider",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OpenSmsApp',
    icon='data/images/desktop.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
