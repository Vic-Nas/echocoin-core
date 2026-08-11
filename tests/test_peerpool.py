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

def test_self_0000_rejected():
    pool = make_pool()
    assert not pool.add("0.0.0.0:8333")

def test_self_loopback_rejected():
    pool = make_pool()
    assert not pool.add("127.0.0.1:8333")

def test_self_lan_ip_rejected():
    pool = make_pool()
    own_ip = pool._detect_lan_ip()
    assert not pool.add(f"{own_ip}:8333")

def test_upnp_ip_rejected_after_set():
    pool = make_pool()
    pool.set_upnp_ip("1.2.3.4")
    assert not pool.add("1.2.3.4:8333")

def test_different_port_same_ip_not_self():
    pool = make_pool()
    assert pool.add("127.0.0.1:9999")

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
    assert pool.count() == 0
    assert "10.0.0.1:8333" not in pool._fails

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


# ---- thread safety on set_upnp_ip ----

def test_set_upnp_ip_is_self():
    """After set_upnp_ip, the UPnP address must be rejected as self."""
    pool = make_pool()
    pool.set_upnp_ip("5.6.7.8")
    assert not pool.add("5.6.7.8:8333")

def test_set_upnp_ip_concurrent_no_crash():
    """Concurrent set_upnp_ip and is_self calls must not crash."""
    import time
    pool = make_pool()
    errors = []
    def set_ip():
        for i in range(50):
            try:
                pool.set_upnp_ip(f"10.0.0.{i % 256}")
            except Exception as e:
                errors.append(e)
    def check_self():
        for _ in range(100):
            try:
                pool.is_self("10.0.0.1:8333")
            except Exception as e:
                errors.append(e)
    t1 = threading.Thread(target=set_ip)
    t2 = threading.Thread(target=check_self)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert not errors


# ---- add() self-check is atomic with the lock ----

def test_add_checks_own_inside_lock():
    """add() must refuse the node's own address even without a prior is_self() call."""
    pool = make_pool()
    # Directly call add with the loopback address (always in _own)
    assert pool.add(f"127.0.0.1:{pool._port}") is False

def test_add_refuses_upnp_ip_set_before_add():
    pool = make_pool()
    pool.set_upnp_ip("9.9.9.9")
    assert pool.add(f"9.9.9.9:{pool._port}") is False
