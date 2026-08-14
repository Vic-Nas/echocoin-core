"""PoB unit tests: burn tx validation, window internals, pool-level helpers.

Score monotonicity, reward distribution, cumulative_score, and fork choice
are covered in test_flow_pob. This file keeps primitive-level tests
that verify the burn output mechanics in isolation.
"""
import pytest
from helpers import *
from pob import BURN_ADDRESS, score, cumulative_score, reward_distribution, BurnWindow
import pob as pob_mod


# ---------------------------------------------------------------------------
# Burn address accepted in transactions
# ---------------------------------------------------------------------------

def test_burn_output_validates():
    sk, pk, pk_hex, addr = make_keypair()
    outputs = [{"to": BURN_ADDRESS, "amount": 1_000}]
    fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(addr, 10_000_000)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert ok, err


def test_burn_output_increases_total_burnt():
    sk, pk, pk_hex, addr = make_keypair()
    outputs = [{"to": BURN_ADDRESS, "amount": 5_000}]
    fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(addr, 10_000_000)
    s.apply_tx(t)
    assert s.total_burnt == 5_000 + fee


def test_burn_output_does_not_credit_burn_address():
    sk, pk, pk_hex, addr = make_keypair()
    outputs = [{"to": BURN_ADDRESS, "amount": 5_000}]
    fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(addr, 10_000_000)
    s.apply_tx(t)
    assert s.get_balance(BURN_ADDRESS) == 0


def test_mixed_burn_and_normal_output():
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, recipient = make_keypair()
    outputs = [{"to": recipient, "amount": 3_000}, {"to": BURN_ADDRESS, "amount": 2_000}]
    fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(addr, 10_000_000)
    s.apply_tx(t)
    assert s.get_balance(recipient) == 3_000
    assert s.total_burnt == 2_000 + fee


# ---------------------------------------------------------------------------
# Burn tagging: beneficiary field
# ---------------------------------------------------------------------------

def test_burn_tags_beneficiary():
    sk, pk, pk_hex, sender = make_keypair()
    _, _, _, pool_addr = make_keypair()
    outputs = [{"to": BURN_ADDRESS, "amount": 10_000, "beneficiary": pool_addr}]
    fee = tx_mod.compute_fee(sender, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(sender, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(sender, 10_000_000)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert ok, err


def test_burn_invalid_beneficiary_rejected():
    sk, pk, pk_hex, sender = make_keypair()
    outputs = [{"to": BURN_ADDRESS, "amount": 1_000, "beneficiary": "not.a.valid.address"}]
    fee = tx_mod.compute_fee(sender, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(sender, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(sender, 10_000_000)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok and "beneficiary" in err


# ---------------------------------------------------------------------------
# score() primitive
# ---------------------------------------------------------------------------

def test_score_returns_int():
    chain = genesis_chain()
    _, _, _, addr = make_keypair()
    assert isinstance(score(chain, addr), int)


# ---------------------------------------------------------------------------
# POB_WINDOW boundary
# ---------------------------------------------------------------------------

def test_burns_outside_window_ignored():
    from params import POB_WINDOW
    from pob import _addr_int, _tip_hash_int
    _, _, _, addr = make_keypair()
    chain = make_chain(POB_WINDOW + 5)
    chain[1]["transactions"] = [{
        "from": addr,
        "outputs": [{"to": BURN_ADDRESS, "amount": 999_999_999_999}],
    }]
    seed_addr = _tip_hash_int(chain) ^ _addr_int(addr)
    assert score(chain, addr) == seed_addr


# ---------------------------------------------------------------------------
# cumulative_score primitive
# ---------------------------------------------------------------------------

def test_cumulative_score_genesis_only():
    assert cumulative_score(genesis_chain()) == 0


# ---------------------------------------------------------------------------
# apply_reward_distribution: state credits
# ---------------------------------------------------------------------------

def test_apply_reward_distribution_credits_all():
    s = state_mod.State()
    _, _, _, a1 = make_keypair()
    _, _, _, a2 = make_keypair()
    s.apply_reward_distribution([(a1, 700_000), (a2, 300_000)])
    assert s.get_balance(a1) == 700_000
    assert s.get_balance(a2) == 300_000
    assert s.total_minted == 1_000_000


def test_apply_reward_distribution_skips_zero():
    s = state_mod.State()
    _, _, _, a1 = make_keypair()
    s.apply_reward_distribution([(a1, 0)])
    assert s.get_balance(a1) == 0
    assert s.total_minted == 0


# ---------------------------------------------------------------------------
# reward_distribution dust exclusion
# ---------------------------------------------------------------------------

def test_reward_distribution_dust_excluded():
    _, _, _, builder = make_keypair()
    _, _, _, dust = make_keypair()
    chain = make_chain(3, builder_addr=builder)
    chain[-1]["transactions"] = [
        {"from": builder, "outputs": [{"to": BURN_ADDRESS, "amount": 999_999_999,
                                        "beneficiary": builder}]},
        {"from": dust,    "outputs": [{"to": BURN_ADDRESS, "amount": 1,
                                        "beneficiary": builder}]},
    ]
    dist = reward_distribution(chain, builder, 1_000)
    assert dust not in [a for a, _ in dist]
    assert builder in [a for a, _ in dist]
