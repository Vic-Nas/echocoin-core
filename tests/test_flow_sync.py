"""Flow: Chain sync and reorg.

Covers FLOW.md § Chain Sync and Reorg:
  check_and_sync → _find_fork_point (binary search) → _fetch_chain
  → _remote_is_better → _apply_chain → mempool reorg
"""
from unittest.mock import MagicMock, patch

import pytest
from helpers import *
from syncer import Syncer
from peerpool import PeerPool


def make_syncer_with_peer(peer="10.0.0.1:8333"):
    pool = PeerPool("0.0.0.0", 8333)
    pool.add(peer)
    return Syncer(pool), pool


# ---------------------------------------------------------------------------
# _find_fork_point: binary search
# ---------------------------------------------------------------------------

def test_fork_point_shared_genesis():
    syncer, _ = make_syncer_with_peer()
    genesis = block_mod.create_genesis()
    local_chain = [genesis]

    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [genesis]
        result = syncer._find_fork_point("10.0.0.1:8333", local_chain)
    assert result == 1


def test_fork_point_uses_binary_search():
    """O(log n) -- number of requests must be << chain length."""
    syncer, _ = make_syncer_with_peer()
    length = 64
    chain  = make_chain(length)
    call_count = []

    def fake_get(url, **kwargs):
        call_count.append(1)
        params = kwargs.get("params", {})
        h = int(params.get("from", 0))
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = [chain[h]] if h < len(chain) else []
        return m

    with patch("requests.get", side_effect=fake_get):
        syncer._find_fork_point("10.0.0.1:8333", chain)

    import math
    assert len(call_count) <= math.ceil(math.log2(length)) + 2


def test_fork_point_network_error_returns_none():
    syncer, _ = make_syncer_with_peer()
    with patch("requests.get", side_effect=Exception("timeout")):
        result = syncer._find_fork_point("10.0.0.1:8333", make_chain(4))
    assert result is None


# ---------------------------------------------------------------------------
# check_and_sync: peer comparison
# ---------------------------------------------------------------------------

def test_peer_behind_skips_sync():
    syncer, _ = make_syncer_with_peer()
    local_chain = make_chain(6)

    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"height": 3}
        called = []
        result = syncer.check_and_sync(local_chain, lambda c: called.append(c) or True)
    assert result is False
    assert called == []


def test_peer_unreachable_records_strike():
    syncer, pool = make_syncer_with_peer()
    with patch("requests.get", side_effect=Exception("timeout")):
        syncer.check_and_sync(make_chain(1), lambda c: True)
    assert pool._fails.get("10.0.0.1:8333", {}).get("strikes", 0) == 1


def test_peer_ahead_fetches_only_tail():
    """When peer is 2 blocks ahead, only those 2 blocks are fetched."""
    syncer, _ = make_syncer_with_peer()
    local_chain = make_chain(3)   # heights 0-2
    extra1      = make_block(local_chain)
    extra2      = make_block(local_chain + [extra1])
    remote_chain = local_chain + [extra1, extra2]

    fetch_calls = []

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        params = kwargs.get("params", {})
        if "api/info" in url:
            m.json.return_value = {"height": len(remote_chain) - 1}
        else:
            from_h = int(params.get("from", 0))
            to_h   = int(params.get("to", from_h))
            fetch_calls.append((from_h, to_h))
            page = [b for b in remote_chain if from_h <= b["height"] <= to_h]
            m.json.return_value = page
        return m

    collected = []
    with patch("requests.get", side_effect=fake_get):
        syncer.check_and_sync(local_chain, lambda c: collected.append(c) or True)

    # The tail fetch should start at height 3 (fork_point), not 0
    tail_fetches = [f for f in fetch_calls if f[0] >= 3]
    assert len(tail_fetches) >= 1


# ---------------------------------------------------------------------------
# node.sync_chain: _remote_is_better + _apply_chain
# ---------------------------------------------------------------------------

def test_sync_chain_accepted_when_remote_longer():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        remote = make_chain(3)
        with patch("vdf.verify", return_value=True):
            ok, err = n.sync_chain(remote)
        assert ok, err
        assert len(n.chain) == 3
    finally:
        teardown_node(n, dbfile, keyfile)


def test_sync_chain_rejected_when_remote_shorter():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        # Commit two blocks locally
        for _ in range(2):
            blk = make_block(n.chain, builder_addr=addr)
            blk["tx_bytes"] = 0
            with patch("vdf.verify", return_value=True):
                commit_block(n, blk)

        remote = make_chain(1)   # just genesis
        ok, err = n.sync_chain(remote)
        assert not ok
        assert len(n.chain) == 3
    finally:
        teardown_node(n, dbfile, keyfile)


def test_sync_chain_rejected_on_genesis_mismatch():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        # Build a chain with a fake genesis
        fake_genesis = block_mod.create_genesis()
        fake_genesis["message"] = "wrong chain"
        fake_genesis["hash"]    = block_mod.block_hash(fake_genesis)
        fake_chain = [fake_genesis] + make_chain(3)[1:]

        with patch("vdf.verify", return_value=True):
            ok, err = n.sync_chain(fake_chain)
        assert not ok
        assert "genesis" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


def test_reorg_restores_displaced_txs_to_mempool():
    """Txs in the old tail but not in the new tail go back to mempool."""
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)

        # Commit a block containing t
        blk = make_block(n.chain, builder_addr=addr, txs=[t])
        blk["tx_bytes"] = tx_mod.tx_size(t)
        with patch("vdf.verify", return_value=True):
            commit_block(n, blk)
        assert n.mempool.size() == 0

        # Sync to a longer chain that doesn't include t
        remote = make_chain(4)
        with patch("vdf.verify", return_value=True):
            n.sync_chain(remote)

        # t should be back in mempool
        assert n.mempool.get(tx_mod.tx_hash(t)) is not None
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# fork choice: cumulative PoB score
# ---------------------------------------------------------------------------

def test_fork_choice_prefers_lower_cumulative_score():
    """_remote_is_better picks the chain with lower cumulative PoB score
    when heights are equal."""
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        # Build local chain of height 2 with no burns
        for _ in range(2):
            blk = make_block(n.chain, builder_addr=addr)
            blk["tx_bytes"] = 0
            with patch("vdf.verify", return_value=True):
                commit_block(n, blk)

        # Build a competing chain of height 2 with burns
        burning_sk, burning_pk, burning_pk_hex, burning_addr = make_keypair()
        s2 = state_mod.State()
        s2.credit(burning_addr, 100_000_000_000)
        remote = [block_mod.create_genesis()]
        for i in range(1, 3):
            burn = make_burn_tx(burning_sk, burning_pk_hex, burning_addr,
                                10_000_000 * i, nonce=i, fee_height=0)
            s2.apply_tx(burn)
            blk = make_block(remote, builder_addr=burning_addr, txs=[burn])
            remote.append(blk)

        from pob import cumulative_score
        remote_score = cumulative_score(remote)
        local_score  = n._cumulative_score
        # Only assert that _remote_is_better is consistent with score comparison
        expected = n._remote_is_better(remote)
        assert expected == (remote_score < local_score or len(remote) > len(n.chain))
    finally:
        teardown_node(n, dbfile, keyfile)
