"""State ledger invariant tests: balances, nonces, fee burns, emission."""
import pytest
from helpers import *


def test_credit_debit():
    s = state_mod.State()
    s.credit("alice", 100)
    assert s.get_balance("alice") == 100
    s.debit("alice", 40)
    assert s.get_balance("alice") == 60


def test_debit_below_zero_raises():
    s = state_mod.State()
    s.credit("alice", 10)
    with pytest.raises(ValueError):
        s.debit("alice", 11)


def test_apply_tx_debits_outputs_and_fee():
    _sk, _pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = funded_state(addr, 1000)
    t = {"from": addr, "outputs": [{"to": to, "amount": 100}],
         "fee": 50, "nonce": 1, "pubkey": pk_hex, "signature": "dummy"}
    s.apply_tx(t)
    assert s.get_balance(addr) == 850
    assert s.get_balance(to) == 100
    assert s.get_nonce(addr) == 1


def test_fee_is_burned_and_tracked():
    """Fee reduces total balances AND increments total_burnt."""
    _sk, _pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = funded_state(addr, 1000)
    t = {"from": addr, "outputs": [{"to": to, "amount": 100}],
         "fee": 50, "nonce": 1, "pubkey": pk_hex, "signature": "dummy"}
    total_before = sum(s.all_balances().values())
    s.apply_tx(t)
    total_after = sum(s.all_balances().values())
    assert total_after == total_before - 50  # fee burned from balances
    assert s.total_burnt == 50               # tracked in emission counter


def test_snapshot_preserves_emission_counters():
    s = state_mod.State()
    s.apply_reward("alice", 1000)
    s.total_burnt = 200
    snap = s.snapshot()
    assert snap.total_minted == 1000
    assert snap.total_burnt == 200


def test_restore_from_snapshot():
    s = state_mod.State()
    s.credit("alice", 500)
    snap = s.snapshot()
    s.debit("alice", 200)
    s.restore(snap)
    assert s.get_balance("alice") == 500


def test_apply_reward_increments_minted():
    s = state_mod.State()
    reward = s.compute_block_reward()
    assert reward > 0
    s.apply_reward("builder", reward)
    assert s.total_minted == reward
    assert s.get_balance("builder") == reward


def test_compute_block_reward_decreases_as_minted_grows():
    s = state_mod.State()
    r0 = s.compute_block_reward()
    s.apply_reward("x", r0)
    r1 = s.compute_block_reward()
    assert r1 < r0  # emission decays


def test_burnt_fees_increase_can_mint():
    """Burnt fees flow back into can_mint, sustaining future rewards."""
    s = state_mod.State()
    r0 = s.compute_block_reward()
    s.apply_reward("x", r0)
    s.total_burnt = r0  # simulate fee burns equal to one block reward
    r1 = s.compute_block_reward()
    # With burns restoring can_mint, reward should be close to r0 again
    assert r1 > r0 * 0.9


def test_multiple_outputs():
    _sk, _pk, pk_hex, addr = make_keypair()
    _, _, _, to1 = make_keypair()
    _, _, _, to2 = make_keypair()
    s = funded_state(addr, 1000)
    t = {"from": addr,
         "outputs": [{"to": to1, "amount": 200}, {"to": to2, "amount": 300}],
         "fee": 10, "nonce": 1, "pubkey": pk_hex, "signature": "dummy"}
    s.apply_tx(t)
    assert s.get_balance(addr) == 490
    assert s.get_balance(to1) == 200
    assert s.get_balance(to2) == 300
