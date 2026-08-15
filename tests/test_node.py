"""Node unit tests: internal helpers not covered by flow tests.

The startup, commit, sync, submit_tx, and censorship flows are covered
in test_flow_startup, test_flow_block_cycle, test_flow_sync,
test_flow_tx_lifecycle, and test_flow_security. This file keeps tests
for internal helpers that aren't exercised there.
"""
import os
import queue as _queue
import tempfile
import threading
from unittest.mock import patch

import pytest
from helpers import *


@pytest.fixture
def node_setup():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    yield n, sk, pk, pk_hex, addr, gossip
    teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# _fee_rate_at: rate lookup by height
# ---------------------------------------------------------------------------

def test_fee_rate_at_genesis_height(node_setup):
    n, *_ = node_setup
    rate = n.cs.fee_rate_at(0)
    assert isinstance(rate, int) and rate >= 1


def test_fee_rate_at_out_of_range_returns_none(node_setup):
    n, *_ = node_setup
    assert n.cs.fee_rate_at(999) is None
    assert n.cs.fee_rate_at(-1) is None


# ---------------------------------------------------------------------------
# _update_exclusion_ages: increments age for missing pending txs
# ---------------------------------------------------------------------------

def test_missing_tx_increments_exclusion_age(node_setup):
    n, sk, _pk, pk_hex, addr, _ = node_setup
    n.cs.state.credit(addr, 100_000_000)
    _, _, _, to = make_keypair()
    rate = n.cs.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n.mempool.add(t)
    blk = make_block(n.cs.chain, txs=[])
    n._update_exclusion_ages(blk)
    assert n._exclusion_age.get(h, 0) == 1


def test_full_block_does_not_increment_exclusion_age(node_setup):
    from params import BLOCK_SIZE_LIMIT
    n, sk, _pk, pk_hex, addr, _ = node_setup
    n.cs.state.credit(addr, 100_000_000)
    _, _, _, to = make_keypair()
    rate = n.cs.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n.mempool.add(t)
    blk = make_block(n.cs.chain, txs=[])
    blk["tx_bytes"] = BLOCK_SIZE_LIMIT  # simulate full block
    n._update_exclusion_ages(blk)
    assert n._exclusion_age.get(h, 0) == 0


def test_included_tx_clears_exclusion_age(node_setup):
    n, sk, _pk, pk_hex, addr, _ = node_setup
    n.cs.state.credit(addr, 100_000_000)
    _, _, _, to = make_keypair()
    rate = n.cs.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n._exclusion_age[h] = 5
    blk = make_block(n.cs.chain, txs=[t])
    n._update_exclusion_ages(blk)
    assert h not in n._exclusion_age


# ---------------------------------------------------------------------------
# _apply_chain edge cases
# ---------------------------------------------------------------------------

def test_apply_chain_genesis_mismatch_rejected(node_setup):
    n, *_ = node_setup
    # Build a longer chain with a bad genesis so is_better_than passes
    # but genesis check fails
    fake = block_mod.create_genesis()
    fake["hash"] = "ff" * 32
    # Pad to be longer than local chain
    chain = [fake] + make_chain(3)[1:]
    ok, err = n.apply_better_chain(chain)
    assert not ok and "genesis" in err.lower()


def test_apply_chain_invalid_block_rejected(node_setup):
    n, *_ = node_setup
    chain = make_chain(3)
    chain[2]["previous_hash"] = "00" * 32
    chain[2]["hash"] = block_mod.block_hash(chain[2])
    with patch("vdf.verify", return_value=True):
        ok, err = n.apply_better_chain(chain)
    assert not ok and "invalid block at 2" in err


# ---------------------------------------------------------------------------
# _rebuild_state
# ---------------------------------------------------------------------------

def test_rebuild_state_credits_block_reward(node_setup):
    n, _, _, _, addr, _ = node_setup
    from chainstate import ChainState
    blk = make_block(n.cs.chain, builder_addr=addr)
    new_cs = ChainState.from_chain(n.cs.chain + [blk])
    assert new_cs.state.get_balance(addr) > 0
    assert new_cs.state.total_minted > 0


# ---------------------------------------------------------------------------
# NodeView
# ---------------------------------------------------------------------------

def test_node_view_stats_points_empty_at_genesis(node_setup):
    n, *_ = node_setup
    assert n.view.stats_points == []


def test_node_view_stats_points_populated_after_block(node_setup):
    n, _, _, _, addr, _ = node_setup
    blk = make_block(n.cs.chain, builder_addr=addr)
    blk["tx_bytes"] = 0
    with patch("vdf.verify", return_value=True):
        n._commit(blk)
    assert len(n.view.stats_points) > 0


# ---------------------------------------------------------------------------
# chain_reloaded_from_disk
# ---------------------------------------------------------------------------

def test_chain_reloaded_from_disk():
    from node import Node
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    blk = make_block(n.cs.chain)
    blk["tx_bytes"] = 0
    n.storage.save_block(blk)
    n2 = Node(keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              _queue.Queue(), db_path=dbfile)
    try:
        assert len(n2.cs.chain) == 2
        assert n2.cs.chain[1]["height"] == 1
    finally:
        n.storage.close()
        n2.storage.close()
        os.unlink(dbfile)
        os.unlink(keyfile)
