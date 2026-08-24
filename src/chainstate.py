"""ChainState: the three values that always move together.

chain, state, and burn_window are always consistent with each other.
ChainState groups them so the node can swap them as a unit and methods
that read chain state get one object instead of three.

ChainState is immutable after construction; mutations return a new one.
The node holds one reference and replaces it atomically (GIL-safe).
"""

import pob as pob_mod
import block as block_mod
import state as state_mod
import tx as tx_mod


class ChainState:
    """Consistent snapshot of chain + ledger state + burn window."""

    __slots__ = ("chain", "state", "burn_window")

    def __init__(self, chain, state, burn_window):
        self.chain       = chain       # list of block dicts
        self.state       = state       # State (balance ledger)
        self.burn_window = burn_window  # BurnWindow (rolling burns)

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
    def _build_window(cls, chain):
        """Build BurnWindow by replaying chain headers.
        Shared by from_storage.
        """
        window = pob_mod.BurnWindow()
        for blk in chain:
            window.add_block(blk)
        return window

    @classmethod
    def from_genesis(cls):
        """Bootstrap a ChainState from the genesis block only."""
        genesis = block_mod.create_genesis()
        window  = pob_mod.BurnWindow()
        window.add_block(genesis)
        return cls([genesis], state_mod.State(), window)

    @classmethod
    def from_chain(cls, chain):
        """Build a ChainState by replaying a fully trusted chain.
        Used at startup and after sync/reorg.
        """
        state  = state_mod.State()
        window = pob_mod.BurnWindow()
        for blk in chain:
            window.add_block(blk)
            h = blk["height"]
            if h == 0:
                continue
            for t in blk["transactions"]:
                state.apply_tx(t)
            builder = blk.get("builder")
            if builder:
                total_fees = sum(t.get("fee", 0) for t in blk.get("transactions", []))
                if total_fees > 0:
                    state.credit(builder, total_fees)
                state.apply_reward_distribution(
                    window.reward_distribution(state.compute_block_reward())
                )
        return cls(list(chain), state, window)

    @classmethod
    def from_storage(cls, chain, stored_state):
        """Build a ChainState from a chain and a pre-loaded State snapshot.
        Avoids replaying txs (balances come from the snapshot). Builds the
        burn window from the chain since it isn't persisted.
        """
        window = cls._build_window(chain)
        return cls(list(chain), stored_state, window)

    # ------------------------------------------------------------------
    # Produce a new ChainState by appending one block
    # ------------------------------------------------------------------

    def validate_and_apply(self, blk):
        """Validate blk against self, then return (ok, err, new_cs).

        Passes the post-validation probe state directly to _apply_block_state
        so transactions are applied only once (validate() already applied them
        to probe). Failure leaves self unchanged.
        """
        probe = self.state.snapshot()
        ok, err = block_mod.validate(blk, probe, self.chain, self.fee_rate_at)
        if not ok:
            return False, err, self
        # probe is now the post-tx state; hand it directly to avoid re-applying.
        return True, None, self._apply_block_with_state(blk, probe)

    def apply_block(self, blk):
        """Return a new ChainState with blk appended. Does not mutate self.
        Used by from_chain replay where no pre-validated probe is available.
        """
        post_tx = self.state.snapshot()
        for t in blk.get("transactions", []):
            post_tx.apply_tx(t)
        return self._apply_block_with_state(blk, post_tx)

    def _apply_block_with_state(self, blk, post_tx_state):
        """Finish applying blk given a state that already has txs applied.

        Shared by apply_block (which builds post_tx via replay) and
        validate_and_apply (which gets post_tx from the validation probe,
        avoiding a second application of all transactions).
        """
        new_window = self.burn_window.copy()
        new_window.add_block(blk)
        builder = blk.get("builder")
        if builder:
            total_fees = sum(t.get("fee", 0) for t in blk.get("transactions", []))
            if total_fees > 0:
                post_tx_state.credit(builder, total_fees)
            post_tx_state.apply_reward_distribution(
                new_window.reward_distribution(post_tx_state.compute_block_reward())
            )
        return ChainState(self.chain + [blk], post_tx_state, new_window)

    # ------------------------------------------------------------------
    # Fork choice: longest chain wins, tip hash breaks ties
    # ------------------------------------------------------------------

    def is_better_than(self, other):
        """Return True if self should replace other.

        Fork choice: the longer chain wins. Tip hash breaks any remaining
        tie deterministically.
        """
        if self.height != other.height:
            return self.height > other.height
        return self.tip["hash"] < other.tip["hash"]
