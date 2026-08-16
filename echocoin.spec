# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Echocoin node.
# Build: make linux  or  make windows

import glob, os, sys
from PyInstaller.utils.hooks import collect_all

nacl_datas,       nacl_binaries,       nacl_hiddenimports       = collect_all("nacl")
cffi_datas,       cffi_binaries,       cffi_hiddenimports       = collect_all("cffi")
oqs_datas,        oqs_binaries,        oqs_hiddenimports        = collect_all("oqs")
chiavdf_datas,    chiavdf_binaries,    chiavdf_hiddenimports     = collect_all("chiavdf")

# oqs-python loads liboqs via ctypes at module import time. PyInstaller cannot
# detect ctypes dependencies through static analysis, so we find and bundle
# liboqs.so* explicitly. They must land at the root of _MEIPASS ("." dest)
# because hook_oqs.py sets OQS_INSTALL_PATH=_MEIPASS and oqs looks for
# $OQS_INSTALL_PATH/lib/liboqs.so -- so dest must be "lib".
_search_roots = [
    "/usr/local/lib",
    "/usr/lib",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    os.path.join(os.path.dirname(sys.executable), "..", "lib"),
]
_liboqs_bins = []
for _root in _search_roots:
    for _pat in ("liboqs.so", "liboqs.so.*", "liboqs.*.dylib", "liboqs.dll", "oqs.dll"):
        for _p in glob.glob(os.path.join(_root, _pat)):
            if os.path.isfile(_p) and not os.path.islink(_p):
                _liboqs_bins.append((_p, "lib"))
                break

# Bundle MSVC runtime DLLs on Windows (no-op on Linux).
_py_dir = os.path.dirname(sys.executable)
_msvc_dlls = [
    (p, ".")
    for pattern in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
    for p in glob.glob(os.path.join(_py_dir, pattern))
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[
        *nacl_binaries,
        *cffi_binaries,
        *oqs_binaries,
        *chiavdf_binaries,
        *_liboqs_bins,
        *_msvc_dlls,
    ],
    datas=[
        ("bip39_english.txt", "."),
        ("docs/whitepaper.md", "."),
        ("echocoin.svg",      "."),
        ("echocoin.png",      "."),
        ("favicon.ico",       "."),
        *nacl_datas,
        *cffi_datas,
        *oqs_datas,
        *chiavdf_datas,
    ],
    hiddenimports=[
        *nacl_hiddenimports,
        *cffi_hiddenimports,
        *oqs_hiddenimports,
        *chiavdf_hiddenimports,
        "oqs",
        "_cffi_backend",
        "libtorrent",
        "miniupnpc",
        "flask",
        "werkzeug",
        "werkzeug.serving",
        "werkzeug.debug",
        "jinja2",
        "jinja2.ext",
        "markdown",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hook_oqs.py"],
    excludes=["pytest", "unittest", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="echocoin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    icon="favicon.ico",
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
