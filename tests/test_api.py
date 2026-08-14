"""API endpoint tests."""
import os
import queue as _queue
import tempfile
import threading

import pytest
from helpers import *


class FakeGossip:
    def __init__(self):
        self.relayed_txs = []
    def relay_tx(self, t):          self.relayed_txs.append(t)
    def broadcast_block(self, b):   pass
    def dandelion_send(self, t, h): pass
    def mark_seen(self, h):         return False
    def _broadcast(self, ep, data): pass


class FakeSyncer:
    def check_and_sync(self, chain, fn): return False


class FakePool:
    def __init__(self):
        self._peers = {}
    def count(self):            return len(self._peers)
    def add(self, addr):        self._peers[addr] = 0; return True
    def strike(self, addr):     pass
    def all_addrs(self):        return list(self._peers.keys())


class FakeDiscovery:
    def enqueue_candidate(self, addr): pass


@pytest.fixture
def client():
    from api import create_app
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
    n.state.credit(addr, 100_000_000)
    n._publish_view()
    discovery = FakeDiscovery()
    app = create_app(n, pool, net_in_q, discovery)
    app.config["TESTING"] = True

    # Drain thread: routes submit_tx queue messages back to node loop.
    _running = True
    def _drain():
        while _running:
            try:
                msg = net_in_q.get(timeout=0.1)
                if msg["type"] == "submit_tx":
                    # Run in drain thread, pretend we're the loop thread.
                    n._loop_thread = threading.current_thread()
                    msg["reply"].put(n.submit_tx(msg["tx"]))
            except _queue.Empty:
                pass
    drain_t = threading.Thread(target=_drain, daemon=True)
    drain_t.start()

    yield app.test_client(), n, sk, pk_hex, addr
    _running = False
    drain_t.join(timeout=2)
    n.storage.close()
    os.unlink(keyfile)
    os.unlink(dbfile)


# ---------------------------------------------------------------------------
# Core JSON endpoints
# ---------------------------------------------------------------------------

def test_info_returns_height(client):
    c, *_ = client
    r = c.get("/api/info")
    assert r.status_code == 200
    data = r.get_json()
    assert data["height"] == 0
    assert "total_minted" in data
    assert "total_burnt" in data


def test_fee_rate_present(client):
    c, *_ = client
    assert "fee_rate" in c.get("/api/fee_rate").get_json()


def test_block_0_has_message(client):
    c, *_ = client
    data = c.get("/api/block/0").get_json()
    assert "Echocoin" in data["message"]


def test_block_not_found(client):
    c, *_ = client
    assert c.get("/api/block/999").status_code == 404


def test_chain_tip(client):
    c, *_ = client
    assert c.get("/api/chain/tip").get_json()["height"] == 0


def test_mempool_initially_empty(client):
    c, *_ = client
    assert c.get("/api/mempool").get_json()["size"] == 0


def test_peers_count(client):
    c, *_ = client
    assert c.get("/api/peers").get_json()["count"] == 0


def test_balance_endpoint(client):
    c, _, _, _, addr = client
    data = c.get(f"/api/address/{addr}/balance").get_json()
    assert data["balance_rings"] == 100_000_000


# ---------------------------------------------------------------------------
# Transaction submission
# ---------------------------------------------------------------------------

def test_send_valid_tx(client):
    c, n, sk, pk_hex, addr = client
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    r = c.post("/api/tx/send", json=t)
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_send_invalid_tx_returns_400(client):
    c, _n, sk, pk_hex, addr = client
    _, _, _, to_addr = make_keypair()
    # nonce 99 is invalid (no prior txs so expected nonce is 1)
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 99, 0, 1)
    r = c.post("/api/tx/send", json=t)
    assert r.status_code == 400


def test_mempool_size_after_tx(client):
    c, n, sk, pk_hex, addr = client
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    c.post("/api/tx/send", json=t)
    assert c.get("/api/mempool").get_json()["size"] == 1


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_receive_tx_rate_limited(client):
    c, *_ = client
    payload = {"type": "tx_fluff", "tx": {"nonce": 1}}
    statuses = [c.post("/api/receive_tx", json=payload).status_code for _ in range(25)]
    assert 200 in statuses
    assert 429 in statuses


def test_receive_tx_rate_limit_recovers(client):
    import time
    c, *_ = client
    payload = {"type": "tx_fluff", "tx": {"nonce": 1}}
    for _ in range(25):
        c.post("/api/receive_tx", json=payload)
    time.sleep(1.5)
    assert c.post("/api/receive_tx", json=payload).status_code == 200


# ---------------------------------------------------------------------------
# No solution endpoint
# ---------------------------------------------------------------------------

def test_receive_solution_endpoint_gone(client):
    """The /api/receive_solution endpoint must not exist in Echocoin."""
    c, *_ = client
    r = c.post("/api/receive_solution", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

def test_dashboard_loads(client):
    c, *_ = client
    r = c.get("/")
    assert r.status_code == 200
    assert b"Echocoin" in r.data


def test_whitepaper_page(client):
    c, *_ = client
    r = c.get("/whitepaper")
    assert r.status_code == 200
