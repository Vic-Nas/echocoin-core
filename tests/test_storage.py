"""Storage tests: persistence, state save/load, emission counters, reorg."""
import os, tempfile
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
    assert db.load_block(0)["hash"] == g["hash"]


def test_chain_height_empty(db):
    assert db.chain_height() == -1


def test_chain_height_after_save(db):
    db.save_block(block_mod.create_genesis())
    assert db.chain_height() == 0


def test_load_all_blocks(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    b1 = make_block([g])
    db.save_block(b1)
    blocks = db.load_all_blocks()
    assert len(blocks) == 2
    assert blocks[0]["height"] == 0
    assert blocks[1]["height"] == 1


def test_state_save_and_load_balances_nonces(db):
    s = state_mod.State()
    s.credit("alice", 500)
    s.credit("bob", 300)
    s.set_nonce("alice", 3)
    db.save_state(s)
    assert db.state_exists()
    balances, nonces, minted, burnt = db.load_state()
    assert balances["alice"] == 500
    assert balances["bob"] == 300
    assert nonces["alice"] == 3


def test_state_save_and_load_emission_counters(db):
    s = state_mod.State()
    s.apply_reward("miner", 1_000_000)
    s.total_burnt = 50_000
    db.save_state(s)
    _, _, minted, burnt = db.load_state()
    assert minted == 1_000_000
    assert burnt == 50_000


def test_state_load_zero_emission_when_empty(db):
    # Fresh DB has no emission rows
    _, _, minted, burnt = db.load_state()
    assert minted == 0 and burnt == 0


def test_save_state_preserves_nonce_for_zero_balance(db):
    """Address that spent its entire balance must retain its nonce."""
    s = state_mod.State()
    s.credit("alice", 1000)
    s.set_nonce("alice", 3)
    s.debit("alice", 1000)  # balance now 0
    db.save_state(s)
    balances, nonces, _, _ = db.load_state()
    assert balances.get("alice", 0) == 0
    assert nonces.get("alice") == 3


def test_replace_chain_atomically(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    b1 = make_block([g])
    db.save_block(b1)
    assert db.chain_height() == 1
    # Replace from height 1 with a different block
    b1b = make_block([g], builder_addr="other_builder")
    db.replace_chain(1, [b1b])
    blocks = db.load_all_blocks()
    assert len(blocks) == 2
    assert blocks[1]["builder"] == "other_builder"


def test_tx_index_get_tx_height(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    fake_tx = {"from": "a", "outputs": [], "fee": 0, "fee_height": 0,
               "nonce": 1, "pubkey": "x", "signature": "s"}
    h = tx_mod.tx_hash(fake_tx)
    blk = make_block([g], txs=[fake_tx])
    db.save_block(blk)
    assert db.get_tx_height(h) == 1


def test_replace_chain_clears_tx_index(db):
    g = block_mod.create_genesis()
    db.save_block(g)
    fake_tx = {"from": "a", "outputs": [], "fee": 0, "fee_height": 0,
               "nonce": 1, "pubkey": "x", "signature": "s"}
    h = tx_mod.tx_hash(fake_tx)
    b1 = make_block([g], txs=[fake_tx])
    db.save_block(b1)
    assert db.get_tx_height(h) == 1
    # Replace with a block that has no txs
    b1b = make_block([g])
    db.replace_chain(1, [b1b])
    assert db.get_tx_height(h) is None


def test_meta(db):
    db.set_meta("foo", "bar")
    assert db.get_meta("foo") == "bar"
    assert db.get_meta("missing", "default") == "default"
