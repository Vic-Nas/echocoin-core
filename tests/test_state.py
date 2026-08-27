"""
Unit tests for state.py

Covers: credit, debit, mark_nonce_used, apply_tx, compute_block_reward,
apply_reward_distribution, snapshot, from_snapshot, and the emission formula.

  - can_mint = SUPPLY_CAP - total_minted
  - reward = int(can_mint * (1 - EMISSION_RATE))
  - the full block reward mints to the builder (no burn-based split)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import state as state_mod
import tx as tx_mod
from state import compute_reward
from params import (
    EMISSION_RATE, SUPPLY_CAP, TICKS_PER_LAPSE
)
from tests.fixtures import address, make_tx, apply_transfer, seed_balance


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. credit / debit / nonce
# ---------------------------------------------------------------------------

class TestCreditDebit:
    def test_credit_increases_balance(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        assert s.get_balance(addr) == 100

    def test_credit_accumulates(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        s.credit(addr, 200)
        assert s.get_balance(addr) == 300

    def test_credit_zero_raises(self):
        s = fresh_state()
        with pytest.raises(ValueError):
            s.credit(address(0), 0)

    def test_credit_negative_raises(self):
        s = fresh_state()
        with pytest.raises(ValueError):
            s.credit(address(0), -1)

    def test_debit_reduces_balance(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 500)
        s.debit(addr, 200)
        assert s.get_balance(addr) == 300

    def test_debit_exact_balance_succeeds(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        s.debit(addr, 100)
        assert s.get_balance(addr) == 0

    def test_debit_overdraft_raises(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        with pytest.raises(ValueError, match="negative"):
            s.debit(addr, 101)

    def test_debit_zero_raises(self):
        s = fresh_state()
        addr = address(0)
        s.credit(addr, 100)
        with pytest.raises(ValueError):
            s.debit(addr, 0)

    def test_get_balance_unknown_address_returns_zero(self):
        s = fresh_state()
        assert s.get_balance("unknown.addr") == 0

    def test_has_used_nonce_unknown_address_is_false(self):
        s = fresh_state()
        assert s.has_used_nonce("unknown.addr", "ab" * 16) is False

    def test_mark_nonce_used_records_it(self):
        s = fresh_state()
        addr = address(0)
        s.mark_nonce_used(addr, "ab" * 16)
        assert s.has_used_nonce(addr, "ab" * 16) is True
        assert s.nonce_count(addr) == 1


# ---------------------------------------------------------------------------
# 2. apply_tx
# ---------------------------------------------------------------------------

class TestApplyTx:
    def test_apply_tx_debits_sender(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        addr0 = address(0)
        bal_before = s.get_balance(addr0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        apply_transfer(s, confirm, resolve)
        expected = bal_before - TICKS_PER_LAPSE - confirm["fee"]
        assert s.get_balance(addr0) == expected

    def test_apply_tx_credits_recipient(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        apply_transfer(s, confirm, resolve)
        assert s.get_balance(address(1)) == TICKS_PER_LAPSE

    def test_apply_tx_advances_nonce(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        apply_transfer(s, confirm, resolve)
        assert s.has_used_nonce(address(0), resolve["payload"]["nonce"]) is True

    def test_multiple_outputs_all_credited(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        outputs = [
            {"to": address(1), "amount": TICKS_PER_LAPSE},
            {"to": address(2), "amount": 2 * TICKS_PER_LAPSE},
        ]
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, outputs_override=outputs)
        apply_transfer(s, confirm, resolve)
        assert s.get_balance(address(1)) == TICKS_PER_LAPSE
        assert s.get_balance(address(2)) == 2 * TICKS_PER_LAPSE

    def test_fee_escrowed_at_confirmation(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        h = tx_mod.tx_hash(confirm)
        s.apply_confirmation(confirm, h)
        assert s.escrowed_fee(h) == confirm["fee"]

    def test_resolution_pays_resolver_the_escrowed_fee(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, resolver_index=3)
        h = tx_mod.tx_hash(confirm)
        s.apply_confirmation(confirm, h)
        resolver_before = s.get_balance(address(3))
        s.apply_resolution(resolve)
        assert s.get_balance(address(3)) == resolver_before + confirm["fee"]
        assert s.escrowed_fee(h) == 0

    def test_resolution_with_invalid_payload_still_pays_resolver_no_transfer(self):
        """The resolver did real, checkable work regardless of whether the
        decrypted payload can still be applied, so they're still paid --
        but no transfer happens and no other state changes (see
        tx.validate_resolution's docstring for why a resolution's
        inclusion can't depend on payload validity)."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, resolver_index=3)
        h = tx_mod.tx_hash(confirm)
        s.apply_confirmation(confirm, h)
        recipient_before = s.get_balance(address(1))
        resolver_before  = s.get_balance(address(3))
        s.apply_resolution(resolve, payload_valid=False)
        assert s.get_balance(address(3)) == resolver_before + confirm["fee"]
        assert s.get_balance(address(1)) == recipient_before  # transfer not applied
        assert s.escrowed_fee(h) == 0


# ---------------------------------------------------------------------------
# 3. Emission formula
# ---------------------------------------------------------------------------

class TestEmission:
    def test_initial_reward_positive(self):
        reward = compute_reward(0)
        assert reward > 0

    def test_reward_decreases_as_minted_increases(self):
        r1 = compute_reward(0)
        r2 = compute_reward(SUPPLY_CAP // 2)
        assert r2 < r1

    def test_reward_zero_when_cap_exhausted(self):
        assert compute_reward(SUPPLY_CAP) == 0

    def test_reward_formula(self):
        """can_mint = SUPPLY_CAP - total_minted;  reward = int(can_mint * (1 - RATE))"""
        minted = 5_000_000 * TICKS_PER_LAPSE
        can_mint = SUPPLY_CAP - minted
        expected = int(can_mint * (1 - EMISSION_RATE))
        assert compute_reward(minted) == expected

    def test_state_compute_block_reward_uses_state_totals(self):
        s = fresh_state()
        s.total_minted = SUPPLY_CAP // 4
        r_state = s.compute_block_reward()
        r_direct = compute_reward(s.total_minted)
        assert r_state == r_direct

    def test_negative_can_mint_returns_zero(self):
        """Guard against can_mint going negative (shouldn't happen normally)."""
        assert compute_reward(SUPPLY_CAP + TICKS_PER_LAPSE) == 0

    def test_compute_can_mint_is_the_shared_pool_formula(self):
        """compute_reward is derived from compute_can_mint, not a second copy."""
        minted = 5_000_000 * TICKS_PER_LAPSE
        pool   = state_mod.compute_can_mint(minted)
        assert pool == SUPPLY_CAP - minted
        assert compute_reward(minted) == int(pool * (1 - EMISSION_RATE))

    def test_compute_can_mint_floors_at_zero(self):
        assert state_mod.compute_can_mint(SUPPLY_CAP + TICKS_PER_LAPSE) == 0

    def test_state_compute_can_mint_uses_state_totals(self):
        s = fresh_state()
        s.total_minted = SUPPLY_CAP // 4
        assert s.compute_can_mint() == state_mod.compute_can_mint(s.total_minted)


# ---------------------------------------------------------------------------
# 4. apply_reward_distribution
# ---------------------------------------------------------------------------

class TestApplyRewardDistribution:
    def test_reward_credits_builder(self):
        s = fresh_state()
        builder = address(0)
        reward = 1000
        s.apply_reward_distribution([(builder, reward)])
        assert s.get_balance(builder) == reward

    def test_reward_increments_total_minted(self):
        s = fresh_state()
        builder = address(0)
        reward = 5000
        s.apply_reward_distribution([(builder, reward)])
        assert s.total_minted == reward

    def test_reward_split_among_contributors(self):
        s = fresh_state()
        a0, a1 = address(0), address(1)
        dist = [(a0, 700), (a1, 300)]
        s.apply_reward_distribution(dist)
        assert s.get_balance(a0) == 700
        assert s.get_balance(a1) == 300
        assert s.total_minted == 1000

    def test_zero_amount_not_credited(self):
        s = fresh_state()
        s.apply_reward_distribution([(address(0), 0)])
        assert s.get_balance(address(0)) == 0
        assert s.total_minted == 0


# ---------------------------------------------------------------------------
# 5. Snapshot / from_snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_is_independent(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        snap = s.snapshot()
        s.credit(address(0), 999)
        assert snap.get_balance(address(0)) != s.get_balance(address(0))

    def test_snapshot_preserves_totals(self):
        s = fresh_state()
        s.total_minted = 12345
        snap = s.snapshot()
        assert snap.total_minted == 12345

    def test_snapshot_modification_does_not_affect_original(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        snap = s.snapshot()
        snap.credit(address(1), 1_000_000)
        assert s.get_balance(address(1)) == 0

    def test_from_snapshot_restores_state(self):
        s = fresh_state()
        seed_balance(s, 0, 10.0)
        balances    = s.all_balances()
        used_nonces = s.all_used_nonces()
        s2 = state_mod.State.from_snapshot(balances, used_nonces, s.total_minted)
        assert s2.get_balance(address(0)) == s.get_balance(address(0))
        assert s2.total_minted == s.total_minted

    def test_all_balances_returns_copy(self):
        s = fresh_state()
        seed_balance(s, 0, 1.0)
        bals = s.all_balances()
        bals["injected"] = 999
        assert "injected" not in s.all_balances()
