# Echocoin Flow

A walkthrough of what the node actually does, in the order it does it.
Start here before reading any source file.


## Startup

```
main.py
  │
  ├─ load or generate keypair (crypto.py)
  ├─ decrypt secret key with passphrase → KEK
  ├─ create PeerPool, Gossip, Syncer, Node, Discovery
  │
  ├─ Node.__init__
  │     └─ _load_cs() → ChainState
  │           ├─ storage has blocks + state snapshot?
  │           │     └─ ChainState.from_storage(blocks, snapshot)
  │           │          ├─ load balances/nonces from snapshot
  │           │          └─ rebuild BurnWindow + score from chain
  │           ├─ storage has blocks, no snapshot?
  │           │     └─ ChainState.from_chain(blocks)  (full replay)
  │           └─ empty storage?
  │                 └─ ChainState.from_genesis()  (create + persist genesis)
  │
  ├─ admit --peer CLI addresses immediately (discovery.add_bootstrap_peer)
  ├─ one-shot sync against first peer if pool non-empty
  │
  ├─ start discovery thread  ──────────────────────────────────────────┐
  ├─ start Flask thread (api.py)                                       │
  └─ node.start(kek) → block loop (main thread)                       │
                                                                       ▼
                                                              [Discovery loop]
```


## Block Loop  (node._run_cycle, repeats forever)

```
┌─────────────────────────────────────────────────────────────────────┐
│  _drain_queue()  -- inbound txs and blocks from net_in_q            │
│  every 3rd cycle: syncer.check_and_sync()                           │
│    └─ binary-search fork point, fetch tail, node.apply_better_chain │
│                                                                      │
│  mempool.prune_stale()  (drop txs with expired fee_height)          │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  vdf.evaluate(tip_hash)          ~120 seconds, BLOCKING      │   │
│  │  chiavdf.prove() over 1024-bit imaginary class group         │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  assemble candidate block                                            │
│    ├─ tx.sort_txs()  → ordered by (fee_height, nonce, tx_hash)      │
│    ├─ block.assemble()  → pack txs up to BLOCK_SIZE_LIMIT           │
│    └─ attach vdf_output, vdf_proof, compute block hash              │
│                                                                      │
│  gossip.broadcast_block()  → HTTP POST to all peers                 │
│                                                                      │
│  wait 5 seconds, drain net_in_q  (collect competing peer blocks)    │
│                                                                      │
│  _pick_winner(candidate, peer_blocks)                               │
│    ├─ filter: height and previous_hash must match tip               │
│    ├─ validate each peer block  (hash, VDF proof, txs, balances)    │
│    ├─ censorship check  → probabilistic rejection for repeat        │
│    │   excluders: P(reject) = 1/effective_exclusion_age            │
│    └─ min(all valid blocks, key=hash)  → lowest hash wins           │
│                                                                      │
│  _commit(winner)                                                     │
│    ├─ _update_exclusion_ages()                                       │
│    ├─ self.cs = self.cs.apply_block(winner)                         │
│    │     ├─ copy BurnWindow, add block, apply txs, apply reward     │
│    │     └─ return new ChainState (self.cs not mutated)             │
│    ├─ storage.save_block() + storage.save_state()                   │
│    ├─ mempool.remove_many()  (confirmed txs)                        │
│    ├─ publish new NodeView  (atomic ref swap, Flask reads this)     │
│    └─ gossip.broadcast_block()  if winner came from a peer          │
└─────────────────────────────────────────────────────────────────────┘
```


## Transaction Lifecycle

```
User submits tx via /send form or /api/submit_tx
  │
  ▼
api.py  →  node.submit_tx_from_api()
             └─ queues {type: submit_tx} on net_in_q
             └─ blocks waiting for reply (timeout 5s)
                         │
                         ▼  (node loop picks it up in _drain_queue → _handle)
             node.submit_tx()
               ├─ tx.validate()   fields, signature, nonce, fee, balance
               ├─ mempool.add()
               └─ gossip.relay_tx()  → Dandelion stem/fluff relay
                    ├─ stem: POST to one random peer with remaining_hops-1
                    └─ fluff (hops exhausted): broadcast to all peers

Inbound tx from a peer arrives at /api/receive_tx
  └─ api.py queues {type: tx} on net_in_q
  └─ node loop: _handle → _handle_inbound_tx
       └─ validate → mempool.add → gossip.relay_tx (dedup via seen-cache)
```


## Peer Discovery

Three sources, one pipeline. All feed `discovery.enqueue_candidate(addr)`.

```
BEP44 DHT  (discovery_dht.py)
  ├─ 256 slots, keys derived from genesis_hash + slot_index
  ├─ each node writes its API address to its assigned slot hourly
  └─ dht_mutable_item_alert → enqueue_candidate()

Genesis-hash torrent  (discovery_dht.py)
  ├─ dht_announce(sha1(genesis_hash), api_port)  hourly
  └─ dht_get_peers_reply_alert → enqueue_candidate()

Peer-list crawl  (discovery_crawl.py)
  └─ GET /api/peers from all current pool members every 10 min
     → enqueue_candidate() for each neighbour

receive_block senders  (api.py)
  └─ enqueue_candidate(sender_ip:sender_port) on every inbound block

Every 15 seconds: _flush_candidates()
  ├─ probe each fresh candidate via GET /api/info  (parallel)
  ├─ reject genesis_hash mismatch immediately
  ├─ rank survivors by ascending nomination count
  │   (lightly-nominated = bridge to under-connected parts of network)
  └─ pool.add() in ranked order up to MAX_PEERS
```


## Chain Sync and Reorg

```
syncer.check_and_sync(local_chain, apply_fn)  (called every 3 cycles)
  ├─ pick random peer from pool
  ├─ GET /api/info  → compare remote height
  ├─ if remote not ahead: return False
  ├─ _find_fork_point()  binary search O(log n) round trips
  ├─ _fetch_chain(from=fork_point)  paginated, 500 blocks/page
  └─ apply_fn(local_prefix + fetched_tail)
       └─ node.apply_better_chain(remote_chain)
            ├─ ChainState.from_chain(remote) to get score
            ├─ remote_cs.is_better_than(local_cs)?
            │    ├─ remote height > local?  → yes
            │    ├─ equal height: lower cumulative score wins
            │    └─ equal score: lower tip hash wins
            ├─ _validate_tail(tail, prefix, fee_rate_at)
            │    └─ builds throw-away ChainState, checks each block
            ├─ _reorg_mempool()  → restore displaced txs
            ├─ storage.replace_chain()
            └─ self.cs = remote_cs  (atomic swap)
```


## PoB Score and Reward

```
ChainState.apply_block(blk) called on every commit:

  new_window = burn_window.copy()       ← independent copy, no mutation
  new_window.add_block(blk)
    └─ scan txs for {to: "burn", amount, beneficiary}
    └─ update rolling totals: beneficiary → contributor → amount
    └─ expire blocks older than POB_WINDOW (500 blocks, ~17 hours)
       O(1) expiry via paired deques

  burn_window.score(tip_hash_int, builder_addr)
    numerator   = tip_hash_int XOR hash(builder_addr)   [as int]
    denominator = max(1, total burns to builder in window)
    score       = numerator / denominator
    → lower score = more committed = higher block-building priority

  burn_window.reward_distribution(builder, reward)
    each contributor gets:  reward * their_burns / total_burns_to_builder
    → no contributors: full reward to builder

  cumulative_score  [fork choice, carried on ChainState]
    += score(parent_tip_hash_int, builder) for each new block
    → lower cumulative = heavier chain = wins fork
```


## Module Map

```
Pure logic (no I/O, no threads):
  params.py        protocol constants
  crypto.py        keys, signing (FALCON-512 via liboqs), address derivation
  tx.py            transaction create / validate / sort
  block.py         block create / validate / assemble
  state.py         balance ledger, emission accounting
  mempool.py       pending tx store
  vdf.py           chiavdf wrapper: evaluate() and verify()
  pob.py           BurnWindow, score, reward distribution
  chainstate.py    ChainState: chain + state + burn_window + score as one unit

I/O and coordination:
  storage.py         SQLite: blocks, state snapshots, tx/addr index
  gossip.py          HTTP POST broadcasts, Dandelion relay
  peerpool.py        active peer set, strike/cooldown
  syncer.py          periodic chain sync against a random peer
  discovery.py       coordinator: candidate pipeline, peer cache, UPnP
  discovery_dht.py   libtorrent session, BEP44, torrent DHT
  discovery_crawl.py peer-list crawl
  node.py            block cycle orchestrator, NodeView publisher
  api.py             Flask endpoints, web UI
  main.py            entry point, thread startup
```


## Security Properties (quick reference)

| Threat | Mitigation |
|---|---|
| Double spend / reorg | VDF makes every block sequentially expensive; PoB requires real burns proportional to honest history |
| Botnet Sybil | PoB denominator = 1 for unburned nodes; their cumulative score is always higher than honest burners |
| Eclipse attack | 256 BEP44 slots re-announced hourly; attacker must overwrite all 256 continuously |
| Transaction replay | Per-address nonce; replayed tx has stale nonce, rejected |
| Transaction censorship | Fees burned (no omission incentive); censorship score probabilistically rejects repeated exclusion |
| Fee manipulation / MEV | Fee = size × protocol_rate; ordering is deterministic (fee_height, nonce, hash) |
| Spam | Asymmetric fee formula: sustained full blocks double fees in ~14 blocks |
| Pool centralisation | Burn weight is address-specific and non-transferable; reward split goes directly to contributors |
| Quantum signatures | FALCON-512 (NIST PQC standard) via liboqs from genesis |
