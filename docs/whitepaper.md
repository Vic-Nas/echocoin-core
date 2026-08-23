# Echocoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its proof-of-work mechanism wastes energy on a puzzle whose only purpose is to make history rewriting expensive. Echocoin replaces proof-of-work with two complementary mechanisms: a Verifiable Delay Function (VDF) that anchors the chain to real elapsed time, and Proof-of-Burn (PoB) that anchors block-building rights to real economic commitment.

## 1. The Problem with Proof-of-Work

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of Bitcoin's launch.

ASIC-resistant algorithms slow the hardware race but cannot stop it: proof-of-work rewards hardware investment, not network participation. The work itself is intentionally wasteful.

A VDF alone without economic weight opens a different attack: an adversary who spins up thousands of parallel instances at near-zero marginal cost can overwhelm the honest network. Proof-of-Burn closes this gap. A botnet that refuses to destroy real coins cannot produce competitive block scores, regardless of instance count.

## 2. Transactions

The base unit is the **ring**. One ECH equals 100,000,000 rings, matching Bitcoin's precision. All amounts are integers in rings.

Fees are set by the protocol, not the sender: `fee = size_bytes × rate`. The rate adjusts every block against a 200 KB soft target, based on the median transaction volume over the last 100 blocks. It rises quickly under load — up to 5% per block — and falls very slowly below target, so a sustained spam attack doubles fees in about 28 minutes while recovery takes hours. All fees are burned, reducing circulating supply and replenishing the mintable emission pool.

**Intentional burns.** Any output addressed to `burn` is a Proof-of-Burn output. The rings are permanently destroyed and credited to the mintable pool alongside fee burns. Unlike fees, intentional burns are recorded per-sender and count toward the sender's block-building priority (Section 3). A single transaction may mix normal outputs and burn outputs.

## 3. Block Production

Every node assembles candidate blocks from its local mempool and broadcasts them to peers. There is no designated block producer and no barrier to participation.

**Timing.** Each block must include a valid VDF proof computed over the previous block's hash. The VDF targets approximately 120 seconds of sequential computation. Because VDF evaluation is strictly sequential, no amount of parallelism shortens it. A faster node finishes sooner and broadcasts immediately; fork choice resolves any competition. Block timestamps are checked only against the near future (more than 30 seconds ahead is rejected); the VDF proof is the sole enforcer of elapsed time. As hardware improves, the iteration count adjusts upward — never downward — to keep average block time near the target.

**Block score.** Every candidate block carries a score:

```
score = Hash(tip_VDF_output XOR builder_address) / max(1, burns_in_window)
```

Lower score means more economic commitment and higher priority. The numerator is unguessable until the tip's VDF completes, so builders cannot pre-select outputs to manipulate their score. The denominator sums the builder's intentional burns from the last 500 blocks (roughly 17 hours), so old burns decay out of influence. A builder with no burns gets denominator 1: a large, uncompetitive score, but never excluded.

**Burn pools.** A burn output may name a beneficiary address. Burns tagged to a beneficiary accumulate that address's scoring weight. When the beneficiary wins a block, contributors share the reward proportionally; the block builder always receives a guaranteed 2% base cut regardless of their tagged contributions. Burn weight is address-specific and non-transferable. A contributor who distrusts the pool operator can stop tagging at any time; their weight expires within ~17 hours.

**Fork choice.** When two valid blocks compete for the same slot, nodes keep the one with the lower score. For competing chains, nodes adopt the one with the higher total eligible burns from the fork point onward. Burns eligible for this comparison are capped to each contributor's balance at the fork point — rewards minted privately after the fork do not count. This closes the self-funding loop: a privately-built chain cannot gain an advantage by burning its own minted rewards. When total eligible burns are equal (including the bootstrap case where no burns exist yet), the longer chain wins; tip hash breaks any remaining tie deterministically.

**History rewriting.** Rewriting block N requires recomputing the VDF for every subsequent block sequentially, taking as long in real time as the honest network took. The honest chain keeps advancing during any attempt, widening the gap permanently.

## 4. Censorship Resistance

Excluding a transaction costs a builder nothing (fees are burned, so omitting them yields nothing) — the only motive is malice. Three coupled mechanisms make sustained censorship prohibitively expensive.

**Distributed lottery.** The score seed is derived from the VDF output XOR-ed with the builder's address. Different builders produce different seeds, so each honest node independently wins some fraction of slots. A censor holding fraction F of total burns wins approximately F of slots; honest nodes collectively win the rest and include any censored transactions.

**Economic cost.** Burns expire after 500 blocks. Holding dominance requires sustaining burns far above the honest network's aggregate. At 2x dominance, the attacker must continuously burn twice what all honest participants combined are burning — making sustained censorship economically self-defeating.

**Validity window.** Transactions reference the fee rate at the block when they were created and remain valid for 20 subsequent blocks. This gives honest nodes enough time to include a censored transaction before it expires. At 2-3x attacker dominance, the probability that no honest node wins within 20 blocks is below 4%.

## 5. Incentive

The block reward is drawn from the mintable supply:

```
can_mint      = 21,000,000 ECH − total_minted + total_burnt
reward(block) = can_mint × (1 − 0.5^(1/5,000,000))
```

The halflife of 5,000,000 blocks corresponds to roughly 20 years at 2 minutes per block. Both fee burns and intentional burns are added back into `can_mint`, so network activity replenishes the pool and sustains rewards indefinitely. At low usage, emission decays smoothly toward zero; at high usage, burns offset emission and stabilize net supply. Rewards are distributed among contributors to the winning builder's burn pool as described in Section 3.

## 6. Privacy

Transactions propagate via Dandelion routing: each transaction travels a random stem path before full broadcast, making the origin node indeterminate to network observers.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors.

## 7. Network

Nodes discover peers through the BitTorrent mainline DHT, publishing signed address announcements to slots derived deterministically from the genesis hash. Slot ownership must be renewed hourly; an attacker trying to eclipse honest peers must overwrite every slot continuously, or honest nodes reclaim their positions on the next renewal cycle.

On first contact, a node verifies that a candidate peer shares the same genesis block hash and rejects peers on different chains immediately. The full block history is stored permanently; balance state is always recoverable by replaying from genesis.

## 8. Security Analysis

**Botnet.** A chain built without burns has zero total eligible burns in its suffix. The honest network accumulates real burn weight with every block; every node independently prefers the honest chain.

**Withholding attack.** An attacker who builds privately and burns the minted rewards gains nothing: eligible burns in the divergent suffix are capped to the attacker's pre-fork balance. Privately minted rewards do not improve the scoring denominator.

**Majority VDF.** VDF sequentiality means a majority of nodes produce blocks at the same rate as a single node. More compute does not accelerate history rewriting.

**Censorship.** A majority of burns wins only approximately that fraction of slots by lottery. The honest minority wins the rest. Sustained censorship requires a burn rate that grows proportionally with its effectiveness, making it economically self-defeating before it approaches completeness.

**Long-range rewrite.** Rewriting old history requires one sequential VDF per block. The honest chain advances continuously; the gap widens permanently.

**Whale dominance.** Burns older than 500 blocks fall out of the scoring window. Sustained priority requires sustained burning, aligning incentives with ongoing participation rather than one-time capital expenditure.

**Comparison with Bitcoin.** Both systems share the same unsolved problem: a new node syncing from scratch cannot cryptographically distinguish the legitimate chain from an attacker's alternative, and both rely on the attacker having no incentive to destroy the network's value. Bitcoin is additionally vulnerable to majority hashpower and ASIC centralization. Echocoin eliminates the hardware arms race; the tradeoff is that block-building priority requires continuous burning, and those burned coins return to the mintable pool to sustain future rewards.

## 9. Conclusion

Echocoin inherits Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function that binds the chain to real elapsed time and Proof-of-Burn that binds block-building rights to real economic commitment. Supply is capped at 21 million ECH with smooth exponential decay, sustained indefinitely by burns recycled into future emission. No halvings, no energy waste, no botnet vulnerability.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. I. Stewart, "Proof of Burn," 2012. https://en.bitcoin.it/wiki/Proof_of_burn
