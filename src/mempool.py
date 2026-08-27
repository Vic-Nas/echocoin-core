"""Pending tx storage. No I/O.

Holds both "confirm" (ciphertext submissions) and "resolve" (published
puzzle solutions) tx kinds, keyed by tx_hash exactly like the old
plaintext-only pool. There is no mempool-wide notion of "pending nonce"
any more: a confirmation's real sender and nonce live inside its
encrypted payload and are invisible until resolution, so nonce tracking
for an outgoing wallet is the wallet's own responsibility (it authored
the inner payload and knows its own pending nonces), not something
derivable by scanning the mempool.
"""

import time

from params import FEE_HEIGHT_MAX_AGE

MEMPOOL_TTL_SECONDS = 30 * 60


class Mempool:
    """
    The node loop is the only writer. Flask threads are read-only.
    CPython GIL makes dict reads and single-key writes atomic, so no
    lock is needed. Flask threads must never call add, remove, or remove_many.
    """

    def __init__(self):
        # tx_hash -> (tx_dict, entered_monotonic)
        self._pool: dict = {}

    def add(self, tx_dict) -> tuple:
        import tx as tx_mod
        h = tx_mod.tx_hash(tx_dict)
        if h in self._pool:
            return False, "duplicate"
        self._pool[h] = (tx_dict, time.monotonic())
        return True, h

    def remove(self, tx_hash):
        self._pool.pop(tx_hash, None)

    def remove_many(self, tx_hashes):
        for h in tx_hashes:
            self._pool.pop(h, None)

    def get(self, tx_hash):
        entry = self._pool.get(tx_hash)
        return entry[0] if entry else None

    def get_txs_by_hashes(self, tx_hashes):
        return [self._pool[h][0] for h in tx_hashes if h in self._pool]

    def size(self):
        return len(self._pool)

    def all_txs(self):
        return [tx for tx, _ in self._pool.values()]

    def confirmations(self):
        """All pending "confirm" txs."""
        return [t for t, _ in self._pool.values() if t.get("kind") == "confirm"]

    def resolutions(self):
        """All pending "resolve" txs."""
        return [t for t, _ in self._pool.values() if t.get("kind") == "resolve"]

    def pending_hashes(self):
        return frozenset(self._pool.keys())

    def prune_stale(self, chain_tip_height, state, ttl_seconds=MEMPOOL_TTL_SECONDS):
        """Evict txs that can never become valid, or have simply aged out.

        "confirm" entries: stale fee_height or too old.
        "resolve" entries: too old, or already resolved on chain (checked
        via state's escrow -- once a confirmed tx's escrow is gone, either
        it was already paid out, or it was never confirmed at all in this
        state; either way a stale resolution for it can be dropped).
        Returns list of pruned hashes.
        """
        now = time.monotonic()
        pruned = []
        for h, (t, entered) in list(self._pool.items()):
            too_old = now - entered > ttl_seconds
            if t.get("kind") == "confirm":
                fh = t.get("fee_height")
                stale_fee = (
                    not isinstance(fh, int)
                    or fh > chain_tip_height
                    or fh < chain_tip_height - (FEE_HEIGHT_MAX_AGE - 1)
                )
                if stale_fee or too_old:
                    pruned.append(h)
                    del self._pool[h]
            else:
                if too_old:
                    pruned.append(h)
                    del self._pool[h]
        return pruned
