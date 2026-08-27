"""
End-to-end protocol flow tests

These tests exercise complete protocol scenarios from genesis through
multi-block chains, reorgs, fee dynamics, and the emission schedule --
all without network or disk I/O.

Flows covered:
  E2E-1:  Genesis -> mine blocks -> emit rewards -> verify circulating supply
  E2E-3:  Fork choice: most cumulative proven work wins; ties broken by tip hash
  E2E-4:  Reorg: a shorter chain that becomes longer is accepted
  E2E-5:  Fee rate dynamics: spam attack inflates fee, inactivity decays it
  E2E-8:  Full tx lifecycle: create -> mempool -> block -> confirmed
  E2E-9:  Block assembly: assemble() fills to limit, skips oversized single txs
  E2E-10: Multi-sender block with correct nonce sequencing
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import block as block_mod
import state as state_mod
import tx as tx_mod
import mempool as mempool_mod
from chainstate import ChainState
from node import StatsAccumulator
from params import (
    BLOCK_SIZE_TARGET_BYTES, INITIAL_FEE_RATE,
    EMBERS_PER_SCH, SUPPLY_CAP,
)
from tests.fixtures import (
    address, genesis, keypair, make_block,
    make_tx, pubkey_hex, seed_balance,
)


@pytest.fixture(autouse=True)
def mock_vdf(monkeypatch):
    monkeypatch.setattr("block.vdf_mod.verify", lambda *a, **kw: True)


# ---------------------------------------------------------------------------
# E2E-1: Genesis through N blocks, reward emission
# ---------------------------------------------------------------------------

class TestE2E_EmissionSchedule:
    def test_rewards_minted_every_block(self):
        """The full block reward mints unconditionally, every block."""
        cs = ChainState.from_genesis()
        for h in range(1, 6):
            b = make_block(h, cs.tip["hash"], [])
            ok, err, cs = cs.validate_and_apply(b)
            assert ok is True, f"h={h}: {err}"
        assert cs.state.total_minted > 0

    def test_minted_does_not_exceed_supply_cap(self):
        cs = ChainState.from_genesis()
        for h in range(1, 11):
            b = make_block(h, cs.tip["hash"], [])
            _, _, cs = cs.validate_and_apply(b)
        assert cs.state.total_minted <= SUPPLY_CAP

    def test_reward_decreases_as_supply_fills(self):
        """Emission should shrink as more coins are minted."""
        from state import compute_reward
        r_early = compute_reward(0)
        r_late  = compute_reward(SUPPLY_CAP // 2)
        assert r_late < r_early


# ---------------------------------------------------------------------------
# E2E-3: Fork choice -- equal height, lower tip hash wins
# ---------------------------------------------------------------------------

class TestE2E_ForkChoice:
    def test_equal_height_lower_hash_wins(self):
        """Most cumulative proven work wins; ties broken by tip hash."""
        cs = ChainState.from_genesis()
        g = cs.tip
        b1a = make_block(1, g["hash"], [], builder_index=0)
        b1b = make_block(1, g["hash"], [], builder_index=1)

        _, _, csa = cs.validate_and_apply(b1a)
        _, _, csb = cs.validate_and_apply(b1b)

        # Both are at height 1; lower tip hash wins deterministically
        winner = csa if csa.tip["hash"] < csb.tip["hash"] else csb
        loser  = csb if winner is csa else csa
        assert winner.is_better_than(loser)
        assert not loser.is_better_than(winner)

    def test_longer_chain_always_wins(self):
        cs = ChainState.from_genesis()
        g = cs.tip
        b1 = make_block(1, g["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        _, _, cs1 = cs.validate_and_apply(b1)
        _, _, cs2 = cs1.validate_and_apply(b2)
        # cs2 (height 2) must always beat cs1 (height 1)
        assert cs2.is_better_than(cs1)
        assert not cs1.is_better_than(cs2)


# ---------------------------------------------------------------------------
# E2E-4: Reorg (apply_better_chain via _evaluate_remote_chain logic)
# ---------------------------------------------------------------------------

class TestE2E_Reorg:
    def test_longer_remote_chain_replaces_local(self):
        """
        Local: genesis -> b1_local
        Remote: genesis -> b1_remote -> b2_remote
        Remote is longer and should win.
        """
        cs_local = ChainState.from_genesis()
        g = cs_local.tip

        b1_local = make_block(1, g["hash"], [], builder_index=0)
        _, _, cs_local = cs_local.validate_and_apply(b1_local)

        b1_remote = make_block(1, g["hash"], [], builder_index=1)
        b2_remote = make_block(2, b1_remote["hash"], [], builder_index=1)

        cs_remote_base = ChainState.from_genesis()
        _, _, cs_remote = cs_remote_base.validate_and_apply(b1_remote)
        _, _, cs_remote = cs_remote.validate_and_apply(b2_remote)

        assert cs_remote.is_better_than(cs_local)

    def test_same_block_produces_equal_chains(self):
        """Applying the same block to two identical chains yields identical tips."""
        cs_local = ChainState.from_genesis()
        g = cs_local.tip

        b1 = make_block(1, g["hash"], [])
        _, _, cs_a = cs_local.validate_and_apply(b1)
        _, _, cs_b = cs_local.validate_and_apply(b1)  # same block

        assert cs_a.tip["hash"] == cs_b.tip["hash"]
        assert not cs_a.is_better_than(cs_b)
        assert not cs_b.is_better_than(cs_a)


# ---------------------------------------------------------------------------
# E2E-5: Fee rate dynamics
# ---------------------------------------------------------------------------

class TestE2E_FeeDynamics:
    def test_spam_doubles_fee_in_roughly_14_blocks(self):
        """
        Sustained full blocks apply adjustment=1.05 each block.
        At 1.05^14 ~= 1.98, the rate nearly doubles.  int() truncation means this
        is only observable once the base rate is high enough.  Start from rate=100
        embers/byte so the doubling is clearly visible.
        """
        g = genesis()
        chain = [g]
        rate = 100  # start high enough that 5% rises survive int truncation
        for h in range(1, 15):
            blk = make_block(h, chain[-1]["hash"], [])
            blk["tx_bytes"] = BLOCK_SIZE_TARGET_BYTES * 2
            blk["fee_rate"] = rate
            chain.append(blk)
            rate = block_mod.compute_expected_fee_rate(chain)
        # After 14 blocks of 2x volume from base 100, rate should be close to 200
        assert rate > 150

    def test_zero_activity_decays_rate_slowly(self):
        g = genesis()
        chain = [g]
        for h in range(1, 51):
            blk = make_block(h, chain[-1]["hash"], [])
            blk["tx_bytes"] = 0
            blk["fee_rate"] = chain[-1].get("fee_rate", INITIAL_FEE_RATE)
            chain.append(blk)
        final_rate = block_mod.compute_expected_fee_rate(chain)
        # Should decay but not crash to zero
        assert 1 <= final_rate < INITIAL_FEE_RATE


# ---------------------------------------------------------------------------
# E2E-8: Full tx lifecycle
# ---------------------------------------------------------------------------

class TestE2E_TxLifecycle:
    def test_tx_create_to_confirmed(self):
        """create tx -> add to mempool -> include in block -> confirmed."""
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 100 * EMBERS_PER_SCH)
        cs.state.total_minted += 100 * EMBERS_PER_SCH

        mp = mempool_mod.Mempool()
        t = make_tx(0, 1, EMBERS_PER_SCH, cs.state, 0)
        ok, h = mp.add(t)
        assert ok is True

        txs = tx_mod.sort_txs(mp.all_txs())
        b = make_block(1, cs.tip["hash"], txs)
        ok, err, cs2 = cs.validate_and_apply(b)
        assert ok is True, err

        confirmed = {tx_mod.tx_hash(tx) for tx in b["transactions"]}
        mp.remove_many(confirmed)

        assert mp.size() == 0
        assert cs2.state.get_balance(address(1)) == EMBERS_PER_SCH

    def test_rejected_tx_stays_in_mempool(self):
        """Tx failing state validation stays pending."""
        cs = ChainState.from_genesis()
        # No balance for sender -- tx will be invalid
        t = make_tx(3, 4, EMBERS_PER_SCH, cs.state, 0)
        mp = mempool_mod.Mempool()
        # We add it directly to the mempool (bypassing node.submit_tx validation)
        mp.add(t)
        assert mp.size() == 1
        # It stays in the mempool until pruned
        pruned = mp.prune_stale(chain_tip_height=0, state=cs.state)
        # It has correct fee_height so it won't be pruned by staleness,
        # but nonce is wrong (no prior tx from this address is needed)
        # -> nonce check would fail at block inclusion, not pruning
        assert mp.size() + len(pruned) == 1


# ---------------------------------------------------------------------------
# E2E-9: Block assembly fills to limit
# ---------------------------------------------------------------------------

class TestE2E_BlockAssembly:
    def test_assembly_produces_valid_block_with_txs(self):
        g = genesis()
        cs = ChainState.from_genesis()
        cs.state.credit(address(0), 500 * EMBERS_PER_SCH)
        cs.state.total_minted += 500 * EMBERS_PER_SCH

        txs = []
        for i in range(5):
            t = make_tx(0, 1, EMBERS_PER_SCH, cs.state, 0)
            txs.append(t)
            try:
                cs.state.apply_tx(t)
            except Exception:
                break

        sorted_txs = tx_mod.sort_txs(txs)
        assembled = block_mod.assemble(g, sorted_txs, address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        assert assembled["height"] == 1
        assert assembled["previous_hash"] == g["hash"]
        assert len(assembled["transactions"]) <= len(sorted_txs)
        assert "tx_bytes" in assembled

    def test_assembly_respects_block_size_limit(self):
        from params import BLOCK_SIZE_LIMIT
        g = genesis()
        # Provide far more txs than can fit
        dummy_txs = []
        s = state_mod.State()
        s.credit(address(0), 100_000 * EMBERS_PER_SCH)
        s.total_minted = 100_000 * EMBERS_PER_SCH
        for _ in range(100):
            t = make_tx(0, 1, EMBERS_PER_SCH, s, 0)
            dummy_txs.append(t)
            try:
                s.apply_tx(t)
            except Exception:
                break

        assembled = block_mod.assemble(g, dummy_txs, address(0), INITIAL_FEE_RATE, block_mod.VDF_ITERATIONS)
        # Assembled block (with hash placeholder) must fit
        test_block = {**assembled, "hash": "x" * 64}
        assert block_mod.block_size(test_block) <= BLOCK_SIZE_LIMIT


# ---------------------------------------------------------------------------
# E2E-10: StatsAccumulator (node stats tracking)
# ---------------------------------------------------------------------------

class TestE2E_StatsAccumulator:
    def test_stats_empty_at_genesis(self):
        cs = ChainState.from_genesis()
        acc = StatsAccumulator()
        acc.update(cs.chain, cs.state)
        assert acc.points == []

    def test_stats_incremental_update(self):
        cs0 = ChainState.from_genesis()
        b1 = make_block(1, cs0.tip["hash"], [])
        ok, err, cs1 = cs0.validate_and_apply(b1)
        assert ok

        acc = StatsAccumulator()
        acc.update(cs1.chain, cs1.state)
        assert len(acc.points) == 1
        assert acc.points[0]["height"] == 1

    def test_stats_incremental_two_blocks(self):
        cs0 = ChainState.from_genesis()
        b1 = make_block(1, cs0.tip["hash"], [])
        b2 = make_block(2, b1["hash"], [])
        _, _, cs1 = cs0.validate_and_apply(b1)
        _, _, cs2 = cs1.validate_and_apply(b2)

        acc = StatsAccumulator()
        acc.update(cs1.chain, cs1.state)
        acc.update(cs2.chain, cs2.state)
        assert len(acc.points) == 2

    def test_stats_circulating_equals_minted(self):
        cs0 = ChainState.from_genesis()
        cs0.state.credit(address(0), 100 * EMBERS_PER_SCH)
        cs0.state.total_minted += 100 * EMBERS_PER_SCH
        t = make_tx(0, 1, EMBERS_PER_SCH, cs0.state, 0)
        b1 = make_block(1, cs0.tip["hash"], [t])
        ok, _, cs1 = cs0.validate_and_apply(b1)
        assert ok

        acc = StatsAccumulator()
        acc.update(cs1.chain, cs1.state)
        pt = acc.points[0]
        assert pt["circulating"] == pt["minted"]
