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
        Shared by from_chain and from_storage; neither applies tx state here.
        """
        window = pob_mod.BurnWindow()
        score  = float("inf")
        for blk in chain:
            window.add_block(blk)
            h = blk["height"]
            builder = blk.get("builder")
            if builder and h > 0:
                parent_hash_int = pob_mod._tip_hash_int([chain[h - 1]])
                score = min(score, window.score(parent_hash_int, builder))
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
        score  = float("inf")
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
                score = min(score, window.score(parent_hash_int, builder))
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
            new_score = min(new_score, new_window.score(pob_mod._tip_hash_int(self.chain), builder))
        return ChainState(self.chain + [blk], post_tx_state, new_window, new_score)

    # ------------------------------------------------------------------
    # Fork choice
    # ------------------------------------------------------------------

    def is_better_than(self, other, fork_point=None):
        """Return True if self should replace other.

        Compares cumulative PoB score in the suffix after fork_point.
        The chain with the lower cumulative suffix score wins.

        Burns used for scoring are capped to each contributor's balance
        at the fork point, so privately minted coins on a divergent chain
        cannot inflate the attacker's denominator.

        fork_point: index of first differing block. If None, defaults to
        min(self.height+1, other.height+1), which is correct when one chain
        simply extends the other.

        Tiebreak on tip hash for determinism.
        """
        if fork_point is None:
            fork_point = min(self.height + 1, other.height + 1)

        self_suffix_len  = max((self.height  + 1) - fork_point, 0)
        other_suffix_len = max((other.height + 1) - fork_point, 0)

        if self_suffix_len <= 0 and other_suffix_len <= 0:
            if self.cumulative_score != other.cumulative_score:
                return self.cumulative_score < other.cumulative_score
            return self.tip["hash"] < other.tip["hash"]
        if self_suffix_len <= 0:
            return False
        if other_suffix_len <= 0:
            return True

        fork_balances    = _balances_at(self.chain, fork_point)
        self_score       = _capped_suffix_score(self.chain,  fork_point, fork_balances)
        other_score      = _capped_suffix_score(other.chain, fork_point, fork_balances)

        if self_score != other_score:
            return self_score < other_score
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
    """Min PoB score across chain[fork_point:] with pre-fork balance cap.

    Each contributor's eligible burns in the suffix are capped to their
    balance at fork_point. Burns beyond that cap came from privately minted
    rewards on the divergent chain and do not improve the scoring denominator.

    Returns the minimum per-block score in the suffix (lower = better).
    A chain with no blocks in the suffix returns float('inf').
    """
    bw = pob_mod.BurnWindow()
    for blk in chain[:fork_point]:
        bw.add_block(blk)

    # Track suffix burns per (beneficiary, contributor) pair
    suffix_contrib: "dict[tuple, int]" = {}

    score = float("inf")
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

        bw.add_block(blk)

        builder = blk.get("builder")
        if builder and i > 0:
            parent_hash_int = pob_mod._tip_hash_int([chain[i - 1]])

            # Rebuild eligible burn with per-contributor suffix cap
            eligible = 0
            window_contribs = bw.burns_for(builder)  # {contributor: window_total}
            for contributor, window_amt in window_contribs.items():
                suffix_amt = suffix_contrib.get((builder, contributor), 0)
                pre_fork_amt = window_amt - suffix_amt
                cap = fork_balances.get(contributor, 0)
                eligible += pre_fork_amt + min(suffix_amt, cap)

            seed = parent_hash_int ^ pob_mod._addr_int(builder)
            score = min(score, seed // max(1, eligible))

    return score
