"""PeerPool and Gossip tests: peer management and message dedup."""
from helpers import *
import queue
from peerpool import PeerPool
from gossip import Gossip


def _make_pool():
    return PeerPool("127.0.0.1", 9000)


def _make_gossip():
    pool = _make_pool()
    return Gossip(pool, 9000), pool


def test_add_peer():
    pool = _make_pool()
    assert pool.add("10.0.0.1:8333")
    assert pool.count() == 1


def test_self_not_added():
    pool = _make_pool()
    assert not pool.add("127.0.0.1:9000")
    assert pool.count() == 0


def test_duplicate_not_counted_twice():
    pool = _make_pool()
    pool.add("10.0.0.1:8333")
    pool.add("10.0.0.1:8333")
    assert pool.count() == 1


def test_strike_bans_after_max():
    pool = _make_pool()
    pool.add("10.0.0.1:8333")
    for _ in range(3):
        pool.strike("10.0.0.1:8333")
    assert pool.count() == 0


def test_not_added_when_full():
    from params import MAX_PEERS
    pool = _make_pool()
    for i in range(MAX_PEERS):
        pool.add(f"10.0.{i // 256}.{i % 256}:8333")
    assert not pool.add("10.1.0.1:8333")


def test_duplicate_tx_dedup():
    """Same tx hash marked seen twice only records once."""
    import tx as tx_mod
    gossip, pool = _make_gossip()
    sk, pk, pk_hex, addr = make_keypair()
    st = state_mod.State()
    st.credit(addr, 10_000_000_000)
    _, t = tx_mod.compute_fee_fixed_point(
        addr, pk_hex, [{"to": addr, "amount": 1_000_000}],
        1, 0, 1, sk
    )
    h = tx_mod.tx_hash(t)
    gossip.mark_seen(h)
    gossip.mark_seen(h)
    assert len(gossip._seen_tx) == 1
