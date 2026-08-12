# Echocoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its proof-of-work mechanism, however, wastes energy to solve an artificial puzzle whose only purpose is to make history rewriting expensive. Echocoin replaces proof-of-work with a Verifiable Delay Function that anchors the chain to real elapsed time without burning energy. Block production is open to every node. The full reward goes to whoever builds the accepted block. Pooling offers no structural advantage because the work being rewarded is network service itself.

## 1. The Problem with Bitcoin Mining

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of launch.

This is not fixable by changing the puzzle. ASIC-resistant algorithms slow the hardware race but cannot stop it, because the problem is structural: proof-of-work rewards hardware investment, not network contribution. The work itself is intentionally wasteful, consuming energy without producing anything of value to the network.

## 2. Transactions

The base unit is the **ring**. One ECH equals 100,000,000 rings, the same precision as Bitcoin. All amounts are integers in rings.

Fees are set by the protocol, not chosen by the sender:

```
fee = transaction_size_bytes * protocol_rate
```

The rate adjusts asymmetrically every block from the median transaction byte volume across the last 100 blocks:

```
if median_vol == 0:
    adjustment = 0.999          # nearly frozen at zero activity
elif vol_ratio > 1:
    adjustment = min(1.05, vol_ratio)   # rises up to 5% per block when above target
else:
    adjustment = max(0.999, vol_ratio ** 0.1)  # falls very slowly below target

fee_rate = max(1, int(current_rate * adjustment))
```

where `vol_ratio = median_vol / BLOCK_SIZE_TARGET`. The soft target is 200 KB. The asymmetry is the spam deterrent: a sustained attack that fills blocks doubles fees in roughly 14 blocks (28 minutes) and those fees take hours to decay back. No floor is hardcoded above 1 ring per byte; fee pressure is the only constraint on block fullness.

All fees are burned, reducing circulating supply. Burnt fees are added back into the mintable pool, so high network usage both deters spam and sustains block rewards indefinitely.

## 3. Block Production and Timing

Every node continuously assembles candidate blocks from its local mempool and broadcasts them to peers. There is no designated assembler and no artificial barrier to participation.

**The chain is its own clock.** Block timing is enforced not by wall clock but by a Verifiable Delay Function (VDF). Each block must include a valid VDF proof computed over the previous block's hash. The VDF is tuned to take approximately 120 seconds of sequential computation on commodity hardware. Because VDF evaluation is strictly sequential, no amount of parallelism accelerates it. The chain advances at real elapsed time.

When a node completes the VDF for the current slot, it assembles its best candidate block, attaches the VDF proof, and broadcasts immediately. Other nodes verify the proof in milliseconds -- verification is fast even though computation is slow -- and accept the block if valid.

**Fork resolution.** Two nodes may complete the VDF at roughly the same time and broadcast competing blocks for the same slot. When this happens, both blocks are valid. Nodes keep whichever has the lower block hash, which is deterministic and locally computable without coordination. The fork resolves naturally when the next slot's VDF is computed over one of the two competing hashes: whoever builds on top first determines which branch extends, and the other dies.

**History rewriting.** An attacker wishing to rewrite block N must recompute the VDF sequentially for every block from N to the present. Since VDF evaluation cannot be parallelized or accelerated by hardware, this takes exactly as long as the honest network took to produce those blocks in real time. An active honest network is always ahead. History rewriting is not merely expensive -- it is impossible faster than real elapsed time.

Every valid block received from a peer is immediately rebroadcast to all other peers. This ensures that nodes behind NAT or with limited connectivity participate in propagation through well-connected peers.

## 4. Transaction Censorship Resistance

Excluding transactions is nearly costless in Echocoin: fees are burned, so builders gain nothing from omitting them. The only motive is malice, which requires sustained coordination across multiple builders over multiple blocks.

Block acceptance is probabilistic based on how long unincluded transactions have been waiting:

```
effective_age(T) = number of non-full blocks since T first appeared that excluded T
score(T) = 1.0                   if effective_age(T) == 0   (first miss: timing noise)
           1 / effective_age(T)  otherwise
block_score = min(score(T) for all missing transactions T)
node accepts block with probability = block_score
```

If no transactions are missing, block_score is 1.0 and the block is always accepted. Age 0 is treated as a special case (score 1.0) because a transaction can miss one block due to propagation timing alone; the formula only activates after repeated exclusion. The formula is lenient by design: missing a transaction across five non-full blocks is a strong signal of censorship (score 0.2). Effective age only increments on non-full blocks, so congestion does not incorrectly penalize honest builders.

A node that probabilistically rejects a block the rest of the network accepted will sync unconditionally when it receives a longer chain. One block lag at most. No permanent fork.

## 5. Incentive

The block reward is not fixed. It is derived from the current mintable supply:

```
SUPPLY_CAP        = 21,000,000 ECH
EMISSION_HALFLIFE = 5,000,000 blocks  (~20 years at 2 minutes per block)
EMISSION_RATE     = 0.5 ^ (1 / EMISSION_HALFLIFE)

can_mint          = SUPPLY_CAP - total_minted + total_burnt
reward(block)     = int(can_mint * (1 - EMISSION_RATE))
```

`can_mint` starts at the full supply cap and decreases as coins are minted. Burnt fees are added back into `can_mint`, so high network usage replenishes the mintable pool and sustains block rewards indefinitely. At low usage, emission decays smoothly toward zero. At high usage, burns partially offset emission, keeping net supply stable.

The full block reward goes to the node that built the accepted block. Because the work being rewarded is network service rather than computation, running more nodes genuinely increases contribution and is rewarded proportionally. If nodes leave, each remaining node wins blocks more frequently. Lower prices attract new entrants. The network stabilizes without any parameter adjustment.

## 6. Privacy and Security

Transactions propagate via Dandelion routing: a random stem path before full broadcast, making the origin node indeterminate.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Migration risk is eliminated by building in quantum resistance from the start.

Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors and familiar to anyone who has used a BIP39 seed phrase.

## 7. Network and History

Nodes discover peers through the BitTorrent mainline DHT using BEP44 mutable items. The 256 discovery slots are deterministic and fixed for the lifetime of the chain: each slot's signing key is derived from the genesis hash and the slot index, so every node computes the same keys independently. Each node claims one slot (derived from its own public key) and re-announces its address to that slot every hour. An attacker trying to displace honest peers must continuously overwrite all 256 slots -- at least once per hour, indefinitely -- or honest nodes will simply re-announce and reclaim their positions on the next cycle.

When no UPnP gateway is available, nodes query a public IP service to determine their externally routable address before announcing to the DHT. This ensures nodes behind NAT, mobile hotspots, or restrictive firewalls advertise a reachable address.

When a candidate peer is found, the connecting node fetches its `/api/info` and checks that the genesis hash matches. Any peer on a different chain is rejected immediately without fetching chain data.

The full block history is stored permanently. Balance state is always recoverable by replaying from genesis. The genesis block hash is hardcoded, so any chain with a different block 0 is rejected outright.

## 8. Security Comparison with Bitcoin

Echocoin and Bitcoin share the same unsolved problem: a new node syncing from scratch cannot cryptographically distinguish the legitimate chain from an attacker's alternative. Both rely ultimately on the attacker having no economic incentive to destroy the coin's value.

**Competing chain against an active network.** Bitcoin is vulnerable above 51% hashpower. Echocoin is not vulnerable by speed: VDF sequentiality means a majority of nodes produce blocks at the same rate as a single honest node. The fork persists but never overtakes.

**Transaction censorship.** Bitcoin above 51% hashpower can exclude transactions indefinitely. In Echocoin, even a majority of nodes only wins roughly that fraction of slots by hash lottery. The honest minority wins the remaining slots and includes censored transactions.

**Deep history rewriting with the honest network offline.** This is where Bitcoin is stronger. Accumulated proof-of-work makes old Bitcoin blocks progressively harder to rewrite. In Echocoin, VDF makes all history equally costly to rewrite: exactly one sequential VDF computation per block, regardless of age.

## 9. Conclusion

Echocoin inherits Bitcoin's guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function that anchors the chain to real elapsed time without burning energy. Supply is bounded by a 21 million ECH cap with smooth exponential decay over a 20-year half-life, sustained indefinitely by fee burns recycled into future emission. No halvings, no fee manipulation incentive, no energy waste, no pool advantage.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
