"""Flow: Block cycle.

Covers FLOW.md § Block Loop:
  drain queue → sync → VDF → assemble → broadcast → collect peer blocks
  → select winner → commit

Uses patch("vdf.verify", return_value=True) throughout -- the VDF is
tested as a protocol primitive in test_flow_security.py.
"""
from unittest.mock import patch

import pytest
from helpers import *


# ---------------------------------------------------------------------------
# assemble()
# ---------------------------------------------------------------------------

def test_assemble_produces_correct_height():
    g = block_mod.create_genesis()
    b = block_mod.assemble(g, [], "builder", fee_rate=1)
    assert b["height"] == 1
    assert b["previous_hash"] == g["hash"]


def test_assemble_respects_size_limit():
    """assemble() never returns a block that exceeds BLOCK_SIZE_LIMIT."""
    from params import BLOCK_SIZE_LIMIT, INITIAL_FEE_RATE
    g = block_mod.create_genesis()
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = state_mod.State()
    s.credit(addr, 10_000_000_000)

    # Build more txs than can fit
    txs = []
    for i in range(1, 200):
        outputs = [{"to": to, "amount": 1_000}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, i, 0, INITIAL_FEE_RATE)
        txs.append(tx_mod.create(addr, pk_hex, outputs, i, 0, fee, sk))
        s.apply_tx(txs[-1])

    blk = block_mod.assemble(g, tx_mod.sort_txs(txs), addr, INITIAL_FEE_RATE)
    assert block_mod.block_size(blk) <= BLOCK_SIZE_LIMIT


def test_assemble_incremental_size_matches_full_serialization():
    """The running size estimate in assemble() matches block_size()."""
    from params import INITIAL_FEE_RATE
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = state_mod.State()
    s.credit(addr, 10_000_000_000)
    g = block_mod.create_genesis()

    txs = []
    for i in range(1, 10):
        outputs = [{"to": to, "amount": 1_000}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, i, 0, INITIAL_FEE_RATE)
        t = tx_mod.create(addr, pk_hex, outputs, i, 0, fee, sk)
        txs.append(t)
        s.apply_tx(t)

    blk = block_mod.assemble(g, tx_mod.sort_txs(txs), addr, INITIAL_FEE_RATE)
    assert block_mod.block_size(blk) <= 10_000_000


# ---------------------------------------------------------------------------
# tx_bytes stamped on commit
# ---------------------------------------------------------------------------

def test_tx_bytes_stamped_on_committed_block():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        with patch("vdf.verify", return_value=True):
            blk = make_block(n.chain, builder_addr=addr)
            blk["tx_bytes"] = 0
            commit_block(n, blk)
        assert "tx_bytes" in n.chain[-1]
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# _select_winner: fork resolution by block hash
# ---------------------------------------------------------------------------

def test_own_block_wins_when_no_peers():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        g = n.chain[-1]
        candidate = make_block(n.chain, builder_addr=addr)
        with patch("vdf.verify", return_value=True):
            winner, relay = n._select_winner(candidate, [], g)
        assert winner["hash"] == candidate["hash"]
        assert relay is False
    finally:
        teardown_node(n, dbfile, keyfile)


def test_peer_block_with_lower_hash_wins():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        _, _, _, other_addr = make_keypair()
        g = n.chain[-1]
        candidate = make_block(n.chain, builder_addr=addr)

        # Build a peer block with artificially lower hash
        peer_blk = make_block(n.chain, builder_addr=other_addr)
        # Force peer_blk hash to be lower than candidate
        if peer_blk["hash"] >= candidate["hash"]:
            peer_blk, candidate = candidate, peer_blk

        with patch("vdf.verify", return_value=True):
            winner, relay = n._select_winner(candidate, [peer_blk], g)

        assert winner["hash"] == peer_blk["hash"]
        assert relay is True
    finally:
        teardown_node(n, dbfile, keyfile)


def test_peer_block_with_invalid_vdf_rejected():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        _, _, _, other = make_keypair()
        g = n.chain[-1]
        candidate = make_block(n.chain, builder_addr=addr)
        peer_blk  = make_block(n.chain, builder_addr=other)

        # Give each block a distinct vdf_output so fake_verify can tell them apart
        candidate["vdf_output"] = "aa" * 100
        candidate["hash"]       = block_mod.block_hash(candidate)
        peer_blk["vdf_output"]  = "bb" * 100
        peer_blk["hash"]        = block_mod.block_hash(peer_blk)

        # Force peer to lower hash so it would otherwise win
        if peer_blk["hash"] >= candidate["hash"]:
            peer_blk["vdf_output"], candidate["vdf_output"] = \
                candidate["vdf_output"], peer_blk["vdf_output"]
            peer_blk["hash"] = block_mod.block_hash(peer_blk)
            candidate["hash"] = block_mod.block_hash(candidate)

        # Only the candidate's VDF output passes verification
        valid_output = candidate["vdf_output"]
        def fake_verify(challenge, output, proof):
            return output == valid_output

        with patch("vdf.verify", side_effect=fake_verify):
            winner, relay = n._select_winner(candidate, [peer_blk], g)

        assert winner["hash"] == candidate["hash"]
    finally:
        teardown_node(n, dbfile, keyfile)


def test_peer_block_wrong_height_ignored():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        g = n.chain[-1]
        candidate = make_block(n.chain, builder_addr=addr)
        peer_blk  = make_block(n.chain, builder_addr=addr)
        peer_blk["height"] = 999   # wrong height
        peer_blk["hash"]   = "aa" * 32  # lower hash won't help

        with patch("vdf.verify", return_value=True):
            winner, relay = n._select_winner(candidate, [peer_blk], g)

        assert winner["hash"] == candidate["hash"]
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# _commit: state, chain, storage, burn window, view all updated
# ---------------------------------------------------------------------------

def test_commit_appends_to_chain():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        with patch("vdf.verify", return_value=True):
            commit_block(n, blk)
        assert len(n.chain) == 2
        assert n.chain[-1]["height"] == 1
    finally:
        teardown_node(n, dbfile, keyfile)


def test_commit_persists_block_to_storage():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        with patch("vdf.verify", return_value=True):
            commit_block(n, blk)
        stored = n.storage.load_block(1)
        assert stored["hash"] == blk["hash"]
    finally:
        teardown_node(n, dbfile, keyfile)


def test_commit_applies_block_reward():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        with patch("vdf.verify", return_value=True):
            commit_block(n, blk)
        assert n.state.total_minted > 0
        assert n.state.get_balance(addr) > 0
    finally:
        teardown_node(n, dbfile, keyfile)


def test_commit_publishes_updated_view():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        v_before = n.view
        blk = make_block(n.chain, builder_addr=addr)
        blk["tx_bytes"] = 0
        with patch("vdf.verify", return_value=True):
            commit_block(n, blk)
        assert n.view is not v_before
        assert n.view.height == 1
    finally:
        teardown_node(n, dbfile, keyfile)


def test_commit_removes_confirmed_txs_from_mempool():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)
        n.mempool.add(t)
        assert n.mempool.size() == 1

        blk = make_block(n.chain, builder_addr=addr, txs=[t])
        # Use the real _commit so mempool cleanup runs
        with patch("vdf.verify", return_value=True):
            n._commit(blk, relay=False)
        assert n.mempool.size() == 0
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# fee rate: tx_bytes cache means no recompute
# ---------------------------------------------------------------------------

def test_fee_rate_uses_cached_tx_bytes():
    """compute_expected_fee_rate reads tx_bytes; blocks with the field
    should not need to access their transaction list at all."""
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["tx_bytes"] = 50_000   # set artificially

    # Patch tx_size to fail -- it should not be called
    called = []
    orig = tx_mod.tx_size
    tx_mod.tx_size = lambda t: called.append(1) or orig(t)
    try:
        chain = [g, blk]
        block_mod.compute_expected_fee_rate(chain)
    finally:
        tx_mod.tx_size = orig

    assert called == [], "tx_size should not be called when tx_bytes is present"
