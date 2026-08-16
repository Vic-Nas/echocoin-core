"""
Unit tests for node.py

Covers every non-VDF method:
  _validate_tail, NodeView, Node._load_cs, Node.is_signing_active,
  Node.mark_tx_seen, Node.get_info, Node.submit_tx, Node.build_and_sign_tx,
  Node._pick_winner, Node._commit, Node._drain_queue, Node._handle,
  Node._handle_inbound_tx, Node._evaluate_remote_chain,
  Node.apply_better_chain, Node._reorg_mempool.

Storage is backed by a real SQLite in-memory equivalent (tmp_path).
Gossip, syncer, pool, net_in_q are mocked. VDF is mocked.
"""

import os
import sys
import queue
import threading
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import crypto
import state as state_mod
import tx as tx_mod
from chainstate import ChainState
from node import Node, NodeView, StatsAccumulator, CensorshipTracker, _validate_tail
from storage import Storage
from params import INITIAL_FEE_RATE, RINGS_PER_ECH
from tests.fixtures import (
    address, genesis, keypair, make_block, make_tx, pubkey_hex, seed_balance,
)


# ---------------------------------------------------------------------------
# VDF mock -- applied everywhere
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# Node factory -- creates a real Node with mocked networking deps
# ---------------------------------------------------------------------------

@pytest.fixture
def node_env(tmp_path):
    """Return (node, keyfile_path) with real storage, mocked net."""
    sk, pk = keypair(0)
    keyfile = str(tmp_path / "node.key")
    passphrase = "testpass"
    crypto.save_key(keyfile, sk, pk, passphrase)
    kek = crypto.derive_kek(keyfile, passphrase)

    gossip  = MagicMock()
    gossip.mark_seen.return_value = False
    syncer  = MagicMock()
    pool    = MagicMock()
    pool.count.return_value = 3
    net_q   = queue.Queue()
    db_path = str(tmp_path / "chain.db")

    node = Node(
        keyfile=keyfile,
        public_key=pk,
        gossip=gossip,
        syncer=syncer,
        pool=pool,
        net_in_q=net_q,
        db_path=db_path,
    )
    node._loop_thread = threading.current_thread()
    node._kek = kek
    return node, keyfile, kek, gossip, syncer, pool, net_q


def fresh_state():
    return state_mod.State()


# ---------------------------------------------------------------------------
# 1. _validate_tail (module-level pure function)
# ---------------------------------------------------------------------------

class TestValidateTail:
    def test_empty_tail_passes(self):
        ok, err = _validate_tail([], [genesis()])
        assert ok is True, err

    def test_single_valid_block_passes(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        ok, err = _validate_tail([b1], [g])
        assert ok is True, err

    def test_invalid_block_in_tail_fails(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b1["previous_hash"] = "00" * 32
        b1["hash"] = block_mod.block_hash(b1)
        ok, err = _validate_tail([b1], [g])
        assert ok is False

    def test_two_valid_blocks_passes(self):
        g = genesis()
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err = _validate_tail([b1, b2], [g])
        assert ok is True, err


# ---------------------------------------------------------------------------
# 2. NodeView
# ---------------------------------------------------------------------------

class TestNodeView:
    def test_node_view_exposes_chain_properties(self):
        cs = ChainState.from_genesis()
        v = NodeView(cs)
        assert v.height == 0
        assert v.tip["hash"] == cs.tip["hash"]
        assert v.genesis_hash == cs.genesis_hash

    def test_node_view_state_is_snapshot(self):
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 1000)
        v = NodeView(cs)
        # Mutate original -- view's snapshot should not change
        cs.state.credit(address(0), 9999)
        assert v.state.get_balance(address(0)) == 1000


# ---------------------------------------------------------------------------
# 3. Node._load_cs (genesis path and reload path)
# ---------------------------------------------------------------------------

class TestLoadCs:
    def test_load_cs_creates_genesis_when_empty(self, node_env):
        node, *_ = node_env
        assert node.cs.height == 0

    def test_load_cs_genesis_saved_to_storage(self, node_env):
        node, *_ = node_env
        stored = node.storage.load_all_blocks()
        assert len(stored) == 1
        assert stored[0]["height"] == 0

    def test_load_cs_reloads_from_storage(self, tmp_path):
        """Second Node creation with same db_path reloads existing chain."""
        sk, pk = keypair(1)
        keyfile = str(tmp_path / "node2.key")
        crypto.save_key(keyfile, sk, pk, "pass")
        db_path = str(tmp_path / "chain2.db")

        # First node -- creates genesis
        n1 = Node(
            keyfile=keyfile, public_key=pk,
            gossip=MagicMock(), syncer=MagicMock(),
            pool=MagicMock(), net_in_q=queue.Queue(),
            db_path=db_path,
        )
        assert n1.cs.height == 0

        # Second node -- should reload genesis from db
        n2 = Node(
            keyfile=keyfile, public_key=pk,
            gossip=MagicMock(), syncer=MagicMock(),
            pool=MagicMock(), net_in_q=queue.Queue(),
            db_path=db_path,
        )
        assert n2.cs.height == 0
        assert n2.cs.tip["hash"] == n1.cs.tip["hash"]


# ---------------------------------------------------------------------------
# 4. Simple accessors
# ---------------------------------------------------------------------------

class TestSimpleAccessors:
    def test_is_signing_active_true_when_kek_set(self, node_env):
        node, *_ = node_env
        assert node.is_signing_active() is True

    def test_is_signing_active_false_when_no_kek(self, node_env):
        node, *_ = node_env
        node._kek = None
        assert node.is_signing_active() is False

    def test_mark_tx_seen_delegates_to_gossip(self, node_env):
        node, _, __, gossip, *_ = node_env
        gossip.mark_seen.return_value = False
        result = node.mark_tx_seen("abc")
        gossip.mark_seen.assert_called_once_with("abc")
        assert result is False

    def test_get_info_returns_expected_keys(self, node_env):
        node, *_ = node_env
        info = node.get_info()
        for key in ["height", "tip_hash", "genesis_hash", "fee_rate",
                    "mempool_size", "address", "peer_count", "total_minted",
                    "total_burnt", "can_mint"]:
            assert key in info

    def test_get_info_height_is_zero_at_genesis(self, node_env):
        node, *_ = node_env
        assert node.get_info()["height"] == 0


# ---------------------------------------------------------------------------
# 5. submit_tx
# ---------------------------------------------------------------------------

class TestSubmitTx:
    def test_submit_valid_tx_returns_true(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        ok, result = node.submit_tx(t)
        assert ok is True
        assert len(result) == 64

    def test_submit_tx_adds_to_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        node.submit_tx(t)
        assert node.mempool.size() == 1

    def test_submit_tx_relays_via_gossip(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        node.submit_tx(t)
        gossip.relay_tx.assert_called_once()

    def test_submit_invalid_tx_returns_false(self, node_env):
        node, *_ = node_env
        # No balance for address(5)
        t = make_tx(5, 1, RINGS_PER_ECH, node.cs.state, 0)
        ok, err = node.submit_tx(t)
        assert ok is False

    def test_submit_duplicate_tx_returns_false(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        node.submit_tx(t)
        ok, err = node.submit_tx(t)
        assert ok is False


# ---------------------------------------------------------------------------
# 6. build_and_sign_tx
# ---------------------------------------------------------------------------

class TestBuildAndSignTx:
    def test_build_and_sign_returns_tx_and_fee(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(node.addr, 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        # Rebuild view so it reflects the updated state
        from node import NodeView
        node.view = NodeView(node.cs)
        outputs = [{"to": address(1), "amount": RINGS_PER_ECH}]
        t, fee = node.build_and_sign_tx(outputs)
        assert isinstance(t, dict)
        assert "signature" in t
        assert isinstance(fee, int) and fee > 0

    def test_build_and_sign_signature_verifies(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(node.addr, 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        from node import NodeView
        node.view = NodeView(node.cs)
        outputs = [{"to": address(1), "amount": RINGS_PER_ECH}]
        t, _ = node.build_and_sign_tx(outputs)
        ok, err = tx_mod.validate(t, node.cs.state, node.cs.height,
                                   node.cs.fee_rate_at)
        assert ok is True, err


# ---------------------------------------------------------------------------
# 7. _pick_winner
# ---------------------------------------------------------------------------

class TestPickWinner:
    def _make_candidate(self, cs):
        g = cs.tip
        blk = make_block(g["height"] + 1, g["hash"], [], builder_index=0)
        return blk

    def test_own_candidate_wins_when_no_peers(self, node_env):
        node, *_ = node_env
        candidate = self._make_candidate(node.cs)
        winner, relay = node._pick_winner(node.cs, candidate, [])
        assert winner is candidate
        assert relay is False

    def test_stale_candidate_returns_none(self, node_env):
        node, *_ = node_env
        candidate = make_block(1, "00" * 32, [])  # wrong previous_hash
        winner, relay = node._pick_winner(node.cs, candidate, [])
        assert winner is None

    def test_invalid_peer_block_ignored(self, node_env):
        node, *_ = node_env
        candidate = self._make_candidate(node.cs)
        # Peer block with wrong height
        bad_peer = make_block(99, node.cs.tip["hash"], [])
        winner, relay = node._pick_winner(node.cs, candidate, [bad_peer])
        assert winner is candidate

    def test_peer_block_accepted_when_lower_score(self, node_env):
        node, *_ = node_env
        # Build a valid peer block for the same slot
        g = node.cs.tip
        peer_blk = make_block(1, g["hash"], [], builder_index=1)
        candidate = make_block(1, g["hash"], [], builder_index=0)
        winner, relay = node._pick_winner(node.cs, candidate, [peer_blk])
        # Winner is whichever has lower PoB score; relay should match
        assert winner in (candidate, peer_blk)
        assert relay == (winner is peer_blk)


# ---------------------------------------------------------------------------
# 8. _commit
# ---------------------------------------------------------------------------

class TestCommit:
    def test_commit_updates_chainstate_height(self, node_env):
        node, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk)
        assert node.cs.height == 1

    def test_commit_removes_confirmed_txs_from_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        node.mempool.add(t)
        blk = make_block(1, node.cs.tip["hash"], [t])
        node._commit(blk)
        assert node.mempool.size() == 0

    def test_commit_updates_view(self, node_env):
        node, *_ = node_env
        old_view = node.view
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk)
        assert node.view is not old_view
        assert node.view.height == 1

    def test_commit_relay_true_broadcasts(self, node_env):
        node, _, __, gossip, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk, relay=True)
        gossip.broadcast_block.assert_called_once()

    def test_commit_relay_false_does_not_broadcast(self, node_env):
        node, _, __, gossip, *_ = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        node._commit(blk, relay=False)
        gossip.broadcast_block.assert_not_called()


# ---------------------------------------------------------------------------
# 9. _drain_queue / _handle
# ---------------------------------------------------------------------------

class TestDrainQueue:
    def test_drain_empty_queue_returns_empty(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        blocks = node._drain_queue()
        assert blocks == []

    def test_drain_block_message(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        blk = make_block(1, node.cs.tip["hash"], [])
        net_q.put({"type": "block", "block": blk})
        blocks = node._drain_queue()
        assert len(blocks) == 1
        assert blocks[0]["hash"] == blk["hash"]

    def test_drain_submit_tx_message(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        reply = queue.Queue()
        node.net_in_q.put({"type": "submit_tx", "tx": t, "reply": reply})
        node._drain_queue()
        ok, _ = reply.get_nowait()
        assert ok is True

    def test_drain_unknown_message_type_ignored(self, node_env):
        node, _, __, ___, ____, _____, net_q = node_env
        net_q.put({"type": "unknown_garbage"})
        blocks = node._drain_queue()  # must not raise
        assert blocks == []


# ---------------------------------------------------------------------------
# 10. _handle_inbound_tx
# ---------------------------------------------------------------------------

class TestHandleInboundTx:
    def test_fluff_valid_tx_added_to_mempool(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        msg = {"tx": t, "relay_type": "tx_fluff", "remaining_hops": 0}
        node._handle_inbound_tx(msg)
        assert node.mempool.size() == 1

    def test_fluff_invalid_tx_not_added(self, node_env):
        node, *_ = node_env
        # No balance for address(5)
        t = make_tx(5, 1, RINGS_PER_ECH, node.cs.state, 0)
        msg = {"tx": t, "relay_type": "tx_fluff", "remaining_hops": 0}
        node._handle_inbound_tx(msg)
        assert node.mempool.size() == 0

    def test_fluff_duplicate_not_added_again(self, node_env):
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        msg = {"tx": t, "relay_type": "tx_fluff", "remaining_hops": 0}
        node._handle_inbound_tx(msg)
        node._handle_inbound_tx(msg)  # second time -- duplicate
        assert node.mempool.size() == 1

    def test_stem_tx_forwarded_without_validation(self, node_env):
        node, _, __, gossip, *_ = node_env
        # Even an invalid tx (no balance) should be forwarded on stem
        t = make_tx(5, 1, RINGS_PER_ECH, node.cs.state, 0)
        msg = {"tx": t, "relay_type": "tx_stem", "remaining_hops": 3}
        node._handle_inbound_tx(msg)
        gossip.dandelion_send.assert_called_once()
        assert node.mempool.size() == 0

    def test_fluff_valid_tx_relayed(self, node_env):
        node, _, __, gossip, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH
        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        msg = {"tx": t, "relay_type": "tx_fluff", "remaining_hops": 0}
        node._handle_inbound_tx(msg)
        gossip.relay_tx.assert_called_once()


# ---------------------------------------------------------------------------
# 11. _evaluate_remote_chain
# ---------------------------------------------------------------------------

class TestEvaluateRemoteChain:
    def test_empty_remote_chain_fails(self, node_env):
        node, *_ = node_env
        ok, err, *_ = node._evaluate_remote_chain([])
        assert ok is False

    def test_wrong_genesis_fails(self, node_env):
        node, *_ = node_env
        wrong_g = dict(genesis())
        wrong_g["message"] = "tampered"
        wrong_g["hash"] = block_mod.block_hash(wrong_g)
        ok, err, *_ = node._evaluate_remote_chain([wrong_g])
        assert ok is False
        assert "genesis" in err

    def test_not_better_than_local_fails(self, node_env):
        node, *_ = node_env
        # Remote chain is identical (same genesis only)
        remote = [node.cs.chain[0]]
        ok, err, *_ = node._evaluate_remote_chain(remote)
        assert ok is False
        assert "not better" in err

    def test_longer_valid_remote_chain_accepted(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err, fork_point, tail, remote_cs = node._evaluate_remote_chain([g, b1, b2])
        assert ok is True, err
        assert remote_cs.height == 2

    def test_invalid_tail_block_fails(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        bad_b1 = make_block(1, "00" * 32, [])  # wrong previous_hash
        ok, err, *_ = node._evaluate_remote_chain([g, bad_b1])
        assert ok is False

    def test_fork_point_correct_for_same_genesis(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, _, fork_point, tail, _ = node._evaluate_remote_chain([g, b1, b2])
        assert ok is True
        assert fork_point == 1  # local is at height 0, diverge at index 1
        assert len(tail) == 2


# ---------------------------------------------------------------------------
# 12. apply_better_chain
# ---------------------------------------------------------------------------

class TestApplyBetterChain:
    def test_apply_better_chain_updates_cs(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        ok, err = node.apply_better_chain([g, b1, b2])
        assert ok is True, err
        assert node.cs.height == 2

    def test_apply_better_chain_updates_view(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        b1 = make_block(1, g["hash"], [])
        node.apply_better_chain([g, b1])
        assert node.view.height == 1

    def test_apply_worse_chain_rejected(self, node_env):
        node, *_ = node_env
        g = node.cs.chain[0]
        # Same-height, same chain -- not better
        ok, err = node.apply_better_chain([g])
        assert ok is False

    def test_apply_better_chain_wrong_genesis_rejected(self, node_env):
        node, *_ = node_env
        wrong_g = dict(genesis())
        wrong_g["message"] = "tampered"
        wrong_g["hash"] = block_mod.block_hash(wrong_g)
        ok, err = node.apply_better_chain([wrong_g])
        assert ok is False
        assert "genesis" in err


# ---------------------------------------------------------------------------
# 13. _reorg_mempool
# ---------------------------------------------------------------------------

class TestReorgMempool:
    def test_reorg_restores_old_chain_txs(self, node_env):
        """Txs from the old chain that aren't in the new chain go back to mempool."""
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH

        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        g = node.cs.chain[0]
        b1_old = make_block(1, g["hash"], [t])
        # Commit the block so t is now confirmed in the old chain
        node._commit(b1_old)
        assert node.cs.height == 1

        # Produce a longer new chain (2 blocks) that does NOT contain t
        b1_new = make_block(1, g["hash"], [], builder_index=1)
        b2_new = make_block(2, b1_new["hash"], [], builder_index=1)
        ok, err = node.apply_better_chain([g, b1_new, b2_new])
        assert ok is True, err
        # t was in the old chain at fork_point=1 and is not in the new chain
        assert node.mempool.get(tx_mod.tx_hash(t)) is not None

    def test_reorg_drops_confirmed_txs(self, node_env):
        """Txs confirmed in both old and new chains are NOT restored to the mempool.
        Calls _reorg_mempool directly since we're testing its logic, not full validation.
        """
        node, *_ = node_env
        node.cs.state.credit(address(0), 100 * RINGS_PER_ECH)
        node.cs.state.total_minted += 100 * RINGS_PER_ECH

        t = make_tx(0, 1, RINGS_PER_ECH, node.cs.state, 0)
        h = tx_mod.tx_hash(t)
        g = node.cs.chain[0]

        # Place t in the old chain (node.cs.chain[1]) so _reorg_mempool sees it
        b1_old = make_block(1, g["hash"], [t])
        # Manually set cs to a chain containing b1_old so old_txs picks up t
        node.cs = ChainState.from_genesis()
        node.cs.chain.append(b1_old)  # add to chain list directly

        # New chain also contains t (same tx confirmed there too)
        b1_new = make_block(1, g["hash"], [t], builder_index=1)
        node._reorg_mempool(fork_point=1, new_chain=[g, b1_new])

        # t is confirmed in new chain -> must NOT appear in mempool
        assert node.mempool.get(h) is None
