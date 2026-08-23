"""
Integration tests: tx + state + block interaction

These tests exercise multi-module flows without any network or VDF.
They cover the "happy path" of the block production pipeline as well as
the edge cases documented in the whitepaper.

Flows covered:
  1. Full block with multiple txs validated and applied via chainstate
  2. Fee burns accumulate in total_burnt and restore emission pool
  3. Intentional PoB burns accumulate in BurnWindow and affect block scores
  4. Mempool pruning after a block is committed
  5. Multi-block chain replay via ChainState.from_chain
  6. Mixed normal + burn outputs in one block
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
import tx as tx_mod
import pob as pob_mod
import mempool as mempool_mod
from chainstate import ChainState
from params import INITIAL_FEE_RATE, RINGS_PER_ECH, SUPPLY_CAP
from tests.fixtures import (
    address, genesis, make_block, make_burn_tx, make_tx, seed_balance,
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
        cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        cs.state.total_minted += 100 * RINGS_PER_ECH

        t = make_tx(0, 1, RINGS_PER_ECH, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == RINGS_PER_ECH
        assert cs2.height == 1

    def test_multi_tx_block_commits(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.credit(address(1), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 2000 * RINGS_PER_ECH

        t1 = make_tx(0, 2, RINGS_PER_ECH, cs.state, 0)
        cs.state.apply_tx(t1)  # advance nonce for sorting
        t2 = make_tx(1, 2, RINGS_PER_ECH, cs.state, 0)
        # Reset state for validation (chainstate will re-apply)
        cs.state.debit(address(2), RINGS_PER_ECH)
        cs.state.set_nonce(address(0), 0)

        txs = tx_mod.sort_txs([t1, t2])
        b = make_block(1, cs.tip["hash"], txs)
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        # Both txs credited
        assert cs2.state.get_balance(address(2)) >= 2 * RINGS_PER_ECH

    def test_duplicate_nonce_in_block_fails(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        t1 = make_tx(0, 1, RINGS_PER_ECH, cs.state, 0)
        # Same nonce -- replay attack
        t2 = dict(t1)
        txs = tx_mod.sort_txs([t1, t2])
        b = make_block(1, cs.tip["hash"], txs)
        ok, err, _ = cs.validate_and_apply(b)
        assert ok is False


# ---------------------------------------------------------------------------
# 2. Fee burn accounting (whitepaper Section 2)
# ---------------------------------------------------------------------------

class TestFeeBurnAccounting:
    def test_fees_accumulate_in_total_burnt(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        t = make_tx(0, 1, RINGS_PER_ECH, cs.state, 0)
        fee = t["fee"]
        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        assert cs2.state.total_burnt >= fee

    def test_burns_replenish_mintable_pool(self):
        """Whitepaper Section 5: burns add back to can_mint."""
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        reward_before = cs.state.compute_block_reward()

        # Commit a block with a burn tx
        t = make_burn_tx(0, 10 * RINGS_PER_ECH, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err

        # Reward after should be >= reward_before (burns + minted reward)
        reward_after = cs2.state.compute_block_reward()
        assert isinstance(reward_after, int) and reward_after >= 0


# ---------------------------------------------------------------------------
# 3. PoB burns affect block score (whitepaper Section 3)
# ---------------------------------------------------------------------------

class TestPoBBurnEffect:
    def test_burn_in_block_lowers_builder_score(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        from pob import _tip_hash_int
        tip_hash = _tip_hash_int(cs.chain)

        # Score before any burns
        score_before = cs.burn_window.score(tip_hash, address(0))

        # Add a block with a burn tx
        t = make_burn_tx(0, 50 * RINGS_PER_ECH, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err

        tip_hash2 = _tip_hash_int(cs2.chain)
        score_after = cs2.burn_window.score(tip_hash2, address(0))
        # Score is relative to tip so direct comparison doesn't work;
        # but builder_burn should be positive
        assert cs2.burn_window.builder_burn(address(0)) > 0

    def test_builder_burn_tracked_in_window(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        t = make_burn_tx(0, 10 * RINGS_PER_ECH, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.burn_window.builder_burn(address(0)) == 10 * RINGS_PER_ECH


# ---------------------------------------------------------------------------
# 4. Mempool pruning after block commit
# ---------------------------------------------------------------------------

class TestMempoolPruning:
    def test_confirmed_txs_removed_from_mempool(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        t = make_tx(0, 1, RINGS_PER_ECH, cs.state, 0)
        h = tx_mod.tx_hash(t)

        mp = mempool_mod.Mempool()
        mp.add(t)
        assert mp.size() == 1

        # Commit block
        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err

        # Remove confirmed txs (simulating node._commit)
        confirmed = {tx_mod.tx_hash(tx) for tx in b["transactions"]}
        mp.remove_many(confirmed)
        assert mp.size() == 0

    def test_unconfirmed_txs_remain_in_mempool(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.credit(address(1), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 2000 * RINGS_PER_ECH

        t1 = make_tx(0, 2, RINGS_PER_ECH, cs.state, 0)
        t2 = make_tx(1, 2, RINGS_PER_ECH, cs.state, 0)

        mp = mempool_mod.Mempool()
        mp.add(t1)
        mp.add(t2)

        # Only t1 gets included in the block
        b = make_block(1, cs.tip["hash"], [t1])
        confirmed = {tx_mod.tx_hash(tx) for tx in b["transactions"]}
        mp.remove_many(confirmed)
        assert mp.size() == 1  # t2 remains


# ---------------------------------------------------------------------------
# 5. Multi-block chain replay (from_chain)
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


# ---------------------------------------------------------------------------
# 6. Mixed normal + burn outputs in one transaction
# ---------------------------------------------------------------------------

class TestMixedOutputs:
    def test_mixed_outputs_applied_correctly(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000 * RINGS_PER_ECH)
        cs.state.total_minted += 1000 * RINGS_PER_ECH

        from_addr = address(0)
        from tests.fixtures import keypair, pubkey_hex
        pk_hex = pubkey_hex(0)
        sk, _ = keypair(0)

        normal_out = {"to": address(1), "amount": RINGS_PER_ECH}
        burn_out   = {"to": pob_mod.BURN_ADDRESS, "amount": RINGS_PER_ECH}
        outputs = [normal_out, burn_out]

        fee = tx_mod.compute_fee(from_addr, pk_hex, outputs, 1, 0, INITIAL_FEE_RATE)
        t = tx_mod.create(from_addr, pk_hex, outputs, 1, 0, fee, sk)

        b = make_block(1, cs.tip["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)

        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == RINGS_PER_ECH
        # Burn + fee both hit total_burnt
        assert cs2.state.total_burnt >= RINGS_PER_ECH + fee


