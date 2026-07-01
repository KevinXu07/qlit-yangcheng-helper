# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— QLIT养成教育助手 .app（onedir）

mitmproxy.app 由 build_macos.sh 在打包后单独 ditto 拷入（保留 framework 签名）。

PyInstaller 6.21 在 macOS arm64 上对内嵌的 .app bundle（Flet.app）签名会失败，
原因是它把 .app 当 Mach-O 处理（binary_to_target_arch + sign_binary），
但 .app 内部有 Frameworks/，codesign 直接签 .app 路径报"bundle format unrecognized"。

Monkey-patch sign_binary：碰到 .app 路径直接跳过（.app 的 Contents/MacOS/* 由
最后一步 codesign --deep 覆盖）。这样 PyInstaller 内部的 sign_binary 不会炸。
"""

import os
from PyInstaller.utils import osx as _osx

import flet_cli.__pyinstaller.config as _flet_hook_config

_qlit_flet_bin_dir = os.environ.get("QLIT_FLET_BIN_DIR")
if _qlit_flet_bin_dir:
    _flet_hook_config.temp_bin_dir = _qlit_flet_bin_dir

QLIT_APP_VERSION = os.environ.get("QLIT_APP_VERSION", "1.0.0")
QLIT_APP_BUILD = os.environ.get("QLIT_APP_BUILD", "1")

_orig_sign_binary = _osx.sign_binary


def _safe_sign_binary(filename, identity=None, entitlements_file=None, deep=False):
    if ".app/Contents/" in filename or filename.endswith(".app") or filename.endswith(".framework"):
        return
    return _orig_sign_binary(filename, identity=identity,
                             entitlements_file=entitlements_file, deep=deep)


_osx.sign_binary = _safe_sign_binary


block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],  # 中文字体（NotoSansSC，解决打包后中文方块）
    hiddenimports=["AppKit", "Foundation"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QLIT养成教育助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QLIT养成教育助手",
)

app = BUNDLE(
    coll,
    name="QLIT养成教育助手.app",
    icon="assets/app.icns",
    bundle_identifier="com.kevinxu.qlit.yangchenghelper",
    info_plist={
        "CFBundleName": "QLIT养成教育助手",
        "CFBundleDisplayName": "QLIT养成教育助手",
        "CFBundleShortVersionString": QLIT_APP_VERSION,
        "CFBundleVersion": QLIT_APP_BUILD,
        "NSHighResolutionCapable": True,
        # 外层宿主 App 从 Dock 隐藏（Flet.app 显式设 LSUIElement=false 来进 Dock）
        "LSUIElement": True,
    },
)
