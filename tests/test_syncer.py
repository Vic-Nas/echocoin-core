"""Syncer: height comparison and chain fetch logic."""
from unittest.mock import MagicMock, patch

from helpers import *

from peerpool import PeerPool
from syncer import Syncer


def make_syncer():
    pool = PeerPool("0.0.0.0", 8333)
    pool.add("10.0.0.1:8333")
    return Syncer(pool), pool


def make_chain(length=1):
    """Return a minimal local chain of given length."""
    genesis = block_mod.create_genesis()
    return [genesis] * length   # single-block chain for most tests


# ---- check_and_sync ----

def test_no_peers_returns_false():
    pool = PeerPool("0.0.0.0", 8333)
    syncer = Syncer(pool)
    assert syncer.check_and_sync([block_mod.create_genesis()], lambda c: True) is False


def test_peer_at_same_height_delegates_to_apply_fn():
    """Same height: fetch tail and let apply_fn decide."""
    syncer, _pool = make_syncer()
    genesis = block_mod.create_genesis()
    local_chain = [genesis]

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        if "api/info" in url:
            m.json.return_value = {"height": 0}
        else:
            # Both the fork-point probe and the tail fetch return [genesis]
            m.json.return_value = [genesis]
        return m

    called = []
    with patch("requests.get", side_effect=fake_get):
        result = syncer.check_and_sync(local_chain, lambda c: called.append(c) or True)
    assert result is True


def test_peer_behind_skips_sync():
    syncer, _pool = make_syncer()
    local_chain = make_chain(6)   # local height = 5
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"height": 3}
        called = []
        result = syncer.check_and_sync(local_chain, lambda c: called.append(c) or True)
    assert result is False
    assert called == []


def test_peer_ahead_calls_apply_fn():
    syncer, _pool = make_syncer()
    genesis = block_mod.create_genesis()
    local_chain = [genesis]

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        if "api/info" in url:
            m.json.return_value = {"height": 10}
        else:
            m.json.return_value = [genesis]
        return m

    with patch("requests.get", side_effect=fake_get):
        called = []
        result = syncer.check_and_sync(local_chain, lambda c: called.append(c) or True)
    assert result is True
    assert called != []


def test_peer_unreachable_strikes():
    syncer, pool = make_syncer()
    with patch("requests.get", side_effect=Exception("timeout")):
        result = syncer.check_and_sync(make_chain(), lambda c: True)
    assert result is False
    assert pool._fails.get("10.0.0.1:8333", {}).get("strikes", 0) == 1


def test_info_non_200_returns_false():
    syncer, _pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 503
        result = syncer.check_and_sync(make_chain(), lambda c: True)
    assert result is False


# ---- _fetch_chain ----

def test_fetch_chain_returns_none_on_error():
    syncer, _pool = make_syncer()
    with patch("requests.get", side_effect=Exception("conn refused")):
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result is None


def test_fetch_chain_returns_none_on_404():
    syncer, _pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result is None


def test_fetch_chain_single_page():
    syncer, _pool = make_syncer()
    genesis = block_mod.create_genesis()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [genesis]
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result == [genesis]


def test_fetch_chain_empty_page_stops():
    syncer, _pool = make_syncer()
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = []
        result = syncer._fetch_chain("10.0.0.1:8333")
    assert result is None


# ---- _find_fork_point ----

def test_find_fork_point_common_genesis():
    """Peer shares genesis, so fork point should be height 1."""
    syncer, _pool = make_syncer()
    genesis = block_mod.create_genesis()
    local_chain = [genesis]

    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [genesis]
        result = syncer._find_fork_point("10.0.0.1:8333", local_chain)
    assert result == 1


def test_find_fork_point_network_error():
    syncer, _pool = make_syncer()
    with patch("requests.get", side_effect=Exception("timeout")):
        result = syncer._find_fork_point("10.0.0.1:8333", make_chain())
    assert result is None
