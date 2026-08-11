"""Storage module tests: persistence, state save/load, reorg truncation."""
import os
import tempfile
import pytest
from helpers import *
from storage import Storage


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = Storage(path)
    yield s
    s.close()
    os.unlink(path)


def test_save_and_load_block(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    loaded = db.load_block(0)
    assert loaded["hash"] == g["hash"]


def test_chain_height_empty(db):
    assert db.chain_height() == -1


def test_chain_height_after_save(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    assert db.chain_height() == 0


def test_load_all_blocks(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    b1 = block_mod.create(
        height=1, previous_hash=g["hash"],
        transactions=[], solver_summaries=[],
        difficulty_target=block_mod.compute_expected_difficulty([g]),
        fee_rate=block_mod.compute_expected_fee_rate([g]),
    )
    db.save_block(b1)
    blocks = db.load_all_blocks()
    assert len(blocks) == 2
    assert blocks[0]["height"] == 0
    assert blocks[1]["height"] == 1


def test_state_save_and_load(db):
    s = state_mod.State()
    s.credit("alice", 500)
    s.credit("bob", 300)
    s.set_nonce("alice", 3)
    db.save_state(s)
    assert db.state_exists()
    balances, nonces = db.load_state()
    assert balances["alice"] == 500
    assert balances["bob"] == 300
    assert nonces["alice"] == 3


def test_truncate_from(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    for i in range(1, 5):
        parent = [block_mod.create_genesis()] if i == 1 else []
        blk = block_mod.create(
            height=i, previous_hash="x" * 64,
            transactions=[], solver_summaries=[],
            difficulty_target=1, fee_rate=1,
        )
        db.save_block(blk)
    assert db.chain_height() == 4
    db.truncate_from(3)
    assert db.chain_height() == 2


def test_meta(db):
    db.set_meta("foo", "bar")
    assert db.get_meta("foo") == "bar"
    assert db.get_meta("missing", "default") == "default"


# ---- tx_index coverage ----

def test_tx_index_populated_on_save(db):
    import tx as tx_mod
    db.save_block(block_mod.create_genesis())
    assert db.get_tx_height("nonexistent") is None


def test_tx_index_get_tx_height(db):
    import json, hashlib
    g = block_mod.create_genesis()
    db.save_block(g)
    # Build a fake tx and a block containing it
    fake_tx = {"from": "a", "to": "b", "amount": 1, "nonce": 1,
               "fee": 0, "fee_height": 0, "pubkey": "x", "outputs": [],
               "signature": "s"}
    import tx as tx_mod
    h = tx_mod.tx_hash(fake_tx)
    blk = {
        "height": 1, "previous_hash": g["hash"], "hash": "aa" * 32,
        "timestamp": g["timestamp"] + 120,
        "transactions": [fake_tx], "solver_summaries": [],
        "difficulty_target": 2**240, "fee_rate": 1,
    }
    db.save_block(blk)
    assert db.get_tx_height(h) == 1


def test_replace_chain_updates_tx_index(db):
    import tx as tx_mod
    g = block_mod.create_genesis()
    db.save_block(g)
    fake_tx = {"from": "a", "nonce": 1, "fee": 0, "fee_height": 0,
               "pubkey": "x", "outputs": [], "signature": "s"}
    h = tx_mod.tx_hash(fake_tx)
    blk1 = {
        "height": 1, "previous_hash": g["hash"], "hash": "bb" * 32,
        "timestamp": g["timestamp"] + 120,
        "transactions": [fake_tx], "solver_summaries": [],
        "difficulty_target": 2**240, "fee_rate": 1,
    }
    db.save_block(blk1)
    assert db.get_tx_height(h) == 1

    # Replace chain from height 1 -- tx_index should be cleared for h
    blk1b = {
        "height": 1, "previous_hash": g["hash"], "hash": "cc" * 32,
        "timestamp": g["timestamp"] + 120,
        "transactions": [], "solver_summaries": [],
        "difficulty_target": 2**240, "fee_rate": 1,
    }
    db.replace_chain(1, [blk1b])
    assert db.get_tx_height(h) is None


def test_save_state_preserves_nonce_for_zero_balance(db):
    """An address that spent its entire balance must retain its nonce in storage,
    otherwise a replay of its old tx would be accepted after reload."""
    import state as state_mod
    s = state_mod.State()
    s.credit("alice", 1000)
    s.set_nonce("alice", 3)
    s.debit("alice", 1000)   # balance now 0
    db.save_state(s)

    balances, nonces = db.load_state()
    # Balance should be gone (zero), but nonce must survive
    assert balances.get("alice", 0) == 0
    assert nonces.get("alice") == 3
