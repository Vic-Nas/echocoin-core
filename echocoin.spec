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

# Explicitly bundle the liboqs shared library -- oqs-python loads it via ctypes
# at runtime and PyInstaller won't detect it through normal import analysis.
_liboqs_bins = [
    (p, ".")
    for pattern in ("liboqs.so*", "liboqs*.dylib", "liboqs*.dll")
    for p in (
        glob.glob(f"/usr/local/lib/{pattern}")
        + glob.glob(f"/usr/lib/{pattern}")
        + glob.glob(f"/usr/lib/x86_64-linux-gnu/{pattern}")
        + glob.glob(os.path.join(os.path.dirname(sys.executable), f"../lib/{pattern}"))
    )
]

# Bundle MSVC runtime DLLs so Windows users don't need VC++ redist installed.
# Globs next to python.exe on a standard Windows Python install.
# Produces nothing on Linux/CI -- harmless.
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
    runtime_hooks=[],
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
    icon="favicon.ico",   # Windows only; ignored on Linux
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
