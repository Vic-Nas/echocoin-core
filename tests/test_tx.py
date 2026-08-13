"""Transaction validation tests: replay, nonce, balance, fee, signature, malleability."""
import pytest
from helpers import *


@pytest.fixture
def setup():
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to_addr = make_keypair()
    s = funded_state(addr, 1_000_000)
    return sk, pk, pk_hex, addr, to_addr, s


# --- Nonce / replay ---

def test_valid_tx(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 1)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert ok, err


def test_replay_rejected(setup):
    """Same tx sent twice: second should fail (nonce already used)."""
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 1)
    ok, _ = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert ok
    s.apply_tx(t)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "nonce" in err.lower()


def test_nonce_gap_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 3, 0, 1)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "nonce" in err.lower()


def test_nonce_zero_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 0, 0, 1)
    ok, _err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok


# --- Balance ---

def test_insufficient_balance(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 999_999_999, 1, 0, 1)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "balance" in err.lower()


def test_zero_amount_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    outputs = [{"to": to_addr, "amount": 0}]
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, 0, sk)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "positive" in err.lower()


def test_negative_amount_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    outputs = [{"to": to_addr, "amount": -5}]
    t = tx_mod.create(addr, pk_hex, outputs, 1, 0, 0, sk)
    ok, _err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok


# --- Signature ---

def test_wrong_signature_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 1)
    sig = t["signature"]
    t["signature"] = "ff" * (len(sig) // 2)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "signature" in err.lower()


def test_wrong_pubkey_rejected(setup):
    sk, _pk, _pk_hex, addr, to_addr, s = setup
    _sk2, _pk2, pk2_hex, _addr2 = make_keypair()
    t = make_signed_tx(sk, pk2_hex, addr, to_addr, 100, 1, 0, 0)
    ok, _err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok


def test_malleability_hash_changes(setup):
    sk, _pk, pk_hex, addr, to_addr, _s = setup
    t1 = make_signed_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 0)
    t2 = dict(t1)
    t2["fee"] = t1["fee"] + 1
    assert tx_mod.tx_hash(t1) != tx_mod.tx_hash(t2)


# --- Fee height ---

def test_future_fee_height_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 10, 1)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "future" in err.lower()


def test_stale_fee_height_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_valid_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 1)
    # chain tip is 10, fee_height 0 is too old
    ok, err = tx_mod.validate(t, s, 10, fee_rate_fn(1))
    assert not ok
    assert "old" in err.lower()


def test_fee_mismatch_rejected(setup):
    sk, _pk, pk_hex, addr, to_addr, s = setup
    t = make_signed_tx(sk, pk_hex, addr, to_addr, 100, 1, 0, 999)
    ok, err = tx_mod.validate(t, s, 0, fee_rate_fn(1))
    assert not ok
    assert "fee" in err.lower()


# --- Ordering ---

def test_sort_is_deterministic():
    sk1, _, pk1_hex, addr1 = make_keypair()
    _, _, _, to = make_keypair()
    txs = []
    for i in range(5):
        t = make_signed_tx(sk1, pk1_hex, addr1, to, 10, i + 1, i % 3, 0)
        txs.append(t)
    sorted1 = tx_mod.sort_txs(txs)
    sorted2 = tx_mod.sort_txs(list(reversed(txs)))
    assert [tx_mod.tx_hash(t) for t in sorted1] == [tx_mod.tx_hash(t) for t in sorted2]


def test_resort_is_noop():
    sk1, _, pk1_hex, addr1 = make_keypair()
    _, _, _, to = make_keypair()
    txs = [make_signed_tx(sk1, pk1_hex, addr1, to, 10, i + 1, 0, 0) for i in range(5)]
    sorted1 = tx_mod.sort_txs(txs)
    sorted2 = tx_mod.sort_txs(sorted1)
    assert [tx_mod.tx_hash(t) for t in sorted1] == [tx_mod.tx_hash(t) for t in sorted2]
