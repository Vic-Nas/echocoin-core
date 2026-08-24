# Echocoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its proof-of-work mechanism wastes energy on a puzzle whose only purpose is to make history rewriting expensive. Echocoin replaces proof-of-work with two complementary mechanisms: a Verifiable Delay Function (VDF) that anchors the chain to real elapsed time, and Proof-of-Burn (PoB) that ties block rewards to real economic commitment. The result is a battle-tested Bitcoin-like consensus model (longest chain wins, first valid block received) with an incentive layer that rewards long-term participants who burn coins and earn back a proportional share of every block's reward.

## 1. The Problem with Proof-of-Work

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of Bitcoin's launch.

ASIC-resistant algorithms slow the hardware race but cannot stop it: proof-of-work rewards hardware investment, not network participation. The work itself is intentionally wasteful.

A VDF alone without economic weight opens a different attack: an adversary who spins up thousands of parallel instances at near-zero marginal cost can overwhelm the honest network. Proof-of-Burn closes this gap. A botnet that refuses to destroy real coins earns no share of rewards, removing the economic incentive for such attacks.

## 2. Transactions

The base unit is the **ring**. One ECH equals 100,000,000 rings, matching Bitcoin's precision. All amounts are integers in rings.

Fees are set by the protocol, not the sender: `fee = size_bytes × rate`. The rate adjusts every block against a 200 KB soft target, based on the median transaction volume over the last 100 blocks. It rises quickly under load — up to 5% per block — and falls very slowly below target, so a sustained spam attack doubles fees in about 28 minutes while recovery takes hours. Fees are collected by the block builder (the node that produced the block), not burned.

**Intentional burns.** Any output addressed to `burn` is a Proof-of-Burn output. The rings are permanently destroyed and added back into the mintable emission pool (Section 5). Intentional burns are recorded per sender over a rolling window of the last 500 blocks, and count toward the sender's proportional share of every future block reward within that window. A single transaction may mix normal outputs and burn outputs.

## 3. Block Production

Every node assembles candidate blocks from its local mempool and broadcasts them to peers. There is no designated block producer and no barrier to participation.

**Timing.** Each block must include a valid VDF proof computed over the previous block's hash. The VDF targets approximately 120 seconds of sequential computation. Because VDF evaluation is strictly sequential, no amount of parallelism shortens it. A faster node finishes sooner and broadcasts immediately; fork choice resolves any competition. Block timestamps are checked only against the near future (more than 30 seconds ahead is rejected); the VDF proof is the sole enforcer of elapsed time. As hardware improves, the iteration count adjusts upward — never downward — to keep average block time near the target.

**Block selection.** When a node receives a valid block from a peer, it accepts it immediately as the new tip if it extends the current best chain. A node's own locally-assembled candidate is displaced by any valid peer block that arrives first. There is no scoring race per-slot: the first valid block a node sees wins.

**Fork choice.** When two valid chains compete, nodes always prefer the longer chain. If two chains are of equal height, the one whose tip block has the lexicographically lower hash wins. This rule is deterministic, requires no additional state, and matches Bitcoin's longest-chain principle.

**History rewriting.** Rewriting block N requires recomputing the VDF for every subsequent block sequentially, taking as long in real time as the honest network took. The honest chain keeps advancing during any attempt, widening the gap permanently.

## 4. Fees and Builder Rewards

The node that produces a block collects all transaction fees from that block as its building reward. Fees are credited to the builder's address in the same block application step as transactions, before the PoB reward distribution, so the builder's balance reflects both.

Fees do not reduce the mintable supply. The rings transferred as fees simply move from sender to builder. This keeps the fee mechanism simple and predictable.

## 5. Incentive

The block reward is drawn from the mintable supply:

```
can_mint      = 21,000,000 ECH − total_minted + total_burnt
reward(block) = floor(can_mint × (1 − 0.5^(1/5,000,000)))
```

The halflife of 5,000,000 blocks corresponds to roughly 20 years at 2 minutes per block. Only intentional PoB burns are added back into `can_mint`; fees are not burned and do not replenish it. Burns sustain rewards indefinitely: at low usage, emission decays smoothly toward zero; at high usage, burns offset emission and stabilize net supply.

**Reward distribution.** Every block reward is distributed proportionally among all senders who burned coins within the last 500 blocks (the PoB window). If sender A burned 3 ECH and sender B burned 1 ECH in the window, A receives 75% and B receives 25% of the block reward. If no one has burned in the window, the reward is not distributed: it remains in `can_mint` for a future block when there are participants to distribute it to. Rounding is truncated toward zero; any remainder stays in the mintable pool.

**Block builder fees.** Transaction fees are separate from the block reward and go entirely to the builder. A builder who has never burned still earns fees for every block they produce.

## 6. Privacy

Transactions propagate via Dandelion routing: each transaction travels a random stem path before full broadcast, making the origin node indeterminate to network observers.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors.

## 7. Network

Nodes discover peers through the BitTorrent mainline DHT, publishing signed address announcements to slots derived deterministically from the genesis hash. Slot ownership must be renewed hourly; an attacker trying to eclipse honest peers must overwrite every slot continuously, or honest nodes reclaim their positions on the next renewal cycle.

On first contact, a node verifies that a candidate peer shares the same genesis block hash and rejects peers on different chains immediately. The full block history is stored permanently; balance state is always recoverable by replaying from genesis.

## 8. Security Analysis

**Botnet.** An attacker running many nodes gains nothing without burning real coins. Block production rights follow the longest chain, not any per-slot scoring. A botnet can try to out-pace the honest network, but doing so requires VDF computation for every block, sequentially, which is no faster on a botnet than on a single node. The honest network accumulates real elapsed time.

**History rewriting.** Rewriting old history requires one sequential VDF per block. The honest chain advances continuously; the gap widens permanently. No parallelism helps.

**Majority VDF.** VDF sequentiality means a majority of nodes produce blocks at the same rate as a single node. More compute does not accelerate history rewriting.

**Whale dominance.** Burns older than 500 blocks fall out of the reward window. Sustained reward share requires sustained burning, aligning incentives with ongoing participation rather than one-time capital expenditure.

**Comparison with Bitcoin.** Both systems share the same unsolved problem: a new node syncing from scratch cannot cryptographically distinguish the legitimate chain from an attacker's alternative, and both rely on the attacker having no incentive to destroy the network's value. Echocoin inherits Bitcoin's battle-tested longest-chain fork choice and first-valid-block acceptance, eliminating the hardware arms race. The tradeoff is that reward share requires continuous burning; those burned coins return to the mintable pool to sustain future rewards for all participants.

## 9. Conclusion

Echocoin inherits Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function that binds the chain to real elapsed time. Block selection and fork choice are Bitcoin-like: simple, battle-tested, no per-slot scoring. Proof-of-Burn is purely an incentive layer: burn coins, earn a proportional share of block rewards. Supply is capped at 21 million ECH with smooth exponential decay, sustained indefinitely by burns recycled into future emission. No halvings, no energy waste, no complex scoring.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. I. Stewart, "Proof of Burn," 2012. https://en.bitcoin.it/wiki/Proof_of_burn
