"""Outbound block/tx broadcasting over UDP.

Replaces HTTP POST gossip. Uses UDPTransport.send_block() and send_tx().
Dandelion stem/fluff logic is unchanged; only the transport differs.
"""

import logging
import threading
from cachetools import LRUCache

import tx as tx_mod

log = logging.getLogger("ec.gossip")

STEM_HOPS          = 4
SEEN_TX_CACHE_SIZE = 50_000


class Gossip:

    def __init__(self, pool, udp):
        self.pool     = pool
        self.udp      = udp
        self._seen_tx = LRUCache(maxsize=SEEN_TX_CACHE_SIZE)
        self._lock    = threading.Lock()

    # ---- Public API (called by Node) ----

    def broadcast_block(self, block):
        self.udp.send_block(block)

    def relay_tx(self, tx_dict):
        h = tx_mod.tx_hash(tx_dict)
        with self._lock:
            if h in self._seen_tx:
                return
            self._seen_tx[h] = True
        self.dandelion_send(tx_dict, STEM_HOPS)

    def mark_seen(self, h):
        """Mark h as seen. Returns True if already seen, False if new."""
        with self._lock:
            if h in self._seen_tx:
                return True
            self._seen_tx[h] = True
            return False

    # ---- Dandelion stem/fluff ----

    def dandelion_send(self, tx_dict, remaining_hops):
        if remaining_hops > 0:
            peer = self.pool.random()
            if peer:
                self.udp.send_tx(tx_dict, peers=[peer])
                return
        # Fluff: broadcast to all
        self.udp.send_tx(tx_dict)
