"""Flow: Startup sequence.

Covers FLOW.md § Startup:
  Node.__init__ → load/create chain → load/rebuild state → rebuild BurnWindow

Every test here exercises what happens before the block loop runs.
"""
import os
import tempfile
import threading
from unittest.mock import patch

import pytest
from helpers import *


# ---------------------------------------------------------------------------
# Fresh database: genesis is created and persisted
# ---------------------------------------------------------------------------

def test_fresh_node_has_genesis():
    n, *_, dbfile, keyfile = make_node()
    try:
        assert len(n.chain) == 1
        assert n.chain[0]["height"] == 0
        assert n.chain[0]["hash"] == block_mod.create_genesis()["hash"]
    finally:
        teardown_node(n, dbfile, keyfile)


def test_genesis_persisted_to_storage():
    n, *_, dbfile, keyfile = make_node()
    try:
        assert n.storage.chain_height() == 0
        assert n.storage.load_block(0)["hash"] == block_mod.create_genesis()["hash"]
    finally:
        teardown_node(n, dbfile, keyfile)


def test_genesis_has_no_vdf_or_builder():
    n, *_, dbfile, keyfile = make_node()
    try:
        g = n.chain[0]
        assert g["builder"] is None
        assert g["vdf_output"] is None
        assert g["vdf_proof"] is None
    finally:
        teardown_node(n, dbfile, keyfile)


def test_genesis_contains_echocoin_message():
    n, *_, dbfile, keyfile = make_node()
    try:
        assert "Echocoin" in n.chain[0]["message"]
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# Loading from an existing database
# ---------------------------------------------------------------------------

def test_node_loads_existing_chain():
    """A second Node instance opening the same DB recovers the full chain."""
    from node import Node
    import queue

    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    genesis_hash = n.chain[0]["hash"]

    # Commit a block without running VDF
    with patch("vdf.verify", return_value=True):
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        commit_block(n, blk)
    n.storage.close()

    # Re-open
    n2 = Node(keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              queue.Queue(), db_path=dbfile)
    try:
        assert len(n2.chain) == 2
        assert n2.chain[0]["hash"] == genesis_hash
        assert n2.chain[1]["height"] == 1
    finally:
        n2.storage.close()
        os.unlink(dbfile)
        os.unlink(keyfile)


def test_state_rebuilt_from_chain_when_snapshot_missing():
    """If the state table is empty, state is replayed from blocks."""
    from node import Node
    import queue

    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()

    # Give addr some balance via a block reward
    with patch("vdf.verify", return_value=True):
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        commit_block(n, blk)

    minted_before = n.state.total_minted
    n.storage.close()

    # Wipe the state table to force replay
    import sqlite3
    conn = sqlite3.connect(dbfile)
    conn.execute("DELETE FROM state")
    conn.execute("DELETE FROM emission")
    conn.commit()
    conn.close()

    n2 = Node(keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              queue.Queue(), db_path=dbfile)
    try:
        assert n2.state.total_minted == minted_before
        assert n2.state.get_balance(addr) > 0
    finally:
        n2.storage.close()
        os.unlink(dbfile)
        os.unlink(keyfile)


# ---------------------------------------------------------------------------
# BurnWindow rebuilt on startup
# ---------------------------------------------------------------------------

def test_burn_window_rebuilt_from_chain():
    """Burns in committed blocks are reflected in BurnWindow after reload."""
    from node import Node
    import queue

    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    s = n.state
    s.credit(addr, 100_000_000)

    burn_tx = make_burn_tx(sk, pk_hex, addr, 50_000, nonce=1, fee_height=0)
    with patch("vdf.verify", return_value=True):
        blk = make_block(n.chain, builder_addr=addr, txs=[burn_tx])
        blk["tx_bytes"] = sum(tx_mod.tx_size(t) for t in blk["transactions"])
        commit_block(n, blk)
    n.storage.close()

    n2 = Node(keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              queue.Queue(), db_path=dbfile)
    try:
        assert n2._burn_window.builder_burn(addr) > 0
    finally:
        n2.storage.close()
        os.unlink(dbfile)
        os.unlink(keyfile)


# ---------------------------------------------------------------------------
# NodeView published at startup
# ---------------------------------------------------------------------------

def test_node_view_published_on_init():
    n, *_, dbfile, keyfile = make_node()
    try:
        v = n.view
        assert v.height == 0
        assert v.genesis_hash == block_mod.create_genesis()["hash"]
        assert v.tip["height"] == 0
    finally:
        teardown_node(n, dbfile, keyfile)


def test_node_info_matches_view():
    n, _, _, _, addr, *_, dbfile, keyfile = make_node()
    try:
        info = n.get_info()
        assert info["height"] == 0
        assert info["address"] == addr
        assert "total_minted" in info
        assert info["genesis_hash"] == n.view.genesis_hash
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# tx_bytes backfill on startup
# ---------------------------------------------------------------------------

def test_tx_bytes_backfilled_for_old_blocks():
    """Blocks without tx_bytes get it stamped on load."""
    from node import Node
    import queue, sqlite3, json

    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    with patch("vdf.verify", return_value=True):
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        commit_block(n, blk)
    n.storage.close()

    # Manually strip tx_bytes from the stored block
    conn = sqlite3.connect(dbfile)
    rows = conn.execute("SELECT height, data FROM blocks WHERE height > 0").fetchall()
    for height, data in rows:
        d = json.loads(data)
        d.pop("tx_bytes", None)
        conn.execute("UPDATE blocks SET data=? WHERE height=?",
                     (json.dumps(d), height))
    conn.commit()
    conn.close()

    n2 = Node(keyfile, pk, FakeGossip(), FakeSyncer(), FakePool(),
              queue.Queue(), db_path=dbfile)
    try:
        for blk in n2.chain:
            assert "tx_bytes" in blk
    finally:
        n2.storage.close()
        os.unlink(dbfile)
        os.unlink(keyfile)
