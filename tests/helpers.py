"""Shared test fixtures and helpers for Echocoin tests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import mempool as mempool_mod
import crypto
import state as state_mod
import tx as tx_mod
from params import BLOCK_CYCLE_SECONDS


def make_keypair():
    sk, pk = crypto.generate_keypair()
    addr = crypto.public_key_to_address(pk)
    return sk, pk, pk.hex(), addr


def funded_state(addr, balance=100_000_000):
    """Return a State with one funded address."""
    s = state_mod.State()
    s.credit(addr, balance)
    return s


def make_signed_tx(sk, pk_hex, from_addr, to_addr, amount, nonce, fee_height, fee):
    outputs = [{"to": to_addr, "amount": amount}]
    return tx_mod.create(from_addr, pk_hex, outputs, nonce, fee_height, fee, sk)


def make_valid_tx(sk, pk_hex, from_addr, to_addr, amount, nonce, fee_height, fee_rate):
    outputs = [{"to": to_addr, "amount": amount}]
    fee = tx_mod.compute_fee(from_addr, pk_hex, outputs, nonce, fee_height, fee_rate)
    return tx_mod.create(from_addr, pk_hex, outputs, nonce, fee_height, fee, sk)


def fee_rate_fn(rate=1):
    """Return a get_fee_rate_at_height callable that always returns `rate`."""
    def _fn(height):
        return rate
    return _fn


def genesis_chain():
    """Return [genesis_block]."""
    return [block_mod.create_genesis()]


def make_block(chain, builder_addr=None, txs=None, timestamp=None):
    """Build a valid-looking block at chain tip.

    Does NOT attach a real VDF proof -- vdf_output and vdf_proof are set to
    sentinel hex strings so structural checks pass, but vdf.verify() is not
    called (block.validate() skips VDF verification when the chain is built
    in tests via a monkeypatched vdf module; see conftest.py or patch directly).
    """
    parent = chain[-1]
    ts = timestamp if timestamp is not None else parent["timestamp"] + BLOCK_CYCLE_SECONDS
    if builder_addr is None:
        _, _, _, builder_addr = make_keypair()
    return block_mod.create(
        height=parent["height"] + 1,
        previous_hash=parent["hash"],
        transactions=txs or [],
        builder=builder_addr,
        fee_rate=block_mod.compute_expected_fee_rate(chain),
        vdf_output="00" * 100,   # sentinel -- not cryptographically valid
        vdf_proof="00" * 200,    # sentinel
        timestamp=ts,
    )


def make_chain(length, builder_addr=None):
    """Build a valid-looking chain of `length` blocks from genesis.

    Blocks have sentinel VDF proofs. Tests that exercise VDF validation
    should monkeypatch vdf.verify to return True.
    """
    chain = [block_mod.create_genesis()]
    for i in range(1, length):
        blk = make_block(chain, builder_addr)
        chain.append(blk)
    return chain
