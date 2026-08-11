"""State ledger invariant tests: balance integrity, nonce tracking, fee burn."""
import pytest
from helpers import *


def test_credit_debit():
    s = state_mod.State()
    s.credit("alice", 100)
    assert s.get_balance("alice") == 100
    s.debit("alice", 40)
    assert s.get_balance("alice") == 60


def test_debit_below_zero_asserts():
    s = state_mod.State()
    s.credit("alice", 10)
    with pytest.raises(AssertionError):
        s.debit("alice", 11)


def test_apply_tx_debits_outputs_and_fee():
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to_addr = make_keypair()
    s = funded_state(addr, 1000)
    t = {"from": addr, "outputs": [{"to": to_addr, "amount": 100}],
         "fee": 50, "nonce": 1, "pubkey": pk_hex, "signature": "dummy"}
    s.apply_tx(t)
    assert s.get_balance(addr) == 850  # 1000 - 100 - 50
    assert s.get_balance(to_addr) == 100
    assert s.get_nonce(addr) == 1


def test_fee_is_burned():
    """Fee is debited from sender but credited to nobody."""
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to_addr = make_keypair()
    s = funded_state(addr, 1000)
    t = {"from": addr, "outputs": [{"to": to_addr, "amount": 100}],
         "fee": 50, "nonce": 1, "pubkey": pk_hex, "signature": "dummy"}
    total_before = sum(s.all_balances().values())
    s.apply_tx(t)
    total_after = sum(s.all_balances().values())
    assert total_after == total_before - 50  # fee burned


def test_snapshot_restore():
    s = state_mod.State()
    s.credit("alice", 500)
    snap = s.snapshot()
    s.debit("alice", 200)
    assert s.get_balance("alice") == 300
    s.restore(snap)
    assert s.get_balance("alice") == 500


def test_multiple_outputs():
    sk, pk, pk_hex, addr = make_keypair()
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


def test_rewards_credit():
    s = state_mod.State()
    s.apply_rewards({"miner1": 5_000_000_000, "miner2": 5_000_000_000})
    assert s.get_balance("miner1") == 5_000_000_000
    assert s.get_balance("miner2") == 5_000_000_000
