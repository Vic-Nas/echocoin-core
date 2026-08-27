"""
Shared test fixtures and helpers used across the LapseCoin test suite.

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
import timelock as timelock_mod
from params import (
    GENESIS_TIMESTAMP,
    INITIAL_FEE_RATE,
    TICKS_PER_LAPSE,
    SUPPLY_CAP,
)

# Tiny puzzle difficulty so tests can solve instantly. Never used for
# anything but test fixtures -- TIMELOCK_ITERATIONS (params.py) is the
# real, fixed protocol constant.
TEST_ITERATIONS = 8

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
# Minimal valid confirm/resolve pair builder
# ---------------------------------------------------------------------------

def make_confirmation(
    broadcaster_index: int,
    inner_payload: dict,
    fee_height: int,
    fee_rate: int = INITIAL_FEE_RATE,
    iterations: int = TEST_ITERATIONS,
):
    """Build and sign a "confirm" ciphertext submission wrapping
    inner_payload, broadcast under broadcaster_index's key."""
    sk = keypair(broadcaster_index)[0]
    bcast_addr = address(broadcaster_index)
    pk_hex = pubkey_hex(broadcaster_index)
    confirm = tx_mod.create_confirmation(bcast_addr, pk_hex, inner_payload,
                                          fee_height, 0, sk, iterations=iterations)
    fee = tx_mod.compute_fee(bcast_addr, pk_hex, confirm["puzzle"], fee_height, fee_rate)
    confirm["fee"] = fee
    msg = crypto.serialize_for_signing(confirm)
    confirm["signature"] = crypto.sign(msg, sk).hex()
    return confirm


def solve_confirmation(confirm_tx, resolver_index: int = 0, iterations: int = TEST_ITERATIONS):
    """Solve a confirmation built with make_confirmation and build the
    matching resolution. Uses TEST_ITERATIONS so this is instant."""
    puzzle = confirm_tx["puzzle"]
    N, x = int(puzzle["N"], 16), int(puzzle["x"], 16)
    K = timelock_mod.solve_for_key(N, x, iterations=iterations)
    payload = timelock_mod.decrypt_with_key(K, N.bit_length(), puzzle["ciphertext"])
    import json
    payload = json.loads(payload)
    confirmed_hash = tx_mod.tx_hash(confirm_tx)
    return tx_mod.create_resolution(confirmed_hash, address(resolver_index), K, payload)


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
    broadcaster_index: int | None = None,
    resolver_index: int = 50,
):
    """Build a (confirm, resolve) pair for a transfer from sender_index to
    recipient_index. Returns (confirm_tx, resolve_tx); including both in a
    block (in that relative order -- or resolve first is fine too, since a
    same-block confirm+resolve pair is looked up from the block's own
    local_confirmations map) reproduces the old atomic single-tx behavior
    for test purposes. broadcaster_index defaults to sender_index (the
    wallet-layer default described in tx.py); pass a different index to
    exercise broadcaster != sender.
    """
    sk, _ = keypair(sender_index)
    from_addr = address(sender_index)
    to_addr   = address(recipient_index)
    pk_hex    = pubkey_hex(sender_index)

    nonce      = (nonce_override if nonce_override is not None
                  else state.get_nonce(from_addr) + 1)
    fee_height = (fee_height_override if fee_height_override is not None
                  else chain_tip_height)

    outputs = outputs_override or [{"to": to_addr, "amount": amount}]
    inner = tx_mod.create_inner_payload(from_addr, pk_hex, outputs, nonce, sk)

    bcast_idx = sender_index if broadcaster_index is None else broadcaster_index
    confirm = make_confirmation(bcast_idx, inner, fee_height, fee_rate)
    resolve = solve_confirmation(confirm, resolver_index=resolver_index)
    return confirm, resolve


def apply_transfer(state, confirm, resolve):
    """Apply a (confirm, resolve) pair directly to a State, bypassing
    validation -- mirrors the old State.apply_tx for test convenience."""
    state.apply_confirmation(confirm, tx_mod.tx_hash(confirm))
    state.apply_resolution(resolve)


# ---------------------------------------------------------------------------
# State seeding
# ---------------------------------------------------------------------------

def seed_balance(state: "state_mod.State", index: int, amount_ech: float = 100.0):
    """Credit an address with ticks. Bypasses tx validation -- for test setup only."""
    ticks = int(amount_ech * TICKS_PER_LAPSE)
    state.credit(address(index), ticks)
    state.total_minted += ticks


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
