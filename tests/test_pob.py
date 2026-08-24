"""
Unit tests for pob.py (Proof-of-Burn reward engine)

Covers: BurnWindow (add_block, sender_totals, reward_distribution,
history, copy, window expiry).

Burns are tracked per sender over the last POB_WINDOW blocks. Every block
reward is split proportionally among all senders with burns in the window.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pob as pob_mod
from pob import BurnWindow, BURN_ADDRESS
from params import POB_WINDOW, EMBERS_PER_SCH
from tests.fixtures import address, genesis, make_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_burn_block(height, prev_hash, sender_index, amount):
    """Return a minimal block dict containing one burn tx."""
    burn_out = {"to": BURN_ADDRESS, "amount": amount}
    tx = {
        "from": address(sender_index),
        "outputs": [burn_out],
        "nonce": 1, "fee": 0, "fee_height": height,
    }
    return make_block(height, prev_hash, [tx])


def window_from_blocks(blocks):
    w = BurnWindow()
    for b in blocks:
        w.add_block(b)
    return w


# ---------------------------------------------------------------------------
# 1. BurnWindow.add_block / sender_totals
# ---------------------------------------------------------------------------

class TestBurnWindowAddBlock:
    def test_empty_window_sender_totals_empty(self):
        w = BurnWindow()
        w.add_block(genesis())
        assert w.sender_totals() == {}

    def test_burn_recorded_for_sender(self):
        g = genesis()
        blk = make_burn_block(1, g["hash"], sender_index=0, amount=EMBERS_PER_SCH)
        w = window_from_blocks([g, blk])
        assert w.sender_totals()[address(0)] == EMBERS_PER_SCH

    def test_burns_accumulate_across_blocks(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], 0, 2 * EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        assert w.sender_totals()[address(0)] == 3 * EMBERS_PER_SCH

    def test_burns_from_multiple_senders(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], 1, 2 * EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        totals = w.sender_totals()
        assert totals[address(0)] == EMBERS_PER_SCH
        assert totals[address(1)] == 2 * EMBERS_PER_SCH


# ---------------------------------------------------------------------------
# 2. BurnWindow window expiry (POB_WINDOW)
# ---------------------------------------------------------------------------

class TestBurnWindowExpiry:
    def test_burns_expire_after_pob_window(self):
        """Burns older than POB_WINDOW blocks drop out."""
        g = genesis()
        chain = [g]
        b1 = make_burn_block(1, chain[-1]["hash"], 0, EMBERS_PER_SCH)
        chain.append(b1)
        for h in range(2, POB_WINDOW + 2):
            blk = make_block(h, chain[-1]["hash"], [])
            chain.append(blk)
        w = window_from_blocks(chain)
        assert w.sender_totals().get(address(0), 0) == 0

    def test_burn_still_in_window_counts(self):
        """A burn POB_WINDOW-1 blocks ago is still within the window."""
        g = genesis()
        chain = [g]
        b1 = make_burn_block(1, chain[-1]["hash"], 0, EMBERS_PER_SCH)
        chain.append(b1)
        for h in range(2, POB_WINDOW):
            blk = make_block(h, chain[-1]["hash"], [])
            chain.append(blk)
        w = window_from_blocks(chain)
        assert w.sender_totals()[address(0)] == EMBERS_PER_SCH


# ---------------------------------------------------------------------------
# 3. BurnWindow.reward_distribution
# ---------------------------------------------------------------------------

class TestRewardDistribution:
    def test_no_burns_returns_empty(self):
        """Without any burns, reward is not distributed (stays in can_mint)."""
        w = BurnWindow()
        w.add_block(genesis())
        dist = w.reward_distribution(1000)
        assert dist == []

    def test_zero_reward_returns_empty(self):
        g = genesis()
        blk = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        w = window_from_blocks([g, blk])
        dist = w.reward_distribution(0)
        assert dist == []

    def test_single_sender_gets_full_reward(self):
        g = genesis()
        blk = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        w = window_from_blocks([g, blk])
        dist = w.reward_distribution(1000)
        assert len(dist) == 1
        assert dist[0][0] == address(0)
        assert dist[0][1] == 1000

    def test_proportional_split_two_senders(self):
        """Reward splits proportional to burns: 3:1 burn ratio → 3:1 reward ratio."""
        g = genesis()
        b1 = make_burn_block(1, g["hash"], sender_index=0, amount=3 * EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], sender_index=1, amount=EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        dist = dict(w.reward_distribution(4000))
        assert dist[address(0)] == 3000
        assert dist[address(1)] == 1000

    def test_distribution_sums_to_at_most_reward(self):
        """Integer rounding means sum may be <= reward."""
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, 3 * EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], 1, 7 * EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        reward = 9999
        dist = w.reward_distribution(reward)
        total = sum(amt for _, amt in dist)
        assert total <= reward

    def test_large_reward_distributed_accurately(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], 1, EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        dist = dict(w.reward_distribution(1_000_000))
        assert dist[address(0)] == 500_000
        assert dist[address(1)] == 500_000


# ---------------------------------------------------------------------------
# 4. BurnWindow.sender_totals
# ---------------------------------------------------------------------------

class TestSenderTotals:
    def test_sender_totals_empty(self):
        w = BurnWindow()
        w.add_block(genesis())
        assert w.sender_totals() == {}

    def test_sender_totals_sums_per_sender(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], 0, 2 * EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        assert w.sender_totals()[address(0)] == 3 * EMBERS_PER_SCH


# ---------------------------------------------------------------------------
# 5. BurnWindow.history
# ---------------------------------------------------------------------------

class TestBurnHistory:
    def test_history_empty_at_genesis(self):
        w = BurnWindow()
        w.add_block(genesis())
        assert w.history() == []

    def test_history_records_burn_entries(self):
        g = genesis()
        b = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        w = window_from_blocks([g, b])
        h = w.history()
        assert len(h) == 1
        assert h[0]["addr"] == address(0)
        assert h[0]["amount"] == EMBERS_PER_SCH
        assert h[0]["height"] == 1

    def test_history_newest_first(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        b2 = make_burn_block(2, b1["hash"], 1, 2 * EMBERS_PER_SCH)
        w = window_from_blocks([g, b1, b2])
        h = w.history()
        assert h[0]["height"] >= h[-1]["height"]


# ---------------------------------------------------------------------------
# 6. BurnWindow.copy
# ---------------------------------------------------------------------------

class TestBurnWindowCopy:
    def test_copy_is_independent(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        w = window_from_blocks([g, b1])
        copy = w.copy()
        b2 = make_burn_block(2, b1["hash"], 1, 2 * EMBERS_PER_SCH)
        copy.add_block(b2)
        assert w.sender_totals().get(address(1), 0) == 0
        assert copy.sender_totals()[address(1)] == 2 * EMBERS_PER_SCH

    def test_copy_preserves_totals(self):
        g = genesis()
        b = make_burn_block(1, g["hash"], 0, EMBERS_PER_SCH)
        w = window_from_blocks([g, b])
        copy = w.copy()
        assert copy.sender_totals() == w.sender_totals()
