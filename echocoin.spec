# -*- mode: python ; coding: utf-8 -*-
#
# Linux:   make linux   -> dist/echocoin.AppImage  (onedir wrapped in AppImage)
# Windows: make windows -> dist/echocoin.exe       (onefile)

import glob, os, sys
from PyInstaller.utils.hooks import collect_all

nacl_datas,    nacl_binaries,    nacl_hiddenimports    = collect_all("nacl")
cffi_datas,    cffi_binaries,    cffi_hiddenimports    = collect_all("cffi")
oqs_datas,     oqs_binaries,     oqs_hiddenimports     = collect_all("oqs")
chiavdf_datas, chiavdf_binaries, chiavdf_hiddenimports = collect_all("chiavdf")

_search_roots = [
    "/usr/local/lib",
    "/usr/lib",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    os.path.join(os.path.dirname(sys.executable), "..", "lib"),
]
_liboqs_bins = []
for _root in _search_roots:
    for _p in sorted(glob.glob(os.path.join(_root, "liboqs.so*"))):
        if os.path.isfile(_p):
            _liboqs_bins.append((_p, "."))
    if _liboqs_bins:
        break

_py_dir = os.path.dirname(sys.executable)
_msvc_dlls = [
    (p, ".")
    for pattern in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
    for p in glob.glob(os.path.join(_py_dir, pattern))
]

_all_binaries = [
    *nacl_binaries, *cffi_binaries, *oqs_binaries,
    *chiavdf_binaries, *_liboqs_bins, *_msvc_dlls,
]
_all_datas = [
    ("bip39_english.txt", "."),
    ("docs/whitepaper.md", "."),
    ("echocoin.svg",       "."),
    ("echocoin.png",       "."),
    ("favicon.ico",        "."),
    ("templates_html",     "templates_html"),
    *nacl_datas, *cffi_datas, *oqs_datas, *chiavdf_datas,
]
_all_hiddenimports = [
    *nacl_hiddenimports, *cffi_hiddenimports,
    *oqs_hiddenimports, *chiavdf_hiddenimports,
    "oqs", "_cffi_backend", "libtorrent", "miniupnpc",
    "flask", "werkzeug", "werkzeug.serving", "werkzeug.debug",
    "jinja2", "jinja2.ext", "markdown",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=_all_binaries,
    datas=_all_datas,
    hiddenimports=_all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hook_oqs.py"],
    excludes=["pytest", "unittest", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

if sys.platform == "win32":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="echocoin",
        debug=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        console=True,
        icon="favicon.ico",
        bootloader_ignore_signals=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    # onedir: exclude_binaries=True tells EXE not to bundle libs itself;
    # COLLECT then assembles everything into dist/echocoin/
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="echocoin",
        debug=False,
        strip=False,
        upx=False,
        console=True,
        bootloader_ignore_signals=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="echocoin",
    )
