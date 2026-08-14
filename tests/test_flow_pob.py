"""Flow: Proof-of-Burn score and reward distribution.

Covers FLOW.md § PoB Score and Reward:
  BurnWindow.add_block → totals → score → reward_distribution
  cumulative_score for fork choice
"""
from unittest.mock import patch
from helpers import *
import pob as pob_mod
from pob import BurnWindow, BURN_ADDRESS, cumulative_score


# ---------------------------------------------------------------------------
# BurnWindow: add and expire
# ---------------------------------------------------------------------------

def test_burn_window_tracks_burns():
    sk, pk, pk_hex, addr = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000)
    burn = make_burn_tx(sk, pk_hex, addr, 50_000, nonce=1, fee_height=0)
    s.apply_tx(burn)

    g = block_mod.create_genesis()
    blk = make_block([g], builder_addr=addr, txs=[burn])
    w = BurnWindow()
    w.add_block(g)
    w.add_block(blk)
    assert w.builder_burn(addr) > 0


def test_burn_window_expires_old_blocks():
    from params import POB_WINDOW
    sk, pk, pk_hex, addr = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000)
    burn = make_burn_tx(sk, pk_hex, addr, 50_000, nonce=1, fee_height=0)
    s.apply_tx(burn)

    g = block_mod.create_genesis()
    blk = make_block([g], builder_addr=addr, txs=[burn])
    w = BurnWindow()
    w.add_block(g)
    w.add_block(blk)
    assert w.builder_burn(addr) > 0

    # Add enough blocks to push the burn outside the window
    chain = [g, blk]
    for i in range(POB_WINDOW + 1):
        next_blk = make_block(chain)
        chain.append(next_blk)
        w.add_block(next_blk)

    assert w.builder_burn(addr) == 0


def test_burn_window_expiry_is_o1_deque():
    """Expiry should only pop from deques, not scan lists.
    Verify by checking _history_blocks deque shrinks on expiry."""
    from params import POB_WINDOW
    w = BurnWindow()
    g = block_mod.create_genesis()
    w.add_block(g)

    # Add a block with a burn
    sk, pk, pk_hex, addr = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000)
    burn = make_burn_tx(sk, pk_hex, addr, 10_000, nonce=1, fee_height=0)
    blk = make_block([g], txs=[burn])
    w.add_block(blk)
    initial_deque_len = len(w._history_blocks)
    assert initial_deque_len > 0

    # Advance past the window
    chain = [g, blk]
    for i in range(POB_WINDOW + 1):
        b = make_block(chain)
        chain.append(b)
        w.add_block(b)

    assert len(w._history_blocks) < initial_deque_len + POB_WINDOW


def test_burn_window_history_newest_first():
    sk, pk, pk_hex, addr = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000)
    g = block_mod.create_genesis()
    chain = [g]
    w = BurnWindow()
    w.add_block(g)

    for i in range(1, 4):
        burn = make_burn_tx(sk, pk_hex, addr, 1_000 * i, nonce=i, fee_height=0)
        s.apply_tx(burn)
        blk = make_block(chain, txs=[burn])
        chain.append(blk)
        w.add_block(blk)

    history = w.history()
    assert len(history) == 3
    heights = [h["height"] for h in history]
    assert heights == sorted(heights, reverse=True)


# ---------------------------------------------------------------------------
# PoB score: burner beats non-burner
# ---------------------------------------------------------------------------

def test_burner_scores_lower_than_non_burner():
    sk, pk, pk_hex, addr = make_keypair()
    _, _, _, other = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000)

    g = block_mod.create_genesis()
    burn = make_burn_tx(sk, pk_hex, addr, 50_000, nonce=1, fee_height=0)
    s.apply_tx(burn)
    blk = make_block([g], builder_addr=addr, txs=[burn])

    chain = [g, blk]
    burner_score = pob_mod.score(chain, addr)
    fresh_score  = pob_mod.score(chain, other)
    assert burner_score < fresh_score


def test_more_burn_means_lower_score():
    """The same address with more total burns gets a lower score.
    We compare two chains: one where addr burns 10k, one where addr burns 50k.
    The XOR numerator is the same for both (same address, same tip) so the
    only difference is the denominator (burn total), making scores comparable.
    """
    sk, pk, pk_hex, addr = make_keypair()
    g = block_mod.create_genesis()

    # Chain A: addr burns 10k
    s_a = state_mod.State(); s_a.credit(addr, 100_000_000)
    burn_small = make_burn_tx(sk, pk_hex, addr, 10_000, nonce=1, fee_height=0)
    s_a.apply_tx(burn_small)
    chain_a = [g, make_block([g], txs=[burn_small])]

    # Chain B: addr burns 50k
    s_b = state_mod.State(); s_b.credit(addr, 100_000_000)
    burn_large = make_burn_tx(sk, pk_hex, addr, 50_000, nonce=1, fee_height=0)
    s_b.apply_tx(burn_large)
    chain_b = [g, make_block([g], txs=[burn_large])]

    score_small = pob_mod.score(chain_a, addr)
    score_large = pob_mod.score(chain_b, addr)
    assert score_large < score_small


# ---------------------------------------------------------------------------
# Reward distribution
# ---------------------------------------------------------------------------

def test_solo_builder_gets_full_reward():
    w = BurnWindow()
    g = block_mod.create_genesis()
    w.add_block(g)
    _, _, _, addr = make_keypair()
    dist = w.reward_distribution(addr, 1_000_000)
    assert dist == [(addr, 1_000_000)]


def test_reward_split_proportional_to_burns():
    sk,  pk,  pk_hex,  builder = make_keypair()
    sk2, pk2, pk_hex2, contrib = make_keypair()
    s = state_mod.State()
    s.credit(builder, 100_000_000)
    s.credit(contrib, 100_000_000)

    g = block_mod.create_genesis()
    # builder burns 3000 to itself; contrib burns 1000 to builder
    burn_self   = make_burn_tx(sk,  pk_hex,  builder, 3_000, nonce=1, fee_height=0)
    burn_proxy  = make_burn_tx(sk2, pk_hex2, contrib, 1_000, nonce=1, fee_height=0,
                                beneficiary=builder)
    s.apply_tx(burn_self); s.apply_tx(burn_proxy)
    blk = make_block([g], txs=[burn_self, burn_proxy])

    w = BurnWindow()
    w.add_block(g)
    w.add_block(blk)

    reward = 4_000_000
    dist   = dict(w.reward_distribution(builder, reward))
    # builder burned 3/4 of total, contrib 1/4
    assert abs(dist.get(builder, 0) - 3_000_000) <= 1
    assert abs(dist.get(contrib, 0) - 1_000_000) <= 1


def test_reward_distribution_sums_to_reward():
    sk, pk, pk_hex, builder = make_keypair()
    sk2, pk2, pk_hex2, c1   = make_keypair()
    sk3, pk3, pk_hex3, c2   = make_keypair()
    s = state_mod.State()
    s.credit(builder, 100_000_000)
    s.credit(c1, 100_000_000)
    s.credit(c2, 100_000_000)

    g = block_mod.create_genesis()
    burns = []
    for sk_, ph_, addr_, amt in [(sk,pk_hex,builder,5000),(sk2,pk_hex2,c1,3000),(sk3,pk_hex3,c2,2000)]:
        b = make_burn_tx(sk_, ph_, addr_, amt, nonce=1, fee_height=0, beneficiary=builder)
        s.apply_tx(b)
        burns.append(b)
    blk = make_block([g], txs=burns)

    w = BurnWindow()
    w.add_block(g)
    w.add_block(blk)

    reward = 1_000_000
    dist   = w.reward_distribution(builder, reward)
    total  = sum(amt for _, amt in dist)
    # Integer rounding may lose at most len(dist) rings
    assert abs(total - reward) <= len(dist)


# ---------------------------------------------------------------------------
# cumulative_score: fork choice
# ---------------------------------------------------------------------------

def test_cumulative_score_increases_with_chain_length():
    chain = make_chain(5)
    s0 = cumulative_score(chain[:1])
    s5 = cumulative_score(chain)
    assert s5 >= s0


def test_honest_chain_beats_botnet_chain():
    """A chain built by active burners has lower cumulative score than
    one built by zero-burners (denominator=1 for all blocks)."""
    sk, pk, pk_hex, addr = make_keypair()
    s = state_mod.State()
    s.credit(addr, 100_000_000_000)

    honest_chain = [block_mod.create_genesis()]
    botnet_chain = [block_mod.create_genesis()]

    for i in range(1, 6):
        burn = make_burn_tx(sk, pk_hex, addr, 10_000 * i, nonce=i, fee_height=0)
        s.apply_tx(burn)
        honest_blk = make_block(honest_chain, builder_addr=addr, txs=[burn])
        honest_chain.append(honest_blk)

        _, _, _, bot_addr = make_keypair()
        bot_blk = make_block(botnet_chain, builder_addr=bot_addr)
        botnet_chain.append(bot_blk)

    assert cumulative_score(honest_chain) < cumulative_score(botnet_chain)
