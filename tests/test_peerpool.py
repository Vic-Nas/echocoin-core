"""PeerPool: exhaustive tests for the peer address store."""
import time
import threading
import pytest
from helpers import *
from peerpool import PeerPool
from params import MAX_PEERS


def make_pool():
    return PeerPool("0.0.0.0", 8333)


# ---- self-detection ----






# ---- add / count ----

def test_add_new_peer():
    pool = make_pool()
    assert pool.add("10.0.0.1:8333") is True

def test_add_returns_false_on_duplicate():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    assert pool.add("10.0.0.1:8333") is False

def test_count_reflects_unique_peers():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    pool.add("10.0.0.2:8333")
    pool.add("10.0.0.1:8333")  # dup
    assert pool.count() == 2

def test_max_peers_enforced():
    pool = make_pool()
    for i in range(MAX_PEERS):
        pool.add(f"10.0.{i // 256}.{i % 256}:8333")
    assert pool.count() == MAX_PEERS
    assert pool.add("10.1.0.1:9999") is False
    assert pool.count() == MAX_PEERS

# ---- strike / ban ----

def test_strike_increments():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    pool.strike("10.0.0.1:8333")
    assert pool._fails["10.0.0.1:8333"]["strikes"] == 1

def test_three_strikes_bans():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    for _ in range(3):
        pool.strike("10.0.0.1:8333")
    # Peer is removed from active pool but retained in _fails with a cooldown.
    assert pool.count() == 0
    assert "10.0.0.1:8333" in pool._fails   # still tracked for cooldown

def test_cooldown_blocks_re_add():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    pool.strike("10.0.0.1:8333")
    # Should be in cooldown now; add should be blocked
    assert pool.add("10.0.0.1:8333") is False

def test_touch_clears_strikes():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    pool.strike("10.0.0.1:8333")
    pool.touch("10.0.0.1:8333")
    assert "10.0.0.1:8333" not in pool._fails

# ---- get_all / random ----

def test_get_all_excludes_cooldown():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    pool.add("10.0.0.2:8333")
    pool.strike("10.0.0.2:8333")  # puts 10.0.0.2 on cooldown
    result = pool.get_all()
    assert "10.0.0.1:8333" in result
    assert "10.0.0.2:8333" not in result

def test_random_returns_none_when_empty():
    pool = make_pool()
    assert pool.random() is None

def test_random_returns_peer():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    assert pool.random() == "10.0.0.1:8333"

# ---- eviction ----

def test_evict_stale_removes_old():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    # Backdate last_seen to force eviction
    with pool._lock:
        pool._peers["10.0.0.1:8333"] = time.time() - 400
    pool.evict_stale()
    assert pool.count() == 0

def test_evict_keeps_fresh():
    pool = make_pool()
    pool.add("10.0.0.1:8333")
    pool.evict_stale()
    assert pool.count() == 1

# ---- thread safety ----

def test_concurrent_adds_no_crash():
    pool = make_pool()
    errors = []
    def add_some(start):
        for i in range(20):
            try:
                pool.add(f"10.{start}.0.{i}:8333")
            except Exception as e:
                errors.append(e)
    threads = [threading.Thread(target=add_some, args=(n,)) for n in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors


