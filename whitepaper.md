# PoolCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its winner-takes-all reward model, however, created the pool centralization its designers hoped to prevent. PoolCoin fixes this at the incentive level: every node that solves a puzzle earns a share of every block reward. There is nothing a pool operator can offer that the protocol does not already provide.

## 1. The Problem with Bitcoin Mining

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of launch.

This is not fixable by changing the puzzle. ASIC-resistant algorithms slow the hardware race but cannot stop it, because the problem is structural: winner-takes-all always produces centralization regardless of the puzzle used.

## 2. Transactions

The base unit is the **seed**. One PC equals 100,000,000 seeds, the same precision as Bitcoin. All amounts are integers in seeds.

Fees are set by the protocol, not chosen by the sender:

```
fee = transaction_size_bytes * protocol_rate
```

The rate retargets every block from the median transaction byte volume across the last 100 blocks. All nodes independently compute the same rate from the same history. No bidding, no mempool games. All fees are burned, so miners have no incentive to manipulate ordering or inflate fees.

## 3. Proof of Work

Each 2-minute cycle has two phases.

**Puzzle phase (60 seconds).** Every node derives a puzzle unique to itself:

```
puzzle   = sha256(previous_block_hash + node_public_key)
solution = sha256(puzzle + nonce) < difficulty_target
```

Because the public key is baked into the puzzle, solutions cannot be reassigned to a different payout. Compute cannot be pooled. A faster machine finds more solutions and earns proportionally more. Every valid solution is broadcast to peers immediately.

**Build phase (60 seconds).** Every node that found at least one solution assembles and broadcasts its own block from its local mempool. No designated assembler and no fallback sequence exist. When two valid blocks arrive at the same height, the one with the lower block hash wins. This is deterministic and locally computable: every node arrives at the same answer without any coordination. Longer chain resolves anything beyond one block.

During the puzzle phase every valid solution is broadcast immediately. Each assembling node collects both its own solutions and its peers', so the accepted block carries a summary of all observed solvers. The block reward is split among all solvers in proportion to their solution count, regardless of who built the accepted block.

Difficulty adjusts so the median solutions per block stays near a fixed target, keeping payouts frequent for small nodes.

## 4. Transaction Censorship Resistance

Excluding transactions is nearly costless in PoolCoin: fees are burned, so builders gain nothing from omitting them. The only motive is malice, which requires sustained coordination across multiple builders over multiple blocks.

Block acceptance is probabilistic based on how long unincluded transactions have been waiting:

```
effective_age(T) = number of non-full blocks since T first appeared that excluded T
score(T) = 1.0              if effective_age(T) == 0   (first miss: timing noise)
           1 / effective_age(T)  otherwise
block_score = min(score(T) for all missing transactions T)
node accepts block with probability = block_score
```

If no transactions are missing, block_score is 1.0 and the block is always accepted. Age 0 is treated as a special case (score 1.0) because a transaction can miss one block due to propagation timing alone; the formula only activates after repeated exclusion. The formula is lenient by design: missing a transaction across five non-full blocks is a strong signal of censorship (score 0.2). Effective age only increments on non-full blocks, so congestion does not incorrectly penalize honest builders.

A node that probabilistically rejects a block the rest of the network accepted will sync unconditionally when it receives a longer chain. One block lag at most. No permanent fork.

## 5. Incentive

The block reward is 10 PC per block with no halving and no hard supply cap. Tail emission keeps mining incentivized indefinitely.

At low usage, supply grows gently. At high usage, burned fees can exceed emission and supply contracts. The coin becomes scarcer as it becomes more useful, without any scheduled supply shock. The net emission rate (newly minted coins minus fees burned that block) is always visible in the node dashboard and the `/api/stats` endpoint, so the market has a real-time signal of supply pressure.

Reward is proportional to hashpower, not identity count. Splitting hardware across many identities yields the same total reward as running it as one. There is no incentive to create fake identities.

If nodes leave, the remaining nodes earn more per block. Lower prices attract new entrants. The network stabilizes without any parameter adjustment.

## 6. Privacy and Security

Transactions propagate via Dandelion routing: a random stem path before full broadcast, making the origin node indeterminate.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Migration risk is eliminated by building in quantum resistance from the start.

Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors and familiar to anyone who has used a BIP39 seed phrase.

## 7. Network and History

Nodes discover peers through the BitTorrent mainline DHT using BEP44 mutable items. The 256 discovery slots are deterministic and fixed for the lifetime of the chain: each slot's signing key is derived from the genesis hash and the slot index, so every node computes the same keys independently. Each node claims one slot (derived from its own public key) and re-announces its address to that slot every hour. An attacker trying to displace honest peers must continuously overwrite all 256 slots -- at least once per hour, indefinitely -- or honest nodes will simply re-announce and reclaim their positions on the next cycle.

When a candidate peer is found, the connecting node fetches its `/api/info` and checks that the genesis hash matches. Any peer on a different chain is rejected immediately without fetching chain data.

Every block carries a timestamp. Validation enforces that each block's timestamp is at least two minutes after its parent's, making block interval a protocol rule rather than a local convention. A block timestamped more than 30 seconds in the future is also rejected, providing a small clock-skew tolerance.

The full block history is stored permanently. Balance state is always recoverable by replaying from genesis. The genesis block hash is hardcoded, so any chain with a different block 0 is rejected outright.

## 8. Conclusion

PoolCoin inherits Bitcoin's guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It corrects the structural flaw that caused Bitcoin's mining ecosystem to centralize.

Every participant is rewarded for every block. Fees are deterministic and burned. The codebase fits in a single reading session.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
