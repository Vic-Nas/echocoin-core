"""Minimal desktop GUI: a small status window with one button, replacing the
console for a double-clicked binary. Not used at all under --no-gui or when
LAPSECOIN_PASSPHRASE is set (headless/server runs keep the console path in
main.py unchanged).

Two things this exists to get right, per real feedback from testing the
console-only version: closing the window must for-sure kill the whole
process (not just the window), and a wrong passphrase must be retryable
in place rather than crashing out.
"""

import logging
import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from tkinter import ttk

import crypto

log = logging.getLogger("ec.gui")

_ICON_SVG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lapsecoin.svg")


def _apply_icon(root):
    """Rasterize the repo's lapsecoin.svg in memory and set it as the window
    icon. No generated asset checked into the repo or written to disk --
    cairosvg is already a hard dependency (used elsewhere for the app), so
    this stays a single source of truth for the logo. Failure here should
    never take down the GUI itself, worst case the window just keeps
    whatever default icon the OS/tkinter picks."""
    try:
        import cairosvg
        from PIL import Image, ImageTk

        png_bytes = cairosvg.svg2png(url=_ICON_SVG, output_width=64, output_height=64)
        img = Image.open(__import__("io").BytesIO(png_bytes))
        photo = ImageTk.PhotoImage(img)
        root.iconphoto(True, photo)
        root._icon_photo = photo  # keep a reference; tkinter drops GC'd PhotoImages
    except Exception as e:
        log.debug("[gui] could not set window icon: %s", e)


class _PassphraseDialog:
    """Blocking passphrase entry with inline retry -- a wrong passphrase or
    a too-short new one re-shows the same dialog with an error message
    instead of crashing or reopening a fresh window each attempt."""

    def __init__(self, keyfile):
        self.keyfile = keyfile
        self.is_new = not os.path.exists(keyfile)
        self.result = None  # (pk, kek) on success

        self.root = tk.Tk()
        self.root.title("LapseCoin")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        _apply_icon(self.root)

        pad = {"padx": 12, "pady": 6}
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, **pad)

        if self.is_new:
            msg = "No key file found. Choose a passphrase for your new wallet."
        else:
            msg = "Enter your wallet passphrase."
        ttk.Label(frame, text=msg, wraplength=280).pack(anchor="w")

        self.error_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.error_var, foreground="#c0392b").pack(anchor="w")

        ttk.Label(frame, text="Passphrase:").pack(anchor="w", pady=(8, 0))
        self.pass1 = tk.StringVar()
        entry1 = ttk.Entry(frame, textvariable=self.pass1, show="*", width=30)
        entry1.pack(anchor="w")

        self.pass2 = None
        if self.is_new:
            ttk.Label(frame, text="Confirm passphrase:").pack(anchor="w", pady=(8, 0))
            self.pass2 = tk.StringVar()
            ttk.Entry(frame, textvariable=self.pass2, show="*", width=30).pack(anchor="w")

        btns = ttk.Frame(frame)
        btns.pack(anchor="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="left", padx=(0, 6))
        submit = ttk.Button(btns, text="Continue", command=self._submit)
        submit.pack(side="left")

        entry1.bind("<Return>", lambda e: self._submit())
        entry1.focus_set()

    def _cancel(self):
        self.result = None
        self.root.destroy()

    def _submit(self):
        p1 = self.pass1.get()
        if self.is_new:
            if len(p1) < 8:
                self.error_var.set("Passphrase must be at least 8 characters.")
                return
            if p1 != self.pass2.get():
                self.error_var.set("Passphrases do not match.")
                self.pass2.set("")
                return
            try:
                sk, pk = crypto.generate_keypair()
                crypto.save_key(self.keyfile, sk, pk, p1)
                kek = crypto.derive_kek(self.keyfile, p1)
                addr = crypto.public_key_to_address(pk)
                del sk
                log.info("[startup] key created  file=%s", self.keyfile)
                log.info("[startup] address=%s", addr)
            except Exception as e:
                self.error_var.set(f"Could not create key: {e}")
                return
            self.result = (pk, kek)
            self.root.destroy()
            return

        try:
            pk = crypto.load_pubkey(self.keyfile)
            kek = crypto.derive_kek(self.keyfile, p1)
            sk_test = crypto.decrypt_secret_key(self.keyfile, kek=kek)
            del sk_test
        except ValueError:
            self.error_var.set("Incorrect passphrase. Try again.")
            self.pass1.set("")
            return
        except Exception as e:
            self.error_var.set(f"Could not load key: {e}")
            return
        log.info("[startup] key loaded  file=%s", self.keyfile)
        self.result = (pk, kek)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


def load_or_create_key_gui(keyfile):
    """GUI equivalent of main._load_or_create_key. Returns (pk, kek), or
    exits the process if the user cancels."""
    result = _PassphraseDialog(keyfile).run()
    if result is None:
        sys.exit(0)
    return result


def _open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 -- local file, user's own log
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:
        log.warning("[gui] could not open %s: %s", path, e)


def run_status_window(node, udp, private_port, log_file):
    """Small always-present status window: live height/peers/mempool, one
    button into the local node UI, one to the log file. Closing the window
    (X or Alt+F4) is the only way to quit -- it must actually end the
    process, not just this window, so the close handler does a best-effort
    graceful stop and then unconditionally hard-exits. Several background
    components (peer_udp's callback pool, discovery's, the periodic HTTP
    reachability prober) run non-daemon ThreadPoolExecutor workers that can
    otherwise keep the interpreter alive waiting to join them even after
    node.stop()/udp.stop() -- os._exit sidesteps that entirely rather than
    trying to track down and gracefully join every one of them."""
    root = tk.Tk()
    root.title("LapseCoin")
    root.resizable(False, False)
    _apply_icon(root)

    pad = {"padx": 14, "pady": 8}
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True, **pad)

    ttk.Label(frame, text="LapseCoin node is running.", font=("", 10, "bold")).pack(anchor="w")

    status_var = tk.StringVar(value="starting…")
    ttk.Label(frame, textvariable=status_var).pack(anchor="w", pady=(4, 10))

    def refresh():
        try:
            info = node.get_info()
            status_var.set(
                f"Height {info['height']}   Peers {info['peer_count']}   "
                f"Mempool {info['mempool_size']}"
            )
        except Exception as e:
            status_var.set(f"(status unavailable: {e})")
        root.after(3000, refresh)

    btns = ttk.Frame(frame)
    btns.pack(anchor="w")
    ttk.Button(
        btns, text="Open Node",
        command=lambda: webbrowser.open(f"http://127.0.0.1:{private_port}"),
    ).pack(side="left", padx=(0, 6))
    ttk.Button(btns, text="Logs", command=lambda: _open_path(log_file)).pack(side="left")

    def on_close():
        log.info("[shutdown] window closed")
        try:
            node.stop()
        except Exception:
            pass
        try:
            udp.stop()
        except Exception:
            pass
        root.destroy()
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.mainloop()
