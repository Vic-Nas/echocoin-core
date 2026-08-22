"""Block creation, validation, serialization. Pure functions on dicts."""

import statistics
import time as _time

import crypto
from crypto import canonical_json
import tx as tx_mod
import vdf as vdf_mod
from params import (
    BLOCK_SIZE_LIMIT,
    BLOCK_SIZE_TARGET_BYTES,
    FEE_RATE_WINDOW,
    GENESIS_MESSAGE,
    GENESIS_TIMESTAMP,
    INITIAL_FEE_RATE,
    VDF_ITERATIONS,
    VDF_ADJUST_INTERVAL,
    VDF_ADJUST_MIN_SECONDS,
    VDF_ADJUST_FACTOR,
)

_TX_SORT_FIELDS = {"fee_height", "nonce"}


def get_vdf_iterations(chain) -> int:
    """Return the VDF iteration count that must be used for the next block.

    Determined by the most recent adjustment block committed to the chain.
    Adjustment blocks are at heights that are multiples of VDF_ADJUST_INTERVAL.
    The genesis block always uses VDF_ITERATIONS as the baseline.

    All nodes compute this identically from chain data — no wall clock used.
    """
    if len(chain) <= 1:
        return VDF_ITERATIONS
    # Find the most recent adjustment block
    tip_height = len(chain) - 1
    last_adj   = (tip_height // VDF_ADJUST_INTERVAL) * VDF_ADJUST_INTERVAL
    if last_adj == 0:
        return VDF_ITERATIONS
    adj_block = chain[last_adj]
    return adj_block.get("vdf_iterations", VDF_ITERATIONS)


def compute_next_vdf_iterations(chain) -> int:
    """Compute what vdf_iterations should be for the upcoming adjustment block.

    Called when assembling a block at height that is a multiple of
    VDF_ADJUST_INTERVAL. Uses the median vdf_seconds from the previous window.
    Only ever increases. Returns current value if no adjustment needed.
    """
    tip_height   = len(chain) - 1
    window_start = tip_height - VDF_ADJUST_INTERVAL + 1
    if window_start < 0:
        return VDF_ITERATIONS

    current_iterations = get_vdf_iterations(chain)

    seconds = [
        chain[h].get("vdf_seconds")
        for h in range(window_start, tip_height + 1)
        if chain[h].get("vdf_seconds") is not None
    ]
    if not seconds:
        return current_iterations

    median_secs = statistics.median(seconds)
    if median_secs < VDF_ADJUST_MIN_SECONDS:
        return int(current_iterations * VDF_ADJUST_FACTOR)
    return current_iterations


def create_genesis():
    """
    Create the genesis block (block 0). Hardcoded and deterministic.
    GENESIS_TIMESTAMP anchors block 0. Every subsequent block carries its own
    timestamp, validated as not more than 30 s in the future.
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
        "vdf_iterations":   VDF_ITERATIONS,
        "vdf_seconds":      None,
    }
    blk["hash"] = block_hash(blk)
    return blk


def create(height, previous_hash, transactions, builder,
           fee_rate, vdf_output=None, vdf_proof=None, timestamp=None,
           vdf_seconds=None, vdf_iterations=None):
    """Create a new block dict.

    builder: address of the node that produced the accepted VDF proof
             and assembled this block. Receives the full block reward.
             None only for the genesis block.
    vdf_output: hex string of the VDF output element (from vdf.evaluate).
    vdf_proof:  hex string of the full VDF proof blob (from vdf.evaluate).
    vdf_seconds: wall clock seconds the builder took to evaluate the VDF.
                 Informational only — used for difficulty adjustment median.
    vdf_iterations: iteration count used for this block's VDF. Set at
                    adjustment boundaries, carried forward otherwise.
    Both vdf_output and vdf_proof are None only for the genesis block.
    """
    blk = {
        "height":         height,
        "previous_hash":  previous_hash,
        "timestamp":      timestamp if timestamp is not None else _time.time(),
        "transactions":   transactions,
        "builder":        builder,
        "fee_rate":       fee_rate,
        "vdf_output":     vdf_output,
        "vdf_proof":      vdf_proof,
        "vdf_iterations": vdf_iterations if vdf_iterations is not None else VDF_ITERATIONS,
        "vdf_seconds":    vdf_seconds,
    }
    blk["hash"] = block_hash(blk)
    return blk


def block_hash(blk):
    """Deterministic hash of block (excludes 'hash' field itself)."""
    fields = {k: v for k, v in blk.items() if k != "hash"}
    return crypto.sha256_hex(canonical_json(fields))


def block_size(blk):
    """Size in bytes of serialized block."""
    return len(canonical_json(blk))


def _check_hash(blk):
    if blk.get("hash") != block_hash(blk):
        return False, "block hash mismatch"
    return True, None


def _check_parent(blk, chain):
    height = blk["height"]
    if height == 0:
        return True, None
    parent = chain[-1] if chain else None
    if parent is None:
        return False, "no parent block"
    if blk["previous_hash"] != parent["hash"]:
        return False, "previous_hash does not match parent"
    if height != parent["height"] + 1:
        return False, "height does not follow parent"
    return True, None


def _check_timestamp(blk, chain):
    height = blk["height"]
    ts = blk.get("timestamp")
    if not isinstance(ts, (int, float)):
        return False, "block missing timestamp"
    if ts > _time.time() + 30:
        return False, f"block timestamp {ts} is too far in the future"
    return True, None


def _check_builder_and_vdf(blk, chain):
    if blk["height"] == 0:
        return True, None
    builder = blk.get("builder")
    if not isinstance(builder, str) or not crypto.is_valid_address(builder):
        return False, f"invalid builder address: {builder!r}"
    vdf_output = blk.get("vdf_output")
    vdf_proof  = blk.get("vdf_proof")
    if not isinstance(vdf_output, str) or not isinstance(vdf_proof, str):
        return False, "missing vdf_output or vdf_proof"

    # Validate vdf_iterations matches what the chain requires at this height
    expected_iterations = get_vdf_iterations(chain)
    block_iterations    = blk.get("vdf_iterations", VDF_ITERATIONS)
    if block_iterations != expected_iterations:
        return False, (f"vdf_iterations mismatch: block has {block_iterations}, "
                       f"chain expects {expected_iterations}")

    challenge = bytes.fromhex(chain[-1]["hash"])
    if not vdf_mod.verify(challenge, vdf_output, vdf_proof, block_iterations):
        return False, "invalid VDF proof"
    return True, None


def _check_tx_ordering(blk):
    txs = blk["transactions"]
    if not txs:
        return True, None
    for i, t in enumerate(txs):
        if not isinstance(t, dict):
            return False, f"transaction at position {i} is not a dict"
        missing = _TX_SORT_FIELDS - t.keys()
        if missing:
            return False, f"transaction at position {i} missing fields for ordering: {missing}"
    # Verify canonical order in O(n) by checking each adjacent pair.
    # Order key: (fee_height asc, nonce asc, tx_hash lex). Pre-compute hashes once.
    keys = [(t["fee_height"], t["nonce"], tx_mod.tx_hash(t)) for t in txs]
    for i in range(len(keys) - 1):
        if keys[i] > keys[i + 1]:
            return False, f"transaction ordering violation at position {i + 1}"
    return True, None


def _check_fee_rate(blk, chain):
    if blk["height"] == 0:
        return True, None
    expected_rate = compute_expected_fee_rate(chain)
    if blk["fee_rate"] != expected_rate:
        return False, f"fee rate mismatch: expected {expected_rate}, got {blk['fee_rate']}"
    return True, None


def _apply_transactions(blk, state, get_fee_rate_at_height):
    height = blk["height"]
    for t in blk["transactions"]:
        ok, err = tx_mod.validate(t, state, height - 1, get_fee_rate_at_height)
        if not ok:
            return False, f"invalid tx: {err}"
        state.apply_tx(t)
    return True, None


def validate(blk, state, chain, get_fee_rate_at_height):
    """Full block validation. Returns (True, None) or (False, error_string).

    Applies transactions to `state` in place. Callers must pass
    state.snapshot() -- not the live state -- so a failure leaves it clean.
    Rewards are NOT applied here; the caller applies them after success.
    """
    size = block_size(blk)
    if size > BLOCK_SIZE_LIMIT:
        return False, f"block exceeds size limit: {size} > {BLOCK_SIZE_LIMIT}"

    for check, args in (
        (_check_hash,            (blk,)),
        (_check_parent,          (blk, chain)),
        (_check_timestamp,       (blk, chain)),
        (_check_builder_and_vdf, (blk, chain)),
        (_check_tx_ordering,     (blk,)),
        (_check_fee_rate,        (blk, chain)),
        (_apply_transactions,    (blk, state, get_fee_rate_at_height)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
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

    Volume is read from blk["tx_bytes"] if present (set by node._commit),
    falling back to recomputing via tx_size() for blocks that pre-date this
    field (e.g. blocks loaded from an old database).
    """
    if not chain:
        return INITIAL_FEE_RATE

    current_rate = chain[-1].get("fee_rate", INITIAL_FEE_RATE)
    window       = chain[-FEE_RATE_WINDOW:]
    byte_volumes = [
        b["tx_bytes"] if "tx_bytes" in b
        else sum(tx_mod.tx_size(t) for t in b.get("transactions", []))
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


def assemble(tip, txs, builder_addr, fee_rate, chain=None, deadline=None):
    """Assemble a candidate block from a mempool snapshot.

    Pure function: does not touch node state. Adds txs one at a time,
    stopping before the block would exceed the size limit or the deadline
    (if given). Returns a block dict without a VDF proof attached -- the
    caller adds vdf_output, vdf_proof, vdf_seconds, and recomputes the hash.

    txs:      pre-sorted list from tx.sort_txs()
    chain:    full chain list, used to determine vdf_iterations for this block.
              Pass None only for genesis (uses VDF_ITERATIONS baseline).
    deadline: float unix time; stop packing if exceeded
    """
    # Determine VDF iteration count for this block height
    next_height = tip["height"] + 1
    if chain is not None and next_height % VDF_ADJUST_INTERVAL == 0:
        iterations = compute_next_vdf_iterations(chain)
    elif chain is not None:
        iterations = get_vdf_iterations(chain)
    else:
        iterations = VDF_ITERATIONS

    skeleton = create(
        height=next_height,
        previous_hash=tip["hash"],
        transactions=[],
        builder=builder_addr,
        fee_rate=fee_rate,
        vdf_iterations=iterations,
    )
    # "[" + "]" = 2 bytes for empty list; we'll add ", ".join(tx_jsons) inside
    base_size   = block_size(skeleton)
    running     = base_size
    valid_txs   = []

    for t in txs:
        if deadline is not None and _time.time() >= deadline:
            break
        # Size of this tx as it would appear serialized inside the block.
        # We add 1 for the "," separator between txs (except the first).
        t_size = tx_mod.tx_size_in_block(t, position=len(valid_txs))
        if running + t_size > BLOCK_SIZE_LIMIT:
            continue  # not break: a later smaller tx might still fit
        valid_txs.append(t)
        running += t_size

    skeleton["transactions"] = valid_txs
    skeleton["tx_bytes"]     = running - base_size
    # Hash is NOT set here: the caller must add vdf_output + vdf_proof before
    # hashing. block_hash is called once in _run_cycle after all fields are final.
    del skeleton["hash"]
    return skeleton
