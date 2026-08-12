"""API endpoint tests."""
import os
import tempfile
import queue as _queue
import pytest
from helpers import *


class FakeGossip:
    def __init__(self):
        self.relayed_txs = []
    def relay_tx(self, t):               self.relayed_txs.append(t)
    def broadcast_block(self, b):        pass
    def broadcast_solution(self, s, c):  pass
    def dandelion_send(self, tx, hops):  pass
    def mark_seen(self, h):              pass
    def _broadcast(self, ep, data):      pass


class FakeSyncer:
    def check_and_sync(self, h, fn):     return False


class FakePool:
    def __init__(self):
        self._peers = {}
    def count(self):                     return len(self._peers)
    def add(self, addr):
        self._peers[addr] = 0
        return True
    def strike(self, addr):              pass
    def all_addrs(self):                 return list(self._peers.keys())


@pytest.fixture
def client():
    from node import Node
    from api import create_app
    sk, pk, pk_hex, addr = make_keypair()
    gossip = FakeGossip()
    syncer = FakeSyncer()
    pool   = FakePool()
    net_in_q = _queue.Queue()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        keyfile = f.name
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbfile = f.name
    crypto.save_key(keyfile, sk, pk, "testpass")
    n = Node(keyfile, pk, gossip, syncer, pool, net_in_q, db_path=dbfile)
    n.state.credit(addr, 1_000_000)
    n._publish_view()
    app = create_app(n, pool, net_in_q)
    app.config["TESTING"] = True

    # Run a simple drain thread so submit_tx_from_api (queue-based) works in tests
    import threading
    _drain_running = True
    def _drain():
        import queue as _dq
        while _drain_running:
            try:
                msg = net_in_q.get(timeout=0.1)
                if msg["type"] == "submit_tx":
                    result = n.submit_tx(msg["tx"])
                    msg["reply"].put(result)
                elif msg["type"] == "tx":
                    pass  # ignore in tests
            except _dq.Empty:
                pass
    drain_t = threading.Thread(target=_drain, daemon=True)
    drain_t.start()

    yield app.test_client(), n, sk, pk_hex, addr
    _drain_running = False
    drain_t.join(timeout=2)
    n.storage.close()
    os.unlink(keyfile)
    os.unlink(dbfile)


def test_receive_tx_rate_limited(client):
    c, n, sk, pk_hex, addr = client
    payload = {"type": "tx_fluff", "tx": {"nonce": 1}}
    statuses = [c.post("/api/receive_tx", json=payload).status_code for _ in range(25)]
    assert 200 in statuses
    assert 429 in statuses, "expected some requests to be rate limited past burst capacity"


def test_receive_tx_rate_limit_recovers_over_time(client):
    import time
    c, n, sk, pk_hex, addr = client
    payload = {"type": "tx_fluff", "tx": {"nonce": 1}}
    for _ in range(25):
        c.post("/api/receive_tx", json=payload)
    time.sleep(1.5)
    r = c.post("/api/receive_tx", json=payload)
    assert r.status_code == 200


def test_info(client):
    c, *_ = client
    r = c.get("/api/info")
    assert r.status_code == 200
    assert r.get_json()["height"] == 0


def test_fee_rate(client):
    c, *_ = client
    assert "fee_rate" in c.get("/api/fee_rate").get_json()


def test_genesis_has_message(client):
    c, *_ = client
    data = c.get("/api/block/0").get_json()
    assert "PoolCoin genesis" in data["message"]


def test_block_not_found(client):
    c, *_ = client
    assert c.get("/api/block/999").status_code == 404


def test_chain_tip(client):
    c, *_ = client
    assert c.get("/api/chain/tip").get_json()["height"] == 0


def test_send_valid_tx(client):
    c, n, sk, pk_hex, addr = client
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    r = c.post("/api/tx/send", json=t)
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_send_invalid_tx(client):
    c, n, sk, pk_hex, addr = client
    _, _, _, to_addr = make_keypair()
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 99, 0, 1)
    r = c.post("/api/tx/send", json=t)
    assert r.status_code == 400


def test_mempool(client):
    c, n, sk, pk_hex, addr = client
    _, _, _, to_addr = make_keypair()
    rate = n.chain[-1]["fee_rate"]
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, rate)
    c.post("/api/tx/send", json=t)
    assert c.get("/api/mempool").get_json()["size"] == 1


def test_balance(client):
    c, _, _, _, addr = client
    assert c.get(f"/api/address/{addr}/balance").get_json()["balance_seeds"] == 1_000_000


def test_peers(client):
    c, *_ = client
    assert c.get("/api/peers").get_json()["count"] == 0


def test_whitepaper_page(client):
    c, *_ = client
    r = c.get("/whitepaper")
    assert r.status_code == 200
    assert b"PoolCoin" in r.data


def test_dashboard(client):
    c, *_ = client
    r = c.get("/")
    assert r.status_code == 200
    assert b"POOLCOIN" in r.data
