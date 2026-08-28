"""
Unit tests for api.py's pure helper functions (no Flask app, no HTTP).

Covers: fee_estimate (the send UI's fee-market summary).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api
import mempool as mempool_mod
import peerpool as peerpool_mod
from chainstate import ChainState
from node import NodeView
from params import TICKS_PER_LAPSE
from tests.fixtures import address, make_tx, seed_balance


class _FakeNode:
    """Just enough of Node's public surface for fee_estimate: .mempool
    and .view (chain/tip)."""

    def __init__(self, cs, addr=None):
        self.mempool = mempool_mod.Mempool()
        self.view = NodeView(cs)
        self.addr = addr or address(0)


def fresh():
    cs = ChainState.from_genesis()
    seed_balance(cs.state, 0, 1000.0)
    return _FakeNode(cs), cs


class TestFeeEstimate:
    def test_empty_mempool_returns_all_zero(self):
        node, _ = fresh()
        fees = api.fee_estimate(node)
        assert fees == {"pending": 0, "min": 0, "median": 0, "max": 0, "next_block": 0}

    def test_reports_pending_count_and_rates(self):
        node, cs = fresh()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, fee=10)
        node.mempool.add(t1)
        fees = api.fee_estimate(node)
        assert fees["pending"] == 1
        # min/median/max should all equal the single tx's own fee-per-byte
        assert fees["min"] == fees["max"] == fees["median"]
        assert fees["min"] > 0

    def test_next_block_zero_when_mempool_below_capacity(self):
        """A mempool that easily fits in one block needs no minimum fee to
        clear the next block."""
        node, cs = fresh()
        t1 = make_tx(0, 1, TICKS_PER_LAPSE, cs.state, fee=0)
        node.mempool.add(t1)
        fees = api.fee_estimate(node)
        assert fees["next_block"] == 0

    def test_next_block_reflects_the_real_cutoff_when_block_is_full(self):
        """When the mempool overflows one block, next_block must match
        whatever block.assemble() itself would actually require -- this
        reuses assemble() directly rather than reimplementing its packing
        logic, so the two can never drift apart."""
        import block as block_mod
        from unittest import mock

        node, cs = fresh()
        seed_balance(cs.state, 0, 100_000.0)
        txs = []
        s = cs.state
        for i in range(20):
            t = make_tx(0, 1, 1, s, fee=i)
            s.apply_tx(t)
            node.mempool.add(t)
            txs.append(t)

        # Force a tiny block size so the mempool clearly overflows one block.
        skeleton_size = block_mod.block_size(block_mod.create(
            height=1, previous_hash=cs.tip["hash"], transactions=[],
            builder=address(0), vdf_iterations=block_mod.VDF_ITERATIONS))
        one_tx_size = block_mod.tx_mod.tx_size_in_block(txs[0], position=0)
        tiny_limit = skeleton_size + one_tx_size * 3  # room for only a few

        with mock.patch("block.BLOCK_SIZE_LIMIT", tiny_limit):
            fees = api.fee_estimate(node)
            iterations = block_mod.get_vdf_iterations(node.view.chain)
            candidate = block_mod.assemble(node.view.tip, node.mempool.all_txs(),
                                            address(0), iterations)

        assert len(candidate["transactions"]) < len(txs)
        expected = min(t.get("fee", 0) / max(block_mod.tx_mod.tx_size(t), 1)
                        for t in candidate["transactions"])
        assert fees["next_block"] == expected


class TestPeersPage:
    """Smoke test the /peers route end to end: real PeerPool.snapshot()
    shape, self-row wiring, and the height/wallet columns all render
    without a template/route mismatch."""

    def _client(self):
        node, cs = fresh()
        pool = peerpool_mod.PeerPool(host="0.0.0.0", port=1234)
        pool.add("1.2.3.4:9000")
        pool.update_info("1.2.3.4:9000", height=5, wallet="peer.wallet.addr")
        pool.add("5.6.7.8:9000")  # no update_info -- height/wallet unknown
        pool.add("9.9.9.9:9000")
        pool.note_relayed_builder("9.9.9.9:9000", "inferred.wallet.addr")
        app = api.create_private_app(node, pool)
        return app.test_client()

    def test_peers_page_renders(self):
        resp = self._client().get("/peers")
        assert resp.status_code == 200

    def test_peers_page_shows_self_and_peer_data(self):
        html = self._client().get("/peers").get_data(as_text=True)
        assert "(you)" in html
        assert "1.2.3.4:9000" in html
        assert "peer.wallet.addr" in html
        assert "5.6.7.8:9000" in html
        assert "unknown" in html  # peer with no cached wallet yet
        assert "?" in html        # peer with no cached height yet
        assert "9.9.9.9:9000" in html
        assert "inferred.wallet.addr" in html
        assert "unconfirmed" in html
