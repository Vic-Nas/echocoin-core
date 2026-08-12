"""Thread-safe peer address store with health tracking.

Pure data structure. No I/O, no threads, no queues. Every module that
touches peers reads from or writes to a PeerPool instance, but they
never call each other.
"""

import time
import socket
import secrets
import logging
import threading

from params import MAX_PEERS

log = logging.getLogger("pc.peerpool")

COOLDOWN_SECONDS     = 60
COOLDOWN_MAX_SECONDS = 300
MAX_STRIKES          = 3
STALE_SECONDS        = 300


class PeerPool:

    def __init__(self, host, port):
        self._host  = host
        self._port  = port
        self._peers = {}          # addr -> last_seen (wall clock)
        self._fails = {}          # addr -> {"strikes": int, "cooldown_until": monotonic}
        self._lock  = threading.Lock()
        self._upnp_ip = None      # set by Discovery after UPnP mapping

        # Precompute self-addresses once; updated when UPnP resolves.
        self._own = self._build_own_set()

    # ---- Self-detection (prevents self-registration) ----

    def _build_own_set(self):
        own = {
            f"{self._host}:{self._port}",
            f"127.0.0.1:{self._port}",
            f"0.0.0.0:{self._port}",
            f"{self._detect_lan_ip()}:{self._port}",
        }
        if self._upnp_ip:
            own.add(f"{self._upnp_ip}:{self._port}")
        return own

    def set_upnp_ip(self, ip):
        with self._lock:
            self._upnp_ip = ip
            self._own = self._build_own_set()

    @property
    def upnp_ip(self):
        return self._upnp_ip

    def my_ip(self):
        if self._upnp_ip:
            return self._upnp_ip
        return self._detect_lan_ip()

    def _detect_lan_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return self._host if self._host != "0.0.0.0" else "127.0.0.1"

    def is_self(self, addr):
        with self._lock:
            return addr in self._own

    # ---- Core operations ----

    def add(self, addr):
        """Add a peer. Returns True if it was new."""
        now_mono = time.monotonic()
        with self._lock:
            if addr in self._own:          # self-check inside the lock
                return False
            if len(self._peers) >= MAX_PEERS:
                return False
            if now_mono < self._fails.get(addr, {}).get("cooldown_until", 0.0):
                return False
            was_new = addr not in self._peers
            self._peers[addr] = time.time()
        if was_new:
            log.debug("[peer] added  addr=%s", addr)
        return was_new

    def touch(self, addr):
        """Update last-seen timestamp and clear strikes on successful contact."""
        with self._lock:
            if addr in self._peers:
                self._peers[addr] = time.time()
            self._fails.pop(addr, None)

    def strike(self, addr):
        """Record a failure. Enough strikes cause removal."""
        with self._lock:
            rec = self._fails.get(addr, {"strikes": 0, "cooldown_until": 0.0})
            strikes = rec["strikes"] + 1
            cooldown = min(COOLDOWN_SECONDS * (2 ** (strikes - 1)), COOLDOWN_MAX_SECONDS)
            self._fails[addr] = {"strikes": strikes, "cooldown_until": time.monotonic() + cooldown}
            if strikes >= MAX_STRIKES:
                self._peers.pop(addr, None)
                self._fails.pop(addr, None)
                log.warning("[peer] banned  addr=%s  strikes=%d", addr, strikes)

    def remove(self, addr):
        with self._lock:
            self._peers.pop(addr, None)

    def evict_stale(self):
        """Remove peers not seen within STALE_SECONDS."""
        cutoff = time.time() - STALE_SECONDS
        with self._lock:
            stale = [p for p, t in self._peers.items() if t < cutoff]
            for p in stale:
                del self._peers[p]
        if stale:
            log.debug("[peer] evicted %d stale peer(s)", len(stale))

    # ---- Queries ----

    def get_all(self):
        """Return list of all peer addresses (snapshot)."""
        now_mono = time.monotonic()
        with self._lock:
            return [
                p for p in self._peers
                if now_mono >= self._fails.get(p, {}).get("cooldown_until", 0.0)
            ]

    def random(self):
        """Pick a random peer, or None if empty."""
        peers = self.get_all()
        return secrets.choice(peers) if peers else None

    def count(self):
        with self._lock:
            return len(self._peers)

    def all_addrs(self):
        """Raw list of all addresses (including those on cooldown). For cache/API."""
        with self._lock:
            return list(self._peers.keys())
