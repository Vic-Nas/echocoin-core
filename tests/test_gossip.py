"""Gossip: tx dedup, dandelion routing shape, broadcast fan-out."""
import threading

from helpers import *

import tx as tx_mod
from gossip import SEEN_TX_CACHE_SIZE, Gossip
from peerpool import PeerPool


def make_gossip():
    pool = PeerPool("0.0.0.0", 8333)
    return Gossip(pool, 8333), pool


# ---- relay_tx dedup ----

def _make_tx(sk, pk_hex, addr):
    outputs = [{"to": addr, "amount": 1_000_000}]
    fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
    return tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)


def test_relay_tx_first_call_not_duplicate():
    gossip, _pool = make_gossip()
    sk, _pk, pk_hex, addr = make_keypair()
    t = _make_tx(sk, pk_hex, addr)
    h = tx_mod.tx_hash(t)
    # No peers, so relay does nothing except mark seen
    gossip.relay_tx(t)
    assert h in gossip._seen_tx

def test_relay_tx_second_call_is_noop():
    gossip, _pool = make_gossip()
    sk, _pk, pk_hex, addr = make_keypair()
    t = _make_tx(sk, pk_hex, addr)
    tx_mod.tx_hash(t)
    gossip.relay_tx(t)
    # Manually clear from cache and relay again — relay_tx should re-gate on seen
    # Actually, relay_tx checks _seen_tx before adding. Test that second call returns early.
    gossip.relay_tx(t)
    assert len(gossip._seen_tx) == 1

# ---- mark_seen return value ----

def test_mark_seen_returns_false_for_new():
    gossip, _ = make_gossip()
    assert gossip.mark_seen("aabbcc") is False

def test_mark_seen_returns_true_for_seen():
    gossip, _ = make_gossip()
    gossip.mark_seen("aabbcc")
    assert gossip.mark_seen("aabbcc") is True

def test_mark_seen_concurrent_only_one_false():
    """Under concurrent access, exactly one thread should see False (new)."""
    gossip, _ = make_gossip()
    results = []
    lock = threading.Lock()
    def try_mark():
        r = gossip.mark_seen("shared-hash")
        with lock:
            results.append(r)
    threads = [threading.Thread(target=try_mark) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count(False) == 1
    assert results.count(True) == 19

# ---- seen_tx cache eviction ----

def test_seen_tx_evicts_oldest_at_capacity():
    gossip, _ = make_gossip()
    # Fill to capacity
    for i in range(SEEN_TX_CACHE_SIZE):
        gossip.mark_seen(f"hash-{i:06d}")
    first = "hash-000000"
    assert first in gossip._seen_tx
    # One more should evict the oldest
    gossip.mark_seen("hash-overflow")
    assert first not in gossip._seen_tx
    assert "hash-overflow" in gossip._seen_tx

# ---- dandelion_send: no peers falls back to broadcast (noop with empty pool) ----

def test_dandelion_with_no_peers_does_not_crash():
    gossip, _pool = make_gossip()
    # Just must not raise
    gossip.dandelion_send({"nonce": 1}, remaining_hops=4)

# ---- broadcast: no peers does nothing ----

def test_broadcast_with_no_peers_does_not_crash():
    gossip, _pool = make_gossip()
    gossip._broadcast("/api/receive_tx", {"type": "tx_fluff", "tx": {}})
