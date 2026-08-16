"""
PyInstaller runtime hook for oqs (liboqs-python).

In onedir mode (Linux) liboqs.so* lands in the same directory as the
binary, which is also sys._MEIPASS. We set OQS_INSTALL_PATH to the
parent of that directory so oqs constructs:
  $OQS_INSTALL_PATH/lib/liboqs.so  ->  _MEIPASS/../lib/  (doesn't exist)

Actually simpler: just pre-load liboqs via ctypes before oqs imports.
ctypes caches loaded libraries by name, so when oqs later calls
cdll.LoadLibrary("liboqs.so") it gets the already-loaded handle.
"""
import ctypes
import glob
import os
import sys

if hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS
    # In onedir mode libs are in _MEIPASS itself.
    # Find any liboqs.so* and load it via ctypes before oqs does.
    candidates = sorted(
        glob.glob(os.path.join(meipass, "liboqs.so*"))
        + glob.glob(os.path.join(meipass, "lib", "liboqs.so*"))
    )
    for lib in candidates:
        if os.path.isfile(lib):
            try:
                ctypes.CDLL(lib)
                # Also set OQS_INSTALL_PATH so _load_shared_obj
                # searches _MEIPASS/lib as additional_searching_path.
                os.environ["OQS_INSTALL_PATH"] = meipass
                break
            except OSError:
                pass
