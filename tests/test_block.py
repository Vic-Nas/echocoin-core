"""
Unit tests for block.py

Covers: create_genesis, create, block_hash, block_size, validate (all sub-checks),
compute_expected_fee_rate, assemble.

VDF verification is mocked because vdf.evaluate takes ~120s.
Whitepaper constraints enforced:
  - hash integrity (tamper detection)
  - parent chain linkage and height sequence
  - timestamp not more than 30s in the future
  - VDF proof mandatory for height > 0
  - fee_rate computed by asymmetric formula (Section 2)
  - tx canonical ordering inside block
  - block size <= BLOCK_SIZE_LIMIT
"""

import os
import sys
import time as _time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
import tx as tx_mod
from params import (
    BLOCK_CYCLE_SECONDS, BLOCK_SIZE_LIMIT, BLOCK_SIZE_TARGET_BYTES,
    GENESIS_MESSAGE, GENESIS_TIMESTAMP, INITIAL_FEE_RATE, TICKS_PER_LAPSE,
)
from tests.fixtures import (
    address, genesis, keypair, make_block, make_tx, seed_balance,
)


# ---------------------------------------------------------------------------
# VDF mock: vdf.verify always True unless explicitly overridden
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


def fresh_state():
    return state_mod.State()


def noop_fee_rate(height):
    return INITIAL_FEE_RATE


# ---------------------------------------------------------------------------
# 1. create_genesis
# ---------------------------------------------------------------------------

class TestCreateGenesis:
    def test_genesis_height_is_zero(self):
        g = genesis()
        assert g["height"] == 0

    def test_genesis_previous_hash_is_zeros(self):
        g = genesis()
        assert g["previous_hash"] == "0" * 64

    def test_genesis_transactions_empty(self):
        g = genesis()
        assert g["transactions"] == []

    def test_genesis_has_hash_field(self):
        g = genesis()
        assert "hash" in g
        assert len(g["hash"]) == 64

    def test_genesis_hash_is_deterministic(self):
        g1 = genesis()
        g2 = genesis()
        assert g1["hash"] == g2["hash"]

    def test_genesis_contains_message(self):
        g = genesis()
        assert g["message"] == GENESIS_MESSAGE

    def test_genesis_timestamp_matches_params(self):
        g = genesis()
        assert g["timestamp"] == GENESIS_TIMESTAMP

    def test_genesis_fee_rate_is_initial(self):
        g = genesis()
        assert g["fee_rate"] == INITIAL_FEE_RATE

    def test_genesis_builder_is_none(self):
        g = genesis()
        assert g["builder"] is None


# ---------------------------------------------------------------------------
# 2. block_hash
# ---------------------------------------------------------------------------

class TestBlockHash:
    def test_hash_excludes_hash_field(self):
        """block_hash must not include the 'hash' field (no circular dependency)."""
        g = genesis()
        h1 = block_mod.block_hash(g)
        g2 = dict(g)
        g2["hash"] = "different"
        h2 = block_mod.block_hash(g2)
        assert h1 == h2

    def test_hash_changes_with_content(self):
        g = genesis()
        g2 = dict(g)
        g2["height"] = 1
        assert block_mod.block_hash(g) != block_mod.block_hash(g2)

    def test_stored_hash_matches_computed(self):
        g = genesis()
        assert g["hash"] == block_mod.block_hash(g)


# ---------------------------------------------------------------------------
# 3. validate -- hash integrity
# ---------------------------------------------------------------------------

class TestValidateHash:
    def test_valid_genesis_passes(self):
        g = genesis()
        ok, err = block_mod.validate(g, fresh_state(), [], noop_fee_rate)
        assert ok is True, err

    def test_tampered_hash_fails(self):
        g = genesis()
        g["hash"] = "00" * 32
        ok, err = block_mod.validate(g, fresh_state(), [], noop_fee_rate)
        assert ok is False
        assert "hash" in err

    def test_tampered_content_fails(self):
        g = dict(genesis())
        g["height"] = 999
        # Recompute hash so it's consistent but height is wrong
        g["hash"] = block_mod.block_hash(g)
        ok, err = block_mod.validate(g, fresh_state(), [], noop_fee_rate)
        # Either height check or hash recompute catches the tamper
        assert ok is False


# ---------------------------------------------------------------------------
# 4. validate -- parent linkage
# ---------------------------------------------------------------------------

class TestValidateParent:
    def test_block_with_correct_parent_passes(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is True, err

    def test_wrong_previous_hash_fails(self):
        g = genesis()
        b = make_block(1, "00" * 32, [])
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False
        assert "previous_hash" in err

    def test_height_not_parent_plus_one_fails(self):
        g = genesis()
        # Build a block with height=2 against a height-0 parent
        b = make_block(2, g["hash"], [])
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False


# ---------------------------------------------------------------------------
# 5. validate -- timestamp
# ---------------------------------------------------------------------------

class TestValidateTimestamp:
    def test_past_timestamp_passes(self):
        # No minimum-gap rule: any past timestamp is valid
        g = genesis()
        b = make_block(1, g["hash"], [], timestamp_offset=-(BLOCK_CYCLE_SECONDS + 1))
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is True, err

    def test_future_timestamp_fails(self):
        g = genesis()
        from params import GENESIS_TIMESTAMP
        far_future = GENESIS_TIMESTAMP + 365 * 24 * 3600 + 3600  # past the mocked "now"
        correct_rate = block_mod.compute_expected_fee_rate([g])
        b = block_mod.create(1, g["hash"], [], address(0),
                             correct_rate, "aa" * 100, "bb" * 100, timestamp=far_future)
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False
        assert "future" in err


# ---------------------------------------------------------------------------
# 6. validate -- VDF proof (whitepaper Section 3: chain is its own clock)
# ---------------------------------------------------------------------------

class TestValidateVDF:
    def test_vdf_verified_for_height_gt_zero(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        # VDF mock returns True by default -- should pass
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is True, err

    def test_invalid_vdf_proof_fails(self, monkeypatch):
        monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: False)
        g = genesis()
        b = make_block(1, g["hash"], [])
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False
        assert "VDF" in err

    def test_missing_vdf_output_fails(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        b["vdf_output"] = None
        b["hash"] = block_mod.block_hash(b)
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False

    def test_challenge_binds_previous_hash_and_builder(self):
        """The challenge covers both inputs, so neither can be varied freely."""
        g = genesis()
        base  = block_mod.vdf_challenge(g["hash"], address(0))
        other_builder = block_mod.vdf_challenge(g["hash"], address(1))
        other_parent  = block_mod.vdf_challenge("cc" * 32, address(0))
        assert base != other_builder
        assert base != other_parent
        assert len(base) == 32
        # Deterministic: same inputs always give the same challenge.
        assert base == block_mod.vdf_challenge(g["hash"], address(0))

    def test_validate_uses_builder_bound_challenge(self, monkeypatch):
        """validate() must verify against vdf_challenge, not the bare parent hash."""
        seen = {}
        monkeypatch.setattr("block.vdf_mod.verify",
                            lambda challenge, *a, **kw: seen.setdefault("c", challenge) or True)
        g = genesis()
        b = make_block(1, g["hash"], [], builder_index=0)
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is True, err
        assert seen["c"] == block_mod.vdf_challenge(g["hash"], address(0))
        assert seen["c"] != bytes.fromhex(g["hash"])

    def test_stolen_vdf_proof_rejected_under_new_builder(self, monkeypatch):
        """A VDF output is not a bearer token.

        Replay of the real attack: a node receives a broadcast block, keeps
        vdf_output/vdf_proof, swaps in its own builder address to claim the
        reward, and rebroadcasts. The proof only verifies against the
        original builder's challenge, so the copy is rejected.
        """
        g = genesis()
        victim, thief = address(0), address(1)
        honest_challenge = block_mod.vdf_challenge(g["hash"], victim)
        # Stand-in for real VDF verification: a proof verifies only against
        # the challenge it was actually evaluated over.
        monkeypatch.setattr("block.vdf_mod.verify",
                            lambda challenge, *a, **kw: challenge == honest_challenge)

        original = make_block(1, g["hash"], [], builder_index=0)
        ok, err = block_mod.validate(original, fresh_state(), [g], noop_fee_rate)
        assert ok is True, err

        stolen = dict(original)
        stolen["builder"] = thief
        stolen["hash"]    = block_mod.block_hash(stolen)
        ok, err = block_mod.validate(stolen, fresh_state(), [g], noop_fee_rate)
        assert ok is False
        assert "VDF" in err

    def test_transactions_not_bound_into_challenge(self, monkeypatch):
        """Content stays swappable on top of a valid proof.

        A block whose transaction list is replaced keeps a verifying VDF
        proof, so a rejected transaction list can be corrected without
        redoing the ~120 s of sequential work.
        """
        g = genesis()
        builder = address(0)
        honest_challenge = block_mod.vdf_challenge(g["hash"], builder)
        monkeypatch.setattr("block.vdf_mod.verify",
                            lambda challenge, *a, **kw: challenge == honest_challenge)

        st = fresh_state()
        seed_balance(st, 2, 100.0)
        t = make_tx(2, 3, TICKS_PER_LAPSE, st, 0)

        empty    = make_block(1, g["hash"], [],  builder_index=0)
        refilled = make_block(1, g["hash"], [t], builder_index=0,
                              vdf_output=empty["vdf_output"],
                              vdf_proof=empty["vdf_proof"])
        assert refilled["vdf_output"] == empty["vdf_output"]
        ok, err = block_mod.validate(refilled, st, [g], noop_fee_rate)
        assert ok is True, err

    def test_genesis_skips_vdf_check(self):
        g = genesis()
        # Genesis has vdf_output=None -- should still pass
        ok, err = block_mod.validate(g, fresh_state(), [], noop_fee_rate)
        assert ok is True, err

    def test_invalid_builder_address_fails(self):
        g = genesis()
        b = make_block(1, g["hash"], [])
        b["builder"] = "not.a.valid.address"
        b["hash"] = block_mod.block_hash(b)
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False
        assert "builder" in err


# ---------------------------------------------------------------------------
# 7. validate -- tx ordering
# ---------------------------------------------------------------------------

class TestValidateTxOrdering:
    def test_canonical_tx_order_passes(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        # fee_height must equal genesis height (0) because block.validate
        # checks txs against chain_tip_height = block.height - 1 = 0
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, chain_tip_height=0)
        g = genesis()
        b = make_block(1, g["hash"], [t])
        ok, err = block_mod.validate(b, s.snapshot(), [g], noop_fee_rate)
        assert ok is True, err

    def test_wrong_tx_order_fails(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        seed_balance(s, 1, 100.0)
        # Create two txs from different senders (independent nonces)
        t1 = make_tx(0, 2, TICKS_PER_LAPSE, s, chain_tip_height=0)
        t2 = make_tx(1, 2, TICKS_PER_LAPSE, s, chain_tip_height=0)
        sorted_txs = tx_mod.sort_txs([t1, t2])
        # Reverse them to create wrong order
        wrong_order = list(reversed(sorted_txs))
        if wrong_order == sorted_txs:
            pytest.skip("Hashes happened to sort into same order")
        g = genesis()
        b = make_block(1, g["hash"], wrong_order)
        # Rebuild hash to match wrong content
        b["hash"] = block_mod.block_hash(b)
        ok, err = block_mod.validate(b, s.snapshot(), [g], noop_fee_rate)
        assert ok is False
        assert "order" in err


# ---------------------------------------------------------------------------
# 8. validate -- fee_rate check
# ---------------------------------------------------------------------------

class TestValidateFeeRate:
    def test_correct_fee_rate_passes(self):
        g = genesis()
        expected_rate = block_mod.compute_expected_fee_rate([g])
        ts = g["timestamp"] + BLOCK_CYCLE_SECONDS
        b = block_mod.create(1, g["hash"], [], address(0),
                             expected_rate, "aa" * 100, "bb" * 100,
                             timestamp=ts)
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is True, err

    def test_wrong_fee_rate_fails(self):
        g = genesis()
        b = make_block(1, g["hash"], [], fee_rate=INITIAL_FEE_RATE + 9999)
        ok, err = block_mod.validate(b, fresh_state(), [g], noop_fee_rate)
        assert ok is False
        assert "fee rate" in err


# ---------------------------------------------------------------------------
# 9. compute_expected_fee_rate (whitepaper Section 2 asymmetric formula)
# ---------------------------------------------------------------------------

class TestComputeExpectedFeeRate:
    def test_empty_chain_returns_initial_rate(self):
        assert block_mod.compute_expected_fee_rate([]) == INITIAL_FEE_RATE

    def test_zero_volume_decays_slowly(self):
        g = genesis()
        # Empty blocks for 100 slots
        chain = [g]
        for h in range(1, 101):
            blk = make_block(h, chain[-1]["hash"], [])
            blk["tx_bytes"] = 0
            chain.append(blk)
        rate = block_mod.compute_expected_fee_rate(chain)
        # Rate should have dropped but still >= 1
        assert rate >= 1
        assert rate <= INITIAL_FEE_RATE

    def test_high_volume_adjustment_is_above_one(self):
        """vol_ratio > 1 -> adjustment = min(1.05, vol_ratio) > 1.
        The rate only visibly rises once it's large enough to survive int truncation
        (int(rate * 1.05) > rate requires rate >= 21). We test the formula directly.
        """
        from params import FEE_RATE_WINDOW
        import statistics
        g = genesis()
        chain = [g]
        # Seed chain with high-volume blocks
        for h in range(1, FEE_RATE_WINDOW + 1):
            blk = make_block(h, chain[-1]["hash"], [])
            blk["tx_bytes"] = BLOCK_SIZE_TARGET_BYTES * 2
            blk["fee_rate"] = INITIAL_FEE_RATE
            chain.append(blk)
        window = chain[-FEE_RATE_WINDOW:]
        vols = [b.get("tx_bytes", 0) for b in window]
        median_vol = statistics.median(vols)
        vol_ratio = median_vol / BLOCK_SIZE_TARGET_BYTES
        # With 2x volume, vol_ratio = 2.0, adjustment = min(1.05, 2.0) = 1.05
        assert vol_ratio > 1
        adjustment = min(1.05, vol_ratio)
        assert adjustment > 1.0

    def test_rate_minimum_is_one(self):
        g = genesis()
        # Drive rate down with zero-volume blocks
        chain = [g]
        for h in range(1, 501):
            blk = make_block(h, chain[-1]["hash"], [])
            blk["tx_bytes"] = 0
            blk["fee_rate"] = max(1, int(chain[-1].get("fee_rate", INITIAL_FEE_RATE) * 0.999))
            chain.append(blk)
        rate = block_mod.compute_expected_fee_rate(chain)
        assert rate >= 1

    def test_fee_rate_rise_capped_at_5pct_per_block(self):
        """Whitepaper: rises up to 5% per block (min(1.05, vol_ratio))."""
        g = genesis()
        chain = [g]
        for h in range(1, 11):
            blk = make_block(h, chain[-1]["hash"], [])
            blk["tx_bytes"] = BLOCK_SIZE_TARGET_BYTES * 100  # extreme volume
            blk["fee_rate"] = INITIAL_FEE_RATE
            chain.append(blk)
        rate = block_mod.compute_expected_fee_rate(chain)
        # Rate increase from previous step can be at most 5%
        prev_rate = chain[-1].get("fee_rate", INITIAL_FEE_RATE)
        assert rate <= int(prev_rate * 1.05) + 1  # +1 for int rounding


# ---------------------------------------------------------------------------
# 10. assemble
# ---------------------------------------------------------------------------

class TestVdfIterationsAdjustment:
    """Regression coverage for the VDF difficulty-adjustment boundary.

    Both the block that carries a genuine adjustment (built via assemble())
    and the validator checking that same block must agree on the required
    iteration count. Historically these used two different code paths
    (compute_next_vdf_iterations vs get_vdf_iterations) that disagreed
    exactly at boundary heights whenever a bump actually triggered.
    """

    def test_boundary_block_validates_when_bump_triggers(self, monkeypatch):
        monkeypatch.setattr("block.VDF_ADJUST_INTERVAL", 3)
        monkeypatch.setattr("block.VDF_ADJUST_MIN_SECONDS", 1_000_000)
        monkeypatch.setattr("block.VDF_ADJUST_FACTOR", 2.0)

        g = genesis()
        chain = [g]
        for h in range(1, 3):
            blk = make_block(h, chain[-1]["hash"], [], chain=chain)
            chain.append(blk)

        # This block sits exactly at the adjustment boundary (height 3).
        iterations = block_mod.get_vdf_iterations(chain)
        assert iterations == block_mod.VDF_ITERATIONS * 2, \
            "sanity check: the window's real timestamps should trigger a bump"

        fee_rate = block_mod.compute_expected_fee_rate(chain)
        blk3 = block_mod.assemble(chain[-1], [], address(0), fee_rate, iterations)
        assert blk3["vdf_iterations"] == iterations
        blk3["vdf_output"] = "aa" * 100
        blk3["vdf_proof"]  = "bb" * 100
        blk3["hash"] = block_mod.block_hash(blk3)

        ok, err = block_mod.validate(blk3, fresh_state(), chain, noop_fee_rate)
        assert ok is True, err

    def test_iterations_never_decrease(self, monkeypatch):
        monkeypatch.setattr("block.VDF_ADJUST_INTERVAL", 3)
        monkeypatch.setattr("block.VDF_ADJUST_MIN_SECONDS", 1)  # never triggers a bump
        monkeypatch.setattr("block.VDF_ADJUST_FACTOR", 2.0)

        g = genesis()
        chain = [g]
        for h in range(1, 4):
            blk = make_block(h, chain[-1]["hash"], [], chain=chain)
            chain.append(blk)

        assert block_mod.get_vdf_iterations(chain) == block_mod.VDF_ITERATIONS


class TestAssemble:
    def test_assemble_returns_block_without_hash(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        assert "hash" not in b

    def test_assemble_includes_valid_txs(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, TICKS_PER_LAPSE, s, 0)
        g = genesis()
        b = block_mod.assemble(g, [t], address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        assert len(b["transactions"]) == 1

    def test_assemble_respects_size_limit(self):
        g = genesis()
        # Create many dummy tx-shaped dicts to fill the block
        dummy_txs = []
        s = fresh_state()
        seed_balance(s, 0, 10_000.0)
        for i in range(50):
            t = make_tx(0, 1, TICKS_PER_LAPSE, s, 0)
            dummy_txs.append(t)
            try:
                s.apply_tx(t)
            except Exception:
                break
        b = block_mod.assemble(g, dummy_txs, address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        blk_size = block_mod.block_size({**b, "hash": "x"})
        assert blk_size <= BLOCK_SIZE_LIMIT

    def test_assemble_records_tx_bytes(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        assert "tx_bytes" in b

    def test_assemble_height_is_parent_plus_one(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        assert b["height"] == g["height"] + 1

    def test_assemble_previous_hash_matches_tip(self):
        g = genesis()
        b = block_mod.assemble(g, [], address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        assert b["previous_hash"] == g["hash"]
