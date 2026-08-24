"""
Shared test fixtures and helpers used across the Scorchcoin test suite.

Mirrors the pattern used by bitcoin-core and ethereum/go-ethereum test
helpers: one place for deterministic keypairs, address generation,
minimal valid block construction, and state seeding so individual test
modules stay focused on the behaviour they exercise.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time

import crypto
import block as block_mod
import state as state_mod
import tx as tx_mod
import pob as pob_mod
from params import (
    GENESIS_TIMESTAMP,
    INITIAL_FEE_RATE,
    EMBERS_PER_SCH,
    SUPPLY_CAP,
)

# ---------------------------------------------------------------------------
# Fixed keypairs -- generated once, reused across the suite for speed.
# FALCON-512 keygen is ~5ms; pre-generating avoids 100+ keygen calls.
# ---------------------------------------------------------------------------

_KEYPAIRS: list[tuple[bytes, bytes]] = []


def _ensure_keypairs(n: int):
    while len(_KEYPAIRS) < n:
        sk, pk = crypto.generate_keypair()
        _KEYPAIRS.append((sk, pk))


def keypair(index: int) -> tuple[bytes, bytes]:
    """Return (sk, pk) for the given index. Generated lazily and cached."""
    _ensure_keypairs(index + 1)
    return _KEYPAIRS[index]


def address(index: int) -> str:
    """Return the BIP39 address for keypair at index."""
    _, pk = keypair(index)
    return crypto.public_key_to_address(pk)


def pubkey_hex(index: int) -> str:
    _, pk = keypair(index)
    return pk.hex()


# ---------------------------------------------------------------------------
# Minimal valid transaction builder
# ---------------------------------------------------------------------------

def make_tx(
    sender_index: int,
    recipient_index: int,
    amount: int,
    state: "state_mod.State",
    chain_tip_height: int,
    fee_rate: int = INITIAL_FEE_RATE,
    outputs_override: list | None = None,
    nonce_override: int | None = None,
    fee_height_override: int | None = None,
):
    """Build and sign a minimal valid transaction."""
    sk, _ = keypair(sender_index)
    from_addr = address(sender_index)
    to_addr   = address(recipient_index)
    pk_hex    = pubkey_hex(sender_index)

    nonce      = (nonce_override if nonce_override is not None
                  else state.get_nonce(from_addr) + 1)
    fee_height = (fee_height_override if fee_height_override is not None
                  else chain_tip_height)

    outputs = outputs_override or [{"to": to_addr, "amount": amount}]

    fee = tx_mod.compute_fee(from_addr, pk_hex, outputs, nonce, fee_height, fee_rate)

    return tx_mod.create(
        from_addr, pk_hex, outputs, nonce, fee_height, fee, sk
    )


def make_burn_tx(
    sender_index: int,
    amount: int,
    state: "state_mod.State",
    chain_tip_height: int,
    fee_rate: int = INITIAL_FEE_RATE,
):
    """Build a transaction with a single burn output."""
    sk, _ = keypair(sender_index)
    from_addr = address(sender_index)
    pk_hex_val = pubkey_hex(sender_index)
    nonce = state.get_nonce(from_addr) + 1

    burn_out = {"to": pob_mod.BURN_ADDRESS, "amount": amount}
    fee = tx_mod.compute_fee(
        from_addr, pk_hex_val, [burn_out], nonce, chain_tip_height, fee_rate
    )
    return tx_mod.create(from_addr, pk_hex_val, [burn_out], nonce, chain_tip_height, fee, sk)


# ---------------------------------------------------------------------------
# State seeding
# ---------------------------------------------------------------------------

def seed_balance(state: "state_mod.State", index: int, amount_ech: float = 100.0):
    """Credit an address with embers. Bypasses tx validation -- for test setup only."""
    embers = int(amount_ech * EMBERS_PER_SCH)
    state.credit(address(index), embers)
    state.total_minted += embers


# ---------------------------------------------------------------------------
# Minimal block builder (bypasses VDF for unit tests)
# ---------------------------------------------------------------------------

_BASE_TS = GENESIS_TIMESTAMP + 120  # one cycle after genesis


def make_block(
    height: int,
    previous_hash: str,
    transactions: list,
    builder_index: int = 0,
    fee_rate: int | None = None,
    timestamp_offset: int = 0,
    vdf_output: str | None = None,
    vdf_proof: str | None = None,
    chain: list | None = None,
) -> dict:
    """Create a block dict without a real VDF proof (for unit tests that mock VDF).

    fee_rate defaults to None, which means: compute the correct rate from
    block.compute_expected_fee_rate(chain).  Pass chain=[...] when you have one,
    or pass an explicit fee_rate to override.  This ensures block.validate
    does not fail with 'fee rate mismatch'.
    """
    if fee_rate is None:
        if chain is not None:
            fee_rate = block_mod.compute_expected_fee_rate(chain)
        else:
            # Build a minimal chain stub sufficient for the fee formula:
            # a single genesis block produces the rate for height-1 blocks.
            stub = [genesis()]
            for h in range(1, height):
                prev = stub[-1]
                stub_blk = {
                    "height": h,
                    "fee_rate": block_mod.compute_expected_fee_rate(stub),
                    "tx_bytes": 0,
                    "transactions": [],
                }
                stub.append(stub_blk)
            fee_rate = block_mod.compute_expected_fee_rate(stub)

    ts = _BASE_TS + height * 120 + timestamp_offset
    blk = block_mod.create(
        height=height,
        previous_hash=previous_hash,
        transactions=transactions,
        builder=address(builder_index),
        fee_rate=fee_rate,
        vdf_output=vdf_output or ("aa" * 100),
        vdf_proof=vdf_proof or ("bb" * 100),
        timestamp=ts,
    )
    return blk


def genesis() -> dict:
    return block_mod.create_genesis()


# ---------------------------------------------------------------------------
# Chain helpers
# ---------------------------------------------------------------------------

def build_chain(length: int, validate_each: bool = False) -> list[dict]:
    """Return a genesis + (length-1) blocks. VDF is mocked via monkeypatching
    at the module level in conftest.py; callers must patch vdf.verify themselves
    when they need block.validate to pass.
    """
    chain = [genesis()]
    for h in range(1, length):
        blk = make_block(h, chain[-1]["hash"], [], builder_index=0)
        chain.append(blk)
    return chain
