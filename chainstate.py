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
        new_score  = self.cumulative_score
        builder    = blk.get("builder")
        if builder:
            post_tx_state.apply_reward_distribution(
                new_window.reward_distribution(builder, post_tx_state.compute_block_reward())
            )
            new_score += new_window.score(pob_mod._tip_hash_int(self.chain), builder)
        return ChainState(self.chain + [blk], post_tx_state, new_window, new_score)

    # ------------------------------------------------------------------
    # Fork choice
    # ------------------------------------------------------------------

    def is_better_than(self, other, fork_point=None):
        """Return True if self should replace other.

        Compares average PoB score per block in the suffix after fork_point.
        The shared prefix is identical on both chains so only the suffix
        matters. The chain with lower average suffix score made better
        choices after the fork — regardless of how many blocks each built.

        fork_point: index of first differing block. If None, defaults to
        min(self.height, other.height) — the length of the shorter chain,
        which is correct when one chain simply extends the other.

        This prevents height-racing and history rewrite attacks:
        - Racing: building more blocks alone doesn't help if average score
          is worse than the competing suffix
        - History rewrite: attacker built alone after fork with no competition,
          so their per-block score reflects a single node's luck, not the
          minimum achievable across competing nodes

        Uses cross-multiplication to avoid float precision issues.
        Tiebreak on tip hash for determinism.
        """
        # Resolve fork_point from chains when not explicitly provided
        if fork_point is None:
            fork_point = min(self.height + 1, other.height + 1)

        # height is 0-based block index; fork_point is array index of first
        # differing block. Suffix length = number of blocks after fork_point.
        self_suffix_len  = (self.height  + 1) - fork_point
        other_suffix_len = (other.height + 1) - fork_point

        self_suffix_len  = max(self_suffix_len,  0)
        other_suffix_len = max(other_suffix_len, 0)

        if self_suffix_len <= 0 and other_suffix_len <= 0:
            # No blocks after fork point — compare cumulative scores directly
            if self.cumulative_score != other.cumulative_score:
                return self.cumulative_score < other.cumulative_score
            return self.tip["hash"] < other.tip["hash"]
        if self_suffix_len <= 0:
            return False
        if other_suffix_len <= 0:
            return True

        self_suffix_score  = self.cumulative_score  - self._prefix_score(fork_point)
        other_suffix_score = other.cumulative_score - other._prefix_score(fork_point)

        # Compare self_suffix_score/self_suffix_len < other_suffix_score/other_suffix_len
        # via cross-multiplication (exact, no floats)
        self_cross  = self_suffix_score  * other_suffix_len
        other_cross = other_suffix_score * self_suffix_len
        if self_cross != other_cross:
            return self_cross < other_cross
        return self.tip["hash"] < other.tip["hash"]

    def _prefix_score(self, fork_point):
        """Cumulative score of blocks 0..fork_point (shared prefix)."""
        # Both chains are identical up to fork_point so we can compute
        # from self.chain. Called only during sync comparison, not hot path.
        import pob as pob_mod
        score = 0
        bw = pob_mod.BurnWindow()
        for i in range(min(fork_point, len(self.chain))):
            blk = self.chain[i]
            bw.add_block(blk)
            if i > 0:
                builder = blk.get("builder")
                if builder:
                    phi = pob_mod._tip_hash_int([self.chain[i - 1]])
                    score += bw.score(phi, builder)
        return score
