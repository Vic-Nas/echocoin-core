"""Outbound message broadcasting and Dandelion tx relay.

Reads PeerPool for peer addresses. Does HTTP POSTs. No queues,
no threads of its own (uses short-lived thread pools for fan-out).
"""

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import requests

import tx as tx_mod

log = logging.getLogger("ec.gossip")

STEM_HOPS          = 4
SEEN_TX_CACHE_SIZE = 50_000
BROADCAST_TIMEOUT  = 2


class Gossip:

    def __init__(self, pool, port):
        self.pool     = pool
        self.port     = port
        self._seen_tx = OrderedDict()
        self._lock    = threading.Lock()

    # ---- Public API (called by Node) ----

    def broadcast_block(self, block):
        self._broadcast("/api/receive_block", {
            "type":        "block",
            "block":       block,
            "sender_port": self.port,
        })

    def relay_tx(self, tx_dict):
        h = tx_mod.tx_hash(tx_dict)
        with self._lock:
            if h in self._seen_tx:
                return
            self._seen_tx[h] = True
            if len(self._seen_tx) > SEEN_TX_CACHE_SIZE:
                self._seen_tx.popitem(last=False)
        self.dandelion_send(tx_dict, STEM_HOPS)

    def mark_seen(self, h):
        """Mark h as seen. Returns True if already seen, False if new."""
        with self._lock:
            if h in self._seen_tx:
                return True
            self._seen_tx[h] = True
            if len(self._seen_tx) > SEEN_TX_CACHE_SIZE:
                self._seen_tx.popitem(last=False)
            return False

    # ---- Dandelion stem/fluff ----

    def dandelion_send(self, tx_dict, remaining_hops):
        if remaining_hops > 0:
            peer = self.pool.random()
            if peer:
                self._send(peer, "/api/receive_tx", {
                    "type": "tx_stem", "tx": tx_dict,
                    "remaining_hops": remaining_hops - 1,
                })
                return
        self._broadcast("/api/receive_tx", {"type": "tx_fluff", "tx": tx_dict})

    # ---- Internals ----

    def _broadcast(self, endpoint, data):
        peers = self.pool.get_all()
        if not peers:
            return
        with ThreadPoolExecutor(max_workers=min(len(peers), 64)) as ex:
            for p in peers:
                ex.submit(self._send, p, endpoint, data)

    def _send(self, peer_addr, endpoint, data):
        try:
            requests.post(
                f"http://{peer_addr}{endpoint}", json=data, timeout=BROADCAST_TIMEOUT
            )
            self.pool.touch(peer_addr)
        except requests.exceptions.Timeout:
            log.debug("[gossip] send timed out  peer=%s", peer_addr)
        except Exception:
            self.pool.strike(peer_addr)
