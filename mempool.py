"""Pending tx storage. No I/O."""

import time

import tx as tx_mod
from params import FEE_HEIGHT_MAX_AGE

# Local mempool hygiene only. Age out an unconfirmed tx after this many
# seconds regardless of nonce/fee_height status, so a stuck slot doesn't
# sit in an honest node's mempool forever.
MEMPOOL_TTL_SECONDS = 30 * 60


def _is_fee_height_stale(tx_dict, chain_tip_height):
    fh = tx_dict.get("fee_height")
    return (
        not isinstance(fh, int)
        or fh > chain_tip_height
        or fh < chain_tip_height - (FEE_HEIGHT_MAX_AGE - 1)
    )


class Mempool:
    """
    The node loop is the only writer. Flask threads are read-only.
    CPython GIL makes dict reads and single-key writes atomic, so no
    lock is needed -- the same reasoning NodeView uses explicitly.
    Flask threads must never call add, remove, or remove_many.
    """

    def __init__(self):
        self._txs     = {}   # tx_hash -> tx_dict
        self._entered = {}   # tx_hash -> monotonic time added

    def add(self, tx_dict):
        """Add a transaction. Returns (True, hash) or (False, reason)."""
        h = tx_mod.tx_hash(tx_dict)
        if h in self._txs:
            return False, "duplicate"
        self._txs[h] = tx_dict
        self._entered[h] = time.monotonic()
        return True, h

    def remove(self, tx_hash):
        """Remove a transaction by hash."""
        self._txs.pop(tx_hash, None)
        self._entered.pop(tx_hash, None)

    def remove_many(self, tx_hashes):
        """Remove multiple transactions."""
        for h in tx_hashes:
            self._txs.pop(h, None)
            self._entered.pop(h, None)

    def get(self, tx_hash):
        return self._txs.get(tx_hash)

    def get_txs_by_hashes(self, tx_hashes):
        """Return list of tx dicts for given hashes (skips missing)."""
        return [self._txs[h] for h in tx_hashes if h in self._txs]

    def size(self):
        return len(self._txs)

    def all_txs(self):
        return list(self._txs.values())

    def pending_hashes(self):
        """Return frozenset of all pending tx hashes. For censorship scoring."""
        return frozenset(self._txs.keys())

    def prune_stale(self, chain_tip_height, state, ttl_seconds=MEMPOOL_TTL_SECONDS):
        """Evict entries that can never become valid again: a stale
        fee_height (older than the tx.validate() acceptance window), a
        nonce already superseded by confirmed state, or (TTL) simply too
        old to keep carrying regardless of nonce/fee_height. Does NOT touch
        entries that are simply queued ahead of their turn (nonce >
        current + 1) since those are still valid future candidates.

        Returns list of pruned tx hashes.
        """
        pruned = []
        now = time.monotonic()
        for h, t in list(self._txs.items()):
            stale = False
            fee_stale  = _is_fee_height_stale(t, chain_tip_height)
            nonce_used = t["nonce"] <= state.get_nonce(t["from"])
            too_old    = now - self._entered.get(h, now) > ttl_seconds
            if fee_stale or nonce_used or too_old:
                stale = True
            if stale:
                pruned.append(h)
                del self._txs[h]
                self._entered.pop(h, None)
        return pruned
