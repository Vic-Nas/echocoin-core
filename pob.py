"""Proof-of-Burn score engine.

Public interface (all via BurnWindow):

  BurnWindow.score(tip_hash_int, address)         -> int   (lower = more committed)
  BurnWindow.reward_distribution(beneficiary, reward) -> list[(addr, amount)]
  BurnWindow.builder_burn(address)                -> int
  BurnWindow.pool_totals()                        -> dict
  BurnWindow.sender_totals()                      -> dict
  BurnWindow.history()                            -> list

Internal helpers used by chainstate.py:

  _tip_hash_int(chain)   -- integer seed from the tip's VDF output
  _addr_int(address)     -- deterministic int from address string

The score formula for a builder at a given chain tip:

  numerator   = Hash(VDF_output_of_tip XOR builder_address_hash)  [as int]
  denominator = max(1, sum of intentional burns by builder in last POB_WINDOW blocks)

Lower score = more burns = more block-building priority.
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
        # deque of (height, {beneficiary: {contributor: amount}}, [history entries]) per block
        self._blocks: "collections.deque" = collections.deque()
        # aggregated: beneficiary -> contributor -> total in window
        self._totals: "dict[str, dict[str, int]]" = {}
        # per-block history deque (newest-first for display); each entry is a list of dicts
        self._history_blocks: "collections.deque" = collections.deque()

    def copy(self):
        """Return an independent copy of this BurnWindow.

        _blocks and _history_blocks contain tuples/lists that are never mutated
        in place — only appended/popped — so a shallow deque copy suffices.
        _totals is a nested dict that IS mutated (counters incremented), so it
        needs a deep copy of one level down.
        """
        import copy
        w = BurnWindow()
        w._blocks         = collections.deque(self._blocks)
        w._history_blocks = collections.deque(self._history_blocks)
        w._totals         = {b: dict(contribs) for b, contribs in self._totals.items()}
        return w

    def add_block(self, blk):
        height = blk["height"]

        # Expire old blocks -- O(1) per block: popleft from both deques together
        while self._blocks and height - self._blocks[0][0] >= POB_WINDOW:
            _, old_burns, _ = self._blocks.popleft()
            self._history_blocks.popleft()
            for beneficiary, contribs in old_burns.items():
                for contributor, amount in contribs.items():
                    self._totals[beneficiary][contributor] -= amount
                    if self._totals[beneficiary][contributor] <= 0:
                        del self._totals[beneficiary][contributor]
                    if not self._totals[beneficiary]:
                        del self._totals[beneficiary]

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
        self._history_blocks.appendleft(block_history)  # newest first

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
        return [entry for block_entries in self._history_blocks for entry in block_entries]

    def builder_burn(self, address):
        """Total burn weight for address as a builder (self + proxy burns)."""
        contribs = self._totals.get(address, {})
        return sum(contribs.values())

    def reward_distribution(self, beneficiary, reward):
        """Compute proportional reward split among contributors to beneficiary.
        The builder always receives a guaranteed 2% base cut so they cannot
        be locked out of their own reward by third-party pool contributors.
        The remaining 98% is split proportionally among contributors.
        """
        if reward == 0:
            return [(beneficiary, 0)]
        # 2% guaranteed to the builder (block producer)
        builder_cut = max(1, reward * 2 // 100)
        remainder   = reward - builder_cut
        burns = self.burns_for(beneficiary)
        total = sum(burns.values())
        if total == 0 or remainder == 0:
            return [(beneficiary, reward)]
        distribution = [
            (addr, remainder * amount // total)
            for addr, amount in burns.items()
            if remainder * amount // total >= 1
        ]
        if not distribution:
            return [(beneficiary, reward)]
        # Add builder's guaranteed cut (merge if builder is also a contributor)
        dist_map = dict(distribution)
        dist_map[beneficiary] = dist_map.get(beneficiary, 0) + builder_cut
        return list(dist_map.items())

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


def _addr_int(address):
    """Deterministic integer derived from an address string."""
    h = hashlib.sha256(address.encode()).digest()
    return struct.unpack(">Q", h[:8])[0]   # 64-bit, good enough for ordering
