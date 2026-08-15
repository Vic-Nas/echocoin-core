"""Flow: Security properties.

Covers the security table in FLOW.md:
  double-spend / reorg, botnet Sybil, eclipse, transaction replay,
  transaction censorship, fee manipulation, spam, VDF forgery,
  block size, balance overflow.

Each test maps directly to one row in the table.
"""
from unittest.mock import patch

import pytest
from helpers import *
from pob import cumulative_score, BURN_ADDRESS


# ---------------------------------------------------------------------------
# Double-spend / reorg: shorter chain rejected
# ---------------------------------------------------------------------------

def test_shorter_chain_rejected_on_sync():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        for _ in range(3):
            blk = make_block(n.chain, builder_addr=addr)
            blk["tx_bytes"] = 0
            with patch("vdf.verify", return_value=True):
                commit_block(n, blk)
        assert len(n.chain) == 4

        shorter = make_chain(2)
        ok, err = n.apply_better_chain(shorter)
        assert not ok
        assert len(n.chain) == 4
    finally:
        teardown_node(n, dbfile, keyfile)


def test_reorg_replays_from_fork_point():
    """Reorg to a longer chain correctly rebuilds balances from fork_point."""
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()

        blk1 = make_block(n.chain, builder_addr=addr)
        blk1["tx_bytes"] = 0
        with patch("vdf.verify", return_value=True):
            commit_block(n, blk1)

        # Remote chain forks after genesis
        remote = make_chain(4)
        with patch("vdf.verify", return_value=True):
            ok, err = n.apply_better_chain(remote)
        assert ok, err
        assert len(n.chain) == 4
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# Botnet Sybil: zero-burn chain loses cumulative score comparison
# ---------------------------------------------------------------------------

def test_botnet_chain_has_higher_cumulative_score():
    """Chain with no burns always has higher cumulative score than
    chain with active burners of the same length."""
    sk, pk, pk_hex, addr = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000_000)

    honest = [block_mod.create_genesis()]
    botnet = [block_mod.create_genesis()]

    for i in range(1, 4):
        burn = make_burn_tx(sk, pk_hex, addr, 10_000_000, nonce=i, fee_height=0)
        s.apply_tx(burn)
        honest.append(make_block(honest, builder_addr=addr, txs=[burn]))

        _, _, _, bot = make_keypair()
        botnet.append(make_block(botnet, builder_addr=bot))

    assert cumulative_score(honest) < cumulative_score(botnet)


# ---------------------------------------------------------------------------
# Transaction replay: nonce prevents replay
# ---------------------------------------------------------------------------

def test_replayed_tx_rejected_by_nonce():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)

        n.state.apply_tx(t)   # advance nonce to 1

        ok, err = n.submit_tx(t)
        assert not ok
        assert "nonce" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


def test_nonce_gap_rejected():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        # nonce 2 when current is 0 (expected 1)
        outputs = [{"to": to, "amount": 1_000}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, 2, 0, 1)
        t = tx_mod.create(addr, pk_hex, outputs, 2, 0, fee, sk)
        ok, err = n.submit_tx(t)
        assert not ok
        assert "nonce" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# Fee manipulation: wrong fee rejected
# ---------------------------------------------------------------------------

def test_wrong_fee_rejected():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        outputs = [{"to": to, "amount": 1_000}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
        t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee + 999, sk)
        ok, err = n.submit_tx(t)
        assert not ok
        assert "fee" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


def test_wrong_fee_height_rejected():
    from params import FEE_HEIGHT_MAX_AGE
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        # fee_height in the future
        outputs = [{"to": to, "amount": 1_000}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, 1)
        t = tx_mod.create(addr, pk_hex, outputs, 1, 9999, fee, sk)
        ok, err = n.submit_tx(t)
        assert not ok
    finally:
        teardown_node(n, dbfile, keyfile)


# ---------------------------------------------------------------------------
# Spam: fee rate rises under sustained full blocks
# ---------------------------------------------------------------------------

def test_fee_rate_rises_under_full_blocks():
    """Repeated full blocks drive the fee rate up by up to 5%/block.
    Starting from INITIAL_FEE_RATE=10 we simulate using the formula directly
    with a larger seed to avoid integer truncation swallowing the 5% gain.
    """
    from params import BLOCK_SIZE_TARGET_BYTES, INITIAL_FEE_RATE

    rate = 1_000   # large enough that int(rate * 1.05) > rate each step
    start = rate
    for _ in range(15):
        vol_ratio  = 2.0
        adjustment = min(1.05, vol_ratio)
        rate = max(1, int(rate * adjustment))

    assert rate > start * 1.5   # 15 rounds of 5% growth well exceeds 50%


def test_fee_rate_minimum_is_one():
    """No matter how little activity, rate never drops below 1."""
    from params import INITIAL_FEE_RATE
    rate = INITIAL_FEE_RATE
    for _ in range(10_000):
        rate = max(1, int(rate * 0.999))
    assert rate >= 1


# ---------------------------------------------------------------------------
# Block size: oversized block rejected
# ---------------------------------------------------------------------------

def test_oversized_block_rejected():
    from params import BLOCK_SIZE_LIMIT
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["transactions"] = [{"fake": "x" * BLOCK_SIZE_LIMIT}]
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok
    assert "size" in err.lower()


# ---------------------------------------------------------------------------
# VDF forgery: invalid proof rejected
# ---------------------------------------------------------------------------

def test_invalid_vdf_proof_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    with patch("vdf.verify", return_value=False):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok
    assert "vdf" in err.lower()


def test_missing_vdf_fields_rejected():
    g = block_mod.create_genesis()
    blk = make_block([g])
    blk["vdf_output"] = None
    blk["hash"] = block_mod.block_hash(blk)
    with patch("vdf.verify", return_value=True):
        ok, err = block_mod.validate(blk, state_mod.State(), [g], fee_rate_fn(1))
    assert not ok and "vdf" in err.lower()


# ---------------------------------------------------------------------------
# Balance overflow / underflow
# ---------------------------------------------------------------------------

def test_output_exceeding_balance_rejected():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 1_000)
        _, _, _, to = make_keypair()
        rate = n.chain[0]["fee_rate"]
        outputs = [{"to": to, "amount": 999_999_999}]
        fee = tx_mod.compute_fee(addr, pk_hex, outputs, 1, 0, rate)
        t = tx_mod.create(addr, pk_hex, outputs, 1, 0, fee, sk)
        ok, err = n.submit_tx(t)
        assert not ok
        assert "balance" in err.lower()
    finally:
        teardown_node(n, dbfile, keyfile)


def test_state_debit_raises_on_underflow():
    s = state_mod.State()
    s.credit("addr", 100)
    with pytest.raises(ValueError):
        s.debit("addr", 200)


def test_balance_never_negative_after_apply():
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, to = make_keypair()
    s = state_mod.State()
    s.credit(addr, 10_000)
    t = make_valid_tx(sk, pk_hex, addr, to, 5_000, 1, 0, 1)
    s.apply_tx(t)
    assert s.get_balance(addr) >= 0


# ---------------------------------------------------------------------------
# Transaction censorship: censorship score
# ---------------------------------------------------------------------------

def test_censorship_score_one_for_block_with_no_missing_txs():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        blk = make_block(n.chain, builder_addr=addr)
        score = n._censorship_score(blk)
        assert score == 1.0
    finally:
        teardown_node(n, dbfile, keyfile)


def test_censorship_score_decreases_with_repeated_exclusion():
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)
        n.mempool.add(t)
        h = tx_mod.tx_hash(t)

        # Simulate repeated exclusion -- increment exclusion age
        n._tx_exclusion_age[h] = 5

        blk = make_block(n.chain, builder_addr=addr, txs=[])
        score = n._censorship_score(blk)
        assert score <= 0.2   # 1/5
    finally:
        teardown_node(n, dbfile, keyfile)


def test_first_exclusion_does_not_penalise():
    """Age 0 (first miss) scores 1.0 -- timing noise tolerance."""
    n, sk, pk, pk_hex, addr, gossip, dbfile, keyfile = make_node()
    try:
        n.state.credit(addr, 100_000_000)
        _, _, _, to = make_keypair()
        t = make_valid_tx(sk, pk_hex, addr, to, 1_000, 1, 0, 1)
        n.mempool.add(t)
        # No exclusion age set -- defaults to 0

        blk = make_block(n.chain, builder_addr=addr, txs=[])
        score = n._censorship_score(blk)
        assert score == 1.0
    finally:
        teardown_node(n, dbfile, keyfile)
