"""Block cycle orchestrator.

One cycle:
  1. drain queue          inbound txs and blocks from net_in_q
  2. sync (every 3)      pull a better chain from a random peer
  3. vdf.evaluate()      blocks ~120s
  4. assemble + broadcast
  5. drain queue (5s)    collect peer blocks
  6. pick winner         first valid peer block received, else own candidate
  7. commit              swap ChainState, persist, publish view

Flask threads read node.view (a NodeView snapshot). The node loop is the
sole writer; every mutation publishes a new snapshot atomically.
"""

import logging
import queue
import secrets as _secrets
import state as state_mod
import threading
import time

import block as block_mod
import crypto
import mempool as mempool_mod
import tx as tx_mod
import vdf as vdf_mod
from chainstate import ChainState
from params import DB_PATH
from storage import Storage

log = logging.getLogger("ec.node")
_rng = _secrets.SystemRandom()

SYNC_EVERY_N_CYCLES = 1   # check every cycle; forks are common at 2-min blocks


# ---------------------------------------------------------------------------
# Tail validation (pure, no node state touched)
# ---------------------------------------------------------------------------

def _validate_tx_by_kind(t, state, height, fee_rate_fn, queue=None):
    """Dispatch validation for a single mempool-bound tx by its kind."""
    kind = t.get("kind")
    if kind == "confirm":
        return tx_mod.validate_confirmation(t, state, height, fee_rate_fn)
    if kind == "resolve":
        confirmed = queue.lookup(t.get("confirmed_tx_hash")) if queue else None
        return tx_mod.validate_resolution(t, confirmed, state)
    return False, f"unknown transaction kind: {kind!r}"


def _validate_tail(tail, prefix):
    """Validate new blocks against a trusted prefix. Pure: nothing is mutated.

    Returns (True, None) or (False, error_string).
    """
    cs = ChainState.from_chain(prefix) if prefix else ChainState.from_genesis()
    for blk in tail:
        ok, err, cs = cs.validate_and_apply(blk)
        if not ok:
            return False, f"invalid block at {blk['height']}: {err}"
    return True, None


# ---------------------------------------------------------------------------
# StatsAccumulator: owned by Node, updated each commit, read by NodeView
# ---------------------------------------------------------------------------

class StatsAccumulator:
    """Maintains the /api/stats chart data incrementally.

    Owned by Node and updated in _commit() so it always stays current
    without NodeView needing to carry forward state from a previous view.
    Flask reads node.stats (GIL-safe reference) directly.
    """

    def __init__(self):
        self.points:    list  = []   # [{height, minted, circulating, net_emission}]
        self._chain_len: int  = 0    # chain length at last update

    def update(self, chain, state):
        """Extend or rebuild points to match chain. Call after every commit."""
        if len(chain) <= 1:
            self.points, self._chain_len = [], len(chain)
            return

        if len(chain) == self._chain_len + 1:
            # Incremental: one new block appended.
            blk         = chain[-1]
            prev_minted = self.points[-1]["minted"] if self.points else 0
            reward      = state.total_minted - prev_minted
            self.points = self.points + [{
                "height":       blk["height"],
                "minted":       state.total_minted,
                "circulating":  state.total_minted,
                "net_emission": reward,
            }]
        else:
            # Full rebuild (startup, reorg, or first call).
            points = []
            running_minted = 0
            for blk in chain[1:]:
                reward          = state_mod.compute_reward(running_minted)
                running_minted += reward
                points.append({
                    "height":       blk["height"],
                    "minted":       running_minted,
                    "circulating":  running_minted,
                    "net_emission": reward,
                })
            if len(points) > 500:
                step   = len(points) / 500
                points = [points[int(i * step)] for i in range(500)]
            self.points = points

        self._chain_len = len(chain)


# ---------------------------------------------------------------------------
# NodeView: read-only snapshot for Flask threads
# ---------------------------------------------------------------------------

class NodeView:
    """Immutable snapshot of node state. Published after every block commit.
    Flask reads node.view; one reference swap, GIL-atomic, no lock needed.
    stats_points comes from node.stats, not carried here.
    """
    __slots__ = ("chain", "height", "tip", "genesis_hash", "state", "queue")

    def __init__(self, cs):
        self.chain        = cs.chain
        self.tip          = cs.tip
        self.height       = cs.height
        self.genesis_hash = cs.genesis_hash
        self.state        = cs.state.snapshot()
        self.queue         = cs.queue


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
        self._next_nonce_hint = None

        self.stats = StatsAccumulator()
        self.cs    = self._load_cs()
        self.stats.update(self.cs.chain, self.cs.state)
        self.view  = NodeView(self.cs)

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
            s = state_mod.State.from_snapshot(*self.storage.load_state())
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
            "can_mint":     v.state.compute_can_mint(),
            "block_reward": v.state.compute_block_reward(),
            "queue_length": len(v.queue.remaining()),
            "queue_front":  v.queue.front(),
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
        """Validate and add a tx (confirmation or resolution). Node loop
        thread only."""
        assert threading.current_thread() is self._loop_thread
        kind = tx_dict.get("kind")
        who  = tx_dict.get("broadcaster") or tx_dict.get("resolver") or "?"
        if kind == "confirm":
            ok, err = tx_mod.validate_confirmation(
                tx_dict, self.cs.state, self.cs.height, self.cs.fee_rate_at)
        elif kind == "resolve":
            confirmed = self.cs.queue.lookup(tx_dict.get("confirmed_tx_hash"))
            ok, err = tx_mod.validate_resolution(tx_dict, confirmed, self.cs.state)
        else:
            ok, err = False, f"unknown transaction kind: {kind!r}"
        if not ok:
            log.debug("[tx] rejected  reason=%s  from=%s", err, who[:24])
            return False, err
        ok, h = self.mempool.add(tx_dict)
        if not ok:
            log.debug("[tx] mempool add failed  reason=%s  from=%s", h, who[:24])
            return False, h
        self.gossip.relay_tx(tx_dict)
        log.info("[tx] accepted  kind=%s  hash=%s  from=%s", kind, h[:12], who[:24])
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
        """Build a ciphertext confirmation for a transfer from this node's
        own address, using this node's own key as both the real sender
        (inside the encrypted inner payload) and the broadcaster (on the
        visible wrapper). This is the wallet-layer default the brief calls
        for; the protocol itself does not require broadcaster == sender
        (see tx.py module docstring) -- a caller wanting sender-address
        privacy would build the inner payload and confirmation separately
        with two different keys.

        Real-sender nonce tracking cannot come from the mempool (the inner
        payload is encrypted and invisible until resolution), so this
        node tracks its own next nonce locally across calls in the same
        session; it resyncs automatically once a resolution updates
        cs.state.
        """
        if not passphrase:
            raise ValueError("passphrase is required to sign a transaction")
        kek        = crypto.derive_kek(self.keyfile, passphrase)
        v          = self.view
        committed  = v.state.get_nonce(self.addr)
        if self._next_nonce_hint is not None and self._next_nonce_hint > committed:
            nonce = self._next_nonce_hint + 1
        else:
            nonce = committed + 1
        self._next_nonce_hint = nonce

        fee_height = v.height
        sk = crypto.decrypt_secret_key(self.keyfile, kek=kek)
        inner = tx_mod.create_inner_payload(self.addr, self.pk_hex, to_outputs, nonce, sk)
        t = tx_mod.create_confirmation(self.addr, self.pk_hex, inner,
                                        fee_height, 0, sk)
        # The puzzle (and its real-size ciphertext) is only known after
        # create_confirmation builds it, so compute the real fee now and
        # re-sign with it.
        fee = tx_mod.compute_fee(self.addr, self.pk_hex, t["puzzle"],
                                  fee_height, v.tip["fee_rate"])
        t["fee"] = fee
        msg = crypto.serialize_for_signing(t)
        t["signature"] = crypto.sign(msg, sk).hex()
        del sk, kek
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
        cs = self.cs   # local alias; can change under sync
        pruned = self.mempool.prune_stale(cs.height, cs.state)
        log.info("[vdf] starting height=%d  tip=%s  peers=%d  mempool=%d  pruned=%d",
                 cs.height + 1, cs.tip["hash"][:12],
                 self.pool.count(), self.mempool.size(), len(pruned))

        # Run VDF in a background thread so the node loop stays responsive
        # to tx submissions and peer messages during the ~120s evaluation.
        import concurrent.futures as _cf
        accumulated_blocks = []
        iterations = block_mod.get_vdf_iterations(cs.chain)
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(
                vdf_mod.evaluate,
                block_mod.vdf_challenge(cs.tip["hash"], self.addr), iterations)
            while not _fut.done():
                accumulated_blocks += self._drain_queue(timeout=1)
            vdf_out, vdf_proof, vdf_seconds = _fut.result()
        log.info("[vdf] proof ready  height=%d  seconds=%.1f  iterations=%d",
                 cs.height + 1, vdf_seconds, iterations)

        fee_rate  = block_mod.compute_expected_fee_rate(cs.chain)
        candidate = block_mod.assemble(cs.tip, self.mempool.all_txs(), self.addr,
                                       fee_rate, iterations, cs.queue)
        candidate["vdf_output"]    = vdf_out
        candidate["vdf_proof"]     = vdf_proof
        candidate["vdf_iterations"] = iterations
        candidate["hash"]          = block_mod.block_hash(candidate)
        ok, err = block_mod.validate(candidate, cs.state.snapshot(), cs.chain,
                                      cs.fee_rate_at, cs.queue)
        if not ok:
            log.error("[vdf] self-produced block failed validation: %s", err)
            return
        self.gossip.broadcast_block(candidate)

        # Drain anything that arrived just as VDF completed, then pick winner.
        # All peer candidates should already be in accumulated_blocks since VDFs
        # take roughly the same time. _drain_queue() with no timeout flushes
        # whatever is already in the queue without blocking.
        peer_blocks = accumulated_blocks + self._drain_queue()
        # Pass cs explicitly; self.cs may have advanced during drain if syncer fired.
        winner, relay = self._pick_winner(cs, candidate, peer_blocks)
        if winner is None:
            return
        self._commit(winner, relay=relay)

    def _pick_winner(self, cs, candidate, peer_blocks):
        """Return (best_block, relay). relay=True means it came from a peer.
        Returns (None, False) if the candidate is stale (tip changed under sync).

        cs: the ChainState candidate was built against; passed explicitly so
        this method is immune to self.cs advancing during the drain window.
        """
        tip = cs.tip

        valid_peers = []
        for blk in peer_blocks:
            if blk.get("height") != tip["height"] + 1:
                continue
            if blk.get("previous_hash") != tip["hash"]:
                continue
            probe = cs.state.snapshot()
            ok, err = block_mod.validate(blk, probe, cs.chain, cs.fee_rate_at, cs.queue)
            if not ok:
                log.debug("[vdf] peer block rejected: %s", err)
                continue
            log.debug("[vdf] peer block accepted  height=%d  hash=%s  builder=%s  tx=%d",
                      blk["height"], blk["hash"][:12],
                      (blk.get("builder") or "")[:24], len(blk.get("transactions", [])))
            valid_peers.append(blk)

        if candidate.get("previous_hash") != tip["hash"]:
            log.warning("[vdf] candidate stale (tip advanced during drain), skipping cycle")
            return None, False

        # First valid peer block received wins; fall back to own candidate.
        winner   = valid_peers[0] if valid_peers else candidate
        is_peer  = winner is not candidate
        log.info("[vdf] winner  hash=%s  peer=%s  candidates=%d  peer_candidates=%d",
                 winner["hash"][:12], is_peer, len(valid_peers) + 1, len(valid_peers))
        return winner, is_peer

    def _commit(self, blk, relay=False):
        """Append a validated block: update ChainState, persist, publish view."""
        confirmed = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        self.cs = self.cs.apply_block(blk)
        self.storage.save_block_and_state(blk, self.cs.state)
        self.mempool.remove_many(confirmed)
        self.stats.update(self.cs.chain, self.cs.state)
        self.view = NodeView(self.cs)

        if relay:
            # Peer-won block: broadcast it now. Our own candidate was already
            # broadcast in _run_cycle before the drain window; relay=False there.
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
        """Route an inbound tx message.

        Stem txs (Dandelion relay) are forwarded without validation; we are
        not the ultimate recipient, just a relay node. Fluff txs are validated
        and added to the mempool if new and valid.
        """
        tx_dict   = msg["tx"]
        sender    = (tx_dict.get("broadcaster") or tx_dict.get("resolver") or "?")[:24]
        remaining = msg.get("remaining_hops", 0)
        if msg.get("relay_type") == "tx_stem" and remaining > 0:
            log.debug("[tx] stem relay  hops_remaining=%d  from=%s", remaining, sender)
            self.gossip.dandelion_send(tx_dict, remaining)
            return
        ok, err = _validate_tx_by_kind(tx_dict, self.cs.state, self.cs.height,
                                        self.cs.fee_rate_at, self.cs.queue)
        if not ok:
            log.debug("[tx] inbound rejected  reason=%s  from=%s", err, sender)
            return
        added, h_or_err = self.mempool.add(tx_dict)
        if added:
            log.debug("[tx] inbound accepted  hash=%s  from=%s", h_or_err[:12], sender)
            self.gossip.relay_tx(tx_dict)
        else:
            log.debug("[tx] inbound duplicate  from=%s", sender)

    # ------------------------------------------------------------------
    # Chain sync / reorg
    # ------------------------------------------------------------------

    def _evaluate_remote_chain(self, remote_chain):
        """Pure evaluation of a candidate remote chain. No state is mutated.

        Returns (ok, err, fork_point, tail, remote_cs) on success,
        or (False, err, None, None, None) on rejection.

        Order of operations matters for security:
          1. Genesis check  cheap, stops wrong-network chains immediately.
          2. Fork point     O(min(local, remote)) hash comparisons.
          3. _validate_tail structural block validation on untrusted data.
          4. from_chain     trusted replay, only runs on validated blocks.
          5. is_better_than fork choice, only after we know it's valid.
        """
        if not remote_chain or remote_chain[0]["hash"] != self.cs.genesis_hash:
            log.warning("[sync] rejected genesis mismatch  remote=%s  expected=%s",
                        (remote_chain[0]["hash"][:12] if remote_chain else "empty"),
                        self.cs.genesis_hash[:12])
            return False, "genesis mismatch", None, None, None

        fork_point = next(
            (i for i, (a, b) in enumerate(zip(self.cs.chain, remote_chain))
             if a["hash"] != b["hash"]),
            min(len(self.cs.chain), len(remote_chain))
        )
        tail = remote_chain[fork_point:]

        ok, err = _validate_tail(tail, remote_chain[:fork_point])
        if not ok:
            log.warning("[sync] rejected: %s", err)
            return False, err, None, None, None

        try:
            remote_cs = ChainState.from_chain(remote_chain)
        except Exception as e:
            log.warning("[sync] chain replay failed: %s", e)
            return False, f"chain replay error: {e}", None, None, None

        if not remote_cs.is_better_than(self.cs):
            log.debug("[sync] remote chain not better  remote_h=%d  local_h=%d",
                      remote_cs.height, self.cs.height)
            return False, "remote chain not better", fork_point, tail, None

        return True, None, fork_point, tail, remote_cs

    def _salvage_fork_txs(self, fork_point, tail):
        """Add unconfirmed, still-valid txs from a rejected fork into the
        local mempool. self.cs is unchanged here (the remote chain lost),
        so txs are checked against the current local state before being
        re-added -- a stale-nonce or otherwise invalid tx from the losing
        fork must not silently sit in the mempool."""
        confirmed = {tx_mod.tx_hash(t)
                     for blk in self.cs.chain[fork_point:]
                     for t in blk.get("transactions", [])}
        for blk in tail:
            for t in blk.get("transactions", []):
                if tx_mod.tx_hash(t) in confirmed:
                    continue
                ok, _ = _validate_tx_by_kind(t, self.cs.state, self.cs.height,
                                              self.cs.fee_rate_at, self.cs.queue)
                if ok:
                    self.mempool.add(t)

    def apply_better_chain(self, remote_chain):
        """Accept remote_chain if it is better than local. Used by syncer."""
        ok, err, fork_point, tail, remote_cs = self._evaluate_remote_chain(remote_chain)
        if not ok:
            if fork_point is not None and tail:
                self._salvage_fork_txs(fork_point, tail)
            return False, err

        # Storage write first; if it fails, mempool stays consistent with self.cs.
        self.storage.replace_chain_and_state(fork_point, tail, remote_cs.state)
        self._reorg_mempool(fork_point, remote_chain)
        self.cs = remote_cs
        self.stats.update(self.cs.chain, self.cs.state)
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
