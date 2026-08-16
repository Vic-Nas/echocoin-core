"""
PyInstaller runtime hook for oqs (liboqs-python).

oqs searches $OQS_INSTALL_PATH/lib/liboqs.so (exact unversioned name).
The bundled file may be versioned (liboqs.so.0.16.0). We set OQS_INSTALL_PATH
and also pre-load the library via ctypes under the name oqs expects so its
own ctypes.cdll.LoadLibrary call succeeds.
"""
import ctypes
import glob
import os
import sys

if hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS
    os.environ["OQS_INSTALL_PATH"] = meipass

    lib_dir = os.path.join(meipass, "lib")
    unversioned = os.path.join(lib_dir, "liboqs.so")

    # If only the versioned file landed in the bundle, symlink/copy it
    # under the unversioned name that oqs looks for.
    if not os.path.exists(unversioned):
        candidates = (
            sorted(glob.glob(os.path.join(lib_dir, "liboqs.so.*")))
            + sorted(glob.glob(os.path.join(meipass, "liboqs.so*")))
        )
        if candidates:
            src = candidates[0]
            try:
                os.symlink(src, unversioned)
            except OSError:
                import shutil
                shutil.copy2(src, unversioned)

    # Pre-load so ctypes finds it by name without needing ldconfig.
    if os.path.exists(unversioned):
        try:
            ctypes.cdll.LoadLibrary(unversioned)
        except OSError:
            pass
