"""Pending tx storage. No I/O."""

import time

import tx as tx_mod

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

    def pending_nonce(self, addr):
        """Highest nonce in the mempool for addr, or 0 if none. Lets a
        wallet queue up several sends in a row without waiting for each
        one to confirm first."""
        nonces = [t["nonce"] for t, _ in self._pool.values()
                  if t.get("from") == addr]
        return max(nonces) if nonces else 0

    def probe_state_for(self, addr, state):
        """State snapshot with addr's already-pending mempool txs applied,
        in nonce order. Validating a newly submitted tx against this
        (instead of the raw confirmed state) is what lets a wallet queue a
        second send before the first confirms: both the nonce and the
        balance it's checked against already account for the first one."""
        probe = state.snapshot()
        pending = sorted(
            (t for t, _ in self._pool.values() if t.get("from") == addr),
            key=lambda t: t["nonce"],
        )
        for t in pending:
            probe.apply_tx(t)
        return probe

    def pending_hashes(self):
        return frozenset(self._pool.keys())

    def prune_stale(self, state, ttl_seconds=MEMPOOL_TTL_SECONDS):
        """Evict txs that can never become valid: a nonce already superseded
        on chain, or simply too old. Returns list of pruned hashes."""
        now = time.monotonic()
        pruned = []
        for h, (t, entered) in list(self._pool.items()):
            if t["nonce"] <= state.get_nonce(t["from"]) or now - entered > ttl_seconds:
                pruned.append(h)
                del self._pool[h]
        return pruned
