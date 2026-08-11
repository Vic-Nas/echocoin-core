"""Block validation tests: ordering, size, difficulty, hash."""
import pytest
from helpers import *


def test_genesis_hash_deterministic():
    g1 = block_mod.create_genesis()
    g2 = block_mod.create_genesis()
    assert g1["hash"] == g2["hash"]


def test_genesis_valid():
    g = block_mod.create_genesis()
    s = state_mod.State()
    ok, err = block_mod.validate(g, s, [], fee_rate_fn(1))
    assert ok, err


def test_block_hash_changes_on_mutation():
    g = block_mod.create_genesis()
    original_hash = g["hash"]
    g2 = dict(g)
    g2["height"] = 999
    g2["hash"] = block_mod.block_hash(g2)
    assert g2["hash"] != original_hash


def test_block_with_wrong_hash_rejected():
    g = block_mod.create_genesis()
    g["hash"] = "ff" * 32
    s = state_mod.State()
    ok, err = block_mod.validate(g, s, [], fee_rate_fn(1))
    assert not ok
    assert "hash" in err.lower()


def test_block_ordering_violation_rejected():
    """Block with transactions in wrong order is rejected."""
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = funded_state(addr, 1_000_000)
    chain = genesis_chain()
    genesis = chain[0]
    rate = 1

    t1 = make_valid_tx(sk, pk_hex, addr, to, 10, 1, 0, rate)
    t2 = make_valid_tx(sk, pk_hex, addr, to, 10, 2, 0, rate)

    sorted_txs = tx_mod.sort_txs([t1, t2])
    h1 = tx_mod.tx_hash(sorted_txs[0])
    h2 = tx_mod.tx_hash(sorted_txs[1])

    wrong_order = list(reversed(sorted_txs))

    if h1 != h2:
        from params import BLOCK_CYCLE_SECONDS
        blk = block_mod.create(
            height=1,
            previous_hash=genesis["hash"],
            transactions=wrong_order,
            solver_summaries=[],
            difficulty_target=block_mod.compute_expected_difficulty(chain),
            fee_rate=block_mod.compute_expected_fee_rate(chain),
            timestamp=genesis["timestamp"] + BLOCK_CYCLE_SECONDS,
        )
        ok, err = block_mod.validate(blk, s, chain, fee_rate_fn(rate))
        assert not ok
        assert "ordering" in err.lower()


def test_block_size_hard_ceiling():
    from params import BLOCK_SIZE_LIMIT
    g = block_mod.create_genesis()
    assert block_mod.block_size(g) < BLOCK_SIZE_LIMIT


def test_block_timestamp_required():
    """A block without a timestamp is rejected."""
    from params import BLOCK_CYCLE_SECONDS
    g = block_mod.create_genesis()
    blk = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    del blk["timestamp"]
    blk["hash"] = block_mod.block_hash(blk)
    import state as state_mod
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok
    assert "timestamp" in err


def test_block_timestamp_must_follow_parent():
    """A block whose timestamp is less than parent + BLOCK_CYCLE_SECONDS is rejected."""
    import time
    from params import BLOCK_CYCLE_SECONDS
    g = block_mod.create_genesis()
    blk = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    # Override to one second before the minimum.
    blk["timestamp"] = g["timestamp"] + BLOCK_CYCLE_SECONDS - 1
    blk["hash"] = block_mod.block_hash(blk)
    import state as state_mod
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok
    assert "timestamp" in err


def test_block_timestamp_future_rejected():
    """A block with a timestamp more than 30s in the future is rejected."""
    import time
    from params import BLOCK_CYCLE_SECONDS
    g = block_mod.create_genesis()
    blk = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    blk["timestamp"] = time.time() + 120
    blk["hash"] = block_mod.block_hash(blk)
    import state as state_mod
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok
    assert "future" in err



    """A tx whose fee_height is outside the acceptance window is pruned."""
    from mempool import Mempool
    import state as state_mod
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()

    mp = Mempool()
    t1 = make_valid_tx(sk, pk_hex, addr, to, 10, 1, 0, 1)
    mp.add(t1)
    assert mp.size() == 1

    st = state_mod.State()
    # Prune at a tip height where fee_height=0 is far outside the window.
    pruned = mp.prune_stale(chain_tip_height=100, state=st)
    assert len(pruned) == 1
    assert mp.size() == 0


def test_mempool_no_lock():
    """Mempool has no _lock attribute."""
    from mempool import Mempool
    mp = Mempool()
    assert not hasattr(mp, "_lock")


def test_difficulty_retarget():
    target = mining.INITIAL_DIFFICULTY_TARGET
    counts = [200] * 100
    new_target = mining.adjust_difficulty(counts, target)
    assert new_target == int(target * 0.5)


def test_fee_rate_retarget():
    chain = genesis_chain()
    rate = block_mod.compute_expected_fee_rate(chain)
    assert isinstance(rate, int)
    assert rate >= 1


# ---- validate coverage ----

def test_validate_genesis_block():
    """Genesis block validates without a parent chain."""
    import state as state_mod
    g = block_mod.create_genesis()
    ok, err = block_mod.validate(g, state_mod.State(), [], lambda h: None)
    assert ok, err


def test_validate_height_not_following_parent():
    from params import BLOCK_CYCLE_SECONDS
    import state as state_mod
    g = block_mod.create_genesis()
    blk = block_mod.create(
        height=2, previous_hash=g["hash"],   # wrong: should be 1
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok and "height" in err


def test_validate_previous_hash_mismatch():
    from params import BLOCK_CYCLE_SECONDS
    import state as state_mod
    g = block_mod.create_genesis()
    blk = block_mod.create(
        height=1, previous_hash="ab" * 32,   # wrong
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok and "previous_hash" in err


def test_validate_difficulty_mismatch():
    from params import BLOCK_CYCLE_SECONDS
    import state as state_mod
    g = block_mod.create_genesis()
    blk = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=12345,   # wrong
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok and "difficulty" in err


def test_validate_duplicate_solver_address():
    from params import BLOCK_CYCLE_SECONDS
    import state as state_mod
    _, _, _, addr = make_keypair()
    g = block_mod.create_genesis()
    summaries = [{"address": addr, "count": 1}, {"address": addr, "count": 2}]
    blk = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=summaries,
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok and "duplicate" in err


def test_validate_solver_count_zero():
    from params import BLOCK_CYCLE_SECONDS
    import state as state_mod
    _, _, _, addr = make_keypair()
    g = block_mod.create_genesis()
    summaries = [{"address": addr, "count": 0}]
    blk = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=summaries,
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
        timestamp=g["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    ok, err = block_mod.validate(blk, state_mod.State(), [g], lambda h: None)
    assert not ok and "count" in err
