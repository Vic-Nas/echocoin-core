"""ChainState: the three values that always move together.

chain, state, and queue are always consistent with each other. ChainState
groups them so the node can swap them as a unit and methods that read
chain state get one object instead of three.

ChainState is immutable after construction; mutations return a new one.
The node holds one reference and replaces it atomically (GIL-safe).
"""

import block as block_mod
import state as state_mod
import tx as tx_mod


def _apply_builder_reward(state, builder, blk):
    """Credit the full newly-minted block reward to the builder.

    Unlike the old plaintext format, confirmation fees do NOT go to the
    builder: they are escrowed at confirmation time (State.apply_confirmation)
    and paid out to whichever resolver's solution lands first
    (State.apply_resolution) -- see tx.py's module docstring. The builder
    still profits unconditionally from the full block reward regardless of
    mempool contents; no Proof-of-Burn split, no per-tx fee cut either.
    """
    reward = state.compute_block_reward()
    if reward >= 1:
        state.apply_reward_distribution([(builder, reward)])


class TxQueue:
    """Canonical global queue position for confirmed ciphertexts.

    A confirmed "confirm" tx gets a canonical position the instant it is
    on-chain (its position in this queue, built in height+index order --
    the same style as the existing tx ordering rule). Resolutions mark
    entries resolved. Answers "what's the current front of the queue"
    from chain state alone, which is what block.py's gapless
    front-of-queue validity check needs.
    """

    __slots__ = ("order", "resolved", "confirmations")

    def __init__(self):
        self.order = []          # confirmed_tx_hash, in canonical chain order
        self.resolved = set()    # confirmed_tx_hash already resolved
        self.confirmations = {}  # confirmed_tx_hash -> its "confirm" tx dict

    def copy(self):
        q = TxQueue()
        q.order = list(self.order)
        q.resolved = set(self.resolved)
        q.confirmations = dict(self.confirmations)
        return q

    def add_block(self, blk):
        for t in blk.get("transactions", []):
            kind = t.get("kind")
            if kind == "confirm":
                h = tx_mod.tx_hash(t)
                self.order.append(h)
                self.confirmations[h] = t
            elif kind == "resolve":
                self.resolved.add(t.get("confirmed_tx_hash"))

    def remaining(self):
        """Confirmed hashes not yet resolved, in canonical queue order."""
        return [h for h in self.order if h not in self.resolved]

    def front(self):
        r = self.remaining()
        return r[0] if r else None

    def lookup(self, confirmed_tx_hash):
        return self.confirmations.get(confirmed_tx_hash)


def _apply_txs_trusted(state, blk, queue):
    """Apply blk's txs to state assuming blk is already trusted (replay
    path: from_chain / apply_block -- no re-validation). Also advances
    queue to reflect this block's confirm/resolve entries.

    A resolution's inner payload may have been semantically inapplicable
    when the block was first validated (see tx.validate_resolution's
    docstring -- a resolution is still validly includable even then, it
    just doesn't move funds). Replay must recompute payload_is_valid the
    same way rather than always applying the transfer, or it would
    silently diverge from the state the block was actually validated
    against.
    """
    for t in blk.get("transactions", []):
        kind = t.get("kind")
        if kind == "confirm":
            state.apply_confirmation(t, tx_mod.tx_hash(t))
        elif kind == "resolve":
            payload_ok, _ = tx_mod.payload_is_valid(t["payload"], state)
            state.apply_resolution(t, payload_valid=payload_ok)
    queue.add_block(blk)


class ChainState:
    """Consistent snapshot of chain + ledger state + ciphertext queue."""

    __slots__ = ("chain", "state", "queue", "cumulative_iterations")

    def __init__(self, chain, state, queue=None, cumulative_iterations=0):
        self.chain = chain       # list of block dicts
        self.state = state       # State (balance ledger)
        self.queue = queue if queue is not None else TxQueue()
        # Sum of vdf_iterations actually proven across the chain (excludes
        # genesis, which has no VDF proof). Used for fork choice instead of
        # raw block count -- see is_better_than().
        self.cumulative_iterations = cumulative_iterations

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
    def _cumulative_iterations(cls, chain):
        """Sum of vdf_iterations actually proven, excluding genesis."""
        return sum(blk.get("vdf_iterations", 0) for blk in chain if blk["height"] > 0)

    @classmethod
    def from_genesis(cls):
        """Bootstrap a ChainState from the genesis block only."""
        genesis = block_mod.create_genesis()
        return cls([genesis], state_mod.State(), TxQueue())

    @classmethod
    def from_chain(cls, chain):
        """Build a ChainState by replaying a fully trusted chain.
        Used at startup and after sync/reorg.
        """
        state = state_mod.State()
        queue = TxQueue()
        for blk in chain:
            h = blk["height"]
            if h == 0:
                continue
            _apply_txs_trusted(state, blk, queue)
            builder = blk.get("builder")
            if builder:
                _apply_builder_reward(state, builder, blk)
        return cls(list(chain), state, queue, cls._cumulative_iterations(chain))

    @classmethod
    def from_storage(cls, chain, stored_state):
        """Build a ChainState from a chain and a pre-loaded State snapshot.
        Avoids replaying txs (balances come from the snapshot), but still
        replays the chain's confirm/resolve entries to rebuild the queue,
        which is not persisted separately.
        """
        queue = TxQueue()
        for blk in chain:
            queue.add_block(blk)
        return cls(list(chain), stored_state, queue, cls._cumulative_iterations(chain))

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
        ok, err = block_mod.validate(blk, probe, self.chain, self.fee_rate_at, self.queue)
        if not ok:
            return False, err, self
        # probe is now the post-tx state; hand it directly to avoid re-applying.
        return True, None, self._apply_block_with_state(blk, probe)

    def apply_block(self, blk):
        """Return a new ChainState with blk appended. Does not mutate self.
        Used by from_chain replay where no pre-validated probe is available.
        """
        post_tx = self.state.snapshot()
        new_queue = self.queue.copy()
        _apply_txs_trusted(post_tx, blk, new_queue)
        return self._apply_block_with_state(blk, post_tx, new_queue)

    def _apply_block_with_state(self, blk, post_tx_state, new_queue=None):
        """Finish applying blk given a state that already has txs applied.

        Shared by apply_block (which builds post_tx via replay) and
        validate_and_apply (which gets post_tx from the validation probe,
        avoiding a second application of all transactions).

        new_queue: pre-advanced queue (apply_block already advanced it via
        _apply_txs_trusted). When omitted (validate_and_apply's path,
        where txs were applied by block.validate itself rather than
        _apply_txs_trusted), this method advances a copy of self.queue.
        """
        if new_queue is None:
            new_queue = self.queue.copy()
            new_queue.add_block(blk)
        builder = blk.get("builder")
        if builder:
            _apply_builder_reward(post_tx_state, builder, blk)
        new_iterations = self.cumulative_iterations + blk.get("vdf_iterations", 0)
        return ChainState(self.chain + [blk], post_tx_state, new_queue, new_iterations)

    # ------------------------------------------------------------------
    # Fork choice: most cumulative proven VDF work wins, tip hash breaks ties
    # ------------------------------------------------------------------

    def is_better_than(self, other):
        """Return True if self should replace other.

        Fork choice: the chain with more cumulative proven VDF iterations
        wins -- not raw block count. A block's vdf_iterations is only
        accepted if its VDF proof actually verifies for that many
        iterations, so this sum can't be inflated by claiming more work
        than was cryptographically proven. Raw height is not used: a
        fork's own adjustment history is derived only from its own block
        timestamps, so an attacker who pads their own timestamps could
        otherwise keep their fork's required iteration count artificially
        low and out-build the honest chain in less real time than it took.
        Tip hash breaks any remaining tie deterministically.
        """
        if self.cumulative_iterations != other.cumulative_iterations:
            return self.cumulative_iterations > other.cumulative_iterations
        return self.tip["hash"] < other.tip["hash"]
