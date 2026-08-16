"""
Unit tests for state.py

Covers: credit, debit, set_nonce, apply_tx, compute_block_reward,
apply_reward_distribution, snapshot, from_snapshot, and the whitepaper
emission formula.

Whitepaper constraints enforced:
  - can_mint = SUPPLY_CAP - total_minted + total_burnt  (Section 5)
  - reward = int(can_mint * (1 - EMISSION_RATE))
  - fee burns AND intentional PoB burns both increase total_burnt
  - reward recipients via pob.reward_distribution
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import state as state_mod
from state import compute_reward
from pob import BURN_ADDRESS
from params import (
    EMISSION_RATE, SUPPLY_CAP, RINGS_PER_ECH
)
from tests.fixtures import address, make_tx, seed_balance


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

    def test_get_nonce_unknown_address_returns_zero(self):
        s = fresh_state()
        assert s.get_nonce("unknown.addr") == 0

    def test_set_nonce_stores_value(self):
        s = fresh_state()
        addr = address(0)
        s.set_nonce(addr, 7)
        assert s.get_nonce(addr) == 7


# ---------------------------------------------------------------------------
# 2. apply_tx
# ---------------------------------------------------------------------------

class TestApplyTx:
    def test_apply_tx_debits_sender(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        addr0 = address(0)
        bal_before = s.get_balance(addr0)
        t = make_tx(0, 1, RINGS_PER_ECH, s, 10)
        s.apply_tx(t)
        expected = bal_before - RINGS_PER_ECH - t["fee"]
        assert s.get_balance(addr0) == expected

    def test_apply_tx_credits_recipient(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, RINGS_PER_ECH, s, 10)
        s.apply_tx(t)
        assert s.get_balance(address(1)) == RINGS_PER_ECH

    def test_apply_tx_advances_nonce(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, RINGS_PER_ECH, s, 10)
        s.apply_tx(t)
        assert s.get_nonce(address(0)) == t["nonce"]

    def test_fee_burn_increases_total_burnt(self):
        """Whitepaper Section 2: all fees are burned."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, RINGS_PER_ECH, s, 10)
        fee = t["fee"]
        s.apply_tx(t)
        assert s.total_burnt == fee

    def test_burn_output_increases_total_burnt(self):
        """Whitepaper Section 2: intentional PoB burns credited to burn pool."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        from tests.fixtures import make_burn_tx
        t = make_burn_tx(0, RINGS_PER_ECH, s, 10)
        burn_amount = t["outputs"][0]["amount"]
        fee = t["fee"]
        s.apply_tx(t)
        assert s.total_burnt == burn_amount + fee

    def test_burn_output_does_not_credit_anyone(self):
        """Burn address is a sink: no balance increase for any address."""
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        from tests.fixtures import make_burn_tx
        t = make_burn_tx(0, RINGS_PER_ECH, s, 10)
        balances_before = dict(s.all_balances())
        s.apply_tx(t)
        for addr, bal in s.all_balances().items():
            if addr == address(0):
                continue
            assert addr not in balances_before or bal == balances_before[addr]

    def test_multiple_outputs_all_credited(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        from_addr = address(0)
        from tests.fixtures import keypair as kp, pubkey_hex
        import tx as tx_mod
        from params import INITIAL_FEE_RATE
        outputs = [
            {"to": address(1), "amount": RINGS_PER_ECH},
            {"to": address(2), "amount": 2 * RINGS_PER_ECH},
        ]
        pk_hex = pubkey_hex(0)
        fee = tx_mod.compute_fee(from_addr, pk_hex, outputs, 1, 10, INITIAL_FEE_RATE)
        sk, _ = kp(0)
        t = tx_mod.create(from_addr, pk_hex, outputs, 1, 10, fee, sk)
        s.apply_tx(t)
        assert s.get_balance(address(1)) == RINGS_PER_ECH
        assert s.get_balance(address(2)) == 2 * RINGS_PER_ECH


# ---------------------------------------------------------------------------
# 3. Emission formula (whitepaper Section 5)
# ---------------------------------------------------------------------------

class TestEmission:
    def test_initial_reward_positive(self):
        reward = compute_reward(0, 0)
        assert reward > 0

    def test_reward_decreases_as_minted_increases(self):
        r1 = compute_reward(0, 0)
        r2 = compute_reward(SUPPLY_CAP // 2, 0)
        assert r2 < r1

    def test_reward_zero_when_cap_exhausted(self):
        """If all coins minted and none burned, reward is 0."""
        assert compute_reward(SUPPLY_CAP, 0) == 0

    def test_burns_restore_mintable_pool(self):
        """Whitepaper Section 5: burns feed back into can_mint."""
        r_no_burn   = compute_reward(SUPPLY_CAP // 2, 0)
        r_with_burn = compute_reward(SUPPLY_CAP // 2, RINGS_PER_ECH * 1000)
        assert r_with_burn > r_no_burn

    def test_reward_formula_matches_whitepaper(self):
        """can_mint = SUPPLY_CAP - total_minted + total_burnt;  reward = int(can_mint * (1 - RATE))"""
        minted = 5_000_000 * RINGS_PER_ECH
        burnt  = 500_000  * RINGS_PER_ECH
        can_mint = SUPPLY_CAP - minted + burnt
        expected = int(can_mint * (1 - EMISSION_RATE))
        assert compute_reward(minted, burnt) == expected

    def test_state_compute_block_reward_uses_state_totals(self):
        s = fresh_state()
        s.total_minted = SUPPLY_CAP // 4
        s.total_burnt  = 1000 * RINGS_PER_ECH
        r_state = s.compute_block_reward()
        r_direct = compute_reward(s.total_minted, s.total_burnt)
        assert r_state == r_direct

    def test_negative_can_mint_returns_zero(self):
        """Guard against can_mint going negative (shouldn't happen normally)."""
        assert compute_reward(SUPPLY_CAP + RINGS_PER_ECH, 0) == 0


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
        s.total_burnt  = 678
        snap = s.snapshot()
        assert snap.total_minted == 12345
        assert snap.total_burnt  == 678

    def test_snapshot_modification_does_not_affect_original(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        snap = s.snapshot()
        snap.credit(address(1), 1_000_000)
        assert s.get_balance(address(1)) == 0

    def test_from_snapshot_restores_state(self):
        s = fresh_state()
        seed_balance(s, 0, 10.0)
        s.total_burnt = 500
        balances = s.all_balances()
        nonces   = s.all_nonces()
        s2 = state_mod.State.from_snapshot(balances, nonces, s.total_minted, s.total_burnt)
        assert s2.get_balance(address(0)) == s.get_balance(address(0))
        assert s2.total_minted == s.total_minted
        assert s2.total_burnt  == s.total_burnt

    def test_all_balances_returns_copy(self):
        s = fresh_state()
        seed_balance(s, 0, 1.0)
        bals = s.all_balances()
        bals["injected"] = 999
        assert "injected" not in s.all_balances()
