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

    def add_block(self, blk):
        """Add a block to the window, expiring blocks outside POB_WINDOW."""
        height = blk["height"]

        # Expire old blocks
        while self._blocks and height - self._blocks[0][0] >= POB_WINDOW:
            _, old_burns = self._blocks.popleft()
            for beneficiary, contribs in old_burns.items():
                for contributor, amount in contribs.items():
                    self._totals[beneficiary][contributor] -= amount
                    if self._totals[beneficiary][contributor] <= 0:
                        del self._totals[beneficiary][contributor]
                    if not self._totals[beneficiary]:
                        del self._totals[beneficiary]

        # Extract burns from this block
        block_burns: "dict[str, dict[str, int]]" = {}
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

        self._blocks.append((height, block_burns))

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


def _burns_by_contributor(chain, beneficiary):
    """Return {contributor_addr: total_burned} for all burns tagged to
    beneficiary in the last POB_WINDOW blocks.

    A burn output tags a beneficiary via {"to": BURN_ADDRESS, "beneficiary": addr}.
    If beneficiary is absent, the sender is the implicit beneficiary (solo burn).
    Only burns where the resolved beneficiary matches the argument are counted.
    """
    window = chain[-POB_WINDOW:] if len(chain) > POB_WINDOW else chain
    totals = {}
    for blk in window:
        for tx in blk.get("transactions", []):
            sender = tx.get("from", "")
            for out in tx.get("outputs", []):
                if out.get("to") != BURN_ADDRESS:
                    continue
                tag = out.get("beneficiary") or sender   # default: self
                if tag != beneficiary:
                    continue
                totals[sender] = totals.get(sender, 0) + out["amount"]
    return totals


def _builder_burn(chain, address):
    """Sum intentional burns tagged to address (self-burns) for score computation.
    For score, only burns where the sender IS the beneficiary count --
    proxy burns (tagging someone else) benefit the tagged address, not the sender.
    """
    return _burns_by_contributor(chain, address).get(address, 0) + _proxy_burns(chain, address)


def _proxy_burns(chain, address):
    """Sum burns by others that tag address as beneficiary."""
    window = chain[-POB_WINDOW:] if len(chain) > POB_WINDOW else chain
    total = 0
    for blk in window:
        for tx in blk.get("transactions", []):
            sender = tx.get("from", "")
            if sender == address:
                continue   # self-burns counted separately
            for out in tx.get("outputs", []):
                if out.get("to") == BURN_ADDRESS and out.get("beneficiary") == address:
                    total += out["amount"]
    return total


def _addr_int(address):
    """Deterministic integer derived from an address string."""
    h = hashlib.sha256(address.encode()).digest()
    return struct.unpack(">Q", h[:8])[0]   # 64-bit, good enough for ordering


def score(chain, address):
    """Compute PoB score for address at the current chain tip.

    Lower is better. Unburnished builders score (tip_seed XOR addr_int),
    which can be large. Heavy burners divide that by their burn total,
    yielding a small score.
    """
    seed      = _tip_hash_int(chain) ^ _addr_int(address)
    burn      = _builder_burn(chain, address)
    denom     = max(1, burn)
    return seed // denom


def cumulative_score(chain):
    """Sum of scores for each block's builder over the whole chain.

    Used as the fork-choice weight: lower cumulative score = heavier chain.
    Genesis (height 0, no builder) contributes 0.
    """
    total = 0
    for i, blk in enumerate(chain):
        builder = blk.get("builder")
        if builder and i > 0:
            total += score(chain[:i], builder)
    return total


def reward_distribution(chain, beneficiary, reward):
    """Compute the reward split for a block won by beneficiary.

    Contributors are addresses who burned coins tagged to beneficiary in the
    last POB_WINDOW blocks. The builder themselves is a contributor if they
    burned with self as beneficiary (self-burns). Proxy burns from others
    improve the builder's score but their reward share goes to the burner,
    not back to the builder as an additional recipient.

    Returns a list of (contributor_addr, amount_rings) for every contributor
    whose integer share >= 1 ring. Total minted may be slightly less than
    reward due to integer rounding; the remainder stays in can_mint.
    Falls back to full reward to builder if no tagged burns exist.
    """
    burns = _burns_by_contributor(chain, beneficiary)
    if not burns or reward == 0:
        return [(beneficiary, reward)]
    total = sum(burns.values())
    distribution = [
        (addr, reward * amount // total)
        for addr, amount in burns.items()
        if reward * amount // total >= 1
    ]
    return distribution if distribution else [(beneficiary, reward)]


def best_builder(candidates, chain):
    """Return the address with the lowest PoB score among candidates."""
    return min(candidates, key=lambda a: score(chain, a))
