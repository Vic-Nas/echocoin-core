"""
Integration tests: tx + state + block interaction

These tests exercise multi-module flows without any network or VDF.
They cover the "happy path" of the block production pipeline as well as
the edge cases documented in the whitepaper.

Flows covered:
  1. Full block with multiple txs validated and applied via chainstate
  2. Fees are credited to the block builder
  3. Mempool pruning after a block is committed
  4. Multi-block chain replay via ChainState.from_chain
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
import tx as tx_mod
import mempool as mempool_mod
from chainstate import ChainState
from params import INITIAL_FEE_RATE, TICKS_PER_LAPSE, SUPPLY_CAP
from tests.fixtures import (
    address, genesis, make_block, make_tx, seed_balance,
)


@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


def get_fee_rate(height):
    return INITIAL_FEE_RATE


# ---------------------------------------------------------------------------
# 1. Full block commit flow
# ---------------------------------------------------------------------------

class TestBlockCommitFlow:
    def test_single_tx_block_commits(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE

        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [confirm, resolve])
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == TICKS_PER_LAPSE
        assert cs2.height == 1

    def test_multi_tx_block_commits(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * TICKS_PER_LAPSE)
        cs.state.credit(address(1), 1000 * TICKS_PER_LAPSE)
        cs.state.total_minted += 2000 * TICKS_PER_LAPSE

        # Confirmation ordering no longer depends on the sender's inner
        # nonce (see tx.sort_key): resolution order is already forced to
        # match confirmation order by the gapless queue rule, so building
        # both confirms straight off the current state is enough.
        c1, r1 = make_tx(0, 2, TICKS_PER_LAPSE, cs.state, 0)
        c2, r2 = make_tx(1, 2, TICKS_PER_LAPSE, cs.state, 0)

        txs = tx_mod.sort_txs([c1, c2]) + [r1, r2]
        b = make_block(1, cs.tip["hash"], txs)
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        # Both txs credited
        assert cs2.state.get_balance(address(2)) >= 2 * TICKS_PER_LAPSE

    def test_duplicate_nonce_in_block_fails(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * TICKS_PER_LAPSE)
        cs.state.total_minted += 1000 * TICKS_PER_LAPSE

        c1, r1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        # Same inner nonce -- replay attack: resolve the same confirmation twice
        b = make_block(1, cs.tip["hash"], [c1, r1, r1])
        ok, err, _ = cs.validate_and_apply(b)
        assert ok is False


# ---------------------------------------------------------------------------
# 2. Fee accounting
# ---------------------------------------------------------------------------

class TestFeeAccounting:
    def test_fees_go_to_resolver_not_builder(self):
        """Fees are escrowed at confirmation and paid to whichever
        resolver's solution lands first -- not to the block builder."""
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * TICKS_PER_LAPSE)
        cs.state.total_minted += 1000 * TICKS_PER_LAPSE

        reward = cs.state.compute_block_reward()
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0, resolver_index=7)
        fee = confirm["fee"]
        b = make_block(1, cs.tip["hash"], [confirm, resolve], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        assert cs2.state.get_balance(address(7)) >= fee
        # Builder gets only the block reward, not the confirmation fee.
        assert cs2.state.get_balance(address(2)) == reward

    def test_builder_receives_full_block_reward(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * TICKS_PER_LAPSE)
        cs.state.total_minted += 1000 * TICKS_PER_LAPSE

        reward = cs.state.compute_block_reward()
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [confirm, resolve], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(2)) == reward


# ---------------------------------------------------------------------------
# 3. Mempool pruning after block commit
# ---------------------------------------------------------------------------

class TestMempoolPruning:
    def test_confirmed_txs_removed_from_mempool(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * TICKS_PER_LAPSE)
        cs.state.total_minted += 1000 * TICKS_PER_LAPSE

        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)

        mp = mempool_mod.Mempool()
        mp.add(confirm)
        mp.add(resolve)
        assert mp.size() == 2

        # Commit block
        b = make_block(1, cs.tip["hash"], [confirm, resolve])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err

        # Remove confirmed txs (simulating node._commit)
        confirmed = {tx_mod.tx_hash(tx) for tx in b["transactions"]}
        mp.remove_many(confirmed)
        assert mp.size() == 0

    def test_unconfirmed_txs_remain_in_mempool(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * TICKS_PER_LAPSE)
        cs.state.credit(address(1), 1000 * TICKS_PER_LAPSE)
        cs.state.total_minted += 2000 * TICKS_PER_LAPSE

        c1, r1 = make_tx(0, 2, TICKS_PER_LAPSE, cs.state, 0)
        c2, r2 = make_tx(1, 2, TICKS_PER_LAPSE, cs.state, 0)

        mp = mempool_mod.Mempool()
        mp.add(c1)
        mp.add(c2)

        # Only c1 gets included in the block
        b = make_block(1, cs.tip["hash"], [c1])
        confirmed = {tx_mod.tx_hash(tx) for tx in b["transactions"]}
        mp.remove_many(confirmed)
        assert mp.size() == 1  # t2 remains


# ---------------------------------------------------------------------------
# 4. Multi-block chain replay (from_chain)
# ---------------------------------------------------------------------------

class TestChainReplay:
    def test_5_block_chain_replays_cleanly(self):
        # Build a chain manually
        cs = ChainState.from_genesis()
        chain = [cs.tip]

        for h in range(1, 6):
            b = make_block(h, chain[-1]["hash"], [])
            _, _, cs = cs.validate_and_apply(b)
            chain.append(b)

        # Replay from scratch
        cs_replayed = ChainState.from_chain(chain)
        assert cs_replayed.height == 5
        assert cs_replayed.tip["hash"] == chain[-1]["hash"]

    def test_replay_matches_incremental_state(self):
        """from_chain replay of an empty-tx chain matches incremental application."""
        cs = ChainState.from_genesis()
        chain = [cs.tip]
        for h in range(1, 4):
            b = make_block(h, chain[-1]["hash"], [])
            _, _, cs = cs.validate_and_apply(b)
            chain.append(b)

        cs_replayed = ChainState.from_chain(chain)
        assert cs_replayed.height == cs.height
        assert cs_replayed.state.total_minted == cs.state.total_minted
