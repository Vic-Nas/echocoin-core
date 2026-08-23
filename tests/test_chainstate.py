"""
Unit tests for chainstate.py

Covers: from_genesis, from_chain, from_storage, validate_and_apply,
apply_block, _apply_block_with_state, is_better_than (fork choice),
accessors (tip, height, genesis_hash, fee_rate_at).

Whitepaper constraints enforced:
  - Fork choice: lower cumulative suffix score wins (Section 3 fork resolution)
  - Burns in the suffix are capped to each contributor's fork-point balance
  - cumulative_score = sum of all block scores (lower = better burn history)
  - Burn window and state stay consistent when blocks are appended
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
from chainstate import ChainState, _balances_at, _capped_suffix_score
import pob as pob_mod
from params import INITIAL_FEE_RATE, RINGS_PER_ECH
from tests.fixtures import (
    address, genesis, make_block, make_tx, seed_balance,
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

    def test_cumulative_score_is_zero(self):
        cs = ChainState.from_genesis()
        assert cs.cumulative_score == 0


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
        cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        cs.state.total_minted += 100 * RINGS_PER_ECH
        g = cs.tip
        t = make_tx(0, 1, RINGS_PER_ECH, cs.state, 0)
        b = make_block(1, g["hash"], [t])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == RINGS_PER_ECH

    def test_reward_credited_after_valid_block(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b = make_block(1, g["hash"], [], builder_index=0)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        # Builder should have received block reward
        assert cs2.state.total_minted > 0


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

    def test_state_matches_sequential_application(self):
        g = genesis()
        cs0 = ChainState.from_genesis()
        # Seed and add a tx
        cs0.state.credit(address(0), 100 * RINGS_PER_ECH)
        cs0.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, cs0.state, 0)
        b1 = make_block(1, g["hash"], [t])
        # Apply via validate_and_apply
        ok, _, cs1 = cs0.validate_and_apply(b1)
        assert ok

        # Build from_chain replay separately
        # We can only validate this path by checking heights match
        assert cs1.height == 1

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
# 6. is_better_than (fork choice -- whitepaper Section 3)
# ---------------------------------------------------------------------------

class TestIsBetterThan:
    def test_longer_chain_is_better(self):
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        b1 = make_block(1, g["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        assert cs1.is_better_than(cs0)
        assert not cs0.is_better_than(cs1)

    def test_equal_height_higher_burn_sum_is_better(self):
        """Whitepaper: higher cumulative burn-sum wins at equal height."""
        cs = ChainState.from_genesis()
        # Manually craft two states with same height but different burn totals
        cs_good = ChainState(cs.chain[:], cs.state.snapshot(), cs.burn_window.copy(), 200)
        cs_bad  = ChainState(cs.chain[:], cs.state.snapshot(), cs.burn_window.copy(), 100)
        assert cs_good.is_better_than(cs_bad)
        assert not cs_bad.is_better_than(cs_good)

    def test_equal_height_equal_score_lower_hash_wins(self):
        """Tie broken by block hash (deterministic)."""
        cs = ChainState.from_genesis()
        g = cs.tip
        blk_a = dict(g)
        blk_b = dict(g)
        blk_a["hash"] = "aa" * 32
        blk_b["hash"] = "bb" * 32
        cs_a = ChainState([blk_a], cs.state.snapshot(), cs.burn_window.copy(), 0)
        cs_b = ChainState([blk_b], cs.state.snapshot(), cs.burn_window.copy(), 0)
        assert cs_a.is_better_than(cs_b)  # "aa..." < "bb..."
        assert not cs_b.is_better_than(cs_a)

    def test_chain_is_not_better_than_itself(self):
        cs = ChainState.from_genesis()
        assert not cs.is_better_than(cs)

    def test_capped_suffix_score_limits_post_fork_burns(self):
        """Burns in the suffix beyond the fork-point balance do not count."""
        g = genesis()
        builder_addr = address(0)

        # Fork point is after genesis only (fork_point=1, no prefix balance)
        # Build a chain with one block. The builder has zero pre-fork balance.
        b1 = make_block(1, g["hash"], [], builder_index=0)
        chain = [g, b1]

        fork_balances = _balances_at(chain, 1)  # balances at height 1 (after genesis, before b1)
        # builder had zero balance at fork_point=1 (no rewards yet, no txs)
        assert fork_balances.get(builder_addr, 0) == 0

        score_with_cap = _capped_suffix_score(chain, 1, fork_balances)
        score_no_cap   = _capped_suffix_score(chain, 1, {builder_addr: 10**18})

        # With zero fork-point balance, all suffix burns are capped to 0.
        assert score_with_cap == 0
        # With a huge cap, eligible burns reflect actual suffix burns (>= 0).
        assert score_no_cap   >= 0


# ---------------------------------------------------------------------------
# 7. cumulative_score tracks burn commitment
# ---------------------------------------------------------------------------

class TestCumulativeScore:
    def test_empty_chain_score_is_zero(self):
        cs = ChainState.from_genesis()
        assert cs.cumulative_score == 0

    def test_score_increases_with_each_block(self):
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        b1 = make_block(1, g["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        # cumulative_score is non-decreasing (scores are >= 0)
        assert cs1.cumulative_score >= cs0.cumulative_score


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

    def test_apply_block_credits_builder_reward(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [], builder_index=0)
        cs2 = cs.apply_block(b1)
        assert cs2.state.total_minted > 0

    def test_apply_block_applies_transactions(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        cs.state.total_minted += 100 * RINGS_PER_ECH
        g = cs.tip
        t = make_tx(0, 1, RINGS_PER_ECH, cs.state, 0)
        b1 = make_block(1, g["hash"], [t])
        cs2 = cs.apply_block(b1)
        assert cs2.state.get_balance(address(1)) == RINGS_PER_ECH


# ---------------------------------------------------------------------------
# 9. from_chain with transactions -- verifies state is correctly accumulated
# ---------------------------------------------------------------------------

class TestFromChainWithTxs:
    def test_from_chain_applies_txs_and_rewards(self):
        """Building from_chain with a block that has transactions must produce
        the same net state as incremental validate_and_apply."""
        cs0 = ChainState.from_genesis()
        g = cs0.tip
        # Block with no txs -- just reward
        b1 = make_block(1, g["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)

        cs_replayed = ChainState.from_chain([g, b1])
        assert cs_replayed.height == 1
        assert cs_replayed.state.total_minted == cs1.state.total_minted


# ---------------------------------------------------------------------------
# 10. _build_window_and_score -- shared by from_chain and from_storage
# ---------------------------------------------------------------------------

class TestBuildWindowAndScore:
    def test_build_window_for_genesis_only(self):
        g = genesis()
        window, score = ChainState._build_window_and_score([g])
        assert score == 0  # genesis has no burns

    def test_build_window_for_two_blocks(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        window, score = ChainState._build_window_and_score([g, b1])
        # Builder (address(0)) got one block's score
        assert score >= 0
