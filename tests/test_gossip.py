"""
Unit tests for gossip.py (UDP transport edition)

Covers: mark_seen (first call, duplicate), relay_tx (dedup via LRU cache),
dandelion_send (stem path with peer, fluff fallback when no peers),
broadcast_block.

UDP calls are mocked via the udp object -- no network.
"""

import os
import sys
from unittest.mock import MagicMock, patch
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gossip import Gossip, STEM_HOPS_MAX as STEM_HOPS, SEEN_TX_CACHE_SIZE
import tx as tx_mod
import state as state_mod
from tests.fixtures import address, make_tx, seed_balance
from params import RINGS_PER_ECH


def make_gossip(peers=None):
    pool = MagicMock()
    pool.get_all.return_value = peers or []
    pool.random.return_value = peers[0] if peers else None
    udp = MagicMock()
    gossip = Gossip(pool=pool, udp=udp)
    return gossip, pool, udp


def sample_tx():
    s = state_mod.State()
    seed_balance(s, 0, 100.0)
    return make_tx(0, 1, RINGS_PER_ECH, s, 10)


# ---------------------------------------------------------------------------
# 1. mark_seen
# ---------------------------------------------------------------------------

class TestMarkSeen:
    def test_first_time_returns_false(self):
        g, _, _ = make_gossip()
        assert g.mark_seen("abc123") is False

    def test_second_time_returns_true(self):
        g, _, _ = make_gossip()
        g.mark_seen("abc123")
        assert g.mark_seen("abc123") is True

    def test_different_hashes_each_new(self):
        g, _, _ = make_gossip()
        assert g.mark_seen("hash1") is False
        assert g.mark_seen("hash2") is False
        assert g.mark_seen("hash1") is True

    def test_mark_seen_thread_safe(self):
        g, _, _ = make_gossip()
        errors = []

        def worker(i):
            try:
                g.mark_seen(f"hash_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ---------------------------------------------------------------------------
# 2. relay_tx -- dedup
# ---------------------------------------------------------------------------

class TestRelayTx:
    def test_relay_tx_first_time_sends(self):
        g, pool, udp = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        with patch.object(g, "dandelion_send") as mock_send:
            g.relay_tx(t)
            mock_send.assert_called_once()

    def test_relay_tx_duplicate_fluff_suppressed(self):
        """Duplicate fluffs are suppressed by the seen cache; stem always forwards."""
        g, pool, udp = make_gossip(peers=[])  # no peers → falls through to fluff
        t = sample_tx()
        g.dandelion_send(t, 0)   # first fluff: goes through
        g.dandelion_send(t, 0)   # second fluff: suppressed by seen cache
        assert udp.send_tx.call_count == 1

    def test_relay_tx_different_txs_both_sent(self):
        g, pool, udp = make_gossip(peers=["1.2.3.4:9000"])
        s = state_mod.State()
        seed_balance(s, 0, 1000.0)
        t1 = make_tx(0, 1, RINGS_PER_ECH, s, 10)
        s.apply_tx(t1)
        t2 = make_tx(0, 1, RINGS_PER_ECH, s, 10)
        with patch.object(g, "dandelion_send") as mock_send:
            g.relay_tx(t1)
            g.relay_tx(t2)
            assert mock_send.call_count == 2


# ---------------------------------------------------------------------------
# 3. dandelion_send
# ---------------------------------------------------------------------------

class TestDandelionSend:
    def test_stem_sends_to_single_peer(self):
        g, pool, udp = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        g.dandelion_send(t, remaining_hops=3)
        udp.send_tx.assert_called_once()
        # stem: peers kwarg has exactly one entry
        call_kwargs = udp.send_tx.call_args[1]
        assert len(call_kwargs.get("peers", [])) == 1

    def test_fluff_when_no_peers(self):
        g, pool, udp = make_gossip(peers=[])
        t = sample_tx()
        g.dandelion_send(t, remaining_hops=3)
        # Falls through to broadcast (send_tx with no peers kwarg)
        udp.send_tx.assert_called_once()
        call_kwargs = udp.send_tx.call_args[1] if udp.send_tx.call_args else {}
        assert "peers" not in call_kwargs

    def test_fluff_when_zero_hops(self):
        g, pool, udp = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        g.dandelion_send(t, remaining_hops=0)
        # Zero hops -> fluff broadcast
        udp.send_tx.assert_called_once()
        call_kwargs = udp.send_tx.call_args[1] if udp.send_tx.call_args else {}
        assert "peers" not in call_kwargs


# ---------------------------------------------------------------------------
# 4. broadcast_block
# ---------------------------------------------------------------------------

class TestBroadcastBlock:
    def test_broadcast_block_calls_udp(self):
        peers = ["1.2.3.4:9000", "1.2.3.5:9000"]
        g, pool, udp = make_gossip(peers=peers)
        block = {"height": 1, "hash": "aa" * 32}
        g.broadcast_block(block)
        udp.send_block.assert_called_once_with(block)
