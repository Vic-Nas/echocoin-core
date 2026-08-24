"""Proof-of-Burn reward engine.

Public interface (all via BurnWindow):

  BurnWindow.reward_distribution(reward) -> list[(addr, amount)]
  BurnWindow.sender_totals()             -> dict
  BurnWindow.history()                   -> list

Burns are tracked per sender over the last POB_WINDOW blocks. Every block
reward is split proportionally among all senders who have burns in the
window. If no one has burned, the reward stays in can_mint.
"""

import collections

from params import POB_WINDOW

BURN_ADDRESS = "burn"   # sentinel; validated as the one non-BIP39 address


class BurnWindow:
    """Rolling window of intentional burns, maintained incrementally.

    Tracks {sender: total_burned} for the last POB_WINDOW blocks.
    Used during chain replay and live block processing to avoid O(window)
    rescans on every block.

    Usage:
        window = BurnWindow()
        for blk in chain:
            window.add_block(blk)
            dist = window.reward_distribution(reward)
    """

    def __init__(self):
        # deque of (height, {sender: amount}, [history entries]) per block
        self._blocks: "collections.deque" = collections.deque()
        # aggregated: sender -> total in window
        self._totals: "dict[str, int]" = {}
        # per-block history deque (newest-first for display)
        self._history_blocks: "collections.deque" = collections.deque()

    def copy(self):
        """Return an independent copy of this BurnWindow."""
        w = BurnWindow()
        w._blocks         = collections.deque(self._blocks)
        w._history_blocks = collections.deque(self._history_blocks)
        w._totals         = dict(self._totals)
        return w

    def add_block(self, blk):
        height = blk["height"]

        # Expire old blocks
        while self._blocks and height - self._blocks[0][0] >= POB_WINDOW:
            _, old_burns, _ = self._blocks.popleft()
            self._history_blocks.popleft()
            for sender, amount in old_burns.items():
                self._totals[sender] = self._totals.get(sender, 0) - amount
                if self._totals[sender] <= 0:
                    del self._totals[sender]

        # Extract burns from this block
        block_burns: "dict[str, int]" = {}
        block_history = []
        for tx in blk.get("transactions", []):
            sender = tx.get("from", "")
            for out in tx.get("outputs", []):
                if out.get("to") != BURN_ADDRESS:
                    continue
                amount = out["amount"]
                block_burns[sender] = block_burns.get(sender, 0) + amount
                block_history.append({
                    "height": height, "addr": sender, "amount": amount,
                })

        self._blocks.append((height, block_burns, block_history))
        self._history_blocks.appendleft(block_history)

        for sender, amount in block_burns.items():
            self._totals[sender] = self._totals.get(sender, 0) + amount

    def sender_totals(self):
        """Return {sender: total_burned} in the current window."""
        return dict(self._totals)

    def history(self):
        """Return burn history entries newest-first."""
        return [entry for block_entries in self._history_blocks for entry in block_entries]

    def reward_distribution(self, reward):
        """Split reward proportionally among all senders with burns in the window.

        Returns a list of (address, amount) pairs. If no burns exist in the
        window, returns an empty list and the reward stays in can_mint.
        Integer rounding means the sum may be slightly less than reward;
        the remainder stays in can_mint to sustain future rewards.
        """
        if reward == 0:
            return []
        total = sum(self._totals.values())
        if total == 0:
            return []
        return [
            (addr, reward * amount // total)
            for addr, amount in self._totals.items()
            if reward * amount // total >= 1
        ]
