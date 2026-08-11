# PoolCoin Architecture

## Attack Surface Analysis

Each attack is listed with whether it applies to Kern and why.

### 1. Double Spend (51% reorg)
APPLIES. Standard PoW concern. An attacker with >50% hashpower can build a
longer private chain and replace the public one, reversing transactions.
PoolCoin's mitigation: same as Bitcoin (majority-honest assumption). The
proportional reward model removes the economic incentive to centralize
hashpower into pools, making 51% harder to reach in practice.
TEST: reorg replays all blocks from common ancestor, balances rebuild correctly.

### 2. Selfish Mining (solution withholding)
DOES NOT APPLY in the classical sense. In Bitcoin, withholding a block lets
you extend a private fork. In Kern, solutions must be broadcast during the
50-second puzzle phase to count. Withholding a solution means losing reward
share. The assembler is chosen by lowest solution hash (deterministic per node's
local solution set, unique per pubkey). Block content is the union of ALL
solvers' candidate lists. Counts in solver_summaries are tolerance-checked
(+/- SOLUTION_COUNT_TOLERANCE) to handle propagation timing at phase boundary.
TEST: assembler selection is deterministic given solutions. Block missing any
candidate-list tx is rejected.

### 3. Eclipse Attack
APPLIES. Isolating a node lets you feed it a false chain. PoolCoin uses DHT
discovery with 24 minimum peers, but eclipse is a network-layer concern
outside consensus logic.
TEST: (network-layer, not consensus-testable in unit tests). Chain sync
picks longest valid chain; invalid chains are rejected.

### 4. Transaction Replay
DOES NOT APPLY. Nonce increments per-address. A replayed tx has a stale
nonce and is rejected.
TEST: tx with nonce <= current address nonce is rejected.

### 5. Transaction Malleability
DOES NOT APPLY. The tx_hash covers the full signed payload including the
FALCON-512 signature. Altering any field breaks the signature. There is no
signature scheme malleability in FALCON-512 (lattice-based, deterministic
for a given message+key).
TEST: any field change invalidates signature. tx_hash changes if any byte
changes.

### 6. Fee Manipulation / MEV
DOES NOT APPLY. Fees are deterministic (size * protocol_rate). Fees are
burned, not paid to miners. No fee bidding exists. Transaction ordering is
deterministic (fee_height, nonce, tx_hash). The assembler cannot reorder.
TEST: fee != size * rate_at_fee_height is rejected. Block with wrong tx
order is rejected.

### 7. Frontrunning / Sandwich Attack
DOES NOT APPLY. Ordering is deterministic and not under any participant's
control. No participant can insert a tx "before" another; position is
determined by (fee_height, nonce, tx_hash).
TEST: tx ordering is a pure function of the three sort keys.

### 8. Time Warp / Difficulty Manipulation
PARTIALLY APPLIES. PoolCoin does not use timestamps for difficulty. It uses
median solution count over last 100 blocks, clamped 0.5x-2.0x. No timestamp
field means no time warp. But an attacker controlling many identities could
try to inflate solution counts to push difficulty down. The linear reward
model means this gains nothing (same total reward for same total hashpower).
TEST: difficulty adjustment uses correct median. Clamp bounds are enforced.

### 9. Sybil Attack (identity splitting)
DOES NOT APPLY for reward gaming. Reward is proportional to valid solutions
found, which is linear in hashpower. Splitting hashpower across N identities
yields the same expected total reward.
TEST: N identities with H/N hashpower each produce same expected total
solutions as 1 identity with H hashpower (statistical, property-based).

### 10. Nonce Manipulation
DOES NOT APPLY. Nonce must equal current_nonce + 1 for the sender address.
Gaps and repeats are both rejected.
TEST: nonce gap rejected. nonce repeat rejected. nonce = expected accepted.

### 11. Balance Overflow / Underflow
APPLIES if not checked. All amounts must be positive integers. Total outputs
+ fee must not exceed sender balance.
TEST: output amount 0 rejected. output amount negative rejected. total
outputs + fee > balance rejected. balance never goes negative after apply.

### 12. Candidate List Censorship
MITIGATED, live nodes only. Suppressing a tx requires every solver in the
round to omit it. Live nodes enforce that the block contains the union of
all solvers' candidate lists during the assembly phase, when those
candidate lists are still available over the network. A node syncing the
chain after the fact has no access to the round's candidate lists and
cannot re-check this; it can only trust that live enforcement happened.
TEST (live only): block missing a tx from any solver's candidate list is
rejected during assembly. Not a sync-time check.

### 13. Fee Height Staleness
APPLIES if not checked. fee_height must refer to an existing block within
the last 5 blocks. Future fee_height is rejected. Fee must match exactly
the rate at that height.
TEST: fee_height > current height rejected. fee_height < current_height - 5
rejected. fee != size * rate_at(fee_height) rejected.

### 14. Block Size
APPLIES if not checked. Fixed hard ceiling of 10 MB, no retargeting.
TEST: block > 10 MB rejected.

### 15. Assembler Fallback
APPLIES. If the first assembler (lowest solution hash) fails, the next in
order takes over. Invalid block from an assembler is rejected, and the next
assembler is tried.
TEST: assembler order is deterministic. Skip to next on invalid/missing
block.


## Module Design

Principle: each module is a pure-logic black box with no I/O. Controllers
(main.py, node.py, network.py, api.py) wire them together in linear flows.
No module imports another module's internals. All inter-module communication
is via explicit function arguments and return values.

### Pure Logic Modules (auditable in isolation)

```
crypto.py      Keys, signing, verification, address derivation,
               encrypted key file storage. No I/O beyond the key file
               itself. Takes/returns bytes, bools.

tx.py          Transaction creation, serialization, fee computation,
               validation, sort ordering. No I/O. Pure functions on dicts.

block.py       Block creation, validation, serialization, expected
               difficulty/fee-rate computation. No I/O. Pure functions
               on dicts.

state.py       Balance ledger + nonce tracking: get_balance, get_nonce,
               credit, debit, apply_tx, apply_rewards, snapshot, restore.
               In-memory dict operations. No disk I/O.

mempool.py     Pending tx storage, candidate-list generation,
               deterministic assembly (union + sort), inclusion
               validation. No I/O.

mining.py      Puzzle derivation, solution checking, assembler
               selection/ordering, reward computation, difficulty
               adjustment. No I/O. Pure math.

params.py      Protocol constants. No logic, no I/O.
```

### Controller Modules (wire I/O to pure logic)

```
node.py        Orchestrates a block cycle: run the puzzle phase,
               collect network solutions, pick/act as assembler,
               validate and apply the resulting block. Calls pure
               modules in a linear sequence. Also handles chain
               sync and reorg.

storage.py     SQLite persistence for blocks, state snapshots, and
               metadata. All disk I/O for the chain lives here.

network.py     DHT discovery, peer connections, message relay,
               Dandelion routing, chain fetch. All socket/HTTP I/O
               lives here.

templates.py   Base HTML template and large static HTML blocks.
               No logic, no I/O. Imported by api.py.

api.py         HTTP endpoints and the local web UI (dashboard, send,
               explorer, address lookup, whitepaper viewer). Thin
               wrapper that calls node/network and renders JSON or HTML.

main.py        Entry point. Loads/creates the key, starts networking,
               the API server, and the node's block-cycle loop.
```

### Data Flow (one block cycle, in `node.py`)

`_run_cycle` is the top-level sequencer; each step is a named method:

```
_wait_for_cycle_boundary()   -- sleep to wall-clock boundary, drain queue
_flush_stale_queue()         -- discard leftover prior-round messages
_setup_round()               -- read tip, compute difficulty/fee_rate/puzzle
_puzzle_phase()              -- mine + collect peer solutions for 50s
  mining.check_solution()      per nonce
  network.broadcast_solution() on each find
  on new_peer: sync_chain() + _reset_round_state() (clears all accumulators)
_assembly_phase()            -- produce or receive a valid block
  mining.assembler_order()     deterministic order from local solution set
  _get_candidate()             build or wait for assembler's block per slot
  _validate_candidate()        block.validate + verify_summary_addresses
                               + compute_achievable_required_set
_commit()                    -- apply rewards, persist, broadcast
  state.apply_rewards()
  storage.save_block() + save_state()
  network.broadcast_block()
```

### Module logger names

All modules use the `pc.*` hierarchy so log level can be controlled in one place:

```
pc.main      startup, shutdown
pc.node      cycle phases, block commits, sync/reorg
pc.network   peer connections, DHT, broadcasts
pc.storage   (silent by default; errors surface as exceptions)
pc.api       (silent by default)
```

Set `logging.getLogger("pc").setLevel(logging.DEBUG)` in main.py for
full per-solution and per-peer detail during development.
