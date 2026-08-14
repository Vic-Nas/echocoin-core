"""Proof-of-Burn score engine.

Public interface:

  score(chain, address)             -> int        (lower = more committed)
  cumulative_score(chain)           -> int        (sum of all block scores)
  best_builder(chain, addrs)        -> str        (address with lowest score)
  reward_distribution(chain, builder, reward) -> list[(addr, amount)]

The score formula for a builder at a given chain tip:

  numerator   = Hash(vdf_output_of_tip + builder_pubkey_address)  [as int]
  denominator = max(1, sum of intentional burns by builder
                       in the last POB_WINDOW blocks)

Lower score = more burns = more block-building priority.

Fork choice: when two valid chains of equal height compete, the one
with the lower cumulative_score wins. Honest burners always beat
unburnished botnets whose denominator stays at the floor of 1.

Intentional burns are tx outputs with {"to": BURN_ADDRESS, "amount": N,
  "beneficiary": <address>}. The beneficiary defaults to the sender if absent.
  Burns tagged to a beneficiary accumulate that beneficiary's pool score and
  entitle the contributor to a proportional share of rewards when that
  beneficiary wins a block.

Fee burns are NOT counted -- they flow into emission but not PoB weight.
"""

import collections
import hashlib
import struct

from params import POB_WINDOW

BURN_ADDRESS = "burn"   # sentinel; validated as the one non-BIP39 address


class BurnWindow:
    """Rolling window of intentional burns, maintained incrementally.

    Tracks {beneficiary: {contributor: total_burned}} for the last
    POB_WINDOW blocks. Used during chain replay and live block processing
    to avoid O(window) rescans on every block.

    Usage:
        window = BurnWindow()
        for blk in chain:
            window.add_block(blk)
            dist = window.reward_distribution(builder, reward)
            # or: burn_for_score = window.builder_burn(address)
    """

    def __init__(self):
        # deque of (height, {beneficiary: {contributor: amount}}) per block
        self._blocks: "collections.deque" = collections.deque()
        # aggregated: beneficiary -> contributor -> total in window
        self._totals: "dict[str, dict[str, int]]" = {}
        # flat history list newest-first for UI display
        self._history: "list[dict]" = []

    def add_block(self, blk):
        """Add a block to the window, expiring blocks outside POB_WINDOW."""
        height = blk["height"]

        # Expire old blocks
        while self._blocks and height - self._blocks[0][0] >= POB_WINDOW:
            _, old_burns, old_history = self._blocks.popleft()
            for beneficiary, contribs in old_burns.items():
                for contributor, amount in contribs.items():
                    self._totals[beneficiary][contributor] -= amount
                    if self._totals[beneficiary][contributor] <= 0:
                        del self._totals[beneficiary][contributor]
                    if not self._totals[beneficiary]:
                        del self._totals[beneficiary]
            for entry in old_history:
                self._history.remove(entry)

        # Extract burns from this block
        block_burns: "dict[str, dict[str, int]]" = {}
        block_history = []
        for tx in blk.get("transactions", []):
            sender = tx.get("from", "")
            for out in tx.get("outputs", []):
                if out.get("to") != BURN_ADDRESS:
                    continue
                beneficiary = out.get("beneficiary") or sender
                amount = out["amount"]
                if beneficiary not in block_burns:
                    block_burns[beneficiary] = {}
                block_burns[beneficiary][sender] = (
                    block_burns[beneficiary].get(sender, 0) + amount
                )
                block_history.append({
                    "height": height, "addr": sender,
                    "beneficiary": beneficiary, "amount": amount,
                })

        self._blocks.append((height, block_burns, block_history))
        self._history = block_history + self._history   # newest first

        # Merge into totals
        for beneficiary, contribs in block_burns.items():
            if beneficiary not in self._totals:
                self._totals[beneficiary] = {}
            for contributor, amount in contribs.items():
                self._totals[beneficiary][contributor] = (
                    self._totals[beneficiary].get(contributor, 0) + amount
                )

    def burns_for(self, beneficiary):
        """Return {contributor: amount} for beneficiary in the current window."""
        return dict(self._totals.get(beneficiary, {}))

    def pool_totals(self):
        """Return {beneficiary: total_tagged_burns} for all pools in window."""
        return {b: sum(c.values()) for b, c in self._totals.items()}

    def sender_totals(self):
        """Return {sender: total_burned} across all beneficiaries."""
        totals = {}
        for contribs in self._totals.values():
            for sender, amount in contribs.items():
                totals[sender] = totals.get(sender, 0) + amount
        return totals

    def history(self):
        """Return burn history entries newest-first."""
        return list(self._history)

    def builder_burn(self, address):
        """Total burn weight for address as a builder (self + proxy burns)."""
        contribs = self._totals.get(address, {})
        return sum(contribs.values())

    def reward_distribution(self, beneficiary, reward):
        """Compute proportional reward split among contributors to beneficiary.
        Same semantics as pob.reward_distribution() but O(contributors) not
        O(window * txs_per_block).
        """
        burns = self.burns_for(beneficiary)
        total = sum(burns.values())
        if total == 0 or reward == 0:
            return [(beneficiary, reward)]
        distribution = [
            (addr, reward * amount // total)
            for addr, amount in burns.items()
            if reward * amount // total >= 1
        ]
        if not distribution:
            return [(beneficiary, reward)]
        return distribution

    def score(self, tip_hash_int, address):
        """Compute PoB score using the rolling window instead of chain scan."""
        seed  = tip_hash_int ^ _addr_int(address)
        burn  = self.builder_burn(address)
        return seed // max(1, burn)


def _tip_hash_int(chain):
    """Return the VDF output of the chain tip as an integer seed."""
    tip = chain[-1]
    vdf_out = tip.get("vdf_output") or tip["hash"]
    return int(vdf_out[:64], 16)      # 256-bit int from first 32 bytes of hex


def _make_window(chain):
    """Build a BurnWindow from a chain slice. Used by one-off callers."""
    w = BurnWindow()
    for blk in chain:
        w.add_block(blk)
    return w


def _addr_int(address):
    """Deterministic integer derived from an address string."""
    h = hashlib.sha256(address.encode()).digest()
    return struct.unpack(">Q", h[:8])[0]   # 64-bit, good enough for ordering


def score(chain, address):
    """Compute PoB score for address at the current chain tip.

    Lower is better. Use BurnWindow.score() during replay/live operation
    to avoid rescanning; this one-off version is for sync candidates and tests.
    """
    w = _make_window(chain)
    return w.score(_tip_hash_int(chain), address)


def cumulative_score(chain):
    """Sum of scores for each block's builder over the whole chain.

    Used as the fork-choice weight: lower cumulative score = heavier chain.
    Genesis (height 0, no builder) contributes 0.

    Uses a single rolling BurnWindow walked forward so cost is
    O(chain * avg_burn_txs_per_block) not O(chain^2).
    """
    total  = 0
    window = BurnWindow()
    for blk in chain:
        window.add_block(blk)
        builder = blk.get("builder")
        if builder and blk["height"] > 0:
            # _tip_hash_int reads vdf_output or hash from the tip block directly.
            vdf_out = blk.get("vdf_output") or blk["hash"]
            tip_hash_int = int(vdf_out[:64], 16)
            total += window.score(tip_hash_int, builder)
    return total


def reward_distribution(chain, beneficiary, reward):
    """Compute the reward split for a block won by beneficiary.

    One-off version for sync candidates and tests. For live/replay use,
    call BurnWindow.reward_distribution() directly.
    """
    return _make_window(chain).reward_distribution(beneficiary, reward)


def best_builder(candidates, chain):
    """Return the address with the lowest PoB score among candidates."""
    return min(candidates, key=lambda a: score(chain, a))
