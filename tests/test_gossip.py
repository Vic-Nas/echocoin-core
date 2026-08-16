"""
Unit tests for gossip.py

Covers: mark_seen (first call, duplicate), relay_tx (dedup via LRU cache),
dandelion_send (stem path with peer, fluff fallback when no peers),
broadcast_block, _send success/failure (touch vs strike on pool).

All HTTP calls are mocked -- no network.
"""

import os
import sys
from unittest.mock import MagicMock, patch, call
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gossip import Gossip, STEM_HOPS, SEEN_TX_CACHE_SIZE
import tx as tx_mod
import state as state_mod
from tests.fixtures import address, make_tx, seed_balance
from params import RINGS_PER_ECH


def make_gossip(peers=None, port=9000):
    pool = MagicMock()
    pool.get_all.return_value = peers or []
    pool.random.return_value = peers[0] if peers else None
    gossip = Gossip(pool=pool, port=port)
    return gossip, pool


def sample_tx():
    s = state_mod.State()
    seed_balance(s, 0, 100.0)
    return make_tx(0, 1, RINGS_PER_ECH, s, 10)


# ---------------------------------------------------------------------------
# 1. mark_seen
# ---------------------------------------------------------------------------

class TestMarkSeen:
    def test_first_time_returns_false(self):
        g, _ = make_gossip()
        assert g.mark_seen("abc123") is False

    def test_second_time_returns_true(self):
        g, _ = make_gossip()
        g.mark_seen("abc123")
        assert g.mark_seen("abc123") is True

    def test_different_hashes_each_new(self):
        g, _ = make_gossip()
        assert g.mark_seen("hash1") is False
        assert g.mark_seen("hash2") is False
        assert g.mark_seen("hash1") is True

    def test_mark_seen_thread_safe(self):
        """Concurrent mark_seen calls must not deadlock or corrupt state."""
        g, _ = make_gossip()
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
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        with patch.object(g, "dandelion_send") as mock_send:
            g.relay_tx(t)
            mock_send.assert_called_once()

    def test_relay_tx_duplicate_not_sent(self):
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        with patch.object(g, "dandelion_send") as mock_send:
            g.relay_tx(t)
            g.relay_tx(t)  # second call -- already seen
            assert mock_send.call_count == 1

    def test_relay_tx_different_txs_both_sent(self):
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
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
# 3. dandelion_send -- Dandelion routing (whitepaper Section 6)
# ---------------------------------------------------------------------------

class TestDandelionSend:
    def test_stem_path_sends_to_single_peer(self):
        """With hops remaining and a peer available, send to one peer (stem)."""
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        with patch.object(g, "_send") as mock_send:
            g.dandelion_send(t, remaining_hops=3)
            mock_send.assert_called_once()
            endpoint = mock_send.call_args[0][1]
            assert endpoint == "/api/receive_tx"

    def test_stem_decrements_hops(self):
        """The forwarded message should have remaining_hops - 1."""
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        sent_data = {}

        def capture_send(peer, endpoint, data):
            sent_data.update(data)

        with patch.object(g, "_send", side_effect=capture_send):
            g.dandelion_send(t, remaining_hops=3)
        assert sent_data.get("remaining_hops") == 2

    def test_fluff_when_no_peers(self):
        """Without peers on stem, fall through to broadcast (fluff phase)."""
        g, pool = make_gossip(peers=[])
        t = sample_tx()
        with patch.object(g, "_broadcast") as mock_bcast:
            g.dandelion_send(t, remaining_hops=3)
            mock_bcast.assert_called_once()

    def test_fluff_when_zero_hops(self):
        """remaining_hops=0 triggers fluff immediately."""
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        t = sample_tx()
        with patch.object(g, "_broadcast") as mock_bcast:
            g.dandelion_send(t, remaining_hops=0)
            mock_bcast.assert_called_once()


# ---------------------------------------------------------------------------
# 4. broadcast_block
# ---------------------------------------------------------------------------

class TestBroadcastBlock:
    def test_broadcast_block_sends_to_all_peers(self):
        peers = ["1.2.3.4:9000", "1.2.3.5:9000"]
        g, pool = make_gossip(peers=peers)
        block = {"height": 1, "hash": "aa" * 32}
        with patch.object(g, "_broadcast") as mock_bcast:
            g.broadcast_block(block)
            mock_bcast.assert_called_once()
            endpoint = mock_bcast.call_args[0][0]
            assert endpoint == "/api/receive_block"


# ---------------------------------------------------------------------------
# 5. _send -- success and failure handling
# ---------------------------------------------------------------------------

class TestSend:
    def test_send_success_touches_peer(self):
        import requests
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        with patch("gossip.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            g._send("1.2.3.4:9000", "/api/receive_tx", {"data": "x"})
            pool.touch.assert_called_once_with("1.2.3.4:9000")

    def test_send_timeout_strikes_peer(self):
        import requests
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        with patch("gossip.requests.post", side_effect=requests.exceptions.Timeout):
            g._send("1.2.3.4:9000", "/api/receive_tx", {"data": "x"})
            pool.strike.assert_called_once_with("1.2.3.4:9000")

    def test_send_connection_error_strikes_peer(self):
        import requests
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        with patch("gossip.requests.post", side_effect=requests.exceptions.ConnectionError):
            g._send("1.2.3.4:9000", "/api/receive_tx", {"data": "x"})
            pool.strike.assert_called_once_with("1.2.3.4:9000")

    def test_send_unexpected_exception_does_not_raise(self):
        g, pool = make_gossip(peers=["1.2.3.4:9000"])
        with patch("gossip.requests.post", side_effect=RuntimeError("unexpected")):
            g._send("1.2.3.4:9000", "/api/receive_tx", {"data": "x"})
            # Should not raise; pool.strike not called for generic errors
