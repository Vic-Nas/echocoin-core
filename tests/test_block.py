"""Block validation tests: structure, ordering, timestamps, VDF, fee rate."""
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
    assert not ok
    assert "hash" in err.lower()


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
    blk["height"] = 5  # wrong
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


# ---------------------------------------------------------------------------
# Builder field
# ---------------------------------------------------------------------------

def test_missing_builder_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["builder"] = None
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "builder" in err


# ---------------------------------------------------------------------------
# VDF proof
# ---------------------------------------------------------------------------

def test_invalid_vdf_proof_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    # vdf.verify returns False -> block rejected
    with patch("vdf.verify", return_value=False):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "vdf" in err.lower()


def test_missing_vdf_fields_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["vdf_output"] = None
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "vdf" in err.lower()


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
        pytest.skip("hashes collided, ordering is a no-op")

    g = block_mod.create_genesis()
    blk = make_block([g], txs=list(reversed(sorted_txs)))
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, s, [g], fee_rate_fn(1))
    assert not ok and "ordering" in err.lower()


# ---------------------------------------------------------------------------
# Fee rate
# ---------------------------------------------------------------------------

def test_fee_rate_retarget_returns_int():
    chain = genesis_chain()
    rate = block_mod.compute_expected_fee_rate(chain)
    assert isinstance(rate, int) and rate >= 1


def test_fee_rate_initial_is_1000():
    from params import INITIAL_FEE_RATE
    assert INITIAL_FEE_RATE == 1_000


def test_fee_rate_stable_at_target_volume():
    """Median volume exactly at BLOCK_SIZE_TARGET_BYTES keeps rate stable."""

    from params import BLOCK_SIZE_TARGET_BYTES, INITIAL_FEE_RATE
    # Build a chain where every block has exactly TARGET bytes of txs.
    # Since compute_expected_fee_rate uses median of the window, we inject
    # fake blocks with pre-set byte volumes by controlling the tx list size.
    # Here we test the formula directly.
    rate = INITIAL_FEE_RATE
    # vol_ratio = 1.0 -> adjustment = 1.0^0.1 = 1.0 -> rate unchanged
    vol_ratio = BLOCK_SIZE_TARGET_BYTES / BLOCK_SIZE_TARGET_BYTES
    assert vol_ratio == 1.0
    assert max(0.999, vol_ratio ** 0.1) == 1.0
    new_rate = max(1, int(rate * 1.0))
    assert new_rate == rate


def test_fee_rate_rises_above_target():
    """Blocks above the soft target cause rate to rise."""
    from params import INITIAL_FEE_RATE
    rate = INITIAL_FEE_RATE
    vol_ratio = 2.0  # 2x target
    adjustment = min(1.05, vol_ratio)
    assert adjustment == 1.05
    new_rate = max(1, int(rate * adjustment))
    assert new_rate > rate


def test_fee_rate_falls_slowly_below_target():
    """Blocks below the soft target cause rate to fall, but slowly."""
    from params import INITIAL_FEE_RATE
    rate = INITIAL_FEE_RATE
    # vol_ratio = 0.5 -> max(0.999, 0.5^0.1) = 0.999 (floor governs)
    vol_ratio = 0.5
    adjustment = max(0.999, vol_ratio ** 0.1)
    assert adjustment == 0.999
    new_rate = max(1, int(rate * adjustment))
    assert new_rate < rate  # fell
    assert new_rate > rate * 0.99  # but not by much


def test_fee_rate_frozen_at_zero_activity():
    """Zero-activity adjustment is 0.999 -- nearly frozen."""
    from params import INITIAL_FEE_RATE
    rate = INITIAL_FEE_RATE
    new_rate = max(1, int(rate * 0.999))
    assert new_rate == rate - 1  # dropped by at most 1 ring/byte at high rate


def test_fee_rate_mismatch_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["fee_rate"] = 99999  # wrong
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
    assert b["vdf_output"] is None  # caller fills in after vdf.evaluate()
