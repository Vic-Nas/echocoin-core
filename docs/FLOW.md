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
  │     ├─ load blocks from SQLite  (storage.py)
  │     ├─ load state snapshot      (storage.py)
  │     │   └─ or replay chain from genesis if snapshot missing
  │     └─ rebuild BurnWindow from chain  (pob.py)
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
│  drain net_in_q  (tx submissions, inbound txs from peers)           │
│  every 3rd cycle: syncer.check_and_sync()                           │
│    └─ binary-search fork point with peer                            │
│    └─ fetch only the differing tail                                  │
│    └─ node.sync_chain() → validate + apply if peer is ahead         │
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
│  _select_winner(candidate, peer_blocks)                             │
│    for each peer block:                                              │
│      ├─ height and previous_hash match?                              │
│      ├─ block_hash lower than current best?                          │
│      ├─ block.validate()  → hash, parent, timestamp, VDF proof,     │
│      │                       tx ordering, fee rate, balances         │
│      └─ _censorship_score()  → probabilistic check                  │
│           (blocks that repeatedly exclude pending txs               │
│            are rejected with probability 1/effective_age)           │
│                                                                      │
│  _commit(winner)                                                     │
│    ├─ _update_exclusion_ages()                                       │
│    ├─ burn_window.add_block()                                        │
│    ├─ compute block reward  (state.compute_block_reward)             │
│    ├─ burn_window.reward_distribution()  → split among contributors  │
│    ├─ state.apply_reward_distribution()                              │
│    ├─ chain.append(winner)                                           │
│    ├─ storage.save_block()  + storage.save_state()                  │
│    ├─ mempool.remove_many()  (confirmed txs)                         │
│    ├─ publish new NodeView  (atomic ref swap, Flask reads this)      │
│    └─ gossip.broadcast_block()  if winner came from a peer           │
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
                         ▼ (node loop thread picks it up in _drain_queue)
             node.submit_tx()
               ├─ tx.validate()   fields, signature, nonce, fee, balance
               ├─ mempool.add()
               └─ gossip.relay_tx()  → Dandelion stem/fluff relay
                    ├─ stem: POST to one random peer with remaining_hops-1
                    └─ fluff (hops exhausted): broadcast to all peers

Inbound tx from a peer arrives at /api/receive_tx
  └─ api.py queues {type: tx} on net_in_q
  └─ node loop: validate → mempool.add → gossip.relay_tx (dedup via seen-cache)
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
  │   └─ highest height where both chains share the same hash
  ├─ _fetch_chain(from=fork_point)  paginated, 500 blocks/page
  └─ apply_fn(local_prefix + fetched_tail)
       └─ node.sync_chain()
            ├─ node._remote_is_better()
            │   ├─ remote height > local?  → yes
            │   ├─ equal height: compare cumulative PoB score  → lower wins
            │   └─ equal score: compare tip hash  → lower wins
            └─ node._apply_chain()
                 ├─ verify genesis hash
                 ├─ find fork point in local chain
                 ├─ replay shared prefix onto fresh state (trusted, no revalidation)
                 ├─ validate + apply each new block in the tail
                 ├─ reorg mempool: restore old-tail txs not in new tail
                 ├─ storage.replace_chain()
                 └─ rebuild BurnWindow, publish new NodeView
```


## PoB Score and Reward

```
Every block, after the winner is chosen:

burn_window.add_block(winner)
  └─ scans winner's transactions for burn outputs  {to: "burn", amount, beneficiary}
  └─ updates rolling totals: beneficiary → contributor → amount
  └─ expires blocks older than POB_WINDOW (500 blocks, ~17 hours)
     O(1) expiry via paired deques

burn_window.score(tip_hash_int, builder_addr)
  numerator   = tip_hash_int XOR hash(builder_addr)   [as int]
  denominator = max(1, total burns tagged to builder in window)
  score       = numerator / denominator
  → lower score = more committed = higher block-building priority

burn_window.reward_distribution(builder, reward)
  burns = {contributor: amount}  for builder in current window
  each contributor gets:  reward * their_burns / total_burns
  → if builder has no contributors: full reward goes to builder

cumulative_score(chain)  [fork choice]
  = sum of score(builder_i) for every block i > 0
  → lower cumulative score = heavier chain = wins fork
```


## Module Map

```
Pure logic (no I/O, no threads):
  params.py      protocol constants
  crypto.py      keys, signing, address derivation
  tx.py          transaction create / validate / sort
  block.py       block create / validate / assemble
  state.py       balance ledger, emission accounting
  mempool.py     pending tx store
  vdf.py         chiavdf wrapper: evaluate() and verify()
  pob.py         BurnWindow, score, reward distribution

I/O and coordination:
  storage.py     SQLite: blocks, state snapshots, tx/addr index
  gossip.py      HTTP POST broadcasts, Dandelion relay
  peerpool.py    active peer set, strike/cooldown
  syncer.py      periodic chain sync against a random peer
  discovery.py   coordinator: candidate pipeline, peer cache, UPnP
  discovery_dht.py   libtorrent session, BEP44, torrent DHT
  discovery_crawl.py peer-list crawl
  node.py        block cycle orchestrator, NodeView publisher
  api.py         Flask endpoints, web UI
  main.py        entry point, thread startup
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
| Quantum signatures | FALCON-512 (NIST PQC standard) from genesis; no migration needed |
