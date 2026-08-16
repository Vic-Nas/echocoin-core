"""
PyInstaller runtime hook for oqs (liboqs-python).

oqs/oqs.py checks OQS_INSTALL_PATH before its default $HOME/_oqs search.
Setting it to sys._MEIPASS (where PyInstaller unpacks bundled files) makes
oqs find liboqs.so immediately instead of triggering its auto-build fallback.
"""
import os
import sys

if hasattr(sys, "_MEIPASS"):
    os.environ.setdefault("OQS_INSTALL_PATH", sys._MEIPASS)
