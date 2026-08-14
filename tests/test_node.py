"""Node tests: startup, chain sync, reorg, submit_tx, censorship, commit."""
import os
import queue as _queue
import tempfile
import threading
from unittest.mock import patch

import pytest
from helpers import *


class FakeGossip:
    def __init__(self):
        self.relayed_txs = []
        self.broadcast_blocks = []
    def relay_tx(self, t):           self.relayed_txs.append(t)
    def broadcast_block(self, b):    self.broadcast_blocks.append(b)
    def dandelion_send(self, tx, h): pass
    def mark_seen(self, h):          return False
    def _broadcast(self, ep, data):  pass


class FakeSyncer:
    def check_and_sync(self, chain, fn): return False


class FakePool:
    def count(self):            return 0
    def random(self):           return None
    def get_all(self):          return []


@pytest.fixture
def node_setup():
    from node import Node
    sk, pk, pk_hex, addr = make_keypair()
    gossip   = FakeGossip()
    syncer   = FakeSyncer()
    pool     = FakePool()
    net_in_q = _queue.Queue()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        keyfile = f.name
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbfile = f.name
    crypto.save_key(keyfile, sk, pk, "testpass")
    n = Node(keyfile, pk, gossip, syncer, pool, net_in_q, db_path=dbfile)
    # Simulate being in the node loop thread so assertions don't fire.
    n._loop_thread = threading.current_thread()
    yield n, sk, pk, pk_hex, addr, gossip
    n.storage.close()
    os.unlink(keyfile)
    os.unlink(dbfile)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def test_node_starts_with_genesis(node_setup):
    n, *_ = node_setup
    assert len(n.chain) == 1
    assert n.chain[0]["height"] == 0


def test_genesis_persisted_on_startup(node_setup):
    n, *_ = node_setup
    assert n.storage.chain_height() == 0


def test_genesis_message_contains_echocoin(node_setup):
    n, *_ = node_setup
    assert "Echocoin" in n.chain[0]["message"]


def test_node_info_fields(node_setup):
    n, _, _, _, addr, _ = node_setup
    info = n.get_info()
    assert info["height"] == 0
    assert info["address"] == addr
    assert "total_minted" in info
    assert "total_burnt" in info
    assert "can_mint" in info


# ---------------------------------------------------------------------------
# submit_tx
# ---------------------------------------------------------------------------

def test_submit_valid_tx(node_setup):
    n, sk, _pk, pk_hex, addr, gossip = node_setup
    _, _, _, to_addr = make_keypair()
    n.state.credit(addr, 100_000_000)
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    ok, _h = n.submit_tx(t)
    assert ok
    assert n.mempool.size() == 1
    assert len(gossip.relayed_txs) == 1


def test_submit_invalid_tx_rejected(node_setup):
    n, _sk, _pk, pk_hex, addr, _ = node_setup
    _, _, _, to_addr = make_keypair()
    t = {"from": addr, "pubkey": pk_hex, "outputs": [{"to": to_addr, "amount": 9999}],
         "nonce": 1, "fee_height": 0, "fee": 1, "signature": "bad"}
    ok, _err = n.submit_tx(t)
    assert not ok


# ---------------------------------------------------------------------------
# Chain sync
# ---------------------------------------------------------------------------

def test_sync_chain_valid(node_setup):
    n, *_ = node_setup
    with patch("vdf.verify", return_value=True):
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
    n, _sk, pk, _pk_hex, _addr, _gossip = node_setup
    blk = make_block(n.chain)
    n.storage.save_block(blk)
    n2 = Node(n.keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              _queue.Queue(), db_path=n.storage.path)
    assert len(n2.chain) == 2
    assert n2.chain[1]["height"] == 1
    n2.storage.close()


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def test_commit_appends_block_and_removes_mempool_txs(node_setup):
    n, sk, _pk, pk_hex, addr, _ = node_setup
    n.state.credit(addr, 100_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    n.mempool.add(t)

    blk = make_block(n.chain, builder_addr=addr, txs=[t])
    probe = n.state.snapshot()
    with patch("vdf.verify", return_value=True):
        block_mod.validate(blk, probe, n.chain, n._fee_rate_at)
    n._commit(blk, probe)

    assert len(n.chain) == 2
    assert n.chain[-1]["hash"] == blk["hash"]
    assert n.mempool.size() == 0


def test_commit_applies_block_reward(node_setup):
    n, _, _, _, addr, _ = node_setup
    blk = make_block(n.chain, builder_addr=addr)
    probe = n.state.snapshot()
    with patch("vdf.verify", return_value=True):
        block_mod.validate(blk, probe, n.chain, n._fee_rate_at)
    balance_before = n.state.get_balance(addr)
    n._commit(blk, probe)
    assert n.state.get_balance(addr) > balance_before


# ---------------------------------------------------------------------------
# Censorship scoring
# ---------------------------------------------------------------------------

def test_censorship_score_is_1_when_no_missing_txs(node_setup):
    n, *_ = node_setup
    blk = make_block(n.chain)
    assert n._censorship_score(blk) == 1.0


def test_non_full_block_missing_tx_increments_age(node_setup):
    n, sk, _pk, pk_hex, addr, _ = node_setup
    n.state.credit(addr, 100_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n.mempool.add(t)

    blk = make_block(n.chain, txs=[])  # missing t
    n._update_exclusion_ages(blk)
    assert n._tx_exclusion_age.get(h, 0) == 1


def test_full_block_does_not_increment_age(node_setup):
    from params import BLOCK_SIZE_LIMIT
    n, sk, _pk, pk_hex, addr, _ = node_setup
    n.state.credit(addr, 100_000_000)
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    h = tx_mod.tx_hash(t)
    n.mempool.add(t)

    blk = make_block(n.chain, txs=[])
    with patch("block.block_size", return_value=BLOCK_SIZE_LIMIT):
        n._update_exclusion_ages(blk)
    assert n._tx_exclusion_age.get(h, 0) == 0


# ---------------------------------------------------------------------------
# _apply_chain edge cases
# ---------------------------------------------------------------------------

def test_apply_chain_genesis_mismatch_rejected(node_setup):
    n, *_ = node_setup
    fake = block_mod.create_genesis()
    fake["hash"] = "ff" * 32
    ok, err = n._apply_chain([fake], "test")
    assert not ok and "genesis" in err.lower()


def test_apply_chain_invalid_block_rejected(node_setup):
    n, *_ = node_setup
    chain = make_chain(3)
    chain[2]["previous_hash"] = "00" * 32
    chain[2]["hash"] = block_mod.block_hash(chain[2])
    with patch("vdf.verify", return_value=True):
        ok, err = n._apply_chain(chain, "test")
    assert not ok and "invalid block at 2" in err


# ---------------------------------------------------------------------------
# Rebuild state
# ---------------------------------------------------------------------------

def test_rebuild_state_from_chain(node_setup):
    n, _, _, _, addr, _ = node_setup
    blk = make_block(n.chain, builder_addr=addr)
    n.chain.append(blk)
    n._rebuild_state()
    # Builder should have received the block reward
    assert n.state.get_balance(addr) > 0
    assert n.state.total_minted > 0
