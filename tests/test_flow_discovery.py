"""Flow: Peer discovery candidate pipeline.

Covers FLOW.md § Peer Discovery:
  enqueue_candidate → _flush_candidates (probe + rank + admit)
  add_bootstrap_peer (immediate admission on genesis match)
"""
from unittest.mock import patch, MagicMock

import pytest
from helpers import *
from discovery import Discovery
from peerpool import PeerPool


def make_discovery(genesis_hash="aa" * 32, port=8333, pubkey_hex="bb" * 32):
    pool = PeerPool("0.0.0.0", port)
    return Discovery(pool, genesis_hash, port, pubkey_hex), pool


# ---------------------------------------------------------------------------
# enqueue_candidate: basic filtering
# ---------------------------------------------------------------------------

def test_enqueue_valid_address():
    d, pool = make_discovery()
    d.enqueue_candidate("10.0.0.1:8333")
    with d._candidates_lock:
        assert "10.0.0.1:8333" in d._candidates


def test_enqueue_invalid_no_port_ignored():
    d, pool = make_discovery()
    d.enqueue_candidate("10.0.0.1")
    with d._candidates_lock:
        assert len(d._candidates) == 0


def test_enqueue_non_string_ignored():
    d, pool = make_discovery()
    d.enqueue_candidate(12345)
    with d._candidates_lock:
        assert len(d._candidates) == 0


def test_enqueue_is_thread_safe():
    """Multiple threads enqueuing concurrently should not raise."""
    import threading
    d, pool = make_discovery()
    errors = []

    def enqueue(i):
        try:
            d.enqueue_candidate(f"10.0.0.{i}:8333")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=enqueue, args=(i,)) for i in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []


# ---------------------------------------------------------------------------
# _flush_candidates: genesis check, ranking, admission
# ---------------------------------------------------------------------------

def test_flush_rejects_genesis_mismatch():
    d, pool = make_discovery(genesis_hash="aa" * 32)
    d.enqueue_candidate("10.0.0.1:8333")

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"genesis_hash": "bb" * 32, "peers": []}
        return m

    with patch("requests.get", side_effect=fake_get):
        d._flush_candidates()
    assert pool.count() == 0


def test_flush_admits_matching_genesis():
    d, pool = make_discovery(genesis_hash="aa" * 32)
    d.enqueue_candidate("10.0.0.1:8333")

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"genesis_hash": "aa" * 32, "peers": []}
        return m

    with patch("requests.get", side_effect=fake_get):
        d._flush_candidates()
    assert pool.count() == 1


def test_flush_ranks_by_nomination_count():
    """Lightly-nominated candidate is admitted before well-nominated ones."""
    d, pool = make_discovery(genesis_hash="aa" * 32)
    candidates = [f"10.0.0.{i}:8333" for i in range(1, 5)]
    for c in candidates:
        d.enqueue_candidate(c)

    # c1 is nominated by c2, c3, c4 -- well-connected
    # c4 is nominated by nobody -- lightly connected (bridge)
    def fake_get(url, **kwargs):
        addr = url.split("//")[1].split("/")[0]
        m = MagicMock()
        m.status_code = 200
        if "10.0.0.4" in addr:
            peers = []   # c4 sees nobody else in our candidate set
        else:
            peers = ["10.0.0.1:8333"]   # all others nominate c1
        m.json.return_value = {"genesis_hash": "aa" * 32, "peers": peers}
        return m

    admitted = []
    real_add = pool.add
    def recording_add(a):
        admitted.append(a)
        return real_add(a)
    pool.add = recording_add

    with patch("requests.get", side_effect=fake_get):
        d._flush_candidates()

    # c4 (lightly nominated) should appear before c1 (heavily nominated)
    if "10.0.0.4:8333" in admitted and "10.0.0.1:8333" in admitted:
        assert admitted.index("10.0.0.4:8333") < admitted.index("10.0.0.1:8333")


def test_flush_skips_already_known_peers():
    d, pool = make_discovery(genesis_hash="aa" * 32)
    pool.add("10.0.0.1:8333")   # already in pool
    d.enqueue_candidate("10.0.0.1:8333")

    with patch("requests.get") as mock_get:
        d._flush_candidates()
        mock_get.assert_not_called()


def test_flush_skips_unreachable_candidates():
    d, pool = make_discovery(genesis_hash="aa" * 32)
    d.enqueue_candidate("10.0.0.1:8333")

    with patch("requests.get", side_effect=Exception("refused")):
        d._flush_candidates()
    assert pool.count() == 0


# ---------------------------------------------------------------------------
# add_bootstrap_peer: immediate admission
# ---------------------------------------------------------------------------

def test_bootstrap_peer_admitted_on_genesis_match():
    d, pool = make_discovery(genesis_hash="aa" * 32)

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"genesis_hash": "aa" * 32}
        return m

    with patch("requests.get", side_effect=fake_get):
        d.add_bootstrap_peer("10.0.0.1:8333")
    assert pool.count() == 1


def test_bootstrap_peer_rejected_on_genesis_mismatch():
    d, pool = make_discovery(genesis_hash="aa" * 32)

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"genesis_hash": "cc" * 32}
        return m

    with patch("requests.get", side_effect=fake_get):
        d.add_bootstrap_peer("10.0.0.1:8333")
    assert pool.count() == 0


def test_bootstrap_peer_unreachable_does_not_raise():
    d, pool = make_discovery()
    with patch("requests.get", side_effect=Exception("timeout")):
        d.add_bootstrap_peer("10.0.0.1:8333")   # should not raise
    assert pool.count() == 0


def test_bootstrap_peer_invalid_address_ignored():
    d, pool = make_discovery()
    d.add_bootstrap_peer("notanaddress")
    assert pool.count() == 0
