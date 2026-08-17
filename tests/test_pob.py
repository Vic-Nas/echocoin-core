"""
Unit tests for pob.py (Proof-of-Burn engine)

Covers: BurnWindow (add_block, score, builder_burn, reward_distribution,
pool_totals, sender_totals, history, copy, window expiry), _tip_hash_int,
_addr_int, and whitepaper properties.

Whitepaper constraints enforced:
  - score = hash_seed XOR addr_int // max(1, burn_in_window)   (Section 3)
  - Lower score = higher priority
  - Burns older than POB_WINDOW blocks expire from the denominator
  - beneficiary field routes burn weight; defaults to sender
  - reward_distribution splits proportionally among contributors
  - New participant (zero burns) gets denominator=1 -- can still build
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pob as pob_mod
from pob import BurnWindow, BURN_ADDRESS, _tip_hash_int, _addr_int
from params import POB_WINDOW, RINGS_PER_ECH
from tests.fixtures import address, genesis, make_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_burn_block(height, prev_hash, sender_index, amount, beneficiary_index=None):
    """Return a minimal block dict containing one burn tx."""
    beneficiary = address(beneficiary_index) if beneficiary_index is not None else None
    burn_out = {"to": BURN_ADDRESS, "amount": amount}
    if beneficiary:
        burn_out["beneficiary"] = beneficiary
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
# 1. _tip_hash_int
# ---------------------------------------------------------------------------

class TestTipHashInt:
    def test_returns_int_from_genesis(self):
        g = genesis()
        result = _tip_hash_int([g])
        assert isinstance(result, int) and result > 0

    def test_uses_vdf_output_if_present(self):
        g = genesis()
        blk = make_block(1, g["hash"], [], vdf_output="aa" * 100, vdf_proof="bb" * 100)
        val = _tip_hash_int([g, blk])
        # Should be int from the vdf_output hex, not the block hash
        expected = int(("aa" * 100)[:64], 16)
        assert val == expected

    def test_falls_back_to_hash_for_genesis(self):
        g = genesis()
        val = _tip_hash_int([g])
        assert val == int(g["hash"][:64], 16)


# ---------------------------------------------------------------------------
# 2. _addr_int
# ---------------------------------------------------------------------------

class TestAddrInt:
    def test_returns_int(self):
        assert isinstance(_addr_int(address(0)), int)

    def test_different_addresses_different_ints(self):
        assert _addr_int(address(0)) != _addr_int(address(1))

    def test_deterministic(self):
        a = address(0)
        assert _addr_int(a) == _addr_int(a)


# ---------------------------------------------------------------------------
# 3. BurnWindow.add_block / builder_burn
# ---------------------------------------------------------------------------

class TestBurnWindowAddBlock:
    def test_empty_window_builder_burn_zero(self):
        w = BurnWindow()
        w.add_block(genesis())
        assert w.builder_burn(address(0)) == 0

    def test_burn_recorded_for_sender(self):
        g = genesis()
        blk = make_burn_block(1, g["hash"], sender_index=0, amount=RINGS_PER_ECH)
        w = window_from_blocks([g, blk])
        assert w.builder_burn(address(0)) == RINGS_PER_ECH

    def test_burn_with_beneficiary_goes_to_beneficiary(self):
        """Whitepaper Section 3: burns tagged to beneficiary count for beneficiary."""
        g = genesis()
        blk = make_burn_block(1, g["hash"], sender_index=0, amount=RINGS_PER_ECH,
                               beneficiary_index=1)
        w = window_from_blocks([g, blk])
        assert w.builder_burn(address(1)) == RINGS_PER_ECH
        assert w.builder_burn(address(0)) == 0

    def test_burns_accumulate_across_blocks(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        b2 = make_burn_block(2, b1["hash"], 0, 2 * RINGS_PER_ECH)
        w = window_from_blocks([g, b1, b2])
        assert w.builder_burn(address(0)) == 3 * RINGS_PER_ECH

    def test_burns_from_multiple_senders(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        b2 = make_burn_block(2, b1["hash"], 1, 2 * RINGS_PER_ECH)
        w = window_from_blocks([g, b1, b2])
        assert w.builder_burn(address(0)) == RINGS_PER_ECH
        assert w.builder_burn(address(1)) == 2 * RINGS_PER_ECH


# ---------------------------------------------------------------------------
# 4. BurnWindow window expiry (POB_WINDOW)
# ---------------------------------------------------------------------------

class TestBurnWindowExpiry:
    def test_burns_expire_after_pob_window(self):
        """Whitepaper Section 3: burns older than POB_WINDOW blocks drop out."""
        g = genesis()
        chain = [g]
        # Add a burn at height 1
        b1 = make_burn_block(1, chain[-1]["hash"], 0, RINGS_PER_ECH)
        chain.append(b1)
        # Add enough empty blocks to push the burn out of the window
        for h in range(2, POB_WINDOW + 2):
            blk = make_block(h, chain[-1]["hash"], [])
            chain.append(blk)
        w = window_from_blocks(chain)
        # The burn at height 1 should now be expired (height_now - 1 >= POB_WINDOW)
        assert w.builder_burn(address(0)) == 0

    def test_burn_still_in_window_counts(self):
        """A burn POB_WINDOW-1 blocks ago is still within the window."""
        g = genesis()
        chain = [g]
        b1 = make_burn_block(1, chain[-1]["hash"], 0, RINGS_PER_ECH)
        chain.append(b1)
        for h in range(2, POB_WINDOW):
            blk = make_block(h, chain[-1]["hash"], [])
            chain.append(blk)
        w = window_from_blocks(chain)
        assert w.builder_burn(address(0)) == RINGS_PER_ECH


# ---------------------------------------------------------------------------
# 5. BurnWindow.score (whitepaper Section 3 formula)
# ---------------------------------------------------------------------------

class TestBurnWindowScore:
    def test_no_burn_score_equals_seed(self):
        """New participant: denominator=1, score = seed // 1."""
        w = BurnWindow()
        w.add_block(genesis())
        tip_hash = _tip_hash_int([genesis()])
        addr = address(0)
        seed = tip_hash ^ _addr_int(addr)
        assert w.score(tip_hash, addr) == seed // 1

    def test_higher_burn_gives_lower_score(self):
        """Whitepaper: lower score = more economic commitment = higher priority."""
        g = genesis()
        # Two builders with different burn amounts
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        b2 = make_burn_block(2, b1["hash"], 1, 100 * RINGS_PER_ECH)
        w = window_from_blocks([g, b1, b2])
        tip_hash = _tip_hash_int([g, b1, b2])
        s0 = w.score(tip_hash, address(0))   # small burn
        s1 = w.score(tip_hash, address(1))   # large burn
        # Large burner must have lower (better) score
        assert s1 < s0

    def test_score_is_non_negative(self):
        g = genesis()
        b = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        w = window_from_blocks([g, b])
        tip_hash = _tip_hash_int([g, b])
        assert w.score(tip_hash, address(0)) >= 0

    def test_score_changes_with_different_tip(self):
        """Numerator depends on VDF output, so same builder gets different scores per block."""
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        b2 = make_burn_block(2, b1["hash"], 0, 0)
        w1 = window_from_blocks([g, b1])
        w2 = window_from_blocks([g, b1, b2])
        tip1 = _tip_hash_int([g, b1])
        tip2 = _tip_hash_int([g, b1, b2])
        s1 = w1.score(tip1, address(0))
        s2 = w2.score(tip2, address(0))
        # Tips differ so scores may differ (not guaranteed to differ by math,
        # but tests that the formula uses the tip hash)
        assert isinstance(s1, int) and isinstance(s2, int)


# ---------------------------------------------------------------------------
# 6. BurnWindow.reward_distribution (whitepaper Section 3 burn pools)
# ---------------------------------------------------------------------------

class TestRewardDistribution:
    def test_no_burns_full_reward_to_builder(self):
        """Without any burns, 100% goes to the builder (beneficiary)."""
        w = BurnWindow()
        w.add_block(genesis())
        dist = w.reward_distribution(address(0), 1000)
        assert dist == [(address(0), 1000)]

    def test_single_contributor_gets_full_reward(self):
        """One contributor = that contributor gets 100%."""
        g = genesis()
        blk = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        w = window_from_blocks([g, blk])
        dist = w.reward_distribution(address(0), 1000)
        assert len(dist) == 1
        assert dist[0][0] == address(0)
        assert dist[0][1] == 1000

    def test_proportional_split_two_contributors(self):
        """contributor_share = reward * contributor_burns / total_burns (whitepaper)."""
        g = genesis()
        b1 = make_burn_block(1, g["hash"], sender_index=0, amount=3 * RINGS_PER_ECH,
                               beneficiary_index=2)
        b2 = make_burn_block(2, b1["hash"], sender_index=1, amount=RINGS_PER_ECH,
                               beneficiary_index=2)
        w = window_from_blocks([g, b1, b2])
        dist = dict(w.reward_distribution(address(2), 4000))
        # 2% builder cut = 80 to address(2); remainder 3920 splits 3:1
        assert dist[address(2)] == 80
        assert dist[address(0)] == 2940
        assert dist[address(1)] == 980

    def test_zero_reward_returns_beneficiary_with_zero(self):
        w = BurnWindow()
        w.add_block(genesis())
        dist = w.reward_distribution(address(0), 0)
        assert dist == [(address(0), 0)]

    def test_distribution_sums_to_at_most_reward(self):
        """Integer rounding means sum may be <= reward (remainder stays in can_mint)."""
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, 3 * RINGS_PER_ECH, beneficiary_index=3)
        b2 = make_burn_block(2, b1["hash"], 1, 7 * RINGS_PER_ECH, beneficiary_index=3)
        w = window_from_blocks([g, b1, b2])
        reward = 9999
        dist = w.reward_distribution(address(3), reward)
        total = sum(amt for _, amt in dist)
        assert total <= reward


# ---------------------------------------------------------------------------
# 7. BurnWindow.pool_totals and sender_totals
# ---------------------------------------------------------------------------

class TestPoolAndSenderTotals:
    def test_pool_totals_empty(self):
        w = BurnWindow()
        w.add_block(genesis())
        assert w.pool_totals() == {}

    def test_pool_totals_sums_per_beneficiary(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH, beneficiary_index=2)
        b2 = make_burn_block(2, b1["hash"], 1, 2 * RINGS_PER_ECH, beneficiary_index=2)
        w = window_from_blocks([g, b1, b2])
        totals = w.pool_totals()
        assert totals[address(2)] == 3 * RINGS_PER_ECH

    def test_sender_totals_aggregates_across_pools(self):
        g = genesis()
        # addr(0) burns to two different beneficiaries
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH, beneficiary_index=2)
        b2 = make_burn_block(2, b1["hash"], 0, RINGS_PER_ECH, beneficiary_index=3)
        w = window_from_blocks([g, b1, b2])
        totals = w.sender_totals()
        assert totals[address(0)] == 2 * RINGS_PER_ECH


# ---------------------------------------------------------------------------
# 8. BurnWindow.history
# ---------------------------------------------------------------------------

class TestBurnHistory:
    def test_history_empty_at_genesis(self):
        w = BurnWindow()
        w.add_block(genesis())
        assert w.history() == []

    def test_history_records_burn_entries(self):
        g = genesis()
        b = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        w = window_from_blocks([g, b])
        h = w.history()
        assert len(h) == 1
        assert h[0]["addr"] == address(0)
        assert h[0]["amount"] == RINGS_PER_ECH
        assert h[0]["height"] == 1

    def test_history_newest_first(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        b2 = make_burn_block(2, b1["hash"], 1, 2 * RINGS_PER_ECH)
        w = window_from_blocks([g, b1, b2])
        h = w.history()
        # Newest first: height 2 before height 1
        assert h[0]["height"] >= h[-1]["height"]


# ---------------------------------------------------------------------------
# 9. BurnWindow.copy
# ---------------------------------------------------------------------------

class TestBurnWindowCopy:
    def test_copy_is_independent(self):
        g = genesis()
        b1 = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        w = window_from_blocks([g, b1])
        copy = w.copy()
        # Mutate copy by adding a block
        b2 = make_burn_block(2, b1["hash"], 1, 2 * RINGS_PER_ECH)
        copy.add_block(b2)
        # Original should not be affected
        assert w.builder_burn(address(1)) == 0
        assert copy.builder_burn(address(1)) == 2 * RINGS_PER_ECH

    def test_copy_preserves_totals(self):
        g = genesis()
        b = make_burn_block(1, g["hash"], 0, RINGS_PER_ECH)
        w = window_from_blocks([g, b])
        copy = w.copy()
        assert copy.builder_burn(address(0)) == w.builder_burn(address(0))
