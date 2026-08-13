"""Block creation, validation, serialization. Pure functions on dicts."""

import json
import statistics
import time as _time

import crypto
import tx as tx_mod
from params import (
    BLOCK_CYCLE_SECONDS,
    BLOCK_SIZE_LIMIT,
    BLOCK_SIZE_TARGET_BYTES,
    FEE_RATE_WINDOW,
    GENESIS_MESSAGE,
    GENESIS_TIMESTAMP,
    INITIAL_FEE_RATE,
)

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
        "builder":          None,
        "fee_rate":         INITIAL_FEE_RATE,
        "timestamp":        GENESIS_TIMESTAMP,
        "message":          GENESIS_MESSAGE,
        "vdf_output":       None,
        "vdf_proof":        None,
    }
    blk["hash"] = block_hash(blk)
    return blk


def create(height, previous_hash, transactions, builder,
           fee_rate, vdf_output=None, vdf_proof=None, timestamp=None):
    """Create a new block dict.

    builder: address of the node that produced the accepted VDF proof
             and assembled this block. Receives the full block reward.
             None only for the genesis block.
    vdf_output: hex string of the VDF output element (from vdf.evaluate).
    vdf_proof:  hex string of the full VDF proof blob (from vdf.evaluate).
    Both are None only for the genesis block.
    """
    blk = {
        "height":        height,
        "previous_hash": previous_hash,
        "timestamp":     timestamp if timestamp is not None else _time.time(),
        "transactions":  transactions,
        "builder":       builder,
        "fee_rate":      fee_rate,
        "vdf_output":    vdf_output,
        "vdf_proof":     vdf_proof,
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

    VDF proof verification is handled by vdf.verify before this function
    is called. This function checks structural integrity, tx validity,
    fee rate, and timestamp rules.

    IMPORTANT: this function applies transactions to `state` in place as
    part of balance-constraint checking. Callers must pass a snapshot
    (state.snapshot()) rather than the live state object, so that a
    validation failure leaves the live state untouched. Rewards are NOT
    applied here; the caller applies them after successful validation.
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

    # 5. Builder field
    if height > 0:
        builder = blk.get("builder")
        if not isinstance(builder, str) or not crypto.is_valid_address(builder):
            return False, f"invalid builder address: {builder!r}"

    # 5b. VDF proof -- verify before checking transactions
    if height > 0:
        import vdf as vdf_mod
        vdf_output = blk.get("vdf_output")
        vdf_proof  = blk.get("vdf_proof")
        if not isinstance(vdf_output, str) or not isinstance(vdf_proof, str):
            return False, "missing vdf_output or vdf_proof"
        challenge = bytes.fromhex(chain[-1]["hash"])
        if not vdf_mod.verify(challenge, vdf_output, vdf_proof):
            return False, "invalid VDF proof"

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

    # 7. Fee rate check
    if height > 0:
        expected_rate = compute_expected_fee_rate(chain)
        if blk["fee_rate"] != expected_rate:
            return False, "fee rate mismatch"

    # 8. Validate and apply each transaction
    for t in blk["transactions"]:
        ok, err = tx_mod.validate(t, state, height - 1, get_fee_rate_at_height)
        if not ok:
            return False, f"invalid tx: {err}"
        state.apply_tx(t)

    return True, None


def compute_expected_fee_rate(chain):
    """Asymmetric fee rate adjustment.

    Signal: median transaction byte volume over the last FEE_RATE_WINDOW blocks.
    Using a median over 100 blocks gives a stable signal -- individual full or
    empty blocks don't cause wild swings.

    Adjustment rules (applied to current_rate each block):
      - Empty network (median_vol == 0): multiply by 0.999 -- nearly frozen,
        falls ~26% per 300 blocks (~10 hours) of zero activity.
      - Below capacity (vol_ratio <= 1): multiply by max(0.999, vol_ratio^0.1)
        -- very slow decay; even at 50% fill the rate only falls ~7% per block.
      - Above capacity (vol_ratio > 1): multiply by min(1.05, vol_ratio)
        -- rises up to 5% per block when blocks are full; a sustained spam
        attack doubles fees in ~14 blocks (~28 minutes).

    Hard minimum: 1 ring/byte (technical floor, not a pricing floor).
    No hardcoded ceiling: fee pressure is the only cap on block fullness.
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

    if median_vol == 0:
        adjustment = 0.999
    else:
        vol_ratio = median_vol / BLOCK_SIZE_TARGET_BYTES
        if vol_ratio > 1:
            adjustment = min(1.05, vol_ratio)
        else:
            adjustment = max(0.999, vol_ratio ** 0.1)

    return max(1, int(current_rate * adjustment))


def assemble(tip, txs, builder_addr, fee_rate, deadline=None):
    """Assemble a candidate block from a mempool snapshot.

    Pure function: does not touch node state. Adds txs one at a time,
    stopping before the block would exceed the size limit or the deadline
    (if given). Returns a block dict without a VDF proof attached -- the
    caller adds vdf_output, vdf_proof, and recomputes the hash.

    txs: pre-sorted list from tx.sort_txs()
    deadline: float unix time; stop packing if exceeded
    """
    import time as _t

    def _candidate(included):
        return create(
            height=tip["height"] + 1,
            previous_hash=tip["hash"],
            transactions=included,
            builder=builder_addr,
            fee_rate=fee_rate,
        )

    valid_txs = []
    for t in txs:
        if deadline is not None and _t.time() >= deadline:
            break
        candidate = _candidate(valid_txs + [t])
        if block_size(candidate) > BLOCK_SIZE_LIMIT:
            continue
        valid_txs.append(t)

    return _candidate(valid_txs)
