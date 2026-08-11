"""Mempool and candidate list tests: deterministic assembly, censorship resistance."""
import pytest
from helpers import *


def test_add_and_retrieve():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 100, 1, 0, 0)
    ok, h = mp.add(t)
    assert ok
    assert mp.get(h) is not None
    assert mp.size() == 1


def test_duplicate_rejected():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 100, 1, 0, 0)
    mp.add(t)
    ok, reason = mp.add(t)
    assert not ok
    assert reason == "duplicate"


def test_prune_stale_removes_old_fee_height_and_superseded_nonce_but_keeps_queued():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    state = funded_state(addr)

    stale_fee_height = make_signed_tx(sk, pk_hex, addr, to, 10, 1, 0, 0)
    _, h_stale_fh = mp.add(stale_fee_height)

    superseded_nonce = make_signed_tx(sk, pk_hex, addr, to, 10, 1, 10, 0)
    _, h_superseded = mp.add(superseded_nonce)

    queued_future = make_signed_tx(sk, pk_hex, addr, to, 10, 2, 10, 0)
    _, h_queued = mp.add(queued_future)

    state.set_nonce(addr, 1)

    pruned = mp.prune_stale(chain_tip_height=10, state=state)

    assert set(pruned) == {h_stale_fh, h_superseded}
    assert mp.get(h_queued) is not None
    assert mp.size() == 1


def test_all_txs_returns_snapshot():
    """all_txs returns a list copy; mutating it does not affect the mempool."""
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    for i in range(3):
        t = make_signed_tx(sk, pk_hex, addr, to, 10, i + 1, 0, 0)
        mp.add(t)
    snapshot = mp.all_txs()
    assert len(snapshot) == 3
    snapshot.clear()
    assert mp.size() == 3


def test_mempool_no_lock():
    """Mempool has no _lock attribute."""
    mp = mempool_mod.Mempool()
    assert not hasattr(mp, "_lock")


def test_remove_after_inclusion():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 100, 1, 0, 0)
    _, h = mp.add(t)
    mp.remove(h)
    assert mp.size() == 0
    assert mp.get(h) is None


def test_prune_stale_ttl_eviction():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 100, 1, 0, 0)
    _, h = mp.add(t)

    s = funded_state(addr, 1_000_000)
    pruned = mp.prune_stale(chain_tip_height=0, state=s, ttl_seconds=0)
    assert h in pruned
    assert mp.get(h) is None


def test_prune_stale_respects_ttl_not_yet_expired():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 100, 1, 0, 0)
    _, h = mp.add(t)

    s = funded_state(addr, 1_000_000)
    pruned = mp.prune_stale(chain_tip_height=0, state=s, ttl_seconds=3600)
    assert h not in pruned
    assert mp.get(h) is not None


# ---- remaining method coverage ----

def test_get_returns_tx():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 10, 1, 0, 0)
    _, h = mp.add(t)
    assert mp.get(h) is t
    assert mp.get("nonexistent") is None


def test_size_reflects_contents():
    mp = mempool_mod.Mempool()
    assert mp.size() == 0
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    t = make_signed_tx(sk, pk_hex, addr, to, 10, 1, 0, 0)
    mp.add(t)
    assert mp.size() == 1


def test_remove_many():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    hashes = []
    for i in range(3):
        t = make_signed_tx(sk, pk_hex, addr, to, 10, i + 1, 0, 0)
        _, h = mp.add(t)
        hashes.append(h)
    mp.remove_many(hashes[:2])
    assert mp.size() == 1
    assert mp.get(hashes[2]) is not None


def test_get_txs_by_hashes():
    mp = mempool_mod.Mempool()
    sk, _, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    txs = []
    hashes = []
    for i in range(3):
        t = make_signed_tx(sk, pk_hex, addr, to, 10, i + 1, 0, 0)
        _, h = mp.add(t)
        txs.append(t)
        hashes.append(h)
    result = mp.get_txs_by_hashes(hashes[:2])
    assert len(result) == 2
    # Missing hash is skipped
    result2 = mp.get_txs_by_hashes([hashes[0], "missing"])
    assert len(result2) == 1
