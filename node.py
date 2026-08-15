"""Block cycle orchestrator.

One cycle:
  1. drain queue          -- inbound txs and blocks from net_in_q
  2. sync (every 3)      -- pull a better chain from a random peer
  3. vdf.evaluate()      -- blocks ~120s
  4. assemble + broadcast
  5. drain queue (5s)    -- collect peer blocks
  6. pick winner         -- lowest hash among valid blocks
  7. commit              -- swap ChainState, persist, publish view

Flask threads read node.view (a NodeView snapshot). The node loop is the
sole writer; every mutation publishes a new snapshot atomically.
"""

import logging
import queue
import secrets as _secrets
import threading
import time

import block as block_mod
import crypto
import mempool as mempool_mod
import pob as pob_mod
import tx as tx_mod
import vdf as vdf_mod
from chainstate import ChainState
from params import BLOCK_SIZE_LIMIT, DB_PATH
from storage import Storage

log = logging.getLogger("ec.node")
_rng = _secrets.SystemRandom()

SYNC_EVERY_N_CYCLES = 3


# ---------------------------------------------------------------------------
# Tail validation (pure -- no node state touched)
# ---------------------------------------------------------------------------

def _validate_tail(tail, prefix, fee_rate_at):
    """Validate new blocks against a trusted prefix. Pure: nothing is mutated.

    Builds a throw-away ChainState from the prefix, then checks each block
    in tail. Returns (True, None) or (False, error_string).
    """
    cs = ChainState.from_chain(prefix) if prefix else ChainState.from_genesis()
    validated = list(prefix)
    for blk in tail:
        probe = cs.state.snapshot()
        ok, err = block_mod.validate(blk, probe, validated, fee_rate_at)
        if not ok:
            return False, f"invalid block at {blk['height']}: {err}"
        # Apply to throw-away state so subsequent blocks see correct balances.
        builder = blk.get("builder")
        window  = cs.burn_window.copy()
        window.add_block(blk)
        if builder:
            probe.apply_reward_distribution(
                window.reward_distribution(builder, probe.compute_block_reward())
            )
        cs = ChainState(validated + [blk], probe, window, cs.cumulative_score)
        validated.append(blk)
    return True, None


# ---------------------------------------------------------------------------
# NodeView: read-only snapshot for Flask threads
# ---------------------------------------------------------------------------

class NodeView:
    """Immutable snapshot of node state. Published after every block commit.
    Flask reads node.view -- one reference swap, GIL-atomic, no lock needed.
    """
    __slots__ = ("chain", "height", "tip", "genesis_hash", "cumulative_score",
                 "state", "stats_points", "_cum_burned")

    def __init__(self, cs, prev_view=None):
        self.chain            = cs.chain
        self.tip              = cs.tip
        self.height           = cs.height
        self.genesis_hash     = cs.genesis_hash
        self.cumulative_score = cs.cumulative_score
        self.state            = cs.state.snapshot()
        prev_points = getattr(prev_view, "stats_points", None)
        prev_cum    = getattr(prev_view, "_cum_burned",  0)
        self.stats_points, self._cum_burned = self._build_stats(
            cs.chain, self.state, prev_points, prev_cum
        )

    @staticmethod
    def _build_stats(chain, state, prev_points=None, prev_cum=0):
        """Chart data for /api/stats. Incremental when chain grew by one block.

        Tracks cumulative minted/burned by replaying per-block totals from
        the chain rather than linearly interpolating, so the chart accurately
        reflects exponential emission decay.
        """
        if len(chain) <= 1:
            return [], 0

        # Incremental path: append one point for the new tip.
        if prev_points is not None and len(prev_points) < 500 \
                and len(chain) == len(prev_points) + 2:
            blk    = chain[-1]
            fee    = sum(t["fee"] for t in blk.get("transactions", []))
            cum    = prev_cum + fee
            minted = state.total_minted
            return prev_points + [{"height": blk["height"], "minted": minted,
                                    "burned_fees": cum, "circulating": minted - cum,
                                    "net_emission": fee}], cum

        # Full rebuild: walk the chain, accumulate actual fee burns per block.
        # total_minted is only available as a final figure from state, so we
        # still use it as the running minted value at the tip. For chart
        # purposes we distribute it proportionally by block reward schedule
        # using the exponential decay factor.
        from params import EMISSION_RATE, SUPPLY_CAP
        points, cum = [], 0
        running_minted = 0
        running_burnt  = 0
        for blk in chain[1:]:
            fee     = sum(t["fee"] for t in blk.get("transactions", []))
            cum    += fee
            # Approximate per-block reward using the emission formula.
            can_mint = SUPPLY_CAP - running_minted + running_burnt
            reward   = int(max(0, can_mint) * (1 - EMISSION_RATE)) if can_mint > 0 else 0
            running_minted += reward
            running_burnt  += fee
            points.append({"height": blk["height"], "minted": running_minted,
                            "burned_fees": cum, "circulating": running_minted - cum,
                            "net_emission": fee})
        if len(points) > 500:
            step   = len(points) / 500
            points = [points[int(i * step)] for i in range(500)]
        return points, cum


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class Node:

    def __init__(self, keyfile, public_key, gossip, syncer, pool, net_in_q,
                 db_path=None):
        self.keyfile      = keyfile
        self.pk           = public_key
        self.pk_hex       = public_key.hex()
        self.addr         = crypto.public_key_to_address(public_key)
        self.gossip       = gossip
        self.syncer       = syncer
        self.pool         = pool
        self.net_in_q     = net_in_q
        self.mempool      = mempool_mod.Mempool()
        self.storage      = Storage(db_path or DB_PATH)
        self.running      = False
        self._kek         = None
        self._loop_thread = None
        self._cycle_count = 0

        # tx_hash -> consecutive non-full blocks that excluded it.
        self._exclusion_age = {}

        self.cs   = self._load_cs()
        self.view = NodeView(self.cs)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _load_cs(self):
        """Load or create ChainState from storage."""
        stored = self.storage.load_all_blocks()
        if not stored:
            cs = ChainState.from_genesis()
            self.storage.save_block(cs.chain[0])
            log.info("[startup] genesis created")
            return cs

        # Backfill tx_bytes for blocks from older databases.
        for blk in stored:
            if "tx_bytes" not in blk:
                blk["tx_bytes"] = sum(tx_mod.tx_size(t)
                                       for t in blk.get("transactions", []))

        if self.storage.state_exists():
            balances, nonces, total_minted, total_burnt = self.storage.load_state()
            import state as state_mod
            s = state_mod.State()
            s._balances    = balances
            s._nonces      = nonces
            s.total_minted = total_minted
            s.total_burnt  = total_burnt
            cs = ChainState.from_storage(stored, s)
        else:
            cs = ChainState.from_chain(stored)

        log.info("[startup] chain loaded  height=%d  tip=%s",
                 cs.height, cs.tip["hash"][:12])
        return cs

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_signing_active(self):
        return self._kek is not None

    def mark_tx_seen(self, tx_hash):
        return self.gossip.mark_seen(tx_hash)

    def get_info(self):
        v = self.view
        return {
            "height":       v.height,
            "tip_hash":     v.tip["hash"],
            "genesis_hash": v.genesis_hash,
            "fee_rate":     v.tip["fee_rate"],
            "mempool_size": self.mempool.size(),
            "address":      self.addr,
            "peer_count":   self.pool.count(),
            "total_minted": v.state.total_minted,
            "total_burnt":  v.state.total_burnt,
            "can_mint":     v.state.compute_block_reward(),
        }

    def start(self, kek):
        self._kek         = kek
        self.running      = True
        self._loop_thread = threading.current_thread()
        log.info("[startup] node ready  addr=%s", self.addr)
        try:
            while self.running:
                try:
                    self._run_cycle()
                except Exception:
                    log.exception("[cycle] unhandled error, sleeping 1s")
                    time.sleep(1)
        finally:
            self._kek = None

    def stop(self):
        self.running = False

    def submit_tx(self, tx_dict):
        """Validate and add a tx. Node loop thread only."""
        assert threading.current_thread() is self._loop_thread
        ok, err = tx_mod.validate(tx_dict, self.cs.state,
                                   self.cs.height, self.cs.fee_rate_at)
        if not ok:
            log.debug("[tx] rejected  reason=%s  from=%s",
                      err, tx_dict.get("from", "?")[:24])
            return False, err
        ok, h = self.mempool.add(tx_dict)
        if not ok:
            return False, h
        self.gossip.relay_tx(tx_dict)
        self.view = NodeView(self.cs, prev_view=self.view)
        log.info("[tx] accepted  hash=%s  from=%s", h[:12],
                 tx_dict.get("from", "?")[:24])
        return True, h

    def submit_tx_from_api(self, tx_dict, timeout=5):
        """Thread-safe bridge: enqueue tx, block until the loop replies."""
        reply = queue.Queue(maxsize=1)
        self.net_in_q.put({"type": "submit_tx", "tx": tx_dict, "reply": reply})
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return False, "node busy (timeout)"

    def build_and_sign_tx(self, to_outputs, passphrase=None):
        if self._kek is not None:
            kek, own_kek = self._kek, False
        elif passphrase:
            kek, own_kek = crypto.derive_kek(self.keyfile, passphrase), True
        else:
            raise RuntimeError("node not running and no passphrase provided")
        v          = self.view
        nonce      = v.state.get_nonce(self.addr) + 1
        fee_height = v.height
        fee        = tx_mod.compute_fee(self.addr, self.pk_hex, to_outputs,
                                         nonce, fee_height, v.tip["fee_rate"])
        sk = crypto.decrypt_secret_key(self.keyfile, kek=kek)
        t  = tx_mod.create(self.addr, self.pk_hex, to_outputs,
                            nonce, fee_height, fee, sk)
        del sk
        if own_kek:
            del kek
        return t, fee

    # ------------------------------------------------------------------
    # Block cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        self._cycle_count += 1
        self._drain_queue()

        if self._cycle_count % SYNC_EVERY_N_CYCLES == 0:
            self.syncer.check_and_sync(
                self.cs.chain,
                lambda chain: self.apply_better_chain(chain)[0],
            )

        cs = self.cs   # local alias -- can change under sync
        log.info("[vdf] starting height=%d  tip=%s  peers=%d  mempool=%d",
                 cs.height + 1, cs.tip["hash"][:12],
                 self.pool.count(), self.mempool.size())

        pruned = self.mempool.prune_stale(cs.height, cs.state)
        if pruned:
            log.info("[vdf] mempool pruned  dropped=%d", len(pruned))

        vdf_out, vdf_proof = vdf_mod.evaluate(bytes.fromhex(cs.tip["hash"]))
        log.info("[vdf] proof ready  height=%d", cs.height + 1)

        fee_rate   = block_mod.compute_expected_fee_rate(cs.chain)
        sorted_txs = tx_mod.sort_txs(self.mempool.all_txs())
        candidate  = block_mod.assemble(cs.tip, sorted_txs, self.addr, fee_rate)
        candidate["vdf_output"] = vdf_out
        candidate["vdf_proof"]  = vdf_proof
        candidate["hash"]       = block_mod.block_hash(candidate)
        self.gossip.broadcast_block(candidate)

        peer_blocks = self._drain_queue(timeout=5) + self._drain_queue()
        winner, relay = self._pick_winner(candidate, peer_blocks)
        if winner is None:
            return
        self._commit(winner, relay=relay)

    def _pick_winner(self, candidate, peer_blocks):
        """Return (best_block, relay). relay=True means it came from a peer.
        Returns (None, False) only if our own candidate is invalid -- shouldn't happen.
        """
        cs = self.cs
        tip = cs.tip

        valid_peers = []
        for blk in peer_blocks:
            if blk.get("height") != tip["height"] + 1:
                continue
            if blk.get("previous_hash") != tip["hash"]:
                continue
            probe = cs.state.snapshot()
            ok, err = block_mod.validate(blk, probe, cs.chain, cs.fee_rate_at)
            if not ok:
                log.debug("[vdf] peer block rejected: %s", err)
                continue
            if _rng.random() > self._censorship_score(blk):
                log.debug("[vdf] peer block failed censorship check")
                continue
            valid_peers.append(blk)

        probe = cs.state.snapshot()
        ok, err = block_mod.validate(candidate, probe, cs.chain, cs.fee_rate_at)
        if not ok:
            log.error("[vdf] own block invalid: %s", err)
            return None, False

        all_valid = valid_peers + [candidate]
        # Fork choice: lowest PoB score wins. Hash breaks a tie (score collision
        # is astronomically rare but must be deterministic).
        chain = cs.chain
        winner = min(all_valid, key=lambda b: (
            pob_mod.score(chain, b["builder"]), b["hash"]
        ))
        return winner, winner is not candidate

    def _commit(self, blk, relay=False):
        """Append a validated block: update ChainState, persist, publish view."""
        if "tx_bytes" not in blk:
            blk["tx_bytes"] = sum(tx_mod.tx_size(t)
                                   for t in blk.get("transactions", []))

        self._update_exclusion_ages(blk)
        self.cs = self.cs.apply_block(blk)
        self.storage.save_block(blk)
        self.storage.save_state(self.cs.state)
        self.mempool.remove_many([tx_mod.tx_hash(t) for t in blk["transactions"]])
        self.view = NodeView(self.cs, prev_view=self.view)

        if relay:
            try:
                self.gossip.broadcast_block(blk)
            except Exception:
                log.exception("[commit] relay broadcast failed height=%d", blk["height"])

        log.info("[commit] height=%d  hash=%s  tx=%d  builder=%s",
                 blk["height"], blk["hash"][:12], len(blk["transactions"]),
                 (blk.get("builder") or "")[:24])

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _drain_queue(self, timeout=0):
        """Drain net_in_q. Returns list of inbound block dicts."""
        assert threading.current_thread() is self._loop_thread
        blocks = []
        try:
            msg = self.net_in_q.get(block=timeout > 0, timeout=timeout or None)
            self._handle(msg, blocks)
        except queue.Empty:
            return blocks
        while True:
            try:
                self._handle(self.net_in_q.get_nowait(), blocks)
            except queue.Empty:
                break
        return blocks

    def _handle(self, msg, block_out):
        """Dispatch one queue message."""
        t = msg.get("type")
        if t == "block":
            block_out.append(msg["block"])
        elif t == "submit_tx":
            msg["reply"].put(self.submit_tx(msg["tx"]))
        elif t == "tx":
            self._handle_inbound_tx(msg)

    def _handle_inbound_tx(self, msg):
        tx_dict   = msg["tx"]
        remaining = msg.get("remaining_hops", 0)
        if msg.get("relay_type") == "tx_stem" and remaining > 0:
            self.gossip.dandelion_send(tx_dict, remaining)
            return
        ok, _ = tx_mod.validate(tx_dict, self.cs.state,
                                  self.cs.height, self.cs.fee_rate_at)
        if ok and self.mempool.add(tx_dict)[0]:
            self.gossip.relay_tx(tx_dict)

    # ------------------------------------------------------------------
    # Censorship resistance
    # ------------------------------------------------------------------

    def _censorship_score(self, blk):
        """Acceptance probability for blk. 1.0 if no long-excluded txs."""
        confirmed = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        score = 1.0
        for h in self.mempool.pending_hashes() - confirmed:
            age = self._exclusion_age.get(h, 0)
            if age > 0:
                score = min(score, 1.0 / age)
        return score

    def _update_exclusion_ages(self, blk):
        confirmed = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        pending   = self.mempool.pending_hashes()
        is_full   = blk.get("tx_bytes", 0) >= BLOCK_SIZE_LIMIT * 0.99
        if not is_full:
            for h in pending - confirmed:
                self._exclusion_age[h] = self._exclusion_age.get(h, 0) + 1
        self._exclusion_age = {h: v for h, v in self._exclusion_age.items()
                                if h in pending}

    # ------------------------------------------------------------------
    # Chain sync / reorg
    # ------------------------------------------------------------------

    def apply_better_chain(self, remote_chain):
        """Accept remote_chain if it is better than local. Used by syncer."""
        # Check genesis first -- before replaying anything -- so a bad peer
        # cannot burn CPU by sending a long chain with a wrong genesis block.
        genesis_hash = self.cs.genesis_hash
        if not remote_chain or remote_chain[0]["hash"] != genesis_hash:
            return False, "genesis mismatch"

        remote_cs = ChainState.from_chain(remote_chain)
        if not remote_cs.is_better_than(self.cs):
            return False, "remote chain not better"

        fork_point = next(
            (i for i, (a, b) in enumerate(zip(self.cs.chain, remote_chain))
             if a["hash"] != b["hash"]),
            min(len(self.cs.chain), len(remote_chain))
        )
        prefix = remote_chain[:fork_point]
        tail   = remote_chain[fork_point:]

        # Build fee rate lookup from the trusted local prefix only.
        # Using remote_cs.fee_rate_at would let an attacker supply arbitrary
        # fee rates for the shared prefix blocks.
        trusted_prefix_cs = ChainState.from_chain(self.cs.chain[:fork_point]) \
            if fork_point > 0 else ChainState.from_genesis()

        ok, err = _validate_tail(tail, prefix, trusted_prefix_cs.fee_rate_at)
        if not ok:
            log.warning("[sync] rejected: %s", err)
            return False, err

        self._reorg_mempool(fork_point, remote_chain)
        self.storage.replace_chain(fork_point, tail)
        self.storage.save_state(remote_cs.state)
        self.cs   = remote_cs
        self.view = NodeView(self.cs)

        if fork_point < self.cs.height:
            log.warning("[reorg] height=%d  fork_point=%d", self.cs.height, fork_point)
        else:
            log.info("[sync] height=%d  fork_point=%d", self.cs.height, fork_point)
        return True, None

    def _reorg_mempool(self, fork_point, new_chain):
        old_txs = {tx_mod.tx_hash(t): t
                   for blk in self.cs.chain[fork_point:]
                   for t in blk.get("transactions", [])}
        new_confirmed = {tx_mod.tx_hash(t)
                         for blk in new_chain[fork_point:]
                         for t in blk.get("transactions", [])}
        self.mempool.remove_many(new_confirmed)
        for h, t in old_txs.items():
            if h not in new_confirmed:
                self.mempool.add(t)
