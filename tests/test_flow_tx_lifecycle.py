"""Flow: Transaction lifecycle.

Covers FLOW.md § Transaction Lifecycle:
  submit_tx_from_api → queue → node loop → validate → mempool → relay
  inbound from peer → dedup → validate → mempool → relay
  Dandelion stem (forward) vs fluff (broadcast)
"""
import queue as _queue
import threading
from unittest.mock import patch, MagicMock

import pytest
from helpers import *


# ---------------------------------------------------------------------------
# submit_tx: validation gate
# ---------------------------------------------------------------------------

def _make_node_tx(n, sk, pk_hex, addr, nonce=1):
    """Build a valid tx using the genesis block fee rate.
    fee_height=0 ensures _fee_rate_at(0) hits genesis on any chain length.
    """
    _, _, _, to = make_keypair()
    fee_height = 0
    rate = n.cs.chain[fee_height]["fee_rate"]
    return make_valid_tx(sk, pk_hex, addr, to, 1_000, nonce, fee_height, rate)


def test_valid_tx_accepted():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        t = _make_node_tx(n, sk, pk_hex, addr)
        ok, result = n.submit_tx(t)
        assert ok, result
        assert n.mempool.size() == 1
    finally:
        teardown_node(n, dbfile, keyfile)


def test_tx_rejected_bad_nonce():
    """Submit a tx whose nonce skips ahead -- state expects 1, we send 5."""
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        fee_height = 0
        rate = n.cs.chain[fee_height]["fee_rate"]
        # Sign a tx with nonce=5 when state expects nonce=1 (gap)
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 5, fee_height, rate)
        ok, err = n.submit_tx(t)
        assert not ok
        assert "nonce" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


def test_tx_rejected_insufficient_balance():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        t = _make_node_tx(n, sk, pk_hex, addr)   # no balance credited
        ok, err = n.submit_tx(t)
        assert not ok
        assert "balance" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


def test_tx_rejected_stale_fee_height():
    from params import FEE_HEIGHT_MAX_AGE
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        fee_height = -(FEE_HEIGHT_MAX_AGE + 1)
        rate = n.cs.chain[0]["fee_rate"]
        outputs = [{"to": to, "amount": 1_000}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, rate)
        t = tx_mod.create(addr, pk_hex, outputs, 1, fee_height, fee, sk)
        ok, err = n.submit_tx(t)
        assert not ok
    finally:
        teardown_node(n, dbfile, keyfile)


def test_accepted_tx_added_to_mempool():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        t = _make_node_tx(n, sk, pk_hex, addr)
        n.submit_tx(t)
        assert n.mempool.size() == 1
        assert n.mempool.get(tx_mod.tx_hash(t)) is not None
    finally:
        teardown_node(n, dbfile, keyfile)


def test_accepted_tx_relayed_via_gossip():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        t = _make_node_tx(n, sk, pk_hex, addr)
        n.submit_tx(t)
        assert len(gossip.relayed_txs) == 1
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# submit_tx_from_api: queue bridge
# ---------------------------------------------------------------------------

def test_submit_tx_from_api_returns_result():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        t = _make_node_tx(n, sk, pk_hex, addr)

        def drain():
            # Pretend this thread IS the loop thread so submit_tx assertion passes
            n._loop_thread = threading.current_thread()
            msg = n.net_in_q.get(timeout=2)
            n._handle(msg, [])

        import threading
        threading.Thread(target=drain, daemon=True).start()
        ok, result = n.submit_tx_from_api(t, timeout=3)
        assert ok, result
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# Inbound tx from peer: dedup
# ---------------------------------------------------------------------------

def test_inbound_tx_deduped_on_second_arrival():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.cs.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)
        h = tx_mod.tx_hash(t)

        # First arrival
        gossip.mark_seen = lambda x: False
        n._handle_inbound_tx({"type": "tx", "tx": t, "relay_type": "tx_fluff"})
        # Second arrival - mark_seen returns True so it should be dropped
        gossip.mark_seen = lambda x: True
        size_before = n.mempool.size()
        n._handle_inbound_tx({"type": "tx", "tx": t, "relay_type": "tx_fluff"})
        assert n.mempool.size() == size_before
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# Dandelion routing
# ---------------------------------------------------------------------------

def test_dandelion_stem_forwards_to_one_peer():
    from gossip import Gossip
    from peerpool import PeerPool
    import requests

    pool = PeerPool("127.0.0.1", 9000)
    pool.add("10.0.0.1:9000")
    g = Gossip(pool, 9000)

    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000)
    t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)

    sent = []
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        g.dandelion_send(t, remaining_hops=2)
        assert mock_post.call_count == 1
        url = mock_post.call_args[0][0]
        assert "receive_tx" in url


def test_dandelion_fluff_broadcasts_to_all():
    from gossip import Gossip
    from peerpool import PeerPool

    pool = PeerPool("127.0.0.1", 9000)
    for i in range(3):
        pool.add(f"10.0.0.{i+1}:9000")
    g = Gossip(pool, 9000)

    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)

    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        g.dandelion_send(t, remaining_hops=0)
        assert mock_post.call_count == 3
