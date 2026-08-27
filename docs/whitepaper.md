# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its proof-of-work mechanism wastes energy on a puzzle whose only purpose is to make history rewriting expensive. LapseCoin replaces proof-of-work with two complementary mechanisms: a Verifiable Delay Function (VDF) that anchors the chain to real elapsed time, and Proof-of-Burn (PoB) that ties block rewards to real economic commitment. The result is a battle-tested Bitcoin-like consensus model (most cumulative proven work wins, first valid block received) with an incentive layer that rewards long-term participants who burn coins and earn back a proportional share of every block's reward.

## 1. The Problem with Proof-of-Work

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of Bitcoin's launch.

ASIC-resistant algorithms slow the hardware race but cannot stop it: proof-of-work rewards hardware investment, not network participation. The work itself is intentionally wasteful.

A VDF alone without economic weight opens a different attack: an adversary who spins up thousands of parallel instances at near-zero marginal cost can overwhelm the honest network. Proof-of-Burn closes this gap. A botnet that refuses to destroy real coins earns no share of rewards, removing the economic incentive for such attacks.

## 2. Transactions

The base unit is the **tick**. One LAPSE equals 100,000,000 ticks, matching Bitcoin's precision. All amounts are integers in ticks.

Fees are set by the protocol, not the sender: `fee = size_bytes × rate`. The rate adjusts every block against a 200 KB soft target, based on the median transaction volume over the last 100 blocks. It rises quickly under load, up to 5% per block, and falls very slowly below target, so a sustained spam attack doubles fees in about 28 minutes while recovery takes hours. Fees are collected by the block builder (the node that produced the block), not burned.

**Intentional burns.** Any output addressed to `burn` is a Proof-of-Burn output. The ticks are permanently destroyed and added back into the mintable emission pool (Section 5). Intentional burns are recorded per sender over a rolling window of the last 500 blocks, and count toward the sender's proportional share of every future block reward within that window. A single transaction may mix normal outputs and burn outputs.

## 3. Block Production

Every node assembles candidate blocks from its local mempool and broadcasts them to peers. There is no designated block producer and no barrier to participation.

**Timing.** Each block must include a valid VDF proof computed over `sha256(previous_block_hash || builder_address)`. The VDF targets approximately 120 seconds of sequential computation. Because VDF evaluation is strictly sequential, no amount of parallelism shortens it. A faster node finishes sooner and broadcasts immediately; fork choice resolves any competition. Block timestamps are checked only against the near future (more than 30 seconds ahead is rejected); the VDF proof is the sole enforcer of elapsed time. As hardware improves, the iteration count adjusts upward, never downward, to keep average block time near the target.

Binding the builder address into the challenge is what stops a VDF output from being a bearer token. Were the challenge the previous hash alone, every builder would evaluate the same VDF, so any node receiving a broadcast block could keep its proof, substitute its own builder address, and rebroadcast a block that verifies identically. The node that spent the 120 seconds would lose the reward to one that spent nothing. With the builder folded in, each builder evaluates a distinct VDF and a copied proof verifies against nobody else's challenge.

The transaction list is deliberately excluded from the challenge. A proof therefore stays valid under any transaction list, so a block rejected for its contents can be corrected and rebroadcast against the same proof rather than costing the network a fresh 120 seconds.

**Block selection.** When a node receives a valid block from a peer, it accepts it immediately as the new tip if it extends the current best chain. A node's own locally-assembled candidate is displaced by any valid peer block that arrives first. There is no scoring race per-slot: the first valid block a node sees wins.

**Fork choice.** When two valid chains compete, nodes prefer the chain with the greater cumulative sum of `vdf_iterations` actually proven across its blocks, not raw block count. A block's `vdf_iterations` is only accepted if its VDF proof verifies for that many iterations, so this sum reflects real, cryptographically-proven sequential work and cannot be inflated by a self-report. Raw height is not used, because each fork's own difficulty-adjustment history is derived only from its own block timestamps: an attacker who pads their own blocks' timestamps toward the near-future bound could otherwise keep their fork's required iteration count artificially low, letting them build a same-height or taller fork in less real time than the honest chain took. Weighing cumulative proven iterations closes that gap, the same way Bitcoin's cumulative-work rule prevents a chain of easier blocks from outweighing one of harder blocks. If two chains are tied on cumulative iterations, the one whose tip block has the lexicographically lower hash wins.

**History rewriting.** Rewriting block N requires recomputing the VDF for every subsequent block sequentially, taking as long in real time as the honest network took. The honest chain keeps advancing during any attempt, widening the gap permanently.

## 4. Fees and Builder Rewards

The node that produces a block collects all transaction fees from that block as its building reward. Fees are credited to the builder's address in the same block application step as transactions, before the PoB reward distribution, so the builder's balance reflects both.

Fees do not reduce the mintable supply. The ticks transferred as fees simply move from sender to builder. This keeps the fee mechanism simple and predictable.

## 5. Incentive

The block reward is drawn from the mintable supply:

```
can_mint      = 21,000,000 LAPSE − total_minted + total_burnt
reward(block) = floor(can_mint × (1 − 0.5^(1/5,000,000)))
```

The halflife of 5,000,000 blocks corresponds to roughly 20 years at 2 minutes per block. Only intentional PoB burns are added back into `can_mint`; fees are not burned and do not replenish it. Burns sustain rewards indefinitely: at low usage, emission decays smoothly toward zero; at high usage, burns offset emission and stabilize net supply.

**Reward distribution.** A fixed 2% of every block reward goes unconditionally to the block's builder, regardless of burn activity. The remaining 98% is distributed proportionally among all senders who burned coins within the last 500 blocks (the PoB window). If sender A burned 3 LAPSE and sender B burned 1 LAPSE in the window, A receives 75% and B receives 25% of that 98%. If no one has burned in the window, only the builder's fixed 2% mints; the remaining 98% is not distributed and stays in `can_mint` for a future block when there are participants to distribute it to. Rounding is truncated toward zero; any remainder stays in the mintable pool.

The builder's share is constant whether or not burns exist in the window, so a builder gains nothing by suppressing burn activity as a class: dropping every burn it sees leaves its own cut unchanged. This removes the blanket incentive, and it guarantees block production stays profitable even with an empty mempool and no burns anywhere in the window, which matters most during network bootstrap before burning activity has started. It does not make any single burner's share safe from a builder that targets that one address, which is a separate and unsolved problem (Section 8).

**Block builder fees.** Transaction fees are separate from the block reward and go entirely to the builder, on top of their fixed reward share. A builder who has never burned still earns fees and the floor share for every block they produce.

## 6. Privacy

Transactions propagate via Dandelion routing: each transaction travels a random stem path before full broadcast, making the origin node indeterminate to network observers.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors.

## 7. Network

Nodes discover peers through the BitTorrent mainline DHT, publishing signed address announcements to slots derived deterministically from the genesis hash. Slot ownership must be renewed hourly; an attacker trying to eclipse honest peers must overwrite every slot continuously, or honest nodes reclaim their positions on the next renewal cycle.

On first contact, a node verifies that a candidate peer shares the same genesis block hash and rejects peers on different chains immediately. The full block history is stored permanently; balance state is always recoverable by replaying from genesis.

## 8. Security Analysis

**Botnet.** An attacker running many nodes gains nothing without burning real coins. Block production rights follow cumulative proven VDF work, not any per-slot scoring. A botnet can try to out-pace the honest network, but doing so requires VDF computation for every block, sequentially, which is no faster on a botnet than on a single node. The honest network accumulates real elapsed time.

**History rewriting.** Rewriting old history requires one sequential VDF per block. The honest chain advances continuously; the gap widens permanently. No parallelism helps.

**Majority VDF.** VDF sequentiality means a majority of nodes produce blocks at the same rate as a single node. More compute does not accelerate history rewriting.

**Proof theft.** A VDF output is bound to the address that will be paid for it, because the builder address is hashed into the challenge (Section 3). Copying a broadcast block's proof under a different builder produces a block whose proof verifies against no valid challenge, so the work cannot be claimed by a node that did not perform it.

**Transaction censorship.** A builder chooses which transactions its own block carries, and nothing in this protocol forces a specific transaction in. A builder that wants to exclude one address can do so for as long as it keeps producing blocks, and a node that never receives a transaction is indistinguishable from one that received it and dropped it, so the exclusion cannot be proven from chain data. Competing builders limit the damage: a transaction excluded by one builder is picked up by the next, and no builder produces blocks indefinitely. What the protocol does not provide is a guarantee of inclusion within a bounded number of blocks against a builder that holds a sustained majority. This is the same exposure Bitcoin has, and it is unsolved here.

**Whale dominance.** Burns older than 500 blocks fall out of the reward window. Sustained reward share requires sustained burning, aligning incentives with ongoing participation rather than one-time capital expenditure.

**Comparison with Bitcoin.** Both systems share the same unsolved problem: a new node syncing from scratch cannot cryptographically distinguish the legitimate chain from an attacker's alternative, and both rely on the attacker having no incentive to destroy the network's value. LapseCoin inherits Bitcoin's battle-tested cumulative-work fork choice and first-valid-block acceptance, eliminating the hardware arms race. The tradeoff is that reward share beyond the builder's fixed floor requires continuous burning; those burned coins return to the mintable pool to sustain future rewards for all participants.

## 9. Conclusion

LapseCoin inherits Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function that binds the chain to real elapsed time. Block selection and fork choice are Bitcoin-like: cumulative proven work wins, no per-slot scoring. Proof-of-Burn is purely an incentive layer: burn coins, earn a proportional share of block rewards, on top of a fixed floor every builder earns regardless of burn activity. Supply is capped at 21 million LAPSE with smooth exponential decay, sustained indefinitely by burns recycled into future emission. No halvings, no energy waste, no complex scoring.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. I. Stewart, "Proof of Burn," 2012. https://en.bitcoin.it/wiki/Proof_of_burn
