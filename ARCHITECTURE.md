# Echocoin Architecture

## Attack Surface Analysis

### 1. Double Spend (competing chain)
APPLIES. An attacker wishing to rewrite history must recompute the VDF
sequentially for every block from the fork point to the present. Since VDF
evaluation cannot be parallelized, this takes exactly as long as the honest
network took in real time. An active honest network is always ahead.
TEST: reorg replays all blocks from common ancestor, balances rebuild correctly.

### 2. Selfish Mining / Block Withholding
DOES NOT APPLY. There is no puzzle to solve privately. The VDF runs
continuously on the chain tip. Withholding a completed block means losing
the slot to any peer who finishes at the same time; there is no benefit.
Fork resolution is by lower block hash, which neither party controls.

### 3. Eclipse Attack
APPLIES. Isolating a node lets you feed it a false chain. Echocoin uses
BEP44 DHT discovery with 256 deterministic slots derived from the genesis
hash. An attacker must overwrite all 256 slots continuously (at least once
per hour) to prevent honest nodes from reclaiming them on re-announcement.
TEST: chain sync picks longest valid chain; invalid chains are rejected.
Genesis hash mismatch causes immediate peer rejection.

### 4. Transaction Replay
DOES NOT APPLY. Nonce increments per-address. A replayed tx has a stale
nonce and is rejected.
TEST: tx with nonce <= current address nonce is rejected.

### 5. Transaction Malleability
DOES NOT APPLY. tx_hash covers the full signed payload including the
FALCON-512 signature. Altering any field breaks the signature. FALCON-512
is deterministic for a given message + key.
TEST: any field change invalidates signature.

### 6. Fee Manipulation / MEV
DOES NOT APPLY. Fees are protocol-determined (size * fee_rate). Fees are
burned entirely -- no fee income for builders. Transaction ordering is
deterministic (fee_height, nonce, tx_hash). No participant controls ordering.
TEST: fee != size * rate_at_fee_height is rejected. Wrong tx order is rejected.

### 7. Frontrunning / Sandwich Attack
DOES NOT APPLY. Ordering is a pure function of (fee_height, nonce, tx_hash).
No participant can insert a tx before another.

### 8. Time Manipulation
DOES NOT APPLY. Block timing is enforced by the VDF, not wall clocks.
A block timestamp must exceed its parent's by at least BLOCK_CYCLE_SECONDS.
No difficulty parameter is influenced by timestamps.
TEST: block timestamp < parent + BLOCK_CYCLE_SECONDS is rejected.

### 9. Transaction Censorship
MITIGATED. Builder gains nothing from excluding transactions (fees are
burned). Sustained exclusion requires winning every relevant slot
indefinitely. The censorship scoring formula probabilistically rejects
blocks that repeatedly exclude pending transactions from non-full blocks,
with acceptance probability = 1/effective_age. A node that rejects a block
syncs unconditionally when a longer chain arrives (at most one block lag).
TEST: block excluding a tx for the Nth consecutive non-full block is
rejected with probability 1 - 1/N.

### 10. Nonce Manipulation
DOES NOT APPLY. Nonce must equal current_nonce + 1. Gaps and repeats rejected.
TEST: nonce gap rejected. nonce repeat rejected. correct nonce accepted.

### 11. Balance Overflow / Underflow
APPLIES if not checked. All amounts must be positive integers. Total outputs
+ fee must not exceed sender balance. State.debit() raises on underflow.
TEST: outputs + fee > balance rejected. balance never negative after apply.

### 12. Fee Height Staleness
APPLIES if not checked. fee_height must reference an existing block within
the last FEE_HEIGHT_MAX_AGE blocks. Fee must match exactly the rate at that
height.
TEST: fee_height > current height rejected. fee_height too old rejected.
fee != size * rate_at(fee_height) rejected.

### 13. Block Size
APPLIES if not checked. Hard ceiling of 10 MB (BLOCK_SIZE_LIMIT). No
retargeting of the cap; cap is raised only by network upgrade.
TEST: block > 10 MB rejected.

### 14. Spam via Fee Pressure
MITIGATED by asymmetric fee formula. Spam fills blocks, pushing median
volume above the 200 KB soft target. Fee rate rises up to 5% per block.
Sustained full blocks for 14 blocks (~28 minutes) doubles fees. Rate decays
at 0.999/block at zero activity -- takes hours to return to baseline.
No hardcoded floor above 1 ring/byte.

### 15. VDF Proof Forgery
DOES NOT APPLY if chiavdf is correct. verify_n_wesolowski() checks the
Wesolowski proof in milliseconds. A forged proof would require solving the
VDF faster than sequential evaluation, which is the hardness assumption.
TEST: block with verify=False rejected. Block with missing VDF fields rejected.

### 16. History Rewriting (offline honest network)
PARTIALLY APPLIES. Unlike Bitcoin, accumulated VDF work is not
progressively harder -- each block costs exactly one sequential VDF
computation regardless of age. An attacker who has been running nodes since
genesis and controls the BEP44 discovery slots can present a longer
alternative chain to new nodes syncing for the first time. Mitigation:
BEP44 slot control requires continuous infrastructure cost (re-announcing
256 slots hourly), and the genesis hash is hardcoded in every binary.


## Module Design

Principle: pure-logic modules have no I/O. Controller modules wire I/O
to pure logic. No module imports another module's internals.

### Pure Logic Modules

```
crypto.py      Keys, signing, verification, address derivation,
               encrypted key file storage. No network I/O.

tx.py          Transaction creation, fee computation, validation,
               sort ordering. Pure functions on dicts.

block.py       Block creation, validation, fee rate computation,
               block assembly. Pure functions on dicts. Calls
               vdf.verify() for proof validation.

state.py       Balance ledger and nonce tracking. Emission accounting
               (total_minted, total_burnt). compute_block_reward()
               implements the exponential decay formula. In-memory only.

mempool.py     Pending tx storage, candidate list, pruning. No I/O.

vdf.py         Thin wrapper around chiavdf. evaluate() blocks ~120s.
               verify() returns in milliseconds. Knows nothing about
               blocks or the chain.

params.py      Protocol constants. No logic, no I/O.
```

### Controller Modules

```
node.py        Orchestrates the block cycle: evaluate VDF, assemble
               block, collect competing peer blocks, commit winner.
               Handles chain sync and reorg. Publishes NodeView for
               Flask threads (single atomic reference swap, no locks).

storage.py     SQLite persistence for blocks, state snapshots, and
               emission counters. All disk I/O lives here.

gossip.py      Outbound block and tx broadcasts. Dandelion stem/fluff
               routing for tx privacy. All HTTP POST I/O lives here.

discovery.py   BEP44 DHT peer discovery. 256 deterministic slots
               derived from genesis hash. Hourly re-announcement.

peerpool.py    Active peer set. Strike/cooldown on invalid data.

syncer.py      Periodic chain sync against a random peer.

templates.py   Base HTML template. No logic, no I/O.

api.py         HTTP endpoints and web UI. Thin wrapper over node.
               Rate-limited inbound endpoints for blocks and txs.

main.py        Entry point. Loads key, starts threads, runs node loop.
```

### Data Flow (one block cycle, in `node.py._run_cycle`)

```
_drain_queue()              drain pending network messages
syncer.check_and_sync()     periodic peer sync (every N cycles)
mempool.prune_stale()       drop txs with stale fee_height
vdf.evaluate(tip_hash)      ~120s sequential computation [BLOCKING]
block.assemble()            pack txs into candidate block
gossip.broadcast_block()    send to peers immediately
_drain_queue(5s window)     collect any competing peer blocks
  for each peer block:
    block.validate()        structural + VDF proof check
    _censorship_score()     probabilistic acceptance
    pick lower block hash   fork resolution
_commit(winner)             apply txs, apply reward, persist, broadcast
```

### Module Logger Names

All modules use the `ec.*` hierarchy:

```
ec.main        startup, shutdown
ec.node        cycle phases, block commits, sync/reorg
ec.gossip      peer broadcasts, Dandelion routing
ec.discovery   DHT, peer announcements
ec.storage     (silent by default)
ec.api         (silent by default)
```

Set `logging.getLogger("ec").setLevel(logging.DEBUG)` for full detail.
