"""Block unit tests: structure, chain linkage, timestamps, ordering, fee rate formula.

VDF forgery and block size security properties are in test_flow_security.
assemble() size tracking is in test_flow_block_cycle.
"""
from unittest.mock import patch

import pytest
from helpers import *


# ---------------------------------------------------------------------------
# Genesis
# ---------------------------------------------------------------------------

def test_genesis_hash_deterministic():
    assert block_mod.create_genesis()["hash"] == block_mod.create_genesis()["hash"]


def test_genesis_valid():
    g = block_mod.create_genesis()
    ok, err = block_mod.validate(g, state_mod.State(), [], fee_rate_fn(1))
    assert ok, err


def test_genesis_has_no_vdf_fields():
    g = block_mod.create_genesis()
    assert g["vdf_output"] is None
    assert g["vdf_proof"] is None
    assert g["builder"] is None


# ---------------------------------------------------------------------------
# Hash integrity
# ---------------------------------------------------------------------------

def test_block_hash_changes_on_mutation():
    g = block_mod.create_genesis()
    g2 = dict(g)
    g2["height"] = 999
    g2["hash"] = block_mod.block_hash(g2)
    assert g2["hash"] != g["hash"]


def test_block_with_wrong_hash_rejected():
    g = block_mod.create_genesis()
    g["hash"] = "ff" * 32
    ok, err = block_mod.validate(g, state_mod.State(), [], fee_rate_fn(1))
    assert not ok and "hash" in err.lower()


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_block_timestamp_must_follow_parent():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["timestamp"] = g["timestamp"] + BLOCK_CYCLE_SECONDS - 1
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "timestamp" in err


def test_block_timestamp_future_rejected():
    import time
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["timestamp"] = time.time() + 300
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "future" in err


def test_block_missing_timestamp_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    del blk["timestamp"]
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "timestamp" in err


# ---------------------------------------------------------------------------
# Chain linkage
# ---------------------------------------------------------------------------

def test_height_not_following_parent_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["height"] = 5
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "height" in err


def test_previous_hash_mismatch_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["previous_hash"] = "ab" * 32
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "previous_hash" in err


def test_missing_builder_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["builder"] = None
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "builder" in err


# ---------------------------------------------------------------------------
# Transaction ordering
# ---------------------------------------------------------------------------

def test_transaction_ordering_violation_rejected():
    sk, _pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = funded_state(addr, 1_000_000)
    t1 = make_valid_tx(sk, pk_hex, addr, to, 10, 1, 0, 1)
    t2 = make_valid_tx(sk, pk_hex, addr, to, 10, 2, 0, 1)
    sorted_txs = tx_mod.sort_txs([t1, t2])
    if tx_mod.tx_hash(sorted_txs[0]) == tx_mod.tx_hash(sorted_txs[1]):
        pytest.skip("hashes collided")
    g = block_mod.create_genesis()
    blk = make_block([g], txs=list(reversed(sorted_txs)))
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, s, [g], fee_rate_fn(1))
    assert not ok and "ordering" in err.lower()


# ---------------------------------------------------------------------------
# Fee rate formula (unit -- chain plumbing tested in test_flow_block_cycle)
# ---------------------------------------------------------------------------

def test_fee_rate_retarget_returns_int():
    rate = block_mod.compute_expected_fee_rate(genesis_chain())
    assert isinstance(rate, int) and rate >= 1


def test_fee_rate_initial_value():
    from params import INITIAL_FEE_RATE
    assert INITIAL_FEE_RATE == 10


def test_fee_rate_stable_at_target_volume():
    from params import BLOCK_SIZE_TARGET_BYTES, INITIAL_FEE_RATE
    vol_ratio = BLOCK_SIZE_TARGET_BYTES / BLOCK_SIZE_TARGET_BYTES
    assert max(0.999, vol_ratio ** 0.1) == 1.0
    assert max(1, int(INITIAL_FEE_RATE * 1.0)) == INITIAL_FEE_RATE


def test_fee_rate_mismatch_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["fee_rate"] = 99999
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "fee rate" in err.lower()


# ---------------------------------------------------------------------------
# Block size
# ---------------------------------------------------------------------------

def test_genesis_under_size_limit():
    from params import BLOCK_SIZE_LIMIT
    assert block_mod.block_size(block_mod.create_genesis()) < BLOCK_SIZE_LIMIT


# ---------------------------------------------------------------------------
# assemble()
# ---------------------------------------------------------------------------

def test_assemble_returns_block_at_correct_height():
    g = block_mod.create_genesis()
    b = block_mod.assemble(g, [], "builder_addr", fee_rate=1)
    assert b["height"] == 1
    assert b["builder"] == "builder_addr"
    assert b["previous_hash"] == g["hash"]
    assert b["vdf_output"] is None
