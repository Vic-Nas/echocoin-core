"""Block creation, validation, serialization. Pure functions on dicts."""

import statistics
import time as _time

import crypto
from crypto import canonical_json
import tx as tx_mod
import vdf as vdf_mod
import timelock as timelock_mod
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

_TX_SORT_FIELDS = {"fee_height"}


class _EmptyQueue:
    """Fallback used when validate()/assemble() are called without a real
    queue (e.g. genesis, or tests that don't exercise the queue rule)."""

    def remaining(self):
        return []

    def lookup(self, confirmed_tx_hash):
        return None


def get_vdf_iterations(chain) -> int:
    """Return the VDF iteration count required for the next block
    (height = len(chain)) built on top of `chain`.

    Deterministically derived from real block timestamps -- no
    self-reported or otherwise-unverifiable field is trusted. Assemblers
    and validators call this on the same chain prefix and always agree,
    including exactly at adjustment boundaries.

    Between boundaries, this is an O(1) lookup of the value fixed at the
    last boundary (itself independently verified when that block was
    accepted, so trusting it here is safe). At a boundary, the window
    that just completed is replayed once to decide whether to bump.
    """
    next_height = len(chain)
    if next_height < VDF_ADJUST_INTERVAL:
        return VDF_ITERATIONS

    last_boundary = (next_height // VDF_ADJUST_INTERVAL) * VDF_ADJUST_INTERVAL
    if next_height > last_boundary:
        return chain[last_boundary].get("vdf_iterations", VDF_ITERATIONS)

    # next_height == last_boundary: assembling/validating the boundary
    # block itself. Fold in the window that just completed, using real
    # timestamp deltas between consecutive blocks (not a self-reported
    # figure) -- the same signal Bitcoin's own retarget relies on.
    window_start      = last_boundary - VDF_ADJUST_INTERVAL
    prior_iterations  = chain[window_start].get("vdf_iterations", VDF_ITERATIONS)
    deltas = [
        chain[h]["timestamp"] - chain[h - 1]["timestamp"]
        for h in range(max(window_start, 1), last_boundary)
    ]
    if deltas and statistics.median(deltas) < VDF_ADJUST_MIN_SECONDS:
        return int(prior_iterations * VDF_ADJUST_FACTOR)
    return prior_iterations


def vdf_challenge(previous_hash: str, builder: str) -> bytes:
    """Challenge a block's VDF must be evaluated over.

    Binds the sequential work to the address that will be paid for it.
    Without the builder in the challenge, a VDF output is a bearer token:
    any node that receives a broadcast block can keep vdf_output and
    vdf_proof, swap in its own builder address and its own transaction
    list, and rebroadcast a block that verifies just as well as the
    original. Whoever's copy arrives first wins, so the node that actually
    spent the ~120 s loses the reward to a node that spent nothing.

    Folding the builder in makes every builder evaluate a different VDF,
    so a stolen output verifies against nobody else's challenge. The
    transaction list is deliberately not folded in: content stays
    swappable on top of a valid proof, which is what lets a block whose
    transactions are rejected be corrected without redoing the ~120 s.
    """
    return crypto.sha256(bytes.fromhex(previous_hash) + builder.encode())


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
    }
    blk["hash"] = block_hash(blk)
    return blk


def create(height, previous_hash, transactions, builder,
           fee_rate, vdf_output=None, vdf_proof=None, timestamp=None,
           vdf_iterations=None):
    """Create a new block dict.

    builder: address of the node that produced the accepted VDF proof
             and assembled this block. Receives the full block reward.
             None only for the genesis block.
    vdf_output: hex string of the VDF output element (from vdf.evaluate).
    vdf_proof:  hex string of the full VDF proof blob (from vdf.evaluate).
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

    challenge = vdf_challenge(chain[-1]["hash"], builder)
    if not vdf_mod.verify(challenge, vdf_output, vdf_proof, block_iterations):
        return False, "invalid VDF proof"
    return True, None


def _check_tx_ordering(blk):
    """Canonical order applies to "confirm" ciphertext submissions only.
    "resolve" txs are governed by the separate gapless front-of-queue rule
    (_check_queue_resolution), not by this sort order -- assemble() puts
    due resolutions first, ahead of any confirmations."""
    confirms = [t for t in blk["transactions"] if isinstance(t, dict) and t.get("kind") == "confirm"]
    if not confirms:
        return True, None
    for i, t in enumerate(confirms):
        missing = _TX_SORT_FIELDS - t.keys()
        if missing:
            return False, f"confirmation at position {i} missing fields for ordering: {missing}"
    # Verify canonical order in O(n) by checking each adjacent pair.
    # Uses tx_mod.sort_key so this can never drift from tx.sort_txs().
    keys = [tx_mod.sort_key(t) for t in confirms]
    for i in range(len(keys) - 1):
        if keys[i] > keys[i + 1]:
            return False, f"confirmation ordering violation at position {i + 1}"
    return True, None


def _check_fee_rate(blk, chain):
    if blk["height"] == 0:
        return True, None
    expected_rate = compute_expected_fee_rate(chain)
    if blk["fee_rate"] != expected_rate:
        return False, f"fee rate mismatch: expected {expected_rate}, got {blk['fee_rate']}"
    return True, None


def _check_queue_resolution(blk, queue):
    """A block is valid only if it resolves at least the current front of
    the ciphertext queue, and any additional resolutions are a gapless
    continuation from the front -- no skipping ahead.

    This is a pure, positional rule: no deadline/window, no self-dealing
    exploit from tying block weight to "how much backlog cleared" (both
    were tried and rejected during design -- see timelock.py / tx.py
    module docstrings). It is what makes even a fully dominant builder
    face an all-or-nothing choice: resolve the front and keep building, or
    stop building entirely. It cannot selectively stall one target while
    operating normally on everything else.
    """
    if blk["height"] == 0:
        return True, None
    remaining = queue.remaining()
    if not remaining:
        return True, None  # nothing pending; resolutions are optional
    resolve_hashes = [t["confirmed_tx_hash"] for t in blk["transactions"]
                      if isinstance(t, dict) and t.get("kind") == "resolve"]
    if not resolve_hashes:
        return False, "block must resolve at least the current front of the queue"
    n = len(resolve_hashes)
    if resolve_hashes != remaining[:n]:
        return False, "resolutions are not a gapless continuation from the front of the queue"
    return True, None


def _apply_transactions(blk, state, get_fee_rate_at_height, queue, chain):
    height = blk["height"]
    local_confirmations = {}
    # One expected difficulty for the whole block: TIMELOCK_ADJUST tracks
    # VDF_ADJUST_INTERVAL (~2 weeks), far longer than FEE_HEIGHT_MAX_AGE
    # (a confirmation's fee_height can only be a few blocks old), so a
    # confirmation's fee_height can never actually straddle a difficulty
    # boundary in practice -- computing this once per block rather than
    # per fee_height is a safe simplification, not an approximation.
    expected_iterations = timelock_mod.get_timelock_iterations(chain)
    for t in blk["transactions"]:
        if not isinstance(t, dict):
            return False, "transaction entry is not a dict"
        kind = t.get("kind")
        if kind == "confirm":
            ok, err = tx_mod.validate_confirmation(t, state, height - 1, get_fee_rate_at_height,
                                                    expected_iterations=expected_iterations)
            if not ok:
                return False, f"invalid confirmation: {err}"
            h = tx_mod.tx_hash(t)
            state.apply_confirmation(t, h)
            local_confirmations[h] = t
        elif kind == "resolve":
            confirmed = (queue.lookup(t.get("confirmed_tx_hash"))
                        or local_confirmations.get(t.get("confirmed_tx_hash")))
            ok, err = tx_mod.validate_resolution(t, confirmed, state)
            if not ok:
                return False, f"invalid resolution: {err}"
            # The crypto proof above is what gates inclusion. Whether the
            # decrypted payload is still an applicable transfer (it may not
            # be, e.g. the sender already spent the same balance via a
            # different confirmation that resolved first) is checked
            # separately and does not fail the block either way -- see
            # tx.validate_resolution's docstring.
            payload_ok, _ = tx_mod.payload_is_valid(t["payload"], state)
            state.apply_resolution(t, payload_valid=payload_ok)
        else:
            return False, f"unknown transaction kind: {kind!r}"
    return True, None


def validate(blk, state, chain, get_fee_rate_at_height, queue=None):
    """Full block validation. Returns (True, None) or (False, error_string).

    Applies transactions to `state` in place. Callers must pass
    state.snapshot() (not the live state) so a failure leaves it clean.
    Rewards are NOT applied here; the caller applies them after success.

    queue: a TxQueue-like object (see chainstate.TxQueue) reflecting the
    ciphertext queue state as of `chain`, i.e. before this block. Defaults
    to an empty queue when omitted (e.g. genesis, or callers not exercising
    the queue rule).
    """
    if queue is None:
        queue = _EmptyQueue()

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
        (_check_queue_resolution, (blk, queue)),
        (_apply_transactions,    (blk, state, get_fee_rate_at_height, queue, chain)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
    return True, None


def compute_expected_fee_rate(chain):
    """Asymmetric fee rate adjustment.

    Signal: median transaction byte volume over the last FEE_RATE_WINDOW blocks.
    Using a median over 100 blocks gives a stable signal; individual full or
    empty blocks don't cause wild swings.

    Adjustment rules (applied to current_rate each block):
      - Empty network (median_vol == 0): multiply by 0.999. Nearly frozen,
        falls ~26% per 300 blocks (~10 hours) of zero activity.
      - Below capacity (vol_ratio <= 1): multiply by max(0.999, vol_ratio^0.1).
        Very slow decay; even at 50% fill the rate only falls ~7% per block.
      - Above capacity (vol_ratio > 1): multiply by min(1.05, vol_ratio).
        Rises up to 5% per block when blocks are full; a sustained spam
        attack doubles fees in ~14 blocks (~28 minutes).

    Hard minimum: 1 tick/byte (technical floor, not a pricing floor).
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


def assemble(tip, txs, builder_addr, fee_rate, iterations, queue=None, deadline=None):
    """Assemble a candidate block from a mempool snapshot.

    Pure function: does not touch node state. Pulls due (front-of-queue,
    in order) resolutions first -- the same priority pattern
    _check_tx_ordering/_check_queue_resolution enforce as a validity rule
    -- then confirmations in canonical sort order. Adds them one at a
    time, stopping before the block would exceed the size limit or the
    deadline (if given). Returns a block dict without a VDF proof
    attached; the caller adds vdf_output, vdf_proof, and recomputes the
    hash.

    txs:        candidate txs from the mempool, both "confirm" and
                "resolve" kinds, in any order.
    queue:      a TxQueue-like object (see chainstate.TxQueue) reflecting
                the ciphertext queue as of `tip`. Defaults to empty.
    iterations: this block's required vdf_iterations, from
                get_vdf_iterations(chain). Taken directly rather than
                a chain argument so callers that already computed it
                (to run the VDF itself) don't pay for it twice.
    deadline:   float unix time; stop packing if exceeded
    """
    if queue is None:
        queue = _EmptyQueue()
    next_height = tip["height"] + 1

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

    # Due resolutions first: a gapless prefix of queue.remaining() for which
    # a solved resolution is already available in the mempool. Stops at the
    # first missing one -- a later, out-of-order resolution can't be
    # substituted without breaking the gapless rule.
    resolutions_by_hash = {t["confirmed_tx_hash"]: t for t in txs
                           if isinstance(t, dict) and t.get("kind") == "resolve"}
    due_resolutions = []
    for h in queue.remaining():
        if h in resolutions_by_hash:
            due_resolutions.append(resolutions_by_hash[h])
        else:
            break

    confirms = tx_mod.sort_txs([t for t in txs if isinstance(t, dict) and t.get("kind") == "confirm"])
    ordered  = due_resolutions + confirms

    for t in ordered:
        if deadline is not None and _time.time() >= deadline:
            break
        # Size of this tx as it would appear serialized inside the block.
        # We add 1 for the "," separator between txs (except the first).
        t_size = tx_mod.tx_size_in_block(t, position=len(valid_txs))
        if running + t_size > BLOCK_SIZE_LIMIT:
            if t in due_resolutions:
                break  # a due resolution can't be skipped without a gap
            continue  # not break: a later smaller confirmation might still fit
        valid_txs.append(t)
        running += t_size

    skeleton["transactions"] = valid_txs
    skeleton["tx_bytes"]     = running - base_size
    # Hash is NOT set here: the caller must add vdf_output + vdf_proof before
    # hashing. block_hash is called once in _run_cycle after all fields are final.
    del skeleton["hash"]
    return skeleton
