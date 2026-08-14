"""Block cycle orchestrator. Linear sequence. Persists every block to SQLite."""

import logging
import os
import queue
import secrets as _secrets
import threading
import time

_rng = _secrets.SystemRandom()

import block as block_mod
import pob as pob_mod
import crypto
import mempool as mempool_mod
import state as state_mod
import tx as tx_mod
from params import (
    BLOCK_SIZE_LIMIT,
    DB_PATH,
)
from storage import Storage


class NodeView:
    """Read-only snapshot of node state for Flask threads.
    Published by the node loop after every chain/state mutation.
    Flask reads node.view -- one attribute swap, GIL-atomic, no lock.
    chain is a frozen copy so Flask iteration can never race with
    node-loop appends."""
    __slots__ = ("chain", "genesis_hash", "height", "state", "tip")

    def __init__(self, chain, state):
        self.chain        = list(chain)
        self.tip          = chain[-1]
        self.genesis_hash = chain[0]["hash"]
        self.state        = state.snapshot()
        self.height       = chain[-1]["height"]


log = logging.getLogger("ec.node")

SYNC_EVERY_N_CYCLES = 3


def _replay_blocks(blocks, state):
    """Replay a slice of blocks onto state in place.
    Applies transactions then block reward for each block.
    Used by both _rebuild_state and _apply_chain to avoid duplication
    and ensure the tx-then-reward order is correct everywhere.
    blocks: iterable of block dicts, skipping genesis (height > 0).
    """
    for blk in blocks:
        if blk["height"] == 0:
            continue
        for t in blk["transactions"]:
            state.apply_tx(t)
        builder = blk.get("builder")
        if builder:
            reward = state.compute_block_reward()
            state.apply_reward(builder, reward)


class Node:
    def __init__(self, keyfile, public_key, gossip, syncer, pool, net_in_q, db_path=None):
        self.keyfile    = keyfile
        self.pk         = public_key
        self.pk_hex     = public_key.hex()
        self.addr       = crypto.public_key_to_address(public_key)
        self.gossip     = gossip
        self.syncer     = syncer
        self.pool       = pool
        self.net_in_q   = net_in_q
        self.state      = state_mod.State()
        self.mempool    = mempool_mod.Mempool()
        self.chain      = []
        self.running    = False
        self._kek       = None
        self._loop_thread = None
        self.storage    = Storage(db_path or DB_PATH)
        self._cycle_count = 0

        # tx_hash -> number of non-full blocks since first appearance that
        # excluded it. Used for the transaction censorship score.
        self._tx_exclusion_age = {}

        self._load_or_init_chain()
        self.view = NodeView(self.chain, self.state)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _load_or_init_chain(self):
        stored = self.storage.load_all_blocks()
        if stored:
            self.chain = stored
            if self.storage.state_exists():
                balances, nonces, total_minted, total_burnt = self.storage.load_state()
                self.state._balances    = balances
                self.state._nonces      = nonces
                self.state.total_minted = total_minted
                self.state.total_burnt  = total_burnt
            else:
                self._rebuild_state()
            log.info("[startup] chain loaded  height=%d  tip=%s",
                     self.chain[-1]["height"], self.chain[-1]["hash"][:12])
        else:
            genesis = block_mod.create_genesis()
            self.chain.append(genesis)
            self.storage.save_block(genesis)
            log.info("[startup] genesis created")

    def _rebuild_state(self):
        """Replay chain from genesis to reconstruct balance/nonce state.
        Uses _replay_blocks so the tx-then-reward order matches _commit."""
        self.state = state_mod.State()
        _replay_blocks(self.chain, self.state)
        log.info("[startup] state rebuilt  blocks=%d", len(self.chain))

    def _publish_view(self):
        """Publish a consistent snapshot for Flask threads. Single ref swap."""
        self.view = NodeView(self.chain, self.state)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_signing_active(self):
        """True when the node loop is running and the KEK is loaded.
        API layer uses this instead of reaching into _kek directly."""
        return self._kek is not None

    def mark_tx_seen(self, tx_hash):
        """Thread-safe dedup check for inbound tx. Returns True if already seen.
        Exposes gossip._seen_tx check so api.py doesn't reach into gossip internals."""
        return self.gossip.mark_seen(tx_hash)


    def start(self, kek):
        """Start the block loop. kek must be derived via crypto.derive_kek()
        before calling; main.py handles passphrase prompting."""
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
        """Validate and add a tx. Called ONLY from the node loop thread."""
        assert threading.current_thread() is self._loop_thread, \
            "submit_tx must be called from the node loop thread"
        tip = self.chain[-1]
        ok, err = tx_mod.validate(tx_dict, self.state, tip["height"], self._fee_rate_at)
        if not ok:
            log.debug("[tx] rejected  reason=%s  from=%s",
                      err, tx_dict.get("from", "?")[:24])
            return False, err
        ok, h = self.mempool.add(tx_dict)
        if not ok:
            return False, h
        self.gossip.relay_tx(tx_dict)
        self._publish_view()
        log.info("[tx] accepted  hash=%s  from=%s  fee=%s",
                 h[:12], tx_dict.get("from", "?")[:24], tx_dict.get("fee"))
        return True, h

    def submit_tx_from_api(self, tx_dict, timeout=5):
        """Thread-safe entry point for Flask. Puts on queue, blocks on reply."""
        reply = queue.Queue(maxsize=1)
        self.net_in_q.put({"type": "submit_tx", "tx": tx_dict, "reply": reply})
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return False, "node busy (timeout)"

    def build_and_sign_tx(self, to_outputs, passphrase=None):
        if self._kek is not None:
            kek     = self._kek
            own_kek = False
        elif passphrase:
            kek     = crypto.derive_kek(self.keyfile, passphrase)
            own_kek = True
        else:
            raise RuntimeError("node not running and no passphrase provided")

        v          = self.view
        nonce      = v.state.get_nonce(self.addr) + 1
        fee_rate   = v.tip["fee_rate"]
        fee_height = v.height

        fee = tx_mod.compute_fee(self.addr, self.pk_hex, to_outputs, nonce, fee_height, fee_rate)
        sk  = crypto.decrypt_secret_key(self.keyfile, kek=kek)
        t   = tx_mod.create(self.addr, self.pk_hex, to_outputs, nonce, fee_height, fee, sk)
        del sk
        if own_kek:
            del kek
        return t, fee

    def get_info(self):
        """Read from published view so Flask threads get consistent data."""
        v = self.view
        return {
            "height":        v.height,
            "tip_hash":      v.tip["hash"],
            "genesis_hash":  v.genesis_hash,
            "fee_rate":      v.tip["fee_rate"],
            "mempool_size":  self.mempool.size(),
            "address":       self.addr,
            "peer_count":    self.pool.count(),
            "total_minted":  v.state.total_minted,
            "total_burnt":   v.state.total_burnt,
            "can_mint":      v.state.compute_block_reward(),
        }

    # ------------------------------------------------------------------
    # Block cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        """One block cycle: evaluate VDF, assemble block, collect winner."""
        import tx as _tx
        import vdf as vdf_mod

        self._cycle_count += 1
        self._drain_queue(timeout=0)

        if self._cycle_count % SYNC_EVERY_N_CYCLES == 0:
            self.syncer.check_and_sync(
                self.chain[-1]["height"],
                self.chain[-1]["hash"],
                lambda chain: self.sync_chain(chain)[0],
            )

        tip      = self.chain[-1]
        fee_rate = block_mod.compute_expected_fee_rate(self.chain)
        log.info("[vdf] starting height=%d  tip=%s  peers=%d  mempool=%d",
                 tip["height"] + 1, tip["hash"][:12],
                 self.pool.count(), self.mempool.size())

        pruned = self.mempool.prune_stale(tip["height"], self.state)
        if pruned:
            log.info("[vdf] mempool pruned  dropped=%d  remaining=%d",
                     len(pruned), self.mempool.size())

        challenge          = bytes.fromhex(tip["hash"])
        vdf_out, vdf_proof = vdf_mod.evaluate(challenge)
        log.info("[vdf] proof ready  height=%d", tip["height"] + 1)

        sorted_txs = _tx.sort_txs(self.mempool.all_txs())
        candidate  = block_mod.assemble(tip, sorted_txs, self.addr, fee_rate)
        candidate["vdf_output"] = vdf_out
        candidate["vdf_proof"]  = vdf_proof
        candidate["hash"]       = block_mod.block_hash(candidate)
        self.gossip.broadcast_block(candidate)

        COLLECTION_WINDOW = 5
        best_block = candidate
        best_probe = None

        peer_blocks  = self._drain_queue(timeout=COLLECTION_WINDOW)
        peer_blocks += self._drain_queue(timeout=0)

        for blk in peer_blocks:
            if blk.get("height") != tip["height"] + 1:
                continue
            if blk.get("previous_hash") != tip["hash"]:
                continue
            if blk.get("hash", "") >= best_block["hash"]:
                continue
            probe = self.state.snapshot()
            ok, err = block_mod.validate(blk, probe, self.chain, self._fee_rate_at)
            if not ok:
                log.debug("[vdf] rejected peer block  reason=%s", err)
                continue
            score = self._censorship_score(blk)
            if _rng.random() >= score:
                log.debug("[vdf] censorship check rejected peer block")
                continue
            best_block = blk
            best_probe = probe

        if best_block is candidate:
            probe = self.state.snapshot()
            ok, err = block_mod.validate(candidate, probe, self.chain, self._fee_rate_at)
            if not ok:
                log.error("[vdf] own block invalid: %s", err)
                return
            self._commit(candidate, probe, relay=False)
        else:
            self._commit(best_block, best_probe, relay=True)

    # ------------------------------------------------------------------
    # Queue handling
    # ------------------------------------------------------------------

    def _drain_queue(self, timeout=0):
        """Drain all pending messages from net_in_q."""
        assert threading.current_thread() is self._loop_thread, \
            "_drain_queue must be called from the node loop thread"
        block_msgs = []
        # First message: block up to timeout seconds.
        try:
            msg = self.net_in_q.get(block=timeout > 0, timeout=timeout if timeout > 0 else None)
            self._dispatch_message(msg, block_msgs)
        except queue.Empty:
            return block_msgs
        # Drain the rest non-blocking.
        while True:
            try:
                msg = self.net_in_q.get_nowait()
                self._dispatch_message(msg, block_msgs)
            except queue.Empty:
                break
        return block_msgs

    def _dispatch_message(self, msg, block_msgs):
        """Route one message. block-type messages go into block_msgs list."""
        mtype = msg.get("type")
        if mtype == "block":
            block_msgs.append(msg["block"])
        else:
            self._handle_message(msg)

    def _handle_message(self, msg):
        """Single dispatch for non-block messages."""
        mtype = msg.get("type")
        if mtype == "submit_tx":
            result = self.submit_tx(msg["tx"])
            msg["reply"].put(result)
        elif mtype == "tx":
            self._handle_tx_message(msg)

    def _handle_tx_message(self, msg):
        tx_dict    = msg["tx"]
        relay_type = msg.get("relay_type", "tx_fluff")
        remaining  = msg.get("remaining_hops", 0)
        if relay_type == "tx_stem" and remaining > 0:
            self.gossip.dandelion_send(tx_dict, remaining)
        else:
            tip = self.chain[-1]
            ok, _ = tx_mod.validate(tx_dict, self.state, tip["height"], self._fee_rate_at)
            if ok:
                added, _ = self.mempool.add(tx_dict)
                if added:
                    # Use relay_tx so the dedup check in gossip._seen_tx fires
                    # and the same tx isn't re-broadcast to all peers twice.
                    self.gossip.relay_tx(tx_dict)

    # ------------------------------------------------------------------
    # Censorship resistance
    # ------------------------------------------------------------------

    def _censorship_score(self, blk):
        """Compute the censorship acceptance probability for blk.

        score = min over all missing T of: 1 / effective_age(T)
        Age 0 (first miss): score 1.0.  Age 2: 0.5.  Age 5: 0.2.
        Pure read -- does not update _tx_exclusion_age.
        """
        confirmed = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        pending   = self.mempool.pending_hashes()
        missing   = pending - confirmed
        score     = 1.0
        for h in missing:
            age = self._tx_exclusion_age.get(h, 0)
            if age > 0:
                score = min(score, 1.0 / age)
        return score

    def _update_exclusion_ages(self, accepted_blk):
        """Update _tx_exclusion_age after a block is accepted."""
        confirmed = {tx_mod.tx_hash(t) for t in accepted_blk.get("transactions", [])}
        pending   = self.mempool.pending_hashes()
        missing   = pending - confirmed
        is_full   = block_mod.block_size(accepted_blk) >= BLOCK_SIZE_LIMIT * 0.99

        if not is_full:
            for h in missing:
                self._tx_exclusion_age[h] = self._tx_exclusion_age.get(h, 0) + 1

        for h in [k for k in self._tx_exclusion_age if k not in pending]:
            del self._tx_exclusion_age[h]

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _commit(self, new_block, new_state, relay=False):
        self._update_exclusion_ages(new_block)
        builder = new_block.get("builder")
        if builder:
            reward = new_state.compute_block_reward()
            new_state.apply_reward(builder, reward)
        self.state = new_state
        self.chain.append(new_block)
        self.storage.save_block(new_block)
        self.storage.save_state(new_state)
        self.mempool.remove_many([tx_mod.tx_hash(t) for t in new_block["transactions"]])
        self._publish_view()
        if relay:
            try:
                self.gossip.broadcast_block(new_block)
            except Exception:
                log.exception("[commit] broadcast failed for block=%d, peers may miss it",
                              new_block["height"])
        log.info("[commit] block=%d  hash=%s  tx=%d  builder=%s",
                 new_block["height"], new_block["hash"][:12],
                 len(new_block["transactions"]),
                 (new_block.get("builder") or "")[:24])

    def _fee_rate_at(self, height):
        if 0 <= height < len(self.chain):
            return self.chain[height]["fee_rate"]
        return None

    # ------------------------------------------------------------------
    # Chain sync / reorg
    # ------------------------------------------------------------------

    def _remote_is_better(self, remote_chain):
        remote_height = len(remote_chain) - 1
        local_height  = len(self.chain) - 1
        if remote_height > local_height:
            return True
        if remote_height == local_height:
            # PoB fork choice: lower cumulative score = more economic commitment.
            # Falls back to hash comparison only if scores are equal (rare).
            remote_score = pob_mod.cumulative_score(remote_chain)
            local_score  = pob_mod.cumulative_score(self.chain)
            if remote_score != local_score:
                return remote_score < local_score
            return remote_chain[-1]["hash"] < self.chain[-1]["hash"]
        return False

    def _apply_chain(self, candidate_chain, label):
        genesis = block_mod.create_genesis()
        if not candidate_chain or candidate_chain[0]["hash"] != genesis["hash"]:
            return False, "genesis mismatch"

        fork_point = next(
            (i for i, (a, b) in enumerate(zip(self.chain, candidate_chain))
             if a["hash"] != b["hash"]),
            min(len(self.chain), len(candidate_chain))
        )

        new_state = state_mod.State()
        validated = []

        def fee_rate_at(h):
            return candidate_chain[h]["fee_rate"] if 0 <= h < len(candidate_chain) else None

        # Replay the shared prefix (no validation needed -- already trusted).
        _replay_blocks(candidate_chain[:fork_point], new_state)
        validated = list(candidate_chain[:fork_point])

        # Validate and apply the new tail.
        for blk in candidate_chain[fork_point:]:
            probe = new_state.snapshot()
            ok, err = block_mod.validate(blk, probe, validated, fee_rate_at)
            if not ok:
                return False, f"invalid block at {blk['height']}: {err}"
            builder = blk.get("builder")
            if builder:
                reward = probe.compute_block_reward()
                probe.apply_reward(builder, reward)
            new_state = probe
            validated.append(blk)

        # Reorg mempool: restore txs from the old tail that aren't in the new tail.
        old_tx_by_hash = {
            tx_mod.tx_hash(t): t
            for blk in self.chain[fork_point:]
            for t in blk.get("transactions", [])
        }
        new_confirmed = {
            tx_mod.tx_hash(t)
            for blk in candidate_chain[fork_point:]
            for t in blk.get("transactions", [])
        }
        self.mempool.remove_many(new_confirmed)
        for h, t in old_tx_by_hash.items():
            if h not in new_confirmed:
                self.mempool.add(t)

        self.storage.replace_chain(fork_point, candidate_chain[fork_point:])
        self.chain = list(candidate_chain)
        self.state = new_state
        self.storage.save_state(new_state)
        self._publish_view()
        if label == "reorg":
            log.warning("[reorg] applied  height=%d  fork_point=%d",
                        len(self.chain) - 1, fork_point)
        else:
            log.info("[sync] applied  height=%d  fork_point=%d",
                     len(self.chain) - 1, fork_point)
        return True, None

    def sync_chain(self, remote_chain):
        if not self._remote_is_better(remote_chain):
            return False, "remote chain not longer or heavier"
        ok, err = self._apply_chain(remote_chain, "sync")
        if not ok:
            log.warning("[sync] rejected  reason=%s", err)
        return ok, err

    def handle_reorg(self, fork_chain):
        if not self._remote_is_better(fork_chain):
            return False, "fork not longer or heavier"
        return self._apply_chain(fork_chain, "reorg")
