# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PoolCoin node.
# Build via the Makefile: make linux or make windows
#
# Uses collect_all() for crypto packages that have compiled C extensions
# and complex import chains (cffi, nacl, pqcrypto) to avoid the whack-a-mole
# of listing individual hidden imports.

from PyInstaller.utils.hooks import collect_all

# Collect everything (submodules, binaries, data) from the crypto stack
nacl_datas,    nacl_binaries,    nacl_hiddenimports    = collect_all("nacl")
cffi_datas,    cffi_binaries,    cffi_hiddenimports    = collect_all("cffi")
pqcrypto_datas, pqcrypto_binaries, pqcrypto_hiddenimports = collect_all("pqcrypto")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[
        *nacl_binaries,
        *cffi_binaries,
        *pqcrypto_binaries,
    ],
    datas=[
        ("bip39_english.txt", "."),
        ("whitepaper.md",     "."),
        *nacl_datas,
        *cffi_datas,
        *pqcrypto_datas,
    ],
    hiddenimports=[
        *nacl_hiddenimports,
        *cffi_hiddenimports,
        *pqcrypto_hiddenimports,
        "_cffi_backend",
        # libtorrent is imported lazily inside maintain_peers.
        "libtorrent",
        # miniupnpc is imported lazily inside _upnp_map_port.
        "miniupnpc",
        # Flask internals that PyInstaller sometimes misses.
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
    excludes=[
        "pytest",
        "unittest",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="poolcoin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
