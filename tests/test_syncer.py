"""
Unit tests for syncer.py

Covers: check_and_sync (no peers, peer not ahead, fetch error, success),
_find_fork_point (binary search, shared tip, genesis diverge, HTTP error),
_fetch_chain (single page, pagination, empty, error).

All HTTP calls are mocked -- no network.
"""

import os
import sys
from unittest.mock import MagicMock, patch, call
import json

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from syncer import Syncer, FETCH_CHAIN_MAX_BLOCKS
from tests.fixtures import genesis, make_block


def make_syncer(peers=None):
    pool = MagicMock()
    pool.random.return_value = peers[0] if peers else None
    return Syncer(pool=pool), pool


def make_response(data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    return r


def chain_of(n):
    chain = [genesis()]
    for h in range(1, n):
        chain.append(make_block(h, chain[-1]["hash"], []))
    return chain


# ---------------------------------------------------------------------------
# 1. check_and_sync
# ---------------------------------------------------------------------------

class TestCheckAndSync:
    def test_no_peers_returns_false(self):
        syncer, pool = make_syncer(peers=None)
        result = syncer.check_and_sync(chain_of(3), apply_fn=MagicMock())
        assert result is False

    def test_peer_not_ahead_returns_false(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(5)
        info = {"height": 3}  # peer is behind
        with patch("syncer.requests.get", return_value=make_response(info)):
            result = syncer.check_and_sync(local, apply_fn=MagicMock())
        assert result is False

    def test_info_fetch_error_returns_false(self):
        import requests
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        with patch("syncer.requests.get", side_effect=requests.exceptions.ConnectionError):
            result = syncer.check_and_sync(chain_of(3), apply_fn=MagicMock())
        assert result is False

    def test_info_non_200_returns_false(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        with patch("syncer.requests.get", return_value=make_response({}, status=500)):
            result = syncer.check_and_sync(chain_of(3), apply_fn=MagicMock())
        assert result is False

    def test_fork_point_none_returns_false(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        info_resp = make_response({"height": 5})
        with patch("syncer.requests.get", return_value=info_resp):
            with patch.object(syncer, "_find_fork_point", return_value=None):
                result = syncer.check_and_sync(local, apply_fn=MagicMock())
        assert result is False

    def test_empty_tail_returns_false(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        info_resp = make_response({"height": 5})
        with patch("syncer.requests.get", return_value=info_resp):
            with patch.object(syncer, "_find_fork_point", return_value=0):
                with patch.object(syncer, "_fetch_chain", return_value=None):
                    result = syncer.check_and_sync(local, apply_fn=MagicMock())
        assert result is False

    def test_success_calls_apply_fn_with_full_chain(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        remote_tail = chain_of(5)[1:]  # blocks 1-4
        apply_fn = MagicMock(return_value=True)
        info_resp = make_response({"height": 4})
        with patch("syncer.requests.get", return_value=info_resp):
            with patch.object(syncer, "_find_fork_point", return_value=1):
                with patch.object(syncer, "_fetch_chain", return_value=remote_tail):
                    result = syncer.check_and_sync(local, apply_fn=apply_fn)
        apply_fn.assert_called_once()
        assert result is True


# ---------------------------------------------------------------------------
# 2. _find_fork_point
# ---------------------------------------------------------------------------

class TestFindForkPoint:
    def test_shared_tip_returns_height_plus_one(self):
        """Peer has same chain -> fork point is local tip height + 1 (fetch nothing new)."""
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)  # heights 0,1,2

        def fake_get(url, params=None, timeout=None):
            h = params["from"]
            return make_response([local[h]])

        with patch("syncer.requests.get", side_effect=fake_get):
            fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 3  # all 3 blocks shared, fetch from height 3

    def test_diverged_at_block_1(self):
        """Chains diverge at block 1 -> fork_point = 1."""
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)
        # Remote has different block at height 1
        remote_blk1 = make_block(1, local[0]["hash"], [], builder_index=1)

        def fake_get(url, params=None, timeout=None):
            h = params["from"]
            if h == 0:
                return make_response([local[0]])
            return make_response([remote_blk1])

        with patch("syncer.requests.get", side_effect=fake_get):
            fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 1

    def test_http_error_returns_none(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)
        with patch("syncer.requests.get", return_value=make_response({}, status=500)):
            fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp is None

    def test_connection_error_returns_none(self):
        import requests
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)
        with patch("syncer.requests.get", side_effect=requests.exceptions.ConnectionError):
            fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp is None

    def test_empty_response_returns_none(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        with patch("syncer.requests.get", return_value=make_response([])):
            fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp is None

    def test_genesis_only_shared_returns_one(self):
        """Only genesis is shared -> fork_point = 1."""
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)  # heights 0,1
        remote_blk1 = make_block(1, local[0]["hash"], [], builder_index=99)

        def fake_get(url, params=None, timeout=None):
            h = params["from"]
            if h == 0:
                return make_response([local[0]])  # genesis shared
            return make_response([remote_blk1])   # block 1 differs

        with patch("syncer.requests.get", side_effect=fake_get):
            fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 1


# ---------------------------------------------------------------------------
# 3. _fetch_chain
# ---------------------------------------------------------------------------

class TestFetchChain:
    def test_single_page_returns_blocks(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        blocks = chain_of(3)
        with patch("syncer.requests.get", return_value=make_response(blocks)):
            result = syncer._fetch_chain("1.2.3.4:9000", from_h=0)
        assert result is not None
        assert len(result) == 3

    def test_empty_page_returns_none(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        with patch("syncer.requests.get", return_value=make_response([])):
            result = syncer._fetch_chain("1.2.3.4:9000", from_h=0)
        assert result is None

    def test_http_error_returns_none(self):
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        with patch("syncer.requests.get", return_value=make_response({}, status=500)):
            result = syncer._fetch_chain("1.2.3.4:9000", from_h=0)
        assert result is None

    def test_connection_error_returns_none(self):
        import requests
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        with patch("syncer.requests.get", side_effect=requests.exceptions.ConnectionError):
            result = syncer._fetch_chain("1.2.3.4:9000", from_h=0)
        assert result is None

    def test_pagination_concatenates_pages(self):
        """500-block pages trigger another request; shorter final page stops pagination."""
        syncer, pool = make_syncer(peers=["1.2.3.4:9000"])
        page1 = chain_of(500)       # full page -> expect another request
        page2 = chain_of(10)[:3]    # partial page -> stop

        responses = iter([make_response(page1), make_response(page2)])
        with patch("syncer.requests.get", side_effect=lambda *a, **kw: next(responses)):
            result = syncer._fetch_chain("1.2.3.4:9000", from_h=0)
        assert result is not None
        assert len(result) == 503
