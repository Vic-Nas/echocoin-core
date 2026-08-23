"""ChainState: the four values that always move together.

chain, state, burn_window, and cumulative_score are always consistent
with each other. ChainState groups them so the node can swap them as a
unit and methods that read chain state get one object instead of four.

ChainState is immutable after construction; mutations return a new one.
The node holds one reference and replaces it atomically (GIL-safe).
"""

import pob as pob_mod
import block as block_mod
import state as state_mod
import tx as tx_mod


def _block_burns(blk):
    """Sum of all burn outputs in a block's transactions."""
    total = 0
    for t in blk.get("transactions", []):
        for out in t.get("outputs", []):
            if out.get("to") == pob_mod.BURN_ADDRESS:
                total += out["amount"]
    return total


class ChainState:
    """Consistent snapshot of chain + ledger state + burn window + PoB score."""

    __slots__ = ("chain", "state", "burn_window", "cumulative_score")

    def __init__(self, chain, state, burn_window, cumulative_score):
        self.chain            = chain           # list of block dicts
        self.state            = state           # State (balance ledger)
        self.burn_window      = burn_window     # BurnWindow (rolling burns)
        self.cumulative_score = cumulative_score  # total burns in chain (rings)

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
        """Build BurnWindow and cumulative burn total by replaying chain headers.
        Shared by from_chain and from_storage.
        """
        window = pob_mod.BurnWindow()
        score  = 0
        for blk in chain:
            window.add_block(blk)
            if blk["height"] > 0:
                score += _block_burns(blk)
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
            score += _block_burns(blk)
            builder = blk.get("builder")
            if builder:
                state.apply_reward_distribution(
                    window.reward_distribution(builder, state.compute_block_reward())
                )
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
        new_score  = self.cumulative_score + _block_burns(blk)
        builder    = blk.get("builder")
        if builder:
            post_tx_state.apply_reward_distribution(
                new_window.reward_distribution(builder, post_tx_state.compute_block_reward())
            )
        return ChainState(self.chain + [blk], post_tx_state, new_window, new_score)

    # ------------------------------------------------------------------
    # Fork choice
    # ------------------------------------------------------------------

    def is_better_than(self, other, fork_point=None):
        """Return True if self should replace other.

        Fork choice: the chain with the higher total eligible burns in its
        suffix wins (burn-sum). Burns are capped to each contributor's
        pre-fork balance, so privately minted rewards on a divergent chain
        cannot inflate the denominator.

        Tiebreaks in order:
          1. Longer chain (more blocks = more opportunity to burn)
          2. Lower tip hash (deterministic)

        fork_point: index of first differing block. If None, defaults to
        min(self.height+1, other.height+1), which is correct when one chain
        simply extends the other.
        """
        if fork_point is None:
            fork_point = min(self.height + 1, other.height + 1)

        self_suffix_len  = max((self.height  + 1) - fork_point, 0)
        other_suffix_len = max((other.height + 1) - fork_point, 0)

        if self_suffix_len <= 0 and other_suffix_len <= 0:
            if self.cumulative_score != other.cumulative_score:
                return self.cumulative_score > other.cumulative_score
            if self.height != other.height:
                return self.height > other.height
            return self.tip["hash"] < other.tip["hash"]
        if self_suffix_len <= 0:
            return False
        if other_suffix_len <= 0:
            return True

        fork_balances = _balances_at(self.chain, fork_point)
        self_burn     = _capped_suffix_score(self.chain,  fork_point, fork_balances)
        other_burn    = _capped_suffix_score(other.chain, fork_point, fork_balances)

        if self_burn != other_burn:
            return self_burn > other_burn          # higher burn-sum wins
        if self_suffix_len != other_suffix_len:
            return self_suffix_len > other_suffix_len  # longer chain tiebreak
        return self.tip["hash"] < other.tip["hash"]


# ---------------------------------------------------------------------------
# Module-level helpers for fork evaluation
# ---------------------------------------------------------------------------

def _balances_at(chain, fork_point):
    """Replay chain[:fork_point] and return per-address balance snapshot."""
    s  = state_mod.State()
    bw = pob_mod.BurnWindow()
    for i in range(min(fork_point, len(chain))):
        blk = chain[i]
        bw.add_block(blk)
        if i == 0:
            continue
        for t in blk.get("transactions", []):
            s.apply_tx(t)
        builder = blk.get("builder")
        if builder:
            s.apply_reward_distribution(
                bw.reward_distribution(builder, s.compute_block_reward())
            )
    return s.all_balances()


def _capped_suffix_score(chain, fork_point, fork_balances):
    """Total eligible burns in chain[fork_point:] with pre-fork balance cap.

    Each contributor's suffix burns are capped to their balance at fork_point.
    Burns beyond the cap came from privately minted rewards and do not count.

    Returns total eligible burn rings (higher = better chain).
    A chain with no suffix blocks returns 0.
    """
    # Accumulate suffix burns per (beneficiary, contributor)
    suffix_contrib: "dict[tuple, int]" = {}

    for i in range(fork_point, len(chain)):
        blk = chain[i]
        for tx in blk.get("transactions", []):
            sender = tx.get("from", "")
            for out in tx.get("outputs", []):
                if out.get("to") != pob_mod.BURN_ADDRESS:
                    continue
                beneficiary = out.get("beneficiary") or sender
                key = (beneficiary, sender)
                suffix_contrib[key] = suffix_contrib.get(key, 0) + out["amount"]

    total = 0
    for (_, contributor), suffix_amt in suffix_contrib.items():
        cap = fork_balances.get(contributor, 0)
        total += min(suffix_amt, cap)
    return total
