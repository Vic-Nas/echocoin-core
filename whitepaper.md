# Echocoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its proof-of-work mechanism, however, wastes energy to solve an artificial puzzle whose only purpose is to make history rewriting expensive. Echocoin replaces proof-of-work with two complementary mechanisms: a Verifiable Delay Function (VDF) that anchors the chain to real elapsed time, and Proof-of-Burn (PoB) that anchors block-building rights to real economic commitment. Block production is open to every node. The full reward goes to whoever builds the accepted block. Pooling offers no structural advantage because the work being rewarded is network service itself, and burn weight is non-transferable.

## 1. The Problem with Bitcoin Mining

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of launch.

This is not fixable by changing the puzzle. ASIC-resistant algorithms slow the hardware race but cannot stop it, because the problem is structural: proof-of-work rewards hardware investment, not network contribution. The work itself is intentionally wasteful, consuming energy without producing anything of value to the network.

A subtler problem is the botnet attack: proof-of-work with a weak puzzle, or a VDF alone without economic weight, can be overwhelmed by an adversary who spins up thousands of parallel instances at near-zero marginal cost. Echocoin closes this gap with Proof-of-Burn. A botnet that refuses to destroy real coins cannot produce competitive block scores, regardless of instance count.

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

**Burn transactions.** Any output with recipient `burn` is an intentional Proof-of-Burn output. The rings are permanently destroyed and credited to the same mintable pool as fee burns. Unlike fees, intentional burns are recorded per-sender in the chain history and count toward the sender's block-building score (Section 3). A single transaction may contain any mix of normal outputs and burn outputs.

## 3. Block Production and Timing

Every node continuously assembles candidate blocks from its local mempool and broadcasts them to peers. There is no designated assembler and no artificial barrier to participation.

**The chain is its own clock.** Block timing is enforced not by wall clock but by a Verifiable Delay Function (VDF). Each block must include a valid VDF proof computed over the previous block's hash. The VDF is tuned to take approximately 120 seconds of sequential computation on commodity hardware. Because VDF evaluation is strictly sequential, no amount of parallelism accelerates it. The chain advances at real elapsed time.

When a node completes the VDF for the current slot, it assembles its best candidate block, attaches the VDF proof, and broadcasts immediately. Other nodes verify the proof in milliseconds -- verification is fast even though computation is slow -- and accept the block if valid.

**Proof-of-Burn block score.** Every builder has a score derived from their recent burn history:

```
numerator   = Hash(VDF_output_of_tip XOR builder_address_hash)
denominator = max(1, sum of intentional burns by builder in last N blocks)

score(builder) = numerator / denominator
```

Lower score means more economic commitment and higher block-building priority. The numerator is deterministic and unguessable before the VDF completes, so builders cannot pre-select outputs to manipulate their score. The denominator counts only burns within the last N = 500 blocks (~17 hours at 2 min/block), so older burns decay out of influence and no single early burn confers permanent dominance.

A builder who has never burned any coins receives `denominator = 1`, making their score equal to the raw hash -- large and uncompetitive against active burners, but not zero. New participants can join and start building immediately; they just score poorly until they commit coins.

**Fork resolution.** Two nodes may complete the VDF at roughly the same time and broadcast competing blocks for the same slot. Both blocks may be structurally valid. Nodes select the block whose builder has the lower PoB score. This is deterministic and locally computable without coordination. If scores are equal (hash coincidence), the lower block hash breaks the tie.

When two valid chains of equal height compete, nodes adopt the one with the lower **cumulative score** -- the sum of all block scores from genesis:

```
cumulative_score(chain) = sum of score(builder_i) for all blocks i > 0
```

A chain built by nodes with real burn commitments will always have a lower cumulative score than one built by a botnet whose denominator stays at 1. The attacker's chain is objectively and locally rejectable, with no voting or peer trust required.

**History rewriting.** An attacker wishing to rewrite block N must recompute the VDF sequentially for every block from N to the present -- that takes as long in real time as the honest network took. They must also produce competitive PoB scores for each rewritten block, which requires burning real coins proportional to the honest network's burn history. Both constraints must be overcome simultaneously.

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

If no transactions are missing, block_score is 1.0 and the block is always accepted. Age 0 is treated as a special case (score 1.0) because a transaction can miss one block due to propagation timing alone; the formula only activates after repeated exclusion. Effective age only increments on non-full blocks, so congestion does not incorrectly penalize honest builders.

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

`can_mint` starts at the full supply cap and decreases as coins are minted. Both fee burns and intentional PoB burns are added back into `can_mint`, so network activity -- whether transactional or burn-based -- replenishes the mintable pool and sustains block rewards indefinitely. At low usage, emission decays smoothly toward zero. At high usage, burns partially offset emission, keeping net supply stable.

The full block reward goes to the node that built the accepted block. Because block-building priority is determined by burn history rather than hardware, running more nodes does not increase a participant's share. Burn weight is personal and non-transferable: a pool operator cannot aggregate the burn scores of contributing members. The pooling incentive that centralized Bitcoin mining does not exist here.

## 6. Privacy and Security

Transactions propagate via Dandelion routing: a random stem path before full broadcast, making the origin node indeterminate.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Migration risk is eliminated by building in quantum resistance from the start.

Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors and familiar to anyone who has used a BIP39 seed phrase.

## 7. Network and History

Nodes discover peers through the BitTorrent mainline DHT using BEP44 mutable items. The 256 discovery slots are deterministic and fixed for the lifetime of the chain: each slot's signing key is derived from the genesis hash and the slot index, so every node computes the same keys independently. Each node claims one slot (derived from its own public key) and re-announces its address to that slot every hour. An attacker trying to displace honest peers must continuously overwrite all 256 slots -- at least once per hour, indefinitely -- or honest nodes will simply re-announce and reclaim their positions on the next cycle.

When no UPnP gateway is available, nodes query a public IP service to determine their externally routable address before announcing to the DHT. This ensures nodes behind NAT, mobile hotspots, or restrictive firewalls advertise a reachable address.

When a candidate peer is found, the connecting node fetches its `/api/info` and checks that the genesis hash matches. Any peer on a different chain is rejected immediately without fetching chain data.

The full block history is stored permanently. Balance state is always recoverable by replaying from genesis. The genesis block hash is hardcoded, so any chain with a different block 0 is rejected outright.

## 8. Security Analysis

**Competing chain against an active network.** A botnet that attempts to build an alternative chain without burning real coins produces blocks with `denominator = 1` and therefore very large scores. The honest network, whose participants have accumulated burn weight over 500-block windows, produces blocks with far lower scores. The honest chain's cumulative score is always lower, and every node independently rejects the botnet's chain without coordination.

**Majority VDF attack.** VDF sequentiality means a majority of nodes produce blocks at the same rate as a single honest node. Acquiring a majority of sequential compute does not help an attacker rewrite history faster. Combined with PoB, any rewritten chain also requires proportional real burns, making the attack doubly costly.

**Transaction censorship.** Even a majority of burners only wins roughly that fraction of slots by score lottery. The honest minority wins the remaining slots and includes censored transactions. The probabilistic acceptance mechanism further penalizes repeated exclusion.

**Deep history rewriting offline.** VDF makes all history equally costly to rewrite per block. An attacker working offline against an old fork point must recompute one VDF per block sequentially, plus produce competitive burns for each rewritten block -- an insurmountable cost for any chain of meaningful length.

**Whale dominance and the decay window.** A participant who burns a large amount early cannot hold a permanent advantage. Burns older than 500 blocks (~17 hours) fall out of the scoring window. Sustained block-building priority requires sustained burning, aligning incentives with ongoing network participation rather than one-time capital expenditure.

**Comparison with Bitcoin.** Echocoin and Bitcoin share the same unsolved problem: a new node syncing from scratch cannot cryptographically distinguish the legitimate chain from an attacker's alternative. Both rely ultimately on the attacker having no economic incentive to destroy the coin's value. Bitcoin is additionally vulnerable to majority hashpower and ASIC centralization. Echocoin eliminates the hardware arms race but introduces a different capital commitment: coins must be continuously burned to maintain block-building priority. This is a feature, not a cost: the burned coins return to the mintable pool and sustain future rewards.

## 9. Conclusion

Echocoin inherits Bitcoin's guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function that anchors the chain to real elapsed time and Proof-of-Burn that anchors block-building rights to real economic commitment. Supply is bounded by a 21 million ECH cap with smooth exponential decay over a 20-year half-life, sustained indefinitely by fee and intentional burns recycled into future emission. No halvings, no fee manipulation incentive, no energy waste, no pool advantage, no botnet vulnerability.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. I. Stewart, "Proof of Burn," 2012. https://en.bitcoin.it/wiki/Proof_of_burn
