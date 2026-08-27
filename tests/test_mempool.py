"""
Unit tests for mempool.py

Covers: add, remove, remove_many, get, get_txs_by_hashes, size, all_txs,
pending_nonce, pending_hashes, prune_stale.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mempool as mempool_mod
import state as state_mod
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_tx, seed_balance


def fresh_mempool():
    return mempool_mod.Mempool()


def fresh_state():
    return state_mod.State()


def sample_tx(sender_index=0, recipient_index=1, amount=TICKS_PER_LAPSE):
    s = fresh_state()
    seed_balance(s, sender_index, 100.0)
    return make_tx(sender_index, recipient_index, amount, s)


# ---------------------------------------------------------------------------
# 1. add
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_returns_true_and_hash(self):
        mp = fresh_mempool()
        t = sample_tx()
        ok, result = mp.add(t)
        assert ok is True
        assert isinstance(result, str) and len(result) == 64

    def test_add_duplicate_returns_false(self):
        mp = fresh_mempool()
        t = sample_tx()
        mp.add(t)
        ok, reason = mp.add(t)
        assert ok is False
        assert reason == "duplicate"

    def test_add_increments_size(self):
        mp = fresh_mempool()
        assert mp.size() == 0
        mp.add(sample_tx())
        assert mp.size() == 1


# ---------------------------------------------------------------------------
# 2. remove / remove_many
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_by_hash(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        mp.remove(h)
        assert mp.size() == 0

    def test_remove_nonexistent_is_noop(self):
        mp = fresh_mempool()
        mp.remove("00" * 32)  # Should not raise

    def test_remove_many(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        _, h1 = mp.add(t1)
        _, h2 = mp.add(t2)
        mp.remove_many([h1, h2])
        assert mp.size() == 0

    def test_remove_many_partial(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        _, h1 = mp.add(t1)
        _, h2 = mp.add(t2)
        mp.remove_many([h1])
        assert mp.size() == 1


# ---------------------------------------------------------------------------
# 3. get / get_txs_by_hashes
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_existing_tx(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        result = mp.get(h)
        assert result == t

    def test_get_missing_returns_none(self):
        mp = fresh_mempool()
        assert mp.get("00" * 32) is None

    def test_get_txs_by_hashes_returns_present(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        _, h1 = mp.add(t1)
        _, h2 = mp.add(t2)
        results = mp.get_txs_by_hashes([h1, h2])
        assert len(results) == 2

    def test_get_txs_by_hashes_skips_missing(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        results = mp.get_txs_by_hashes([h, "missing" * 4])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 4. all_txs / pending_nonce / pending_hashes
# ---------------------------------------------------------------------------

class TestAllTxs:
    def test_all_txs_empty(self):
        mp = fresh_mempool()
        assert mp.all_txs() == []

    def test_all_txs_returns_all(self):
        mp = fresh_mempool()
        t1 = sample_tx(0, 1)
        t2 = sample_tx(2, 1)
        mp.add(t1)
        mp.add(t2)
        all_t = mp.all_txs()
        assert len(all_t) == 2

    def test_pending_nonce_returns_zero_when_absent(self):
        mp = fresh_mempool()
        assert mp.pending_nonce(address(0)) == 0

    def test_pending_nonce_returns_highest_for_sender(self):
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t1 = make_tx(0, 1, 1, s)
        mp.add(t1)
        t2 = make_tx(0, 1, 1, s, nonce_override=t1["nonce"] + 1)
        mp.add(t2)
        assert mp.pending_nonce(address(0)) == t2["nonce"]

    def test_pending_hashes_returns_frozenset(self):
        mp = fresh_mempool()
        t = sample_tx()
        _, h = mp.add(t)
        hashes = mp.pending_hashes()
        assert isinstance(hashes, frozenset)
        assert h in hashes


# ---------------------------------------------------------------------------
# 5. prune_stale
# ---------------------------------------------------------------------------

class TestPruneStale:
    def test_prune_superseded_nonce(self):
        """Tx whose nonce is already used by state is pruned."""
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        mp.add(t)
        s.apply_tx(t)  # nonce is now consumed
        pruned = mp.prune_stale(state=s)
        assert len(pruned) == 1
        assert mp.size() == 0

    def test_prune_stale_ttl(self):
        """Tx older than TTL is pruned."""
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        mp.add(t)
        # Force very short TTL
        pruned = mp.prune_stale(state=s, ttl_seconds=0)
        assert len(pruned) == 1

    def test_prune_valid_tx_stays(self):
        mp = fresh_mempool()
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s)
        mp.add(t)
        pruned = mp.prune_stale(state=s)
        assert len(pruned) == 0
        assert mp.size() == 1
