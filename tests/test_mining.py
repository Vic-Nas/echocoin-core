"""Mining tests: puzzle uniqueness, sybil resistance, solution count validation, difficulty."""
import pytest
from helpers import *


def test_puzzle_unique_per_pubkey():
    _, pk1, _, _ = make_keypair()
    _, pk2, _, _ = make_keypair()
    p1 = mining.derive_puzzle("00" * 32, pk1)
    p2 = mining.derive_puzzle("00" * 32, pk2)
    assert p1 != p2


def test_puzzle_unique_per_prev_hash():
    _, pk, _, _ = make_keypair()
    p1 = mining.derive_puzzle("00" * 32, pk)
    p2 = mining.derive_puzzle("ff" * 32, pk)
    assert p1 != p2


def test_solution_not_transferable():
    """A valid solution for one pubkey is invalid for another."""
    _, pk1, pk1_hex, _ = make_keypair()
    _, pk2, pk2_hex, _ = make_keypair()
    difficulty = mining.INITIAL_DIFFICULTY_TARGET
    puzzle1 = mining.derive_puzzle("00" * 32, pk1)
    for nonce in range(300_000):
        valid, sol_hash = mining.check_solution(puzzle1, nonce, difficulty)
        if valid:
            puzzle2 = mining.derive_puzzle("00" * 32, pk2)
            valid2, _ = mining.check_solution(puzzle2, nonce, difficulty)
            assert not valid2
            break


def test_verify_summary_addresses_range_check():
    """Binary range check: [observed/2, observed*2]."""
    sk1, pk1, pk1_hex, addr1 = make_keypair()

    solutions = [{"pubkey": pk1_hex, "nonce": i, "solution_hash": "aa" * 32}
                 for i in range(10)]

    # Exact count passes.
    summary = [{"address": addr1, "count": 10}]
    ok, _ = mining.verify_summary_addresses(summary, solutions)
    assert ok

    # Within range: 5 is exactly local/2.
    summary = [{"address": addr1, "count": 5}]
    ok, _ = mining.verify_summary_addresses(summary, solutions)
    assert ok

    # Within range: 20 is exactly local*2.
    summary = [{"address": addr1, "count": 20}]
    ok, _ = mining.verify_summary_addresses(summary, solutions)
    assert ok

    # Out of range: 4 < local/2.
    summary = [{"address": addr1, "count": 4}]
    ok, _ = mining.verify_summary_addresses(summary, solutions)
    assert not ok

    # Out of range: 21 > local*2.
    summary = [{"address": addr1, "count": 21}]
    ok, _ = mining.verify_summary_addresses(summary, solutions)
    assert not ok


def test_verify_summary_ghost_node():
    """Ghost node: unseen solver reported with nonzero count fails."""
    sk1, pk1, pk1_hex, addr1 = make_keypair()
    sk2, pk2, pk2_hex, addr2 = make_keypair()

    # Only pk1 has solutions; pk2 has none (ghost).
    solutions = [{"pubkey": pk1_hex, "nonce": 0, "solution_hash": "aa" * 32}]
    summary = [
        {"address": addr1, "count": 1},
        {"address": addr2, "count": 1},   # ghost: observed=0, so upper bound=0
    ]
    ok, _ = mining.verify_summary_addresses(summary, solutions)
    assert not ok


def test_reward_proportional():
    sk1, pk1, pk1_hex, addr1 = make_keypair()
    sk2, pk2, pk2_hex, addr2 = make_keypair()
    solutions = [
        {"pubkey": pk1_hex, "solution_hash": "x"},
        {"pubkey": pk1_hex, "solution_hash": "y"},
        {"pubkey": pk1_hex, "solution_hash": "z"},
        {"pubkey": pk2_hex, "solution_hash": "w"},
    ]
    rewards = mining.reward_addresses(solutions)
    from params import BLOCK_REWARD
    assert rewards[addr1] == (BLOCK_REWARD * 3) // 4
    assert rewards[addr2] == (BLOCK_REWARD * 1) // 4


def test_sybil_same_total_reward():
    """
    Splitting hashpower across N identities yields the same expected total
    reward as running it as one identity (given same total solutions).
    """
    from params import BLOCK_REWARD
    sk1, pk1, pk1_hex, addr1 = make_keypair()
    sk2, pk2, pk2_hex, addr2 = make_keypair()

    solutions_single = [{"pubkey": pk1_hex, "solution_hash": str(i)} for i in range(4)]
    rewards_single = mining.reward_addresses(solutions_single)

    solutions_split = [
        {"pubkey": pk1_hex, "solution_hash": "0"},
        {"pubkey": pk1_hex, "solution_hash": "1"},
        {"pubkey": pk2_hex, "solution_hash": "2"},
        {"pubkey": pk2_hex, "solution_hash": "3"},
    ]
    rewards_split = mining.reward_addresses(solutions_split)

    assert sum(rewards_single.values()) == sum(rewards_split.values())


def test_difficulty_clamp():
    target = mining.INITIAL_DIFFICULTY_TARGET
    counts = [1000] * 100
    new_target = mining.adjust_difficulty(counts, target)
    assert new_target == int(target * 0.5)

    counts = [0] * 100
    new_target = mining.adjust_difficulty(counts, target)
    assert new_target == int(target * 2.0)


def test_difficulty_stable_at_target():
    target = mining.INITIAL_DIFFICULTY_TARGET
    counts = [mining.TARGET_SOLUTIONS_PER_BLOCK] * 100
    new_target = mining.adjust_difficulty(counts, target)
    assert new_target == target


def test_summarize_solutions_compact():
    """summarize_solutions: one entry per solver, sorted, no reward field stored."""
    sk1, pk1, pk1_hex, addr1 = make_keypair()
    sk2, pk2, pk2_hex, addr2 = make_keypair()

    solutions = [
        {"pubkey": pk1_hex, "nonce": 0, "solution_hash": "aa" * 32},
        {"pubkey": pk1_hex, "nonce": 1, "solution_hash": "bb" * 32},
        {"pubkey": pk2_hex, "nonce": 0, "solution_hash": "cc" * 32},
    ]
    summary = mining.summarize_solutions(solutions)

    assert len(summary) == 2
    counts = {s["address"]: s["count"] for s in summary}
    assert counts[addr1] == 2
    assert counts[addr2] == 1

    for s in summary:
        assert "reward" not in s

    addrs = [s["address"] for s in summary]
    assert addrs == sorted(addrs)


def test_reward_addresses_from_summary():
    """reward_addresses_from_summary produces same payout as reward_addresses."""
    sk1, pk1, pk1_hex, addr1 = make_keypair()
    sk2, pk2, pk2_hex, addr2 = make_keypair()

    solutions = [
        {"pubkey": pk1_hex, "nonce": 0, "solution_hash": "aa" * 32},
        {"pubkey": pk1_hex, "nonce": 1, "solution_hash": "bb" * 32},
        {"pubkey": pk2_hex, "nonce": 0, "solution_hash": "cc" * 32},
    ]
    direct = mining.reward_addresses(solutions)
    summary = mining.summarize_solutions(solutions)
    from_summary = mining.reward_addresses_from_summary(summary)

    assert direct == from_summary

    from params import BLOCK_REWARD
    assert sum(from_summary.values()) <= BLOCK_REWARD


def test_remainder_is_burned():
    """Remainder from floor division is burned, not given to any solver."""
    sk1, pk1, pk1_hex, _ = make_keypair()
    sk2, pk2, pk2_hex, _ = make_keypair()
    sk3, pk3, pk3_hex, _ = make_keypair()

    solutions = [
        {"pubkey": pk1_hex, "nonce": 0, "solution_hash": "aa" * 32},
        {"pubkey": pk2_hex, "nonce": 0, "solution_hash": "bb" * 32},
        {"pubkey": pk3_hex, "nonce": 0, "solution_hash": "cc" * 32},
    ]
    rewards = mining.reward_addresses(solutions)
    from params import BLOCK_REWARD
    total = sum(rewards.values())
    assert total <= BLOCK_REWARD
    assert total == BLOCK_REWARD - (BLOCK_REWARD % 3)


def test_reward_addresses_empty_solutions():
    assert mining.reward_addresses([]) == {}


def test_verify_summary_unknown_address():
    """An address not derivable from any known pubkey is rejected."""
    sk1, pk1, pk1_hex, addr1 = make_keypair()
    _, _, _, fake_addr = make_keypair()
    solutions = [{"pubkey": pk1_hex, "nonce": 0, "solution_hash": "aa" * 32}]
    summary = [{"address": fake_addr, "count": 1}]
    ok, err = mining.verify_summary_addresses(summary, solutions)
    assert not ok
