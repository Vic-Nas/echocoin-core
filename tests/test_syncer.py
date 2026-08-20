"""
Unit tests for syncer.py (UDP transport edition)

Covers: check_and_sync (no peers, peer not ahead, fetch error, success),
_find_fork_point (binary search, shared tip, genesis diverge, error),
_fetch_chain (single page, pagination, empty, error).

UDP calls are mocked via udp.request_sync -- no network.
"""

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from syncer import Syncer, FETCH_CHUNK
from tests.fixtures import genesis, make_block


def make_syncer(peers=None):
    pool = MagicMock()
    pool.random.return_value = peers[0] if peers else None
    udp = MagicMock()
    return Syncer(pool=pool, udp=udp), pool, udp


def chain_of(n):
    chain = [genesis()]
    for h in range(1, n):
        chain.append(make_block(h, chain[-1]["hash"], []))
    return chain


def wrap_chain(blocks):
    """Wrap a block list in the SYNC response envelope."""
    return {"genesis": "test", "chain": blocks}


def wrap_info(height, tip_hash=""):
    """Wrap an info response in the SYNC response envelope."""
    return {"genesis": "test", "chain": {"height": height, "tip_hash": tip_hash}}


# ---------------------------------------------------------------------------
# 1. check_and_sync
# ---------------------------------------------------------------------------

class TestCheckAndSync:
    def test_no_peers_returns_false(self):
        syncer, pool, udp = make_syncer(peers=None)
        assert syncer.check_and_sync(chain_of(3), apply_fn=MagicMock()) is False

    def test_info_request_fails_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = None
        assert syncer.check_and_sync(chain_of(3), apply_fn=MagicMock()) is False
        pool.strike.assert_called_once()

    def test_peer_not_ahead_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 2, "tip_hash": ""}
        local = chain_of(5)
        assert syncer.check_and_sync(local, apply_fn=MagicMock()) is False

    def test_fork_point_none_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 10, "tip_hash": ""}
        with patch.object(syncer, "_find_fork_point", return_value=None):
            assert syncer.check_and_sync(chain_of(2), apply_fn=MagicMock()) is False

    def test_empty_tail_returns_false(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.get_info.return_value = {"height": 10, "tip_hash": ""}
        with patch.object(syncer, "_find_fork_point", return_value=0):
            with patch.object(syncer, "_fetch_chain", return_value=None):
                assert syncer.check_and_sync(chain_of(2), apply_fn=MagicMock()) is False

    def test_success_calls_apply_fn(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        remote_tail = chain_of(5)[1:]
        apply_fn = MagicMock(return_value=True)
        udp.get_info.return_value = {"height": 4, "tip_hash": "aa" * 32}
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
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)

        def fake_sync(peer, from_h, to_h, timeout):
            return wrap_chain([local[from_h]])

        udp.request_sync.side_effect = fake_sync
        fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 3

    def test_diverged_at_block_1(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(3)
        remote_blk1 = make_block(1, local[0]["hash"], [], builder_index=99)

        def fake_sync(peer, from_h, to_h, timeout):
            if from_h == 0:
                return wrap_chain([local[0]])
            return wrap_chain([remote_blk1])

        udp.request_sync.side_effect = fake_sync
        fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 1

    def test_request_fails_returns_none(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.request_sync.return_value = None
        fp = syncer._find_fork_point("1.2.3.4:9000", chain_of(3))
        assert fp is None

    def test_empty_chain_in_response_returns_none(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.request_sync.return_value = wrap_chain([])
        fp = syncer._find_fork_point("1.2.3.4:9000", chain_of(2))
        assert fp is None

    def test_genesis_only_shared_returns_one(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        local = chain_of(2)
        remote_blk1 = make_block(1, local[0]["hash"], [], builder_index=99)

        def fake_sync(peer, from_h, to_h, timeout):
            if from_h == 0:
                return wrap_chain([local[0]])
            return wrap_chain([remote_blk1])

        udp.request_sync.side_effect = fake_sync
        fp = syncer._find_fork_point("1.2.3.4:9000", local)
        assert fp == 1


# ---------------------------------------------------------------------------
# 3. _fetch_chain
# ---------------------------------------------------------------------------

class TestFetchChain:
    def test_single_page_returns_blocks(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        blocks = chain_of(3)
        udp.request_sync.return_value = wrap_chain(blocks)
        result = syncer._fetch_chain("1.2.3.4:9000", from_h=0, remote_height=2)
        assert result is not None
        assert len(result) == 3

    def test_none_response_returns_none(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.request_sync.return_value = None
        assert syncer._fetch_chain("1.2.3.4:9000", from_h=0, remote_height=5) is None

    def test_empty_chain_returns_none(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        udp.request_sync.return_value = wrap_chain([])
        assert syncer._fetch_chain("1.2.3.4:9000", from_h=0, remote_height=5) is None

    def test_pagination_concatenates_pages(self):
        syncer, pool, udp = make_syncer(peers=["1.2.3.4:9000"])
        page1 = chain_of(FETCH_CHUNK)
        page2 = chain_of(3)

        responses = iter([wrap_chain(page1), wrap_chain(page2)])
        udp.request_sync.side_effect = lambda *a, **kw: next(responses)
        result = syncer._fetch_chain("1.2.3.4:9000", from_h=0,
                                     remote_height=FETCH_CHUNK + 2)
        assert result is not None
        assert len(result) == FETCH_CHUNK + 3
