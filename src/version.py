"""This node's own baked-in version, read from the VERSION file bundled
alongside the source (or the PyInstaller bundle root when frozen)."""

import os
import sys


def _load_local_version():
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        # Dev/source checkout: src/version.py -> VERSION lives one level up.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "VERSION")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


LOCAL_VERSION = _load_local_version()
