"""
Unit tests for tx.py: the ciphertext transaction format.

Covers: inner payload creation/validation, confirmation creation/
validation (fields, signature, fee, balance), resolution creation/
validation, tx_hash/tx_size, sort_txs.

All tests are pure and local -- no network, no chain, no disk. Uses
fixtures.TEST_ITERATIONS (a tiny puzzle difficulty) so solving is instant;
the real TIMELOCK_ITERATIONS is a separate, much larger protocol constant
never exercised directly in unit tests.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import crypto
import tx as tx_mod
import state as state_mod
from params import INITIAL_FEE_RATE, TICKS_PER_LAPSE
from tests.fixtures import (
    keypair, address, pubkey_hex, make_tx, make_confirmation,
    solve_confirmation, apply_transfer, seed_balance,
)


def fresh_state():
    return state_mod.State()


def get_fee_rate(height):
    return INITIAL_FEE_RATE


# ---------------------------------------------------------------------------
# 1. Inner payload
# ---------------------------------------------------------------------------

class TestInnerPayload:
    def test_create_returns_dict_with_required_fields(self):
        s = fresh_state()
        seed_balance(s, 0)
        inner = tx_mod.create_inner_payload(
            address(0), pubkey_hex(0), [{"to": address(1), "amount": 1}],
            1, keypair(0)[0])
        for field in ["from", "pubkey", "outputs", "nonce", "signature"]:
            assert field in inner

    def test_valid_inner_payload_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        ok, err = tx_mod.validate_inner_payload(resolve["payload"], s)
        assert ok is True, err

    def test_inner_signature_must_match_from_address(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        payload = dict(resolve["payload"])
        payload["pubkey"] = pubkey_hex(2)
        ok, err = tx_mod.validate_inner_payload(payload, s)
        assert ok is False

    def test_inner_nonce_must_not_be_reused(self):
        """Nonces only need to be unique per sender, not sequential (see
        tx.generate_nonce): resolution order is already forced to match
        confirmation order by the gapless queue rule, so replay protection
        is the nonce's only remaining job."""
        s = fresh_state()
        seed_balance(s, 0)
        nonce = tx_mod.generate_nonce()
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, nonce_override=nonce)
        ok, err = tx_mod.validate_inner_payload(resolve["payload"], s)
        assert ok is True, err

        s.mark_nonce_used(address(0), nonce)
        ok, err = tx_mod.validate_inner_payload(resolve["payload"], s)
        assert ok is False
        assert "nonce" in err

    def test_inner_nonce_must_be_fixed_width_hex(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10,
                                    nonce_override="not-a-valid-nonce")
        ok, err = tx_mod.validate_inner_payload(resolve["payload"], s)
        assert ok is False
        assert "nonce" in err

    def test_inner_balance_checked_against_outputs_only_no_fee(self):
        """The inner payload carries no fee (fee is collected from the
        broadcaster at confirmation time), so its balance check is against
        total outputs alone."""
        s = fresh_state()
        s.credit(address(0), TICKS_PER_LAPSE)
        s.total_minted += TICKS_PER_LAPSE
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        ok, err = tx_mod.validate_inner_payload(resolve["payload"], s)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 2. Confirmation: fields, signature, fee, balance
# ---------------------------------------------------------------------------

class TestConfirmationFields:
    def test_valid_confirmation_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is True, err

    def test_missing_field_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        del confirm["broadcaster"]
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False
        assert "missing field" in err

    def test_wrong_kind_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        confirm["kind"] = "resolve"
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False

    def test_malformed_puzzle_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        confirm["puzzle"]["N"] = "not hex!!"
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False

    def test_non_positive_iterations_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        confirm["iterations"] = 0
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False
        assert "iterations" in err

    def test_iterations_mismatch_against_chain_expectation_fails(self):
        """iterations is not sender-chosen: it must equal what every
        validator independently derives from chain state (see
        timelock.get_timelock_iterations), the same way block.py checks
        vdf_iterations. A confirmation is not allowed to claim a difficulty
        the chain didn't actually require."""
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        ok, err = tx_mod.validate_confirmation(
            confirm, s, 10, get_fee_rate, expected_iterations=confirm["iterations"] + 1)
        assert ok is False
        assert "iterations mismatch" in err

    def test_negative_fee_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        confirm["fee"] = -1
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False


class TestConfirmationSignature:
    def test_wrong_pubkey_for_broadcaster_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        confirm["pubkey"] = pubkey_hex(2)
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False
        assert "pubkey" in err or "signature" in err

    def test_tampered_signature_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        confirm["signature"] = "00" * 752
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False

    def test_broadcaster_need_not_be_real_sender(self):
        """Protocol requirement: broadcaster != sender must be accepted."""
        s = fresh_state()
        seed_balance(s, 0)
        seed_balance(s, 9)  # broadcaster needs balance to cover the fee
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, broadcaster_index=9)
        assert confirm["broadcaster"] == address(9)
        assert resolve["payload"]["from"] == address(0)
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is True, err


class TestConfirmationFeeAndBalance:
    def test_fee_mismatch_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        sk, _ = keypair(0)
        confirm["fee"] = confirm["fee"] + 999
        msg = crypto.serialize_for_signing(confirm)
        confirm["signature"] = crypto.sign(msg, sk).hex()
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False
        assert "fee mismatch" in err

    def test_fee_height_in_future_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, fee_height_override=15)
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False
        assert "future" in err

    def test_insufficient_balance_for_fee_fails(self):
        s = fresh_state()  # broadcaster has zero balance
        inner = tx_mod.create_inner_payload(
            address(0), pubkey_hex(0), [{"to": address(1), "amount": 1}], 1, keypair(0)[0])
        confirm = make_confirmation(0, inner, fee_height=10)
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is False
        assert "insufficient" in err

    def test_only_fee_is_checked_not_transfer_amount(self):
        """The broadcaster's balance check covers the fee only -- the real
        transfer amount is invisible until resolution."""
        s = fresh_state()
        s.credit(address(0), 1)  # tiny balance: can't cover a real transfer
        s.total_minted += 1
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        # Give exactly the fee as balance (already credited 1 tick, bump to fee amount)
        s.credit(address(0), confirm["fee"])
        ok, err = tx_mod.validate_confirmation(confirm, s, 10, get_fee_rate)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 3. Resolution
# ---------------------------------------------------------------------------

class TestResolution:
    def test_valid_resolution_passes(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        ok, err = tx_mod.validate_resolution(resolve, confirm, s)
        assert ok is True, err

    def test_unknown_confirmation_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        ok, err = tx_mod.validate_resolution(resolve, None, s)
        assert ok is False
        assert "confirmed_tx_hash" in err

    def test_wrong_key_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        resolve = dict(resolve)
        resolve["K_hex"] = format(int(resolve["K_hex"], 16) ^ 1, "x")
        ok, err = tx_mod.validate_resolution(resolve, confirm, s)
        assert ok is False

    def test_tampered_payload_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        resolve = dict(resolve)
        resolve["payload"] = dict(resolve["payload"])
        resolve["payload"]["outputs"] = [{"to": address(9), "amount": 999}]
        ok, err = tx_mod.validate_resolution(resolve, confirm, s)
        assert ok is False

    def test_invalid_resolver_address_fails(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        resolve = dict(resolve)
        resolve["resolver"] = "not.an.address"
        ok, err = tx_mod.validate_resolution(resolve, confirm, s)
        assert ok is False

    def test_resolution_with_unpayable_payload_still_validates(self):
        """A resolution's crypto proof is independent of whether the
        decrypted payload can still be applied: the payload's content was
        fixed by whoever built the original confirmation, not by the
        resolver, so its later unspendability (e.g. the sender already
        spent the same balance via a different confirmation that resolved
        first) must not make an otherwise-correct resolution invalid --
        see validate_resolution's docstring for why (queue liveness)."""
        s = fresh_state()
        seed_balance(s, 0, 1.0)  # just enough for one transfer, not two
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        # Sender's balance is now fully spent elsewhere by the time this
        # resolution is checked (simulating the other confirmation winning
        # the race to resolve first).
        s.debit(address(0), s.get_balance(address(0)))
        ok, err = tx_mod.validate_resolution(resolve, confirm, s)
        assert ok is True, err
        payload_ok, payload_err = tx_mod.payload_is_valid(resolve["payload"], s)
        assert payload_ok is False
        assert "insufficient" in payload_err

    def test_resolution_with_reused_nonce_payload_still_validates(self):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        s.mark_nonce_used(address(0), resolve["payload"]["nonce"])
        ok, err = tx_mod.validate_resolution(resolve, confirm, s)
        assert ok is True, err
        payload_ok, payload_err = tx_mod.payload_is_valid(resolve["payload"], s)
        assert payload_ok is False
        assert "nonce" in payload_err

    def test_first_solver_identity_cannot_be_proven(self):
        """Whitepaper: no cryptographic way exists to prove who solved
        first. Anyone who has seen a published resolution can resubmit it
        under their own resolver address and it still validates -- this
        is a known, accepted fee-fairness limitation, not a bug."""
        s = fresh_state()
        seed_balance(s, 0)
        confirm, resolve = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, resolver_index=1)
        hijacked = dict(resolve)
        hijacked["resolver"] = address(7)
        ok, err = tx_mod.validate_resolution(hijacked, confirm, s)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 4. tx_hash / tx_size
# ---------------------------------------------------------------------------

class TestTxHashAndSize:
    def test_hash_returns_64_char_hex(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        h = tx_mod.tx_hash(confirm)
        assert isinstance(h, str) and len(h) == 64

    def test_hash_is_deterministic(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        assert tx_mod.tx_hash(confirm) == tx_mod.tx_hash(confirm)

    def test_different_confirmations_have_different_hashes(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        confirm1, resolve1 = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        apply_transfer(s, confirm1, resolve1)
        confirm2, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        assert tx_mod.tx_hash(confirm1) != tx_mod.tx_hash(confirm2)

    def test_tx_size_excludes_signature(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        size = tx_mod.tx_size(confirm)
        fields_no_sig = {k: v for k, v in confirm.items() if k != "signature"}
        raw_size = len(crypto.canonical_json(fields_no_sig))
        assert size == raw_size

    def test_tx_size_in_block_first_position_no_comma(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        s0 = tx_mod.tx_size_in_block(confirm, position=0)
        s1 = tx_mod.tx_size_in_block(confirm, position=1)
        assert s1 == s0 + 1


# ---------------------------------------------------------------------------
# 5. compute_fee
# ---------------------------------------------------------------------------

class TestComputeFee:
    def test_fee_positive_for_realistic_confirmation(self):
        s = fresh_state()
        seed_balance(s, 0)
        confirm, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10)
        assert confirm["fee"] > 0

    def test_zero_rate_yields_zero_fee(self):
        inner = tx_mod.create_inner_payload(
            address(0), pubkey_hex(0), [{"to": address(1), "amount": 1}], 1, keypair(0)[0])
        confirm = make_confirmation(0, inner, fee_height=10, fee_rate=0)
        assert confirm["fee"] == 0

    def test_higher_rate_yields_higher_fee(self):
        inner = tx_mod.create_inner_payload(
            address(0), pubkey_hex(0), [{"to": address(1), "amount": 1}], 1, keypair(0)[0])
        low  = make_confirmation(0, inner, fee_height=10, fee_rate=1)
        high = make_confirmation(0, inner, fee_height=10, fee_rate=100)
        assert high["fee"] > low["fee"]


# ---------------------------------------------------------------------------
# 6. sort_txs (confirmations only, no per-broadcaster nonce component)
# ---------------------------------------------------------------------------

class TestSortTxs:
    def test_empty_list_returns_empty(self):
        assert tx_mod.sort_txs([]) == []

    def test_sorted_by_fee_height_asc(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        c1, r1 = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, fee_height_override=10)
        apply_transfer(s, c1, r1)
        c2, _ = make_tx(0, 1, TICKS_PER_LAPSE, s, 10, fee_height_override=8)
        txs = tx_mod.sort_txs([c1, c2])
        assert txs[0]["fee_height"] <= txs[1]["fee_height"]

    def test_sort_is_stable_by_hash_tiebreak(self):
        s = fresh_state()
        seed_balance(s, 0, 1000.0)
        seed_balance(s, 1, 1000.0)
        c1, _ = make_tx(0, 2, TICKS_PER_LAPSE, s, 10)
        c2, _ = make_tx(1, 2, TICKS_PER_LAPSE, s, 10)
        sorted_once  = tx_mod.sort_txs([c1, c2])
        sorted_twice = tx_mod.sort_txs([c2, c1])
        assert ([tx_mod.tx_hash(t) for t in sorted_once]
                == [tx_mod.tx_hash(t) for t in sorted_twice])
