# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['SPT_WATCHDOG_patched_v11.py'],
    pathex=[],
    binaries=[],
    datas=[('watchdog.ico', '.'), ('watchdog.png', '.')],
    hiddenimports=['pystray', 'PIL'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SPT_WATCHDOG_v1_3',
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
    icon=['watchdog.ico'],
)
