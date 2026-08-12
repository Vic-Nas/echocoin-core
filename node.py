"""Block cycle orchestrator. Linear sequence. Persists every block to SQLite."""

import time
import queue
import getpass
import logging
import random

import crypto
import tx as tx_mod
import block as block_mod
import mining
import mempool as mempool_mod
import state as state_mod
from storage import Storage
from params import (
    PUZZLE_PHASE_SECONDS,
    BUILD_PHASE_SECONDS,
    BLOCK_CYCLE_SECONDS,
    BLOCK_SIZE_LIMIT,
    DB_PATH,
)


class NodeView:
    """Read-only snapshot of node state for Flask threads.
    Published by the node loop after every chain/state mutation.
    Flask reads node.view -- one attribute swap, GIL-atomic, no lock.
    chain is a frozen copy so Flask iteration can never race with
    node-loop appends."""
    __slots__ = ("tip", "chain", "state", "genesis_hash", "height")

    def __init__(self, chain, state):
        self.chain        = list(chain)
        self.tip          = chain[-1]
        self.genesis_hash = chain[0]["hash"]
        self.state        = state.snapshot()
        self.height       = chain[-1]["height"]


log = logging.getLogger("pc.node")

SYNC_EVERY_N_CYCLES = 3


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
        self.storage    = Storage(db_path or DB_PATH)
        self._cycle_count = 0

        # tx_hash -> number of non-full blocks since it first appeared that
        # excluded it. Used for the transaction censorship score.
        self._tx_exclusion_age = {}

        self._load_or_init_chain()
        self.current_difficulty = block_mod.compute_expected_difficulty(self.chain)
        self.view = NodeView(self.chain, self.state)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _load_or_init_chain(self):
        stored = self.storage.load_all_blocks()
        if stored:
            self.chain = stored
            if self.storage.state_exists():
                balances, nonces = self.storage.load_state()
                self.state._balances = balances
                self.state._nonces   = nonces
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
        self.state = state_mod.State()
        for blk in self.chain[1:]:
            for t in blk["transactions"]:
                self.state.apply_tx(t)
            self.state.apply_rewards(mining.reward_addresses_from_summary(blk["solver_summaries"]))
        log.info("[startup] state rebuilt  blocks=%d", len(self.chain))

    def _publish_view(self):
        """Publish a consistent snapshot for Flask threads. Single ref swap."""
        self.view = NodeView(self.chain, self.state)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self, kek=None):
        if kek is None:
            passphrase = getpass.getpass("Signing passphrase: ")
            try:
                kek = crypto.derive_kek(self.keyfile, passphrase)
                sk_test = crypto.decrypt_secret_key(self.keyfile, kek=kek)
                del sk_test
            except (ValueError, FileNotFoundError, OSError) as e:
                raise RuntimeError(f"could not load key: {e}") from None
            finally:
                del passphrase

        self._kek    = kek
        self.running = True
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
        tip = self.chain[-1]
        ok, err = tx_mod.validate(tx_dict, self.state, tip["height"], self._fee_rate_at)
        if not ok:
            return False, err
        ok, h = self.mempool.add(tx_dict)
        if not ok:
            return False, h
        self.gossip.relay_tx(tx_dict)
        self._publish_view()
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
            "height":       v.height,
            "tip_hash":     v.tip["hash"],
            "genesis_hash": v.genesis_hash,
            "fee_rate":     v.tip["fee_rate"],
            "mempool_size": self.mempool.size(),
            "address":      self.addr,
            "peer_count":   self.pool.count(),
        }

    # ------------------------------------------------------------------
    # Block cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        self._cycle_count += 1
        cycle_start = self._wait_for_cycle_boundary(BLOCK_CYCLE_SECONDS)
        self._flush_stale_queue()

        tip, difficulty, fee_rate, puzzle = self._setup_round()

        my_solutions, peer_solutions, peer_block_candidates = self._puzzle_phase(
            cycle_start, tip, difficulty, puzzle
        )

        new_block, new_state = self._build_phase(
            cycle_start, tip, difficulty, fee_rate,
            my_solutions, peer_solutions, peer_block_candidates
        )

        if new_block is not None:
            self._commit(new_block, new_state, my_solutions + peer_solutions)

    def _wait_for_cycle_boundary(self, cycle_seconds):
        now         = time.time()
        cycle_start = (int(now) // cycle_seconds + 1) * cycle_seconds

        if self._cycle_count % SYNC_EVERY_N_CYCLES == 0:
            tip = self.chain[-1]
            self.syncer._local_tip_hash = lambda: tip["hash"]
            self.syncer.check_and_sync(
                tip["height"],
                lambda chain: self.sync_chain(chain)[0],
            )

        # Collect peer blocks arriving during the wait window and apply only
        # the lowest-hash valid one at the end, so first-arrived does not win
        # over a lower-hash block that arrives moments later.
        pending_blocks = []

        while time.time() < cycle_start:
            remaining = cycle_start - time.time()
            if remaining <= 0:
                break
            try:
                msg = self.net_in_q.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if msg.get("type") == "block":
                pending_blocks.append(msg["block"])
            else:
                self._handle_message(msg)

        # Apply the best pending block (if any) before starting the cycle.
        if pending_blocks:
            self._apply_best_peer_block(pending_blocks)

        return cycle_start

    def _apply_best_peer_block(self, candidates):
        """From a list of peer blocks all at tip+1, apply the lowest-hash valid one."""
        expected_height = self.chain[-1]["height"] + 1
        valid = []
        for blk in candidates:
            if blk.get("height") != expected_height:
                continue
            if blk.get("previous_hash") != self.chain[-1]["hash"]:
                continue
            probe = self.state.snapshot()
            ok, _ = block_mod.validate(blk, probe, self.chain, self._fee_rate_at)
            if ok:
                valid.append((blk["hash"], blk, probe))
        if not valid:
            return
        _, best_blk, best_probe = min(valid, key=lambda t: t[0])
        # Accept with probability = censorship_score (same direction as _try_adopt).
        # score=1.0 (no missing txs) -> always accept.
        # score<1.0 -> probabilistically reject censoring blocks.
        if random.random() < self._censorship_score(best_blk):
            self._update_exclusion_ages(best_blk)
            best_probe.apply_rewards(
                mining.reward_addresses_from_summary(best_blk.get("solver_summaries", [])))
            self.state = best_probe
            self.chain.append(best_blk)
            self.storage.save_block(best_blk)
            self.storage.save_state(self.state)
            self._publish_view()
            self.mempool.remove_many(
                [tx_mod.tx_hash(t) for t in best_blk.get("transactions", [])])
            log.debug("[wait] applied peer block  height=%d  hash=%s",
                      best_blk["height"], best_blk["hash"][:12])

    def _flush_stale_queue(self):
        pending_blocks = []
        while True:
            try:
                msg = self.net_in_q.get_nowait()
            except queue.Empty:
                break
            if msg.get("type") == "block":
                pending_blocks.append(msg["block"])
            else:
                self._handle_message(msg)
        if pending_blocks:
            self._apply_best_peer_block(pending_blocks)

    def _handle_message(self, msg):
        """Single dispatch for all message types across all phases."""
        mtype = msg.get("type")

        if mtype == "submit_tx":
            result = self.submit_tx(msg["tx"])
            msg["reply"].put(result)

        elif mtype == "tx":
            self._handle_tx_message(msg)

        # "block" messages are handled by _apply_best_peer_block in the
        # wait and flush phases, and collected as peer_block_candidates in
        # the puzzle phase. They never route through _handle_message.
        # Unknown types are silently dropped.

    def _setup_round(self):
        tip        = self.chain[-1]
        difficulty = block_mod.compute_expected_difficulty(self.chain)
        fee_rate   = block_mod.compute_expected_fee_rate(self.chain)
        puzzle     = mining.derive_puzzle(tip["hash"], self.pk)
        self.current_difficulty = difficulty
        self.mempool.prune_stale(tip["height"], self.state)
        log.info("[cycle] height=%d  tip=%s  peers=%d  mempool=%d",
                 tip["height"], tip["hash"][:12], self.pool.count(), self.mempool.size())
        return tip, difficulty, fee_rate, puzzle

    def _puzzle_phase(self, cycle_start, tip, difficulty, puzzle):
        """Mine during the puzzle phase. Every valid solution is broadcast immediately.
        Peer blocks that arrive are collected as candidates but NOT applied to
        self.chain or self.state -- the chain is only mutated at commit time,
        keeping each cycle linear. The build phase picks among all candidates.
        Returns (my_solutions, peer_solutions, peer_block_candidates).
        """
        puzzle_end           = cycle_start + PUZZLE_PHASE_SECONDS
        my_solutions         = []
        peer_solutions       = []
        peer_block_candidates = []
        seen_keys            = set()
        nonce                = 0

        while time.time() < puzzle_end:
            valid, sol_hash = mining.check_solution(puzzle, nonce, difficulty)
            if valid:
                sol = {"pubkey": self.pk_hex, "nonce": nonce, "solution_hash": sol_hash}
                my_solutions.append(sol)
                self.gossip.broadcast_solution(sol)
            nonce += 1

            # Drain the inbound queue without blocking.
            while True:
                try:
                    msg = self.net_in_q.get_nowait()
                except queue.Empty:
                    break

                mtype = msg.get("type")

                if mtype in ("submit_tx", "tx"):
                    self._handle_message(msg)

                elif mtype == "block":
                    blk = msg["block"]
                    if blk.get("height") == tip["height"] + 1:
                        peer_block_candidates.append(blk)

                elif mtype == "solution":
                    sol        = msg["solution"]
                    pubkey_hex = sol.get("pubkey", "")
                    nonce_val  = sol.get("nonce")
                    key        = (pubkey_hex, nonce_val)
                    if key in seen_keys:
                        continue
                    try:
                        peer_puzzle = mining.derive_puzzle(tip["hash"], bytes.fromhex(pubkey_hex))
                        valid_peer, expected = mining.check_solution(peer_puzzle, nonce_val, difficulty)
                    except Exception:
                        continue
                    if not valid_peer or sol.get("solution_hash") != expected:
                        log.debug("[puzzle] invalid solution  peer=%s", pubkey_hex[:8])
                        continue
                    seen_keys.add(key)
                    peer_solutions.append(sol)

        log.debug("[puzzle] done  mine=%d  peers=%d  nonces=%d",
                  len(my_solutions), len(peer_solutions), nonce)
        return my_solutions, peer_solutions, peer_block_candidates

    def _build_phase(self, cycle_start, tip, difficulty, fee_rate,
                     my_solutions, peer_solutions, peer_block_candidates):
        """Build phase: every node that found solutions assembles its own block
        and broadcasts it. No designated assembler.
        The lowest-hash valid block wins.
        self.chain and self.state are not mutated here -- they are stable
        throughout the cycle and only updated in _commit.
        Returns (best_block, validated_state) or (None, None).
        """
        all_solutions = my_solutions + peer_solutions
        build_end     = cycle_start + PUZZLE_PHASE_SECONDS + BUILD_PHASE_SECONDS

        if not all_solutions:
            log.warning("[build] no solutions this round, skipping")
            return None, None

        best_block = None
        best_state = None

        def _try_adopt(candidate, check_summaries=False):
            """Validate candidate and adopt it if it beats best_block.

            check_summaries: only True for our own local block, where we have
            the full solution list. For peer blocks we cannot verify summary
            accuracy (we may not have seen all solutions), so structural
            validation from block.validate is sufficient.
            """
            nonlocal best_block, best_state
            if candidate.get("previous_hash") != tip["hash"]:
                return
            probe = self.state.snapshot()
            ok, err = block_mod.validate(candidate, probe, self.chain, self._fee_rate_at)
            if not ok:
                log.warning("[build] invalid block  reason=%s", err)
                return
            if check_summaries:
                ok2, err2 = mining.verify_summary_addresses(
                    candidate["solver_summaries"], all_solutions)
                if not ok2:
                    log.warning("[build] reward check failed  reason=%s", err2)
                    return
            if random.random() >= self._censorship_score(candidate):
                log.warning("[build] censorship rejection")
                return
            if best_block is None or candidate["hash"] < best_block["hash"]:
                best_block = candidate
                best_state = probe

        # Build and broadcast our own block if we have solutions.
        if my_solutions:
            local_block = self._assemble_block(tip, all_solutions, difficulty, fee_rate, build_end)
            if local_block:
                self.gossip.broadcast_block(local_block)
                _try_adopt(local_block, check_summaries=True)

        # Try peer blocks that arrived during the puzzle phase.
        for blk in peer_block_candidates:
            _try_adopt(blk)

        # Drain the queue for the rest of the build phase.
        # Block messages are evaluated as candidates only -- chain/state are not
        # mutated during the build phase. Other messages are handled normally.
        while time.time() < build_end:
            remaining = build_end - time.time()
            if remaining <= 0:
                break
            try:
                msg = self.net_in_q.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue

            mtype = msg.get("type")
            if mtype == "block":
                _try_adopt(msg["block"])
            elif mtype in ("submit_tx", "tx"):
                self._handle_message(msg)
            # Solutions arriving in build phase are ignored: the puzzle window closed.

        if best_block is None:
            log.warning("[build] no valid block this round")
        return best_block, best_state

    def _censorship_score(self, blk):
        """Compute the censorship acceptance probability for blk.

        score = min over all missing T of: 1 / effective_age(T)
        Age 0 (first miss): score 1.0.  Age 2: 0.5.  Age 5: 0.2.  Age 10: 0.1.
        Pure read -- does not update _tx_exclusion_age.
        """
        confirmed_hashes = {tx_mod.tx_hash(t) for t in blk.get("transactions", [])}
        mempool_hashes   = self.mempool.pending_hashes()
        missing          = mempool_hashes - confirmed_hashes
        min_score = 1.0
        for h in missing:
            age = self._tx_exclusion_age.get(h, 0)
            if age > 0:
                min_score = min(min_score, 1.0 / age)
        return min_score

    def _update_exclusion_ages(self, accepted_blk):
        """Update _tx_exclusion_age after a block is accepted for this round.

        Call exactly once per cycle, after the winning block is chosen.
        Increments ages only for txs missing from non-full blocks.
        Evicts hashes that have left the mempool.
        """
        confirmed_hashes = {tx_mod.tx_hash(t) for t in accepted_blk.get("transactions", [])}
        mempool_hashes   = self.mempool.pending_hashes()
        missing          = mempool_hashes - confirmed_hashes
        is_full          = block_mod.block_size(accepted_blk) >= BLOCK_SIZE_LIMIT * 0.99

        if not is_full:
            for h in missing:
                self._tx_exclusion_age[h] = self._tx_exclusion_age.get(h, 0) + 1

        for h in [k for k in self._tx_exclusion_age if k not in mempool_hashes]:
            del self._tx_exclusion_age[h]

    def _commit(self, new_block, new_state, all_solutions):
        self._update_exclusion_ages(new_block)
        new_state.apply_rewards(mining.reward_addresses(all_solutions))
        self.state = new_state
        self.chain.append(new_block)
        self.storage.save_block(new_block)
        self.storage.save_state(new_state)
        self.mempool.remove_many([tx_mod.tx_hash(t) for t in new_block["transactions"]])
        self._publish_view()
        try:
            self.gossip.broadcast_block(new_block)
        except Exception:
            log.exception("[commit] broadcast failed for block=%d, peers may miss it",
                          new_block["height"])
        mine  = sum(1 for s in all_solutions if s.get("pubkey") == self.pk_hex)
        peers = len(all_solutions) - mine
        log.info("[commit] block=%d  hash=%s  tx=%d  solvers=%d  mine=%d  peers=%d",
                 new_block["height"], new_block["hash"][:12],
                 len(new_block["transactions"]),
                 len(new_block["solver_summaries"]),
                 mine, peers)

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
                    self.gossip._broadcast("/api/receive_tx",
                                           {"type": "tx_fluff", "tx": tx_dict})

    def _assemble_block(self, tip, solutions, difficulty, fee_rate, build_deadline):
        """Assemble a block from this node's own mempool.
        Adds txs one at a time, stopping before the block would exceed the
        size limit, so the block is always valid and never silently dropped.
        """
        sorted_txs = tx_mod.sort_txs(self.mempool.all_txs())
        test_state = self.state.snapshot()
        valid_txs  = []
        summaries  = mining.summarize_solutions(solutions)

        def _make_candidate(txs):
            return block_mod.create(
                height=tip["height"] + 1, previous_hash=tip["hash"],
                transactions=txs, solver_summaries=summaries,
                difficulty_target=difficulty, fee_rate=fee_rate,
            )

        for t in sorted_txs:
            if time.time() >= build_deadline:
                log.warning("[build] deadline reached, including %d/%d tx(s)",
                            len(valid_txs), len(sorted_txs))
                break
            ok, _ = tx_mod.validate(t, test_state, tip["height"], self._fee_rate_at)
            if not ok:
                continue
            # Check size before committing this tx.
            if block_mod.block_size(_make_candidate(valid_txs + [t])) > BLOCK_SIZE_LIMIT:
                log.debug("[build] tx would exceed block size limit, skipping")
                continue
            test_state.apply_tx(t)
            valid_txs.append(t)

        return _make_candidate(valid_txs)

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
            return remote_chain[-1]["hash"] < self.chain[-1]["hash"]
        return False

    def _apply_chain(self, candidate_chain, label):
        genesis = block_mod.create_genesis()
        if not candidate_chain or candidate_chain[0]["hash"] != genesis["hash"]:
            return False, "genesis mismatch"

        # Find the fork point before validating: we only need to replay blocks
        # from there onwards, not the entire chain from genesis.
        fork_point = next(
            (i for i, (a, b) in enumerate(zip(self.chain, candidate_chain))
             if a["hash"] != b["hash"]),
            min(len(self.chain), len(candidate_chain))
        )

        # Validate all blocks from genesis up to fork_point implicitly (they
        # are already in self.chain and were validated on arrival). Start from
        # the pre-fork state snapshot by replaying only the shared prefix.
        # For a sync extending the tip (most common case), fork_point == tip
        # and this loop does almost nothing.
        new_state = state_mod.State()
        validated = []

        def rate_at(h, cc=candidate_chain):
            return cc[h]["fee_rate"] if 0 <= h < len(cc) else None

        for i, blk in enumerate(candidate_chain):
            if i == 0:
                validated.append(blk)
                continue
            if i < fork_point:
                # Blocks before the fork are already trusted; apply rewards
                # to rebuild state without re-running full validation.
                for t in blk["transactions"]:
                    new_state.apply_tx(t)
                new_state.apply_rewards(
                    mining.reward_addresses_from_summary(blk["solver_summaries"]))
                validated.append(blk)
                continue
            probe_state = new_state.snapshot()
            ok, err     = block_mod.validate(blk, probe_state, validated, rate_at)
            if not ok:
                return False, f"invalid block at {i}: {err}"
            probe_state.apply_rewards(mining.reward_addresses_from_summary(blk["solver_summaries"]))
            new_state = probe_state
            validated.append(blk)

        # Update mempool: evict txs confirmed in the new chain; re-add any
        # txs that were confirmed on the old fork but are absent from the new
        # chain (they may be valid again).
        old_confirmed = {
            tx_mod.tx_hash(t)
            for blk in self.chain[fork_point:]
            for t in blk.get("transactions", [])
        }
        new_confirmed = {
            tx_mod.tx_hash(t)
            for blk in candidate_chain[fork_point:]
            for t in blk.get("transactions", [])
        }
        # Remove txs now confirmed in the new chain.
        self.mempool.remove_many(new_confirmed)
        # Re-add txs that were on the old fork but not in the new chain.
        for h in old_confirmed - new_confirmed:
            # Reconstruct the tx dict from the old fork blocks.
            for blk in self.chain[fork_point:]:
                for t in blk.get("transactions", []):
                    if tx_mod.tx_hash(t) == h:
                        self.mempool.add(t)

        self.storage.replace_chain(fork_point, candidate_chain[fork_point:])
        self.chain = list(candidate_chain)
        self.state = new_state
        self.storage.save_state(new_state)
        self._publish_view()
        log.info("[%s] height=%d  fork_point=%d", label, len(self.chain) - 1, fork_point)
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
