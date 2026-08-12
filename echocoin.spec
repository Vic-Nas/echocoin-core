# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Echocoin node.
# Build: make linux  or  make windows

from PyInstaller.utils.hooks import collect_all

nacl_datas,       nacl_binaries,       nacl_hiddenimports       = collect_all("nacl")
cffi_datas,       cffi_binaries,       cffi_hiddenimports       = collect_all("cffi")
pqcrypto_datas,   pqcrypto_binaries,   pqcrypto_hiddenimports   = collect_all("pqcrypto")
chiavdf_datas,    chiavdf_binaries,    chiavdf_hiddenimports     = collect_all("chiavdf")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[
        *nacl_binaries,
        *cffi_binaries,
        *pqcrypto_binaries,
        *chiavdf_binaries,
    ],
    datas=[
        ("bip39_english.txt", "."),
        ("whitepaper.md",     "."),
        ("echocoin.svg",      "."),
        ("echocoin.png",      "."),
        ("favicon.ico",       "."),
        *nacl_datas,
        *cffi_datas,
        *pqcrypto_datas,
        *chiavdf_datas,
    ],
    hiddenimports=[
        *nacl_hiddenimports,
        *cffi_hiddenimports,
        *pqcrypto_hiddenimports,
        *chiavdf_hiddenimports,
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
    icon="echocoin.ico",   # Windows only; ignored on Linux
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
