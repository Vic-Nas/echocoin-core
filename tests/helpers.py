"""Shared test fixtures and helpers."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import crypto
import tx as tx_mod
import state as state_mod
import block as block_mod
import mining
import mempool as mempool_mod


def make_keypair():
    sk, pk = crypto.generate_keypair()
    addr = crypto.public_key_to_address(pk)
    return sk, pk, pk.hex(), addr


def funded_state(addr, balance=1_000_000):
    """Return a State with one funded address."""
    s = state_mod.State()
    s.credit(addr, balance)
    return s


def make_signed_tx(sk, pk_hex, from_addr, to_addr, amount, nonce, fee_height, fee):
    """Create a signed transaction."""
    outputs = [{"to": to_addr, "amount": amount}]
    return tx_mod.create(from_addr, pk_hex, outputs, nonce, fee_height, fee, sk)


def make_valid_tx(sk, pk_hex, from_addr, to_addr, amount, nonce, fee_height, fee_rate):
    """Create a signed tx with correctly computed fee (fixed-point)."""
    outputs = [{"to": to_addr, "amount": amount}]
    fee, t = tx_mod.compute_fee_fixed_point(from_addr, pk_hex, outputs, nonce, fee_height, fee_rate, sk)
    return t


def fee_rate_fn(rate=1):
    """Return a get_fee_rate_at_height that always returns `rate`."""
    def _fn(height):
        return rate
    return _fn


def make_solution(prev_hash, pk_bytes, pk_hex, nonce_int, difficulty_target):
    """Try a specific nonce and return solution dict if valid, None otherwise."""
    puzzle = mining.derive_puzzle(prev_hash, pk_bytes)
    valid, sol_hash = mining.check_solution(puzzle, nonce_int, difficulty_target)
    if valid:
        return {"pubkey": pk_hex, "nonce": nonce_int, "solution_hash": sol_hash}
    return None


def find_valid_solution(prev_hash, pk_bytes, pk_hex, difficulty_target, max_attempts=100000):
    """Brute-force find a valid solution."""
    puzzle = mining.derive_puzzle(prev_hash, pk_bytes)
    for n in range(max_attempts):
        valid, sol_hash = mining.check_solution(puzzle, n, difficulty_target)
        if valid:
            return {"pubkey": pk_hex, "nonce": n, "solution_hash": sol_hash}
    raise RuntimeError("Could not find valid solution in max_attempts")


def genesis_chain():
    """Return [genesis_block]."""
    return [block_mod.create_genesis()]


def make_chain(length):
    """Build a valid chain of `length` blocks starting from genesis.
    Each block's timestamp is parent_timestamp + BLOCK_CYCLE_SECONDS so
    timestamp validation passes without sleeping.
    """
    from params import BLOCK_CYCLE_SECONDS
    chain = [block_mod.create_genesis()]
    for i in range(1, length):
        parent = chain[-1]
        blk = block_mod.create(
            height=i,
            previous_hash=parent["hash"],
            transactions=[],
            solver_summaries=[],
            difficulty_target=block_mod.compute_expected_difficulty(chain),
            fee_rate=block_mod.compute_expected_fee_rate(chain),
            timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
        )
        chain.append(blk)
    return chain
