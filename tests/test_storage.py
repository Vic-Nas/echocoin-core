"""
Unit tests for storage.py

Covers: save_block, load_block, load_all_blocks, chain_height,
save_state, load_state, state_exists, replace_chain, save_block_and_state,
replace_chain_and_state, TxIndex, AddrIndex, get_meta, set_meta.

Uses an in-memory SQLite database via tmp_path fixture.
No network. No real VDF.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import state as state_mod
import tx as tx_mod
from storage import Storage
from tests.fixtures import (
    address, genesis, make_block, make_tx, seed_balance,
)
from params import EMBERS_PER_SCH


# ---------------------------------------------------------------------------
# Fixture: fresh Storage pointing at a temp file
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "test_chain.db")
    s = Storage(path)
    yield s
    s.close()


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. save_block / load_block / load_all_blocks / chain_height
# ---------------------------------------------------------------------------

class TestBlockPersistence:
    def test_save_and_load_genesis(self, store):
        g = genesis()
        store.save_block(g)
        loaded = store.load_block(0)
        assert loaded["hash"] == g["hash"]

    def test_load_nonexistent_block_returns_none(self, store):
        assert store.load_block(999) is None

    def test_chain_height_empty(self, store):
        assert store.chain_height() == -1

    def test_chain_height_after_genesis(self, store):
        store.save_block(genesis())
        assert store.chain_height() == 0

    def test_chain_height_after_two_blocks(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        store.save_block(g)
        store.save_block(b1)
        assert store.chain_height() == 1

    def test_load_all_blocks_empty(self, store):
        assert store.load_all_blocks() == []

    def test_load_all_blocks_ordered_by_height(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        store.save_block(b1)
        store.save_block(g)
        blocks = store.load_all_blocks()
        assert [b["height"] for b in blocks] == [0, 1]

    def test_save_block_is_idempotent(self, store):
        g = genesis()
        store.save_block(g)
        store.save_block(g)  # should not raise
        assert store.chain_height() == 0


# ---------------------------------------------------------------------------
# 2. State persistence: save_state / load_state / state_exists
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_state_exists_false_initially(self, store):
        assert store.state_exists() is False

    def test_state_exists_true_after_save(self, store):
        s = fresh_state()
        s.credit(address(0), 1000)
        store.save_state(s)
        assert store.state_exists() is True

    def test_load_state_restores_balances(self, store):
        s = fresh_state()
        s.credit(address(0), 5000)
        store.save_state(s)
        balances, nonces, minted = store.load_state()
        assert balances[address(0)] == 5000

    def test_load_state_restores_nonces(self, store):
        s = fresh_state()
        s.set_nonce(address(0), 7)
        store.save_state(s)
        _, nonces, _ = store.load_state()
        assert nonces[address(0)] == 7

    def test_load_state_restores_emission(self, store):
        s = fresh_state()
        s.total_minted = 12345
        store.save_state(s)
        _, _, minted = store.load_state()
        assert minted == 12345

    def test_save_state_replaces_previous(self, store):
        s1 = fresh_state()
        s1.credit(address(0), 1000)
        store.save_state(s1)
        s2 = fresh_state()
        s2.credit(address(0), 9999)
        store.save_state(s2)
        balances, _, _ = store.load_state()
        assert balances[address(0)] == 9999


# ---------------------------------------------------------------------------
# 3. TxIndex and AddrIndex
# ---------------------------------------------------------------------------

class TestTxAndAddrIndex:
    def test_tx_not_indexed_before_block_save(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, EMBERS_PER_SCH, s, 0)
        h = tx_mod.tx_hash(t)
        assert store.get_tx_height(h) is None

    def test_tx_indexed_after_block_save(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, EMBERS_PER_SCH, s, 0)
        h = tx_mod.tx_hash(t)
        g = genesis()
        b1 = make_block(1, g["hash"], [t])
        store.save_block(g)
        store.save_block(b1)
        assert store.get_tx_height(h) == 1

    def test_addr_index_for_sender(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, EMBERS_PER_SCH, s, 0)
        g = genesis()
        b1 = make_block(1, g["hash"], [t])
        store.save_block(g)
        store.save_block(b1)
        rows = store.get_tx_heights_for_addr(address(0))
        assert len(rows) == 1
        assert rows[0][0] == 1  # block height

    def test_addr_index_for_recipient(self, store):
        s = fresh_state()
        seed_balance(s, 0, 100.0)
        t = make_tx(0, 1, EMBERS_PER_SCH, s, 0)
        g = genesis()
        b1 = make_block(1, g["hash"], [t])
        store.save_block(g)
        store.save_block(b1)
        rows = store.get_tx_heights_for_addr(address(1))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 4. replace_chain
# ---------------------------------------------------------------------------

class TestReplaceChain:
    def test_replace_chain_removes_old_blocks(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        store.save_block(g)
        store.save_block(b1)
        store.save_block(b2)
        # Reorg: replace from height 1 with a new block
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        store.replace_chain(from_height=1, blocks=[b1_new])
        assert store.chain_height() == 1
        assert store.load_block(2) is None

    def test_replace_chain_adds_new_blocks(self, store):
        g = genesis()
        store.save_block(g)
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        store.replace_chain(from_height=1, blocks=[b1_new])
        loaded = store.load_block(1)
        assert loaded["builder"] == address(1)


# ---------------------------------------------------------------------------
# 5. save_block_and_state (atomic)
# ---------------------------------------------------------------------------

class TestSaveBlockAndState:
    def test_block_and_state_saved_together(self, store):
        g = genesis()
        store.save_block(g)
        b1 = make_block(1, g["hash"], [])
        s = fresh_state()
        s.credit(address(0), 42_000)
        store.save_block_and_state(b1, s)
        assert store.chain_height() == 1
        balances, _, _ = store.load_state()
        assert balances[address(0)] == 42_000


# ---------------------------------------------------------------------------
# 6. replace_chain_and_state (atomic)
# ---------------------------------------------------------------------------

class TestReplaceChainAndState:
    def test_replace_chain_and_state_atomic(self, store):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        s1 = fresh_state()
        s1.credit(address(0), 1000)
        store.save_block(g)
        store.save_block_and_state(b1, s1)

        # Reorg with new block and new state
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        s2 = fresh_state()
        s2.credit(address(0), 9999)
        store.replace_chain_and_state(fork_point=1, blocks=[b1_new], state=s2)

        assert store.chain_height() == 1
        balances, _, _ = store.load_state()
        assert balances[address(0)] == 9999


# ---------------------------------------------------------------------------
# 7. Meta key-value store
# ---------------------------------------------------------------------------

class TestMeta:
    def test_get_meta_missing_returns_default(self, store):
        assert store.get_meta("no_key") is None
        assert store.get_meta("no_key", default="fallback") == "fallback"

    def test_set_and_get_meta(self, store):
        store.set_meta("version", "1.2.3")
        assert store.get_meta("version") == "1.2.3"

    def test_set_meta_overwrites(self, store):
        store.set_meta("key", "old")
        store.set_meta("key", "new")
        assert store.get_meta("key") == "new"
