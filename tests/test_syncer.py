"""Syncer unit tests: fetch primitives not covered by test_flow_sync.

Fork-point binary search, tail-only fetch, and peer comparison are in
test_flow_sync. This file keeps the low-level _fetch_chain and edge cases.
"""
from unittest.mock import MagicMock, patch
from helpers import *
from peerpool import PeerPool
from syncer import Syncer


def make_syncer():
    pool = PeerPool("0.0.0.0", 8333)
    pool.add("10.0.0.1:8333")
    return Syncer(pool), pool


# ---------------------------------------------------------------------------
# No peers
# ---------------------------------------------------------------------------

def test_no_peers_returns_false():
    pool = PeerPool("0.0.0.0", 8333)
    syncer = Syncer(pool)
    assert syncer.check_and_sync([block_mod.create_genesis()], lambda c: True) is False


# ---------------------------------------------------------------------------
# info endpoint failures
# ---------------------------------------------------------------------------

def test_info_non_200_returns_false():
    syncer, _ = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 503
        result = syncer.check_and_sync(make_chain(1), lambda c: True)
    assert result is False


# ---------------------------------------------------------------------------
# _fetch_chain primitives
# ---------------------------------------------------------------------------

def test_fetch_chain_returns_none_on_error():
    syncer, _ = make_syncer()
    with patch("requests.get", side_effect=Exception("conn refused")):
        assert syncer._fetch_chain("10.0.0.1:8333") is None


def test_fetch_chain_returns_none_on_404():
    syncer, _ = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        assert syncer._fetch_chain("10.0.0.1:8333") is None


def test_fetch_chain_single_page():
    syncer, _ = make_syncer()
    genesis = block_mod.create_genesis()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [genesis]
        assert syncer._fetch_chain("10.0.0.1:8333") == [genesis]


def test_fetch_chain_empty_page_returns_none():
    syncer, _ = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = []
        assert syncer._fetch_chain("10.0.0.1:8333") is None


# ---------------------------------------------------------------------------
# _find_fork_point: network error
# ---------------------------------------------------------------------------

def test_find_fork_point_network_error_returns_none():
    syncer, _ = make_syncer()
    with patch("requests.get", side_effect=Exception("timeout")):
        assert syncer._find_fork_point("10.0.0.1:8333", make_chain(1)) is None
