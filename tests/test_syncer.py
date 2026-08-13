"""Syncer: height comparison and chain fetch logic."""
import pytest
from unittest.mock import patch, MagicMock
from helpers import *
from peerpool import PeerPool
from syncer import Syncer


def make_syncer():
    pool = PeerPool("0.0.0.0", 8333)
    pool.add("10.0.0.1:8333")
    return Syncer(pool), pool


# ---- check_and_sync ----

def test_no_peers_returns_false():
    pool = PeerPool("0.0.0.0", 8333)
    syncer = Syncer(pool)
    assert syncer.check_and_sync(5, "aaaa", lambda c: True) is False

def test_peer_at_same_height_same_hash_skips_sync():
    """Same height and same tip hash: no sync needed."""
    syncer, pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"height": 5, "tip_hash": "aabb"}
        called = []
        result = syncer.check_and_sync(5, "aabb", lambda c: called.append(c) or True)
    assert result is False
    assert called == []


def test_peer_at_same_height_lower_hash_syncs():
    """Same height but peer has lower tip hash: should sync."""
    syncer, pool = make_syncer()
    genesis = block_mod.create_genesis()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"height": 0, "tip_hash": "aaaa"}
        mock_get.return_value.json.side_effect = [
            {"height": 0, "tip_hash": "aaaa"},
            [genesis],
            [],
        ]
        called = []
        result = syncer.check_and_sync(0, "ffff", lambda c: called.append(c) or True)
    assert result is True

def test_peer_behind_skips_sync():
    syncer, pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"height": 3, "tip_hash": "aaaa"}
        called = []
        result = syncer.check_and_sync(5, "aaaa", lambda c: called.append(c) or True)
    assert result is False
    assert called == []

def test_peer_ahead_calls_apply_fn():
    syncer, pool = make_syncer()
    genesis = block_mod.create_genesis()
    fake_chain = [genesis]
    def fake_get(url, **kwargs):
        m = MagicMock()
        if "api/info" in url:
            m.status_code = 200
            m.json.return_value = {"height": 10}
        else:
            m.status_code = 200
            m.json.return_value = fake_chain
        return m
    with patch("requests.get", side_effect=fake_get):
        called = []
        result = syncer.check_and_sync(0, "ffff", lambda c: called.append(c) or True)
    assert result is True
    assert called == [fake_chain]

def test_peer_unreachable_strikes():
    syncer, pool = make_syncer()
    with patch("requests.get", side_effect=Exception("timeout")):
        result = syncer.check_and_sync(0, "aaaa", lambda c: True)
    assert result is False
    assert pool._fails.get("10.0.0.1:8333", {}).get("strikes", 0) == 1

def test_info_non_200_returns_false():
    syncer, pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 503
        result = syncer.check_and_sync(0, "aaaa", lambda c: True)
    assert result is False

# ---- fetch_chain_from pagination ----

def test_fetch_chain_returns_none_on_error():
    syncer, pool = make_syncer()
    with patch("requests.get", side_effect=Exception("conn refused")):
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result is None

def test_fetch_chain_returns_none_on_404():
    syncer, pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result is None

def test_fetch_chain_single_page():
    syncer, pool = make_syncer()
    genesis = block_mod.create_genesis()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        # Return a short page (< 500) so pagination stops after one call
        mock_get.return_value.json.return_value = [genesis]
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result == [genesis]

def test_fetch_chain_empty_page_stops():
    syncer, pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = []
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result is None
