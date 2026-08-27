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
import tx as tx_mod
import state as state_mod
from chainstate import ChainState, TxQueue
from params import INITIAL_FEE_RATE, TICKS_PER_LAPSE
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
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b = make_block(1, g["hash"], [confirm, resolve])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(1)) == TICKS_PER_LAPSE

    def test_confirmation_with_wrong_iterations_rejected(self, monkeypatch):
        """A confirmation's recorded iterations must match what the chain
        actually requires (timelock.get_timelock_iterations) -- it is not
        the sender's to choose, the same way a block's vdf_iterations must
        match block.get_vdf_iterations. Signed while the chain expected one
        difficulty, then the chain's expectation moves before validation --
        e.g. a late-arriving confirmation from just before a bump."""
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b = make_block(1, g["hash"], [confirm, resolve])

        import timelock as timelock_mod
        monkeypatch.setattr(timelock_mod, "TIMELOCK_ITERATIONS",
                            confirm["iterations"] + 1)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is False
        assert "iterations" in err

    def test_resolver_credited_the_fee_after_valid_block(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0, resolver_index=7)
        b = make_block(1, g["hash"], [confirm, resolve], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        # The resolver -- not the builder -- receives the confirmation's fee.
        assert cs2.state.get_balance(address(7)) == confirm["fee"]


# ---------------------------------------------------------------------------
# 3b. Builder reward (unconditional, full reward, no split)
# ---------------------------------------------------------------------------

class TestBuilderReward:
    def test_builder_earns_full_reward_with_no_txs(self):
        cs = ChainState.from_genesis()
        reward = cs.state.compute_block_reward()
        b = make_block(1, cs.tip["hash"], [], builder_index=2)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        assert cs2.state.get_balance(address(2)) == reward

    def test_reward_is_full_amount_not_a_fraction(self):
        cs = ChainState.from_genesis()
        reward = cs.state.compute_block_reward()
        b1 = make_block(1, cs.tip["hash"], [], builder_index=2)
        _, _, cs1 = cs.validate_and_apply(b1)
        assert cs1.state.get_balance(address(2)) == reward
        assert reward > 0


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
        cs_a = ChainState([blk_a], cs.state.snapshot())
        cs_b = ChainState([blk_b], cs.state.snapshot())
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
        heavy = ChainState(cs.chain, cs.state,
                            cumulative_iterations=1_000_000)
        light = ChainState(cs.chain + [make_block(1, cs.tip["hash"], [])],
                            cs.state,
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
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        g = cs.tip
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b1 = make_block(1, g["hash"], [confirm, resolve])
        cs2 = cs.apply_block(b1)
        assert cs2.state.get_balance(address(1)) == TICKS_PER_LAPSE


# ---------------------------------------------------------------------------
# 9. TxQueue: canonical global queue position for confirmed ciphertexts
# ---------------------------------------------------------------------------

class TestTxQueue:
    def test_empty_queue_has_no_front(self):
        assert TxQueue().front() is None

    def test_confirm_appends_to_order_and_confirmations(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [confirm])
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err
        h = tx_mod.tx_hash(confirm)
        assert cs2.queue.front() == h
        assert cs2.queue.lookup(h) == confirm

    def test_resolve_advances_front(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b1 = make_block(1, cs.tip["hash"], [confirm])
        _, _, cs1 = cs.validate_and_apply(b1)
        b2 = make_block(2, cs1.tip["hash"], [resolve])
        ok, err, cs2 = cs1.validate_and_apply(b2)
        assert ok is True, err
        assert cs2.queue.front() is None

    def test_chain_does_not_halt_on_a_double_spent_confirmation(self):
        """Reproduces the scenario tx.validate_resolution's docstring
        exists to prevent: a sender confirms two ciphertexts both spending
        their entire balance (to different recipients, e.g. two wallet
        sends racing each other). Whichever resolves first succeeds; the
        second's real payload is now permanently unpayable. That must not
        stop the chain from ever accepting a resolution for it -- the
        gapless queue rule requires the front be resolved every block, and
        there is exactly one payload the second ciphertext can ever decrypt
        to, so if that payload's invalidity blocked inclusion, no block
        could ever be valid again."""
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE

        confirm1, resolve1 = make_tx(0, 1, 100 * TICKS_PER_LAPSE, cs.state, 0)
        confirm2, resolve2 = make_tx(0, 2, 100 * TICKS_PER_LAPSE, cs.state, 0)

        b1 = make_block(1, cs.tip["hash"], tx_mod.sort_txs([confirm1, confirm2]))
        ok, err, cs1 = cs.validate_and_apply(b1)
        assert ok is True, err

        front = cs1.queue.front()
        resolve_by_hash = {tx_mod.tx_hash(confirm1): resolve1,
                           tx_mod.tx_hash(confirm2): resolve2}

        # Resolve the front: succeeds and drains the sender's balance.
        b2 = make_block(2, cs1.tip["hash"], [resolve_by_hash[front]])
        ok, err, cs2 = cs1.validate_and_apply(b2)
        assert ok is True, err
        assert cs2.queue.remaining() != []  # one confirmation still pending

        # Resolve the second (now-unpayable) front: must still be a valid
        # block -- this is the assertion that would fail without the fix.
        second_front = cs2.queue.front()
        b3 = make_block(3, cs2.tip["hash"], [resolve_by_hash[second_front]])
        ok, err, cs3 = cs2.validate_and_apply(b3)
        assert ok is True, err
        assert cs3.queue.remaining() == []  # queue fully drained, chain not stuck

        # And the chain keeps producing blocks afterward -- not halted.
        b4 = make_block(4, cs3.tip["hash"], [])
        ok, err, cs4 = cs3.validate_and_apply(b4)
        assert ok is True, err
        assert cs4.height == 4

    def test_queue_rebuilt_by_from_storage(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * TICKS_PER_LAPSE)
        cs.state.total_minted += 100 * TICKS_PER_LAPSE
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, 0)
        b = make_block(1, cs.tip["hash"], [confirm])
        _, _, cs1 = cs.validate_and_apply(b)
        rebuilt = ChainState.from_storage(cs1.chain, cs1.state)
        assert rebuilt.queue.front() == cs1.queue.front()
