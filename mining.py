"""Puzzle derivation, solution checking, difficulty adjustment. No I/O."""

import statistics
import crypto
from params import (
    BLOCK_REWARD,
    DIFFICULTY_WINDOW,
    DIFFICULTY_CLAMP_LOW,
    DIFFICULTY_CLAMP_HIGH,
    INITIAL_DIFFICULTY_TARGET,
    TARGET_SOLUTIONS_PER_BLOCK,
)


def derive_puzzle(prev_block_hash, node_pubkey_bytes):
    """
    puzzle = sha256(previous_block_hash + node_public_key)
    Returns puzzle bytes.
    """
    if isinstance(prev_block_hash, str):
        prev_block_hash = bytes.fromhex(prev_block_hash)
    return crypto.sha256(prev_block_hash + node_pubkey_bytes)


def check_solution(puzzle_bytes, nonce, difficulty_target):
    """
    solution = sha256(puzzle + nonce_bytes)
    Valid if int(solution) < difficulty_target.
    Returns (valid: bool, solution_hash_hex: str).
    """
    if isinstance(nonce, int):
        nonce = nonce.to_bytes(8, "big")
    solution = crypto.sha256(puzzle_bytes + nonce)
    solution_int = int.from_bytes(solution, "big")
    return solution_int < difficulty_target, solution.hex()


def summarize_solutions(solutions):
    """
    Compress a list of full solution dicts (used during the live round)
    into the compact per-solver summary stored in the block.

    Full solutions carry pubkey (1794 hex chars), nonce, and solution_hash
    -- all needed during the round for PoW verification, but not needed by
    any syncing node afterward. The summary stores only what is needed for
    state rebuilding and difficulty adjustment:

      address:     reward recipient address, derived from the full pubkey
                   while it is still available (live-round).
      count:       number of valid solutions this solver submitted.

    Reward is not stored -- it is a deterministic function of count and the
    total count across all solvers (floor division, remainder burned), so
    storing it would be redundant and create a consistency risk.

    Returns list sorted by address for determinism.
    """
    counts = {}
    for sol in solutions:
        pk = sol["pubkey"]
        counts[pk] = counts.get(pk, 0) + 1

    summary = []
    for pk_hex, count in counts.items():
        pk_bytes = bytes.fromhex(pk_hex)
        addr = crypto.public_key_to_address(pk_bytes)
        summary.append({"address": addr, "count": count})

    return sorted(summary, key=lambda s: s["address"])


def reward_addresses(solutions):
    """
    Compute {address: reward} from full solution dicts (live-round format).
    Called at block-creation time; result applied to state immediately.
    """
    if not solutions:
        return {}
    counts = {}
    for sol in solutions:
        pk = sol["pubkey"]
        counts[pk] = counts.get(pk, 0) + 1
    total = sum(counts.values())
    result = {}
    for pk_hex, count in counts.items():
        addr = crypto.public_key_to_address(bytes.fromhex(pk_hex))
        amount = (BLOCK_REWARD * count) // total
        result[addr] = result.get(addr, 0) + amount
    return result


def verify_summary_addresses(solver_summaries, all_solutions):
    """
    Cross-check that each solver_summaries entry's address matches the
    address derived from the actual pubkey in all_solutions for that solver,
    and that each reported count is within the binary range
    [observed/2, observed*2] of what this node observed locally.

    The multiplicative range is harder to game than an additive band: claiming
    double requires the observer to have seen zero, which is detectable.
    A node with observed count 0 has upper bound 0; any nonzero reported
    count for an unseen node fails immediately (ghost node attack).

    Returns (True, None) or (False, error).
    """
    # Build local count and address per pubkey
    local_counts   = {}
    addr_by_pubkey = {}
    for sol in all_solutions:
        pk = sol["pubkey"]
        local_counts[pk]   = local_counts.get(pk, 0) + 1
        addr_by_pubkey[pk] = crypto.public_key_to_address(bytes.fromhex(pk))

    valid_addrs = set(addr_by_pubkey.values())

    for s in solver_summaries:
        addr  = s["address"]
        count = s["count"]

        if addr not in valid_addrs:
            return False, f"solver summary address not derived from any known pubkey: {addr}"

        # Sum local observations for this address across all pubkeys
        local = sum(
            c for pk, c in local_counts.items()
            if addr_by_pubkey.get(pk) == addr
        )

        # Binary range check: reported must be within [local/2, local*2].
        # If local is 0, upper bound is 0 -- any nonzero claim fails.
        lo = local // 2
        hi = local * 2
        if not (lo <= count <= hi):
            return False, (
                f"solver count for {addr} is {count}, "
                f"locally observed {local}, "
                f"outside accepted range [{lo}, {hi}]"
            )

    return True, None


def reward_addresses_from_summary(solver_summaries):
    """
    Derive {address: reward} map from stored solver_summaries.
    Used by _rebuild_state and _apply_chain during chain sync, where the
    original full solution list is no longer available.

    Reward is recomputed from counts using the same floor-division formula
    as compute_rewards, with remainder burned.
    """
    if not solver_summaries:
        return {}

    total = sum(s["count"] for s in solver_summaries)
    if total == 0:
        return {}

    result = {}
    for s in solver_summaries:
        share = (BLOCK_REWARD * s["count"]) // total
        if share > 0:
            result[s["address"]] = result.get(s["address"], 0) + share
    return result


def adjust_difficulty(solution_counts, current_target):
    """
    solution_counts: list of total solution counts for recent blocks
                     (up to DIFFICULTY_WINDOW).
    current_target: current difficulty target (int).

    Returns new difficulty target (int).

    Uses median of solution counts. If median > target, make harder
    (lower target). If median < target, make easier (higher target).
    Clamped between 0.5x and 2.0x of current target.
    """
    if not solution_counts:
        return current_target

    window           = solution_counts[-DIFFICULTY_WINDOW:]
    median_solutions = statistics.median(window)

    if median_solutions == 0:
        return min(int(current_target * DIFFICULTY_CLAMP_HIGH), 2**256 - 1)

    ratio      = TARGET_SOLUTIONS_PER_BLOCK / median_solutions
    ratio      = max(DIFFICULTY_CLAMP_LOW, min(DIFFICULTY_CLAMP_HIGH, ratio))
    new_target = int(current_target * ratio)
    return max(1, min(new_target, 2**256 - 1))
