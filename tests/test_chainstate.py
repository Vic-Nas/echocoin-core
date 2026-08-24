"""
Unit tests for chainstate.py

Covers: from_genesis, from_chain, from_storage, validate_and_apply,
apply_block, _apply_block_with_state, is_better_than (fork choice),
accessors (tip, height, genesis_hash, fee_rate_at).

Fork choice: most cumulative proven VDF work wins; tip hash breaks ties.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
from chainstate import ChainState
import pob as pob_mod
from params import INITIAL_FEE_RATE, EMBERS_PER_SCH
from tests.fixtures import (
    address, genesis, make_block, make_burn_tx, make_tx, seed_balance,
)


@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


def fee_rate_at(height):
    return INITIAL_FEE_RATE


# ---------------------------------------------------------------------------
# 1. from_genesis
# ---------------------------------------------------------------------------

class TestFromGenesis:
    def test_creates_chain_with_one_block(self):
        cs = ChainState.from_genesis()
        assert len(cs.chain) == 1

    def test_height_is_zero(self):
        cs = ChainState.from_genesis()
        assert cs.height == 0

    def test_tip_is_genesis(self):
        cs = ChainState.from_genesis()
        g = genesis()
        assert cs.tip["hash"] == g["hash"]

    def test_state_is_empty(self):
        cs = ChainState.from_genesis()
        assert cs.state.total_minted == 0
        assert cs.state.total_burnt == 0


# ---------------------------------------------------------------------------
# 2. Accessors
# ---------------------------------------------------------------------------

class TestAccessors:
    def test_genesis_hash_property(self):
        cs = ChainState.from_genesis()
        assert cs.genesis_hash == cs.chain[0]["hash"]

    def test_fee_rate_at_known_height(self):
        cs = ChainState.from_genesis()
        rate = cs.fee_rate_at(0)
        assert rate == INITIAL_FEE_RATE

    def test_fee_rate_at_unknown_height_returns_none(self):
        cs = ChainState.from_genesis()
        assert cs.fee_rate_at(999) is None

    def test_fee_rate_at_negative_returns_none(self):
        cs = ChainState.from_genesis()
        assert cs.fee_rate_at(-1) is None


# ---------------------------------------------------------------------------
# 3. validate_and_apply
# ---------------------------------------------------------------------------

class TestValidateAndApply:
    def test_valid_block_appends_chain(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.height == 1

    def test_valid_block_does_not_mutate_original(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [])
        cs.validate_and_apply(b)
        assert cs.height == 0  # original unchanged

    def test_invalid_block_returns_original(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [])
        b["height"] = 999
        b["hash"] = block_mod.block_hash(b)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is False
        assert cs2 is cs  # same object returned

    def test_block_with_tx_updates_state(self):
        cs = ChainState.from_genesis()
        # Seed balance directly into state for simplicity
        cs.state.credit(address(0), 100 * EMBERS_PER_SCH)
        cs.state.total_minted += 100 * EMBERS_PER_SCH
        g = cs.tip
        t = make_tx(0, 1, EMBERS_PER_SCH, cs.state, 0)
        b = make_block(1, g["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == EMBERS_PER_SCH

    def test_fees_credited_to_builder_after_valid_block(self):
        from params import BUILDER_REWARD_SHARE

        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * EMBERS_PER_SCH)
        cs.state.total_minted += 100 * EMBERS_PER_SCH
        g = cs.tip
        reward = cs.state.compute_block_reward()
        t = make_tx(0, 1, EMBERS_PER_SCH, cs.state, 0)
        b = make_block(1, g["hash"], [t], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        # Builder receives the fee plus its fixed floor share of the reward
        expected = t["fee"] + int(reward * BUILDER_REWARD_SHARE)
        assert cs2.state.get_balance(address(2)) == expected


# ---------------------------------------------------------------------------
# 3b. Builder reward floor (unconditional, independent of burn activity)
# ---------------------------------------------------------------------------

class TestBuilderRewardFloor:
    def test_builder_earns_floor_share_with_no_burns_and_no_txs(self):
        cs = ChainState.from_genesis()
        b = make_block(1, cs.tip["hash"], [], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(2)) > 0

    def test_floor_share_is_constant_regardless_of_burns(self):
        """The builder's floor share is the same fraction of the reward
        whether or not anyone burned in the window -- no incentive to
        exclude burn transactions."""
        from params import BUILDER_REWARD_SHARE

        cs_no_burn = ChainState.from_genesis()
        reward = cs_no_burn.state.compute_block_reward()
        b1 = make_block(1, cs_no_burn.tip["hash"], [], builder_index=2)
        _, _, cs1 = cs_no_burn.validate_and_apply(b1)
        floor_no_burn = cs1.state.get_balance(address(2))
        assert floor_no_burn == int(reward * BUILDER_REWARD_SHARE)

        cs_burn = ChainState.from_genesis()
        cs_burn.state.credit(address(0), 1000 * EMBERS_PER_SCH)
        t = make_burn_tx(0, 10 * EMBERS_PER_SCH, cs_burn.state, 0)
        # The intentional burn increases total_burnt, which bumps can_mint
        # (and therefore the reward) -- compute it post-tx to match what
        # _apply_builder_reward actually sees.
        post_tx = cs_burn.state.snapshot()
        post_tx.apply_tx(t)
        reward2 = post_tx.compute_block_reward()

        b2 = make_block(1, cs_burn.tip["hash"], [t], builder_index=2)
        ok, err, cs2 = cs_burn.validate_and_apply(b2)
        assert ok is True, err
        floor_with_burn = int(reward2 * BUILDER_REWARD_SHARE)
        # Builder (not the burner) gets exactly its fee plus the fixed floor
        # share -- the same fraction of the reward as the no-burn case above.
        assert cs2.state.get_balance(address(2)) == t["fee"] + floor_with_burn


# ---------------------------------------------------------------------------
# 4. from_chain (replay)
# ---------------------------------------------------------------------------

class TestFromChain:
    def test_from_genesis_only(self):
        g = genesis()
        cs = ChainState.from_chain([g])
        assert cs.height == 0

    def test_from_two_blocks(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        cs = ChainState.from_chain([g, b1])
        assert cs.height == 1

    def test_genesis_hash_preserved_in_chain(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        cs = ChainState.from_chain([g, b1])
        assert cs.genesis_hash == g["hash"]


# ---------------------------------------------------------------------------
# 5. from_storage
# ---------------------------------------------------------------------------

class TestFromStorage:
    def test_from_storage_uses_provided_state(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        s = state_mod.State()
        s.credit(address(99), 999_999)
        cs = ChainState.from_storage([g, b1], s)
        # The state provided is used directly
        assert cs.state.get_balance(address(99)) == 999_999

    def test_from_storage_builds_burn_window(self):
        g = genesis()
        cs = ChainState.from_storage([g], state_mod.State())
        assert cs.burn_window is not None


# ---------------------------------------------------------------------------
# 6. is_better_than (fork choice: cumulative proven work)
# ---------------------------------------------------------------------------

class TestIsBetterThan:
    def test_longer_chain_is_better(self):
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        b1 = make_block(1, g["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        assert cs1.is_better_than(cs0)
        assert not cs0.is_better_than(cs1)

    def test_equal_height_lower_hash_wins(self):
        """Tie broken by block hash (deterministic)."""
        cs = ChainState.from_genesis()
        g = cs.tip
        blk_a = dict(g)
        blk_b = dict(g)
        blk_a["hash"] = "aa" * 32
        blk_b["hash"] = "bb" * 32
        cs_a = ChainState([blk_a], cs.state.snapshot(), cs.burn_window.copy())
        cs_b = ChainState([blk_b], cs.state.snapshot(), cs.burn_window.copy())
        assert cs_a.is_better_than(cs_b)  # "aa..." < "bb..."
        assert not cs_b.is_better_than(cs_a)

    def test_chain_is_not_better_than_itself(self):
        cs = ChainState.from_genesis()
        assert not cs.is_better_than(cs)

    def test_two_block_chain_beats_one_block_chain(self):
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        _, _, cs2 = cs1.validate_and_apply(b2)
        assert cs2.is_better_than(cs1)
        assert not cs1.is_better_than(cs2)

    def test_fewer_blocks_with_more_proven_iterations_wins(self):
        """Fork choice weighs cumulative proven VDF work, not raw block
        count: a shorter chain whose blocks each proved more iterations
        can outweigh a longer chain of cheaper blocks."""
        cs = ChainState.from_genesis()
        heavy = ChainState(cs.chain, cs.state, cs.burn_window,
                            cumulative_iterations=1_000_000)
        light = ChainState(cs.chain + [make_block(1, cs.tip["hash"], [])],
                            cs.state, cs.burn_window,
                            cumulative_iterations=500_000)
        assert heavy.is_better_than(light)
        assert not light.is_better_than(heavy)


# ---------------------------------------------------------------------------
# 8. apply_block (direct test -- not via validate_and_apply)
# ---------------------------------------------------------------------------

class TestApplyBlock:
    def test_apply_block_increments_height(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [])
        cs2 = cs.apply_block(b1)
        assert cs2.height == 1

    def test_apply_block_does_not_mutate_original(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [])
        cs.apply_block(b1)
        assert cs.height == 0

    def test_apply_block_applies_transactions(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * EMBERS_PER_SCH)
        cs.state.total_minted += 100 * EMBERS_PER_SCH
        g = cs.tip
        t = make_tx(0, 1, EMBERS_PER_SCH, cs.state, 0)
        b1 = make_block(1, g["hash"], [t])
        cs2 = cs.apply_block(b1)
        assert cs2.state.get_balance(address(1)) == EMBERS_PER_SCH


# ---------------------------------------------------------------------------
# 9. from_chain with transactions -- verifies state is correctly accumulated
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 10. _build_window -- shared by from_storage
# ---------------------------------------------------------------------------

class TestBuildWindow:
    def test_build_window_for_genesis_only(self):
        g = genesis()
        window = ChainState._build_window([g])
        assert window.sender_totals() == {}

    def test_build_window_for_two_blocks(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        window = ChainState._build_window([g, b1])
        assert window.sender_totals() == {}
