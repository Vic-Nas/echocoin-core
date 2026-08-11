"""Block creation, validation, serialization. Pure functions on dicts."""

import json
import statistics

import crypto
import tx as tx_mod
import mining
import time as _time

from params import (
    BLOCK_SIZE_LIMIT,
    BLOCK_CYCLE_SECONDS,
    FEE_RATE_WINDOW,
    BLOCK_SIZE_TARGET_BYTES,
    INITIAL_FEE_RATE,
    GENESIS_MESSAGE,
    GENESIS_TIMESTAMP,
)

# Re-export so tests can import from here
INITIAL_DIFFICULTY_TARGET = mining.INITIAL_DIFFICULTY_TARGET

_TX_SORT_FIELDS = {"fee_height", "nonce"}


def create_genesis():
    """
    Create the genesis block (block 0). Hardcoded and deterministic.
    GENESIS_TIMESTAMP anchors block 0. Every subsequent block carries its own
    timestamp, validated as at least BLOCK_CYCLE_SECONDS after its parent.
    """
    blk = {
        "height":           0,
        "previous_hash":    "0" * 64,
        "transactions":     [],
        "solver_summaries": [],
        "difficulty_target": INITIAL_DIFFICULTY_TARGET,
        "fee_rate":         INITIAL_FEE_RATE,
        "timestamp":        GENESIS_TIMESTAMP,
        "message":          GENESIS_MESSAGE,
    }
    blk["hash"] = block_hash(blk)
    return blk


def create(height, previous_hash, transactions, solver_summaries,
           difficulty_target, fee_rate, timestamp=None):
    """Create a new block dict.

    timestamp: unix time for this block. Defaults to now. Tests should pass
    an explicit value (e.g. parent_timestamp + BLOCK_CYCLE_SECONDS) to build
    chains with valid intervals without sleeping.
    solver_summaries: compact per-solver record produced by
        mining.summarize_solutions(). Each entry is
        {"address": <str>, "count": <int>}.
        Full solution dicts (pubkey, nonce, solution_hash) are used
        during the live round for PoW verification, then discarded.
        Rewards are applied to state at block-creation time from the
        live solution list; the summary is stored only to support
        difficulty adjustment and block explorer display on sync.
    """
    blk = {
        "height": height,
        "previous_hash": previous_hash,
        "timestamp": timestamp if timestamp is not None else _time.time(),
        "transactions": transactions,
        "solver_summaries": solver_summaries,
        "difficulty_target": difficulty_target,
        "fee_rate": fee_rate,
    }
    blk["hash"] = block_hash(blk)
    return blk


def block_hash(blk):
    """Deterministic hash of block (excludes 'hash' field itself)."""
    fields = {k: v for k, v in blk.items() if k != "hash"}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return crypto.sha256_hex(canonical)


def block_size(blk):
    """Size in bytes of serialized block."""
    return len(json.dumps(blk, sort_keys=True, separators=(",", ":")).encode())


def validate(blk, state, chain, get_fee_rate_at_height):
    """
    Full block validation. Returns (True, None) or (False, error).

    blk: block dict
    state: State object (balances/nonces at parent block)
    chain: list of previous blocks
    get_fee_rate_at_height: callable(height) -> fee_rate

    PoW verification (individual nonce/solution_hash checks) is performed
    by live validators during the round from in-memory broadcast data.
    Syncing nodes verify structural integrity and reward consistency from
    the compact solver_summaries field instead.

    IMPORTANT: this function applies transactions to `state` in place as
    part of balance-constraint checking (step 8). Callers must pass a
    snapshot (state.snapshot()) rather than the live state object, so that
    a validation failure leaves the live state untouched. Rewards are NOT
    applied here; the caller is responsible for calling apply_rewards()
    after a successful validation.
    """
    height = blk["height"]

    # 1. Hash integrity
    if blk.get("hash") != block_hash(blk):
        return False, "block hash mismatch"

    # 2. Previous hash
    if height > 0:
        parent = chain[-1] if chain else None
        if parent is None:
            return False, "no parent block"
        if blk["previous_hash"] != parent["hash"]:
            return False, "previous_hash does not match parent"
        if blk["height"] != parent["height"] + 1:
            return False, "height does not follow parent"

    # 3. Timestamp
    # Every block must carry a unix timestamp. Two rules:
    #   a) Not more than 30 seconds in the future (clock skew tolerance).
    #   b) At least BLOCK_CYCLE_SECONDS after its parent's timestamp.
    # Rule (b) makes block interval a protocol rule, not just a local convention.
    ts = blk.get("timestamp")
    if not isinstance(ts, (int, float)):
        return False, "block missing timestamp"
    now = _time.time()
    if ts > now + 30:
        return False, f"block timestamp {ts} is too far in the future"
    if height > 0:
        parent_ts = chain[-1].get("timestamp")
        if not isinstance(parent_ts, (int, float)):
            return False, "parent block missing timestamp"
        if ts < parent_ts + BLOCK_CYCLE_SECONDS:
            return False, (
                f"block timestamp {ts} is less than "
                f"parent {parent_ts} + {BLOCK_CYCLE_SECONDS}s"
            )

    # 4. Block size
    size = block_size(blk)
    if size > BLOCK_SIZE_LIMIT:
        return False, f"block exceeds size limit: {size} > {BLOCK_SIZE_LIMIT}"

    # 5. Validate solver_summaries structure
    if height > 0:
        summaries = blk.get("solver_summaries", [])
        if not isinstance(summaries, list):
            return False, "solver_summaries must be a list"
        seen_addrs = set()
        for s in summaries:
            if not isinstance(s, dict):
                return False, "each solver summary must be a dict"
            addr = s.get("address", "")
            count = s.get("count", 0)
            if not isinstance(addr, str) or not crypto.is_valid_address(addr):
                return False, f"invalid address in solver summary: {addr!r}"
            if not isinstance(count, int) or count < 1:
                return False, "solver summary count must be a positive integer"
            if addr in seen_addrs:
                return False, f"duplicate address in solver_summaries: {addr}"
            seen_addrs.add(addr)

    # 6. Transaction ordering
    if blk["transactions"]:
        for i, t in enumerate(blk["transactions"]):
            if not isinstance(t, dict):
                return False, f"transaction at position {i} is not a dict"
            missing = _TX_SORT_FIELDS - t.keys()
            if missing:
                return False, f"transaction at position {i} missing fields for ordering: {missing}"
        sorted_txs = tx_mod.sort_txs(blk["transactions"])
        for i, (actual, expected) in enumerate(zip(blk["transactions"], sorted_txs)):
            if tx_mod.tx_hash(actual) != tx_mod.tx_hash(expected):
                return False, f"transaction ordering violation at position {i}"

    # 7. Difficulty target check
    if height > 0:
        expected_target = compute_expected_difficulty(chain)
        if blk["difficulty_target"] != expected_target:
            return False, "difficulty target mismatch"

    # 8. Fee rate check
    if height > 0:
        expected_rate = compute_expected_fee_rate(chain)
        if blk["fee_rate"] != expected_rate:
            return False, "fee rate mismatch"

    # 9. Validate and apply each transaction
    for t in blk["transactions"]:
        ok, err = tx_mod.validate(t, state, height - 1, get_fee_rate_at_height)
        if not ok:
            return False, f"invalid tx: {err}"
        state.apply_tx(t)

    return True, None


def compute_expected_difficulty(chain):
    if not chain:
        return INITIAL_DIFFICULTY_TARGET
    window = chain[-mining.DIFFICULTY_WINDOW:]
    solution_counts = [
        sum(s["count"] for s in b.get("solver_summaries", []))
        for b in window
    ]
    current_target = chain[-1]["difficulty_target"]
    return mining.adjust_difficulty(solution_counts, current_target)


def compute_expected_fee_rate(chain):
    """
    Fee rate targets a fixed byte volume per block (BLOCK_SIZE_TARGET_BYTES),
    not solver count. Block byte volume is the direct signal for network
    congestion; solver count is irrelevant to fee pressure.
    """
    if not chain:
        return INITIAL_FEE_RATE

    current_rate = chain[-1].get("fee_rate", INITIAL_FEE_RATE)
    window       = chain[-FEE_RATE_WINDOW:]
    byte_volumes = [
        sum(tx_mod.tx_size(t) for t in b.get("transactions", []))
        for b in window
    ]
    median_vol = statistics.median(byte_volumes) if byte_volumes else 0
    ratio      = median_vol / BLOCK_SIZE_TARGET_BYTES
    return max(1, int(current_rate * max(0.5, min(2.0, ratio))))
