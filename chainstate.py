"""ChainState: the four values that always move together.

chain, state, burn_window, and cumulative_score are always consistent
with each other. ChainState groups them so the node can swap them as a
unit and methods that read chain state get one object instead of four.

ChainState is immutable after construction -- mutations return a new one.
The node holds one reference and replaces it atomically (GIL-safe).
"""

import pob as pob_mod
import block as block_mod
import state as state_mod
import tx as tx_mod


class ChainState:
    """Consistent snapshot of chain + ledger state + burn window + PoB score."""

    __slots__ = ("chain", "state", "burn_window", "cumulative_score")

    def __init__(self, chain, state, burn_window, cumulative_score):
        self.chain            = chain           # list of block dicts
        self.state            = state           # State (balance ledger)
        self.burn_window      = burn_window     # BurnWindow (rolling burns)
        self.cumulative_score = cumulative_score

    # ------------------------------------------------------------------
    # Convenient accessors
    # ------------------------------------------------------------------

    @property
    def tip(self):
        return self.chain[-1]

    @property
    def height(self):
        return self.chain[-1]["height"]

    @property
    def genesis_hash(self):
        return self.chain[0]["hash"]

    def fee_rate_at(self, height):
        if 0 <= height < len(self.chain):
            return self.chain[height]["fee_rate"]
        return None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def _build_window_and_score(cls, chain):
        """Build BurnWindow and cumulative score by replaying chain headers.
        Shared by from_chain and from_storage — neither applies tx state here.
        """
        window = pob_mod.BurnWindow()
        score  = 0
        for blk in chain:
            window.add_block(blk)
            h = blk["height"]
            builder = blk.get("builder")
            if builder and h > 0:
                parent_hash_int = pob_mod._tip_hash_int([chain[h - 1]])
                score += window.score(parent_hash_int, builder)
        return window, score

    @classmethod
    def from_genesis(cls):
        """Bootstrap a ChainState from the genesis block only."""
        genesis = block_mod.create_genesis()
        window  = pob_mod.BurnWindow()
        window.add_block(genesis)
        return cls([genesis], state_mod.State(), window, 0)

    @classmethod
    def from_chain(cls, chain):
        """Build a ChainState by replaying a fully trusted chain.
        Used at startup and after sync/reorg.
        """
        state  = state_mod.State()
        window = pob_mod.BurnWindow()
        score  = 0
        for blk in chain:
            window.add_block(blk)
            h = blk["height"]
            if h == 0:
                continue
            for t in blk["transactions"]:
                state.apply_tx(t)
            builder = blk.get("builder")
            if builder:
                state.apply_reward_distribution(
                    window.reward_distribution(builder, state.compute_block_reward())
                )
                parent_hash_int = pob_mod._tip_hash_int([chain[h - 1]])
                score += window.score(parent_hash_int, builder)
        return cls(list(chain), state, window, score)

    @classmethod
    def from_storage(cls, chain, stored_state):
        """Build a ChainState from a chain and a pre-loaded State snapshot.
        Avoids replaying txs (balances come from the snapshot). Builds the
        burn window and cumulative score from the chain since those aren't
        persisted.
        """
        window, score = cls._build_window_and_score(chain)
        return cls(list(chain), stored_state, window, score)

    # ------------------------------------------------------------------
    # Produce a new ChainState by appending one block
    # ------------------------------------------------------------------

    def validate_and_apply(self, blk):
        """Validate blk against self, then return (ok, err, new_cs).

        Uses a snapshot so failure leaves self unchanged. On success,
        new_cs is the result of apply_block(blk). Uses self.fee_rate_at
        directly — callers no longer need to pass it.
        """
        probe = self.state.snapshot()
        ok, err = block_mod.validate(blk, probe, self.chain, self.fee_rate_at)
        if not ok:
            return False, err, self
        return True, None, self.apply_block(blk)

    def apply_block(self, blk):
        """Return a new ChainState with blk appended. Does not mutate self."""
        new_state  = self.state.snapshot()
        new_window = self.burn_window.copy()   # independent copy -- self is not mutated
        new_window.add_block(blk)

        for t in blk.get("transactions", []):
            new_state.apply_tx(t)

        new_score = self.cumulative_score
        builder   = blk.get("builder")
        if builder:
            new_state.apply_reward_distribution(
                new_window.reward_distribution(builder, new_state.compute_block_reward())
            )
            new_score += new_window.score(pob_mod._tip_hash_int(self.chain), builder)

        return ChainState(self.chain + [blk], new_state, new_window, new_score)

    # ------------------------------------------------------------------
    # Fork choice
    # ------------------------------------------------------------------

    def is_better_than(self, other):
        """Return True if self should replace other (longer or heavier chain)."""
        if self.height != other.height:
            return self.height > other.height
        if self.cumulative_score != other.cumulative_score:
            return self.cumulative_score < other.cumulative_score
        return self.tip["hash"] < other.tip["hash"]
