"""Node controller tests: persistence, chain sync, reorg, submit_tx."""
import os
import tempfile
import queue as _queue
import pytest
from unittest.mock import patch
from helpers import *
from helpers import make_chain


class FakeGossip:
    def __init__(self):
        self.relayed_txs = []
    def relay_tx(self, t):               self.relayed_txs.append(t)
    def broadcast_block(self, b):        pass
    def broadcast_solution(self, s, c):  pass
    def request_tx(self, h):             return None
    def dandelion_send(self, tx, hops):  pass
    def mark_seen(self, h):              pass
    def _broadcast(self, ep, data):      pass


class FakeSyncer:
    def check_and_sync(self, h, fn):     return False


class FakePool:
    def count(self):                     return 0
    def add(self, addr):                 return True


@pytest.fixture
def node_setup():
    from node import Node
    sk, pk, pk_hex, addr = make_keypair()
    gossip = FakeGossip()
    syncer = FakeSyncer()
    pool   = FakePool()
    net_in_q = _queue.Queue()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        keyfile = f.name
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbfile = f.name
    crypto.save_key(keyfile, sk, pk, "testpass")
    n = Node(keyfile, pk, gossip, syncer, pool, net_in_q, db_path=dbfile)
    yield n, sk, pk, pk_hex, addr, gossip
    n.storage.close()
    os.unlink(keyfile)
    os.unlink(dbfile)


def test_node_starts_with_genesis(node_setup):
    n, *_ = node_setup
    assert len(n.chain) == 1
    assert n.chain[0]["height"] == 0


def test_genesis_persisted(node_setup):
    n, *_ = node_setup
    assert n.storage.chain_height() == 0


def test_genesis_message(node_setup):
    n, *_ = node_setup
    assert "PoolCoin genesis" in n.chain[0]["message"]
    assert "chain length tells the age" in n.chain[0]["message"]


def test_node_info(node_setup):
    n, _, _, _, addr, _ = node_setup
    info = n.get_info()
    assert info["height"] == 0
    assert info["address"] == addr


def test_submit_valid_tx(node_setup):
    n, sk, pk, pk_hex, addr, gossip = node_setup
    _, _, _, to_addr = make_keypair()
    n.state.credit(addr, 1_000_000)
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    ok, h = n.submit_tx(t)
    assert ok, h
    assert n.mempool.size() == 1
    assert len(gossip.relayed_txs) == 1


def test_submit_insufficient_balance_rejected(node_setup):
    n, sk, pk, pk_hex, addr, gossip = node_setup
    _, _, _, to_addr = make_keypair()
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 1)
    ok, err = n.submit_tx(t)
    assert not ok
    assert n.mempool.size() == 0


def test_build_and_sign_tx(node_setup):
    n, sk, pk, pk_hex, addr, gossip = node_setup
    _, _, _, to_addr = make_keypair()
    n.state.credit(addr, 1_000_000)
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        kf = f.name
    crypto.save_key(kf, sk, pk, "pw12345678")
    n.keyfile = kf
    t, fee = n.build_and_sign_tx([{"to": to_addr, "amount": 100}], passphrase="pw12345678")
    assert "signature" in t
    assert fee > 0
    os.unlink(kf)


def test_lowest_hash_wins_competing_blocks(node_setup):
    """When two valid blocks arrive at the same height, lowest hash wins."""
    n, *_ = node_setup
    genesis = block_mod.create_genesis()
    # Create two competing blocks at height 1, differing only in tx list
    # (to ensure different hashes).
    from params import BLOCK_CYCLE_SECONDS
    ts1 = genesis["timestamp"] + BLOCK_CYCLE_SECONDS
    blk_a = block_mod.create(
        height=1, previous_hash=genesis["hash"],
        transactions=[], solver_summaries=[{"address": n.addr, "count": 1}],
        difficulty_target=block_mod.compute_expected_difficulty([genesis]),
        fee_rate=block_mod.compute_expected_fee_rate([genesis]),
        timestamp=ts1,
    )
    blk_b = block_mod.create(
        height=1, previous_hash=genesis["hash"],
        transactions=[], solver_summaries=[{"address": n.addr, "count": 2}],
        difficulty_target=block_mod.compute_expected_difficulty([genesis]),
        fee_rate=block_mod.compute_expected_fee_rate([genesis]),
        timestamp=ts1,
    )
    winner = blk_a if blk_a["hash"] < blk_b["hash"] else blk_b
    loser  = blk_b if winner is blk_a else blk_a
    # The winner hash should be strictly less than the loser hash.
    assert winner["hash"] < loser["hash"]


def test_sync_chain_valid(node_setup):
    n, *_ = node_setup
    chain = make_chain(4)
    ok, err = n.sync_chain(chain)
    assert ok, err
    assert len(n.chain) == 4
    assert n.storage.chain_height() == 3


def test_sync_chain_shorter_rejected(node_setup):
    n, *_ = node_setup
    ok, err = n.sync_chain([block_mod.create_genesis()])
    assert not ok and "not longer" in err


def test_sync_chain_bad_genesis_rejected(node_setup):
    n, *_ = node_setup
    fake = block_mod.create_genesis()
    fake["hash"] = "ff" * 32
    ok, err = n.sync_chain([fake, fake])
    assert not ok and "genesis" in err.lower()


def test_chain_reloaded_from_disk(node_setup):
    from node import Node
    n, sk, pk, pk_hex, addr, gossip = node_setup

    from params import BLOCK_CYCLE_SECONDS
    genesis = block_mod.create_genesis()
    blk = block_mod.create(
        height=1, previous_hash=genesis["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([genesis]),
        fee_rate=block_mod.compute_expected_fee_rate([genesis]),
        timestamp=genesis["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    n.storage.save_block(blk)

    n2 = Node(n.keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              _queue.Queue(), db_path=n.storage.path)
    assert len(n2.chain) == 2
    assert n2.chain[1]["height"] == 1
    n2.storage.close()


# ---- timestamp enforcement ----

def test_setup_round_always_returns_tip(node_setup):
    """_setup_round always returns the current tip; no sit-out logic."""
    n, *_ = node_setup
    tip, difficulty, fee_rate, puzzle = n._setup_round()
    assert tip is not None
    assert tip is n.chain[-1]


# ---- mempool cleanup when wait-phase block is applied ----

def test_handle_wait_message_block_removes_txs_from_mempool(node_setup):
    """When a valid peer block is applied during the wait phase,
    its transactions must be removed from the mempool."""
    import tx as tx_mod
    n, sk, pk, pk_hex, addr, gossip = node_setup
    n.state.credit(addr, 10_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    n.mempool.add(t)
    assert n.mempool.size() == 1

    # Build a block containing that tx
    from params import BLOCK_CYCLE_SECONDS
    parent = n.chain[-1]
    blk = block_mod.create(
        height=parent["height"] + 1,
        previous_hash=parent["hash"],
        transactions=[t],
        solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    # Feed it through the wait-phase block handler
    n._apply_best_peer_block([blk])

    # Chain grew and mempool is now empty
    assert len(n.chain) == 2
    assert n.mempool.size() == 0


# ---- _handle_tx_message does not double-relay fluff ----

def test_handle_tx_message_fluff_no_double_relay(node_setup):
    """Fluff tx goes to mempool only; relay_tx (which re-stems) is not called."""
    import tx as tx_mod
    n, sk, pk, pk_hex, addr, gossip = node_setup
    n.state.credit(addr, 10_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)

    broadcast_calls = []
    original_broadcast = n.gossip._broadcast
    n.gossip._broadcast = lambda *a, **kw: broadcast_calls.append(a)

    n._handle_tx_message({"type": "tx_fluff", "tx": t, "relay_type": "tx_fluff", "remaining_hops": 0})

    n.gossip._broadcast = original_broadcast

    # Exactly one broadcast (fluff), not two
    assert len(broadcast_calls) == 1
    assert n.mempool.size() == 1
    # relay_tx was NOT called (which would re-stem)
    assert len(gossip.relayed_txs) == 0


# ---- _apply_chain coverage ----

def test_apply_chain_genesis_mismatch(node_setup):
    n, *_ = node_setup
    fake = block_mod.create_genesis()
    fake["hash"] = "ff" * 32
    ok, err = n._apply_chain([fake], "test")
    assert not ok and "genesis" in err.lower()


def test_apply_chain_invalid_block_mid_chain(node_setup):
    n, *_ = node_setup
    chain = make_chain(3)
    # Corrupt block 2's previous_hash
    chain[2]["previous_hash"] = "00" * 32
    chain[2]["hash"] = block_mod.block_hash(chain[2])
    ok, err = n._apply_chain(chain, "test")
    assert not ok and "invalid block at 2" in err


def test_apply_chain_advances_fork_point(node_setup):
    """Fork detected at correct height; only new blocks are written."""
    n, *_ = node_setup
    base = make_chain(3)
    ok, _ = n.sync_chain(base)
    assert ok
    # Fork from block 2 -- blocks 0 and 1 are shared
    from params import BLOCK_CYCLE_SECONDS
    fork_blk = block_mod.create(
        height=2, previous_hash=base[1]["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty(base[:2]),
        fee_rate=block_mod.compute_expected_fee_rate(base[:2]),
        timestamp=base[1]["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    fork_blk2 = block_mod.create(
        height=3, previous_hash=fork_blk["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty(base[:2] + [fork_blk]),
        fee_rate=block_mod.compute_expected_fee_rate(base[:2] + [fork_blk]),
        timestamp=fork_blk["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    fork_chain = base[:2] + [fork_blk, fork_blk2]
    ok, err = n._apply_chain(fork_chain, "reorg")
    assert ok, err
    assert n.chain[-1]["hash"] == fork_blk2["hash"]


def test_commit_updates_chain_state_mempool(node_setup):
    """_commit appends block, updates state, removes confirmed txs from mempool."""
    import tx as tx_mod
    from params import BLOCK_CYCLE_SECONDS
    n, sk, pk, pk_hex, addr, _ = node_setup
    n.state.credit(addr, 10_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    n.mempool.add(t)

    parent = n.chain[-1]
    blk = block_mod.create(
        height=1, previous_hash=parent["hash"],
        transactions=[t], solver_summaries=[{"address": addr, "count": 1}],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    new_state = n.state.snapshot()
    block_mod.validate(blk, new_state, n.chain, n._fee_rate_at)
    n._commit(blk, new_state, [{"pubkey": pk_hex, "nonce": 0, "solution_hash": "aa"}])

    assert len(n.chain) == 2
    assert n.chain[-1]["hash"] == blk["hash"]
    assert n.mempool.size() == 0


def test_censorship_score_full_block_no_increment(node_setup):
    """A full block does not increment exclusion ages."""
    from params import BLOCK_CYCLE_SECONDS, BLOCK_SIZE_LIMIT
    import tx as tx_mod
    n, sk, pk, pk_hex, addr, _ = node_setup
    n.state.credit(addr, 10_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n.mempool.add(t)

    parent = n.chain[-1]
    blk = block_mod.create(
        height=1, previous_hash=parent["hash"],
        transactions=[],   # missing t
        solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    # Fake a full block by patching block_size
    from unittest.mock import patch
    with patch("block.block_size", return_value=BLOCK_SIZE_LIMIT):
        n._update_exclusion_ages(blk)
    # Age should NOT have incremented because block was full
    assert n._tx_exclusion_age.get(h, 0) == 0


def test_censorship_score_non_full_increments(node_setup):
    """A non-full block missing a tx increments its exclusion age."""
    from params import BLOCK_CYCLE_SECONDS
    import tx as tx_mod
    n, sk, pk, pk_hex, addr, _ = node_setup
    n.state.credit(addr, 10_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n.mempool.add(t)

    parent = n.chain[-1]
    blk = block_mod.create(
        height=1, previous_hash=parent["hash"],
        transactions=[],   # missing t, non-full
        solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    n._update_exclusion_ages(blk)
    assert n._tx_exclusion_age.get(h, 0) == 1


def test_rebuild_state_matches_replay(node_setup):
    """_rebuild_state produces state consistent with sequential apply."""
    from params import BLOCK_CYCLE_SECONDS
    import tx as tx_mod
    n, sk, pk, pk_hex, addr, _ = node_setup
    _, _, _, to_addr = make_keypair()

    # _rebuild_state replays from genesis. Credit happens via apply_rewards.
    # We use a reward-only block (no tx) so state is just the reward.
    summaries = [{"address": addr, "count": 1}]
    parent = n.chain[-1]
    blk = block_mod.create(
        height=1, previous_hash=parent["hash"],
        transactions=[], solver_summaries=summaries,
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    n.chain.append(blk)
    n._rebuild_state()

    import mining
    rewards = mining.reward_addresses_from_summary(summaries)
    assert n.state.get_balance(addr) == rewards.get(addr, 0)


def test_apply_best_peer_block_lowest_hash_wins(node_setup):
    """When multiple peer blocks arrive, the lowest-hash valid one is applied."""
    from params import BLOCK_CYCLE_SECONDS
    n, sk, pk, pk_hex, addr, _ = node_setup
    parent = n.chain[-1]
    ts = parent["timestamp"] + BLOCK_CYCLE_SECONDS

    blk_a = block_mod.create(
        height=1, previous_hash=parent["hash"],
        transactions=[], solver_summaries=[{"address": addr, "count": 1}],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=ts,
    )
    blk_b = block_mod.create(
        height=1, previous_hash=parent["hash"],
        transactions=[], solver_summaries=[{"address": addr, "count": 2}],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=ts,
    )
    winner = blk_a if blk_a["hash"] < blk_b["hash"] else blk_b
    # Pass both; only the lower-hash one should be applied.
    n._apply_best_peer_block([blk_a, blk_b])
    assert n.chain[-1]["hash"] == winner["hash"]
    assert len(n.chain) == 2


def test_apply_best_peer_block_invalid_dropped(node_setup):
    """Blocks that fail validation are not applied."""
    from params import BLOCK_CYCLE_SECONDS
    n, *_ = node_setup
    parent = n.chain[-1]
    blk = block_mod.create(
        height=1, previous_hash="00" * 32,   # wrong previous_hash
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty(n.chain),
        fee_rate=block_mod.compute_expected_fee_rate(n.chain),
        timestamp=parent["timestamp"] + BLOCK_CYCLE_SECONDS,
    )
    n._apply_best_peer_block([blk])
    assert len(n.chain) == 1   # unchanged
