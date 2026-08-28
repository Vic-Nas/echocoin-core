"""Thread-safe peer address store with health tracking.

Pure data structure. No I/O, no threads, no queues. Every module that
touches peers reads from or writes to a PeerPool instance, but they
never call each other.
"""

import ipaddress
import logging
import secrets
import threading
import time

from params import MAX_PEERS

log = logging.getLogger("ec.peerpool")

COOLDOWN_SECONDS     = 60
COOLDOWN_MAX_SECONDS = 300
MAX_STRIKES          = 3
STALE_SECONDS        = 300


def is_routable_peer_addr(addr: str) -> bool:
    """Reject loopback/private/link-local/multicast hosts. A malicious DHT
    or peer-exchange entry pointing at e.g. 127.0.0.1 or a 10.x address
    would otherwise make this node send UDP probes into its own host or
    internal network on the attacker's behalf."""
    try:
        host, _port = addr.rsplit(":", 1)
        ip = ipaddress.ip_address(host)
    except (ValueError, AttributeError):
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class PeerPool:

    def __init__(self, host, port, max_peers=None):
        self._max_peers = max_peers if max_peers is not None else MAX_PEERS
        self._peers     = {}          # addr -> last_seen (wall clock)
        self._fails     = {}          # addr -> {"strikes": int, "cooldown_until": monotonic}
        self._lock      = threading.Lock()

    # ---- Core operations ----

    def add(self, addr):
        """Add a peer. Returns True if it was new."""
        if not is_routable_peer_addr(addr):
            return False
        now_mono = time.monotonic()
        with self._lock:
            if len(self._peers) >= self._max_peers:
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
            rec     = self._fails.get(addr, {"strikes": 0, "cooldown_until": 0.0})
            strikes = rec["strikes"] + 1
            banned  = strikes >= MAX_STRIKES
            cooldown = COOLDOWN_MAX_SECONDS if banned else min(
                COOLDOWN_SECONDS * (2 ** (strikes - 1)), COOLDOWN_MAX_SECONDS
            )
            self._fails[addr] = {"strikes": strikes,
                                  "cooldown_until": time.monotonic() + cooldown}
            if banned:
                self._peers.pop(addr, None)
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

    def snapshot(self):
        """Return [(addr, last_seen, active)] for display. active is False
        while a peer is in cooldown after repeated failures."""
        now_mono = time.monotonic()
        with self._lock:
            return [
                (addr, last_seen,
                 now_mono >= self._fails.get(addr, {}).get("cooldown_until", 0.0))
                for addr, last_seen in self._peers.items()
            ]
