"""Tests for Proof-of-Burn score engine and burn transaction handling."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch
import pytest

import pob as pob_mod
import state as state_mod
import tx as tx_mod
import block as block_mod
from pob import BURN_ADDRESS, score, cumulative_score
from helpers import make_keypair, make_chain, make_block, fee_rate_fn, genesis_chain


# ---------------------------------------------------------------------------
# Burn address accepted in transactions
# ---------------------------------------------------------------------------

def test_burn_output_validates():
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, other = make_keypair()
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
    outputs = [
        {"to": recipient,    "amount": 3_000},
        {"to": BURN_ADDRESS, "amount": 2_000},
    ]
    fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)
    s = state_mod.State()
    s.credit(addr, 10_000_000)
    s.apply_tx(t)
    assert s.get_balance(recipient) == 3_000
    assert s.get_balance(BURN_ADDRESS) == 0
    assert s.total_burnt == 2_000 + fee


# ---------------------------------------------------------------------------
# PoB score
# ---------------------------------------------------------------------------

def test_score_returns_int():
    chain = genesis_chain()
    _, _, _, addr = make_keypair()
    assert isinstance(score(chain, addr), int)


def test_score_lower_for_burner(monkeypatch):
    """Builder who burned in the window should score lower than one who did not."""
    _, _, _, burner = make_keypair()
    _, _, _, virgin = make_keypair()
    chain = make_chain(3)

    # Inject a fake burn into the chain for `burner`
    chain[-1]["transactions"] = [{
        "from": burner,
        "outputs": [{"to": BURN_ADDRESS, "amount": 1_000_000_000}],
    }]

    s_burner = score(chain, burner)
    s_virgin = score(chain, virgin)
    assert s_burner < s_virgin


def test_score_decreases_with_more_burns(monkeypatch):
    _, _, _, addr = make_keypair()
    chain = make_chain(3)

    chain[-1]["transactions"] = [{
        "from": addr,
        "outputs": [{"to": BURN_ADDRESS, "amount": 1_000}],
    }]
    s_small = score(chain, addr)

    chain[-1]["transactions"] = [{
        "from": addr,
        "outputs": [{"to": BURN_ADDRESS, "amount": 1_000_000_000}],
    }]
    s_large = score(chain, addr)

    assert s_large < s_small


# ---------------------------------------------------------------------------
# Cumulative score and fork choice
# ---------------------------------------------------------------------------

def test_cumulative_score_genesis_only():
    chain = genesis_chain()
    assert cumulative_score(chain) == 0


def test_cumulative_score_increases_with_chain():
    chain = make_chain(5)
    assert cumulative_score(chain) > 0


def test_lower_cumulative_score_chain_preferred():
    """Chain where builder burned heavily should have lower cumulative score."""
    _, _, _, burner = make_keypair()
    _, _, _, virgin = make_keypair()

    # Build two chains from same genesis, different builders
    honest_chain = make_chain(4, builder_addr=burner)
    botnet_chain = make_chain(4, builder_addr=virgin)

    # Inject burns into honest chain blocks.
    # Burns must be in blocks BEFORE the scored block (score looks at chain[:i]).
    # So put burns in blocks 1 and 2; they count when scoring block 2 and 3.
    burn_amount = 10_000_000_000
    for blk in honest_chain[1:3]:
        blk["transactions"] = [{
            "from": burner,
            "outputs": [{"to": BURN_ADDRESS, "amount": burn_amount}],
        }]

    honest_score = cumulative_score(honest_chain)
    botnet_score = cumulative_score(botnet_chain)
    assert honest_score < botnet_score


# ---------------------------------------------------------------------------
# POB_WINDOW boundary
# ---------------------------------------------------------------------------

def test_burns_outside_window_ignored():
    from params import POB_WINDOW
    _, _, _, addr = make_keypair()

    # Chain longer than window, burn only in the oldest block (outside window)
    chain = make_chain(POB_WINDOW + 5)
    chain[1]["transactions"] = [{
        "from": addr,
        "outputs": [{"to": BURN_ADDRESS, "amount": 999_999_999_999}],
    }]

    # Score should be same as an unburnished builder (burn not counted)
    _, _, _, fresh = make_keypair()
    s_old_burn = score(chain, addr)
    s_no_burn  = score(chain, fresh)

    # Both unburnished at this tip -- scores differ only by address hash, not burn
    # The old burn falls outside the window so addr gets denom=1 same as fresh
    from pob import _addr_int, _tip_hash_int
    seed_addr  = _tip_hash_int(chain) ^ _addr_int(addr)
    seed_fresh = _tip_hash_int(chain) ^ _addr_int(fresh)
    assert s_old_burn == seed_addr   # denom=1, burn outside window
    assert s_no_burn  == seed_fresh


# ---------------------------------------------------------------------------
# Burn tagging and reward_distribution
# ---------------------------------------------------------------------------

def test_burn_tags_beneficiary():
    """A burn output with beneficiary field tags that address."""
    sk, pk, pk_hex, sender = make_keypair()
    _, _, _, pool_addr    = make_keypair()
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
    assert not ok
    assert "beneficiary" in err


def test_reward_distribution_no_burns_goes_to_builder():
    chain = genesis_chain()
    _, _, _, builder = make_keypair()
    dist = pob_mod.reward_distribution(chain, builder, 1_000_000)
    assert dist == [(builder, 1_000_000)]


def test_reward_distribution_solo_burn():
    """Builder who burned only to themselves gets the full reward."""
    _, _, _, builder = make_keypair()
    chain = make_chain(3, builder_addr=builder)
    # Inject self-burn in chain
    chain[-1]["transactions"] = [{
        "from": builder,
        "outputs": [{"to": BURN_ADDRESS, "amount": 5_000_000}],
    }]
    dist = pob_mod.reward_distribution(chain, builder, 1_000_000)
    assert len(dist) == 1
    assert dist[0][0] == builder
    assert dist[0][1] == 1_000_000


def test_reward_distribution_two_equal_contributors():
    _, _, _, builder = make_keypair()
    _, _, _, contrib = make_keypair()
    chain = make_chain(3, builder_addr=builder)
    chain[-1]["transactions"] = [
        {"from": builder, "outputs": [{"to": BURN_ADDRESS, "amount": 5_000_000,
                                        "beneficiary": builder}]},
        {"from": contrib, "outputs": [{"to": BURN_ADDRESS, "amount": 5_000_000,
                                        "beneficiary": builder}]},
    ]
    dist = pob_mod.reward_distribution(chain, builder, 1_000_000)
    total_dist = sum(a for _, a in dist)
    # Two equal contributors: each gets ~500_000 (may differ by 1 due to rounding)
    assert len(dist) == 2
    assert abs(total_dist - 1_000_000) <= 1  # rounding remainder at most 1 ring
    for addr, amount in dist:
        assert 499_999 <= amount <= 500_001


def test_reward_distribution_dust_excluded():
    """A contributor with 1 ring burned against a pool of millions gets 0 -- excluded."""
    _, _, _, builder = make_keypair()
    _, _, _, dust    = make_keypair()
    chain = make_chain(3, builder_addr=builder)
    chain[-1]["transactions"] = [
        {"from": builder, "outputs": [{"to": BURN_ADDRESS, "amount": 999_999_999,
                                        "beneficiary": builder}]},
        {"from": dust,    "outputs": [{"to": BURN_ADDRESS, "amount": 1,
                                        "beneficiary": builder}]},
    ]
    dist = pob_mod.reward_distribution(chain, builder, 1_000)
    # dust contributor's share: 1000 * 1 // 1_000_000_000 = 0 -- excluded
    addrs = [a for a, _ in dist]
    assert dust not in addrs
    assert builder in addrs


def test_apply_reward_distribution_credits_all():
    s = state_mod.State()
    _, _, _, a1 = make_keypair()
    _, _, _, a2 = make_keypair()
    dist = [(a1, 700_000), (a2, 300_000)]
    s.apply_reward_distribution(dist)
    assert s.get_balance(a1) == 700_000
    assert s.get_balance(a2) == 300_000
    assert s.total_minted == 1_000_000


def test_apply_reward_distribution_skips_zero():
    s = state_mod.State()
    _, _, _, a1 = make_keypair()
    dist = [(a1, 0)]
    s.apply_reward_distribution(dist)
    assert s.get_balance(a1) == 0
    assert s.total_minted == 0
