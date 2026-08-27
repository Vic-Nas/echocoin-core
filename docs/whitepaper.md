# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

Bitcoin proved that trust between strangers can be replaced by cryptographic proof on a public ledger. Its proof-of-work mechanism wastes energy on a puzzle whose only purpose is to make history rewriting expensive. LapseCoin keeps Bitcoin's battle-tested consensus model (most cumulative proven work wins, first valid block received) but replaces proof-of-work with a Verifiable Delay Function (VDF) that anchors the chain to real elapsed time. The mechanism unique to this design lives at the transaction layer: every transaction is submitted as an opaque, time-lock-encrypted ciphertext, and blocks must resolve these ciphertexts in strict, gapless, global order, with at least the front of the queue mandatory in every block. A builder cannot selectively skip one transaction without either resolving it or ceasing to build blocks entirely, and cannot discriminate on content before resolution because the content is encrypted. This is what delivers censorship resistance, in place of the economic-commitment role burning played in earlier designs.

## 1. The Problem with Proof-of-Work

When the full block reward goes to whoever solves a puzzle first, variance drives small miners into pools. Pool operators gain control over transaction inclusion. The "one CPU, one vote" principle became fictional within a few years of Bitcoin's launch.

ASIC-resistant algorithms slow the hardware race but cannot stop it: proof-of-work rewards hardware investment, not network participation. The work itself is intentionally wasteful.

A VDF alone does not, by itself, protect a transaction from being singled out and excluded by whoever is currently building blocks: a builder can simply choose not to include the one transaction it dislikes while otherwise operating normally. Section 3 describes the mechanism this design uses to close that specific gap.

## 2. Transactions: time-lock-encrypted, not plaintext

The base unit is the **tick**. One LAPSE equals 100,000,000 ticks, matching Bitcoin's precision. All amounts are integers in ticks.

Every transaction submission has two parts:

- **The inner payload** -- the real sender, recipient(s), amount(s), and a per-sender nonce -- is never broadcast in the clear. It is encrypted behind an RSW (Rivest-Shamir-Wagner, 1996) time-lock puzzle: a disposable RSA modulus `N = p·q`, a random base `x`, and a fixed, protocol-wide iteration count `T`. The sender computes the answer `K = x^(2^T) mod N` instantly, via `phi(N) = (p-1)(q-1)` and a single fast modular exponentiation, derives a symmetric key from `K`, encrypts the inner payload with it, and then discards `p`, `q`, and `phi(N)` forever. Only `N`, `x`, and the ciphertext are published (`T` itself is a protocol constant and is not repeated per transaction -- see Section 3 for why).
- **The wrapper** -- a broadcaster address, a signature, and a fee -- is ordinary and visible immediately, checked with the same signature/fee/balance pattern a plaintext transaction would use, but against the broadcaster's balance and signature only.

Critically, the broadcaster does not have to be the real sender. This is intentional: it protects against sender-address-level targeting, a more realistic and more damaging threat than targeting based on transaction content, since content is invisible until resolution anyway. The wallet/app layer defaults to broadcasting under the sender's own key for simplicity, but the protocol places no such requirement on it.

**Fees stay deterministic.** `fee = size_bytes × rate`, set by the protocol, not the sender, for two reasons that both survive independently of any burn mechanism: first, a variable sender-bid fee is a visible, discriminable metadata signal on the wrapper the instant it is broadcast, even though the wrapper's signer need not be the real sender; second, paying more to jump the queue would directly contradict the gapless-order rule in Section 3 that delivers censorship resistance -- the two are mutually exclusive. The rate adjusts every block against a 200 KB soft target, based on median transaction volume over the last 100 blocks: it rises up to 5% per block under load and falls very slowly below target, so a sustained spam attack roughly doubles fees in about 28 minutes while recovery takes hours.

**Resolving a ciphertext.** Anyone who has solved the puzzle -- performed the `T` sequential squarings `x, x², x⁴, ..., x^(2^T) mod N`, which is the only way to recover `K` without the factorization -- can publish a resolution: the confirmed transaction it answers, the solved key, and the decrypted inner payload. Verifying a resolution is cheap regardless of how expensive solving it was: re-deriving the symmetric key from the published `K` and checking that it correctly decrypts the published ciphertext is a single authenticated-decryption operation, not a redo of the `T` squarings. The first resolution to land in the winning block collects that transaction's fee. There is no cryptographic way to prove who actually solved a puzzle first -- the answer space has no room for an identity nonce the way the VDF's builder-binding challenge does (Section 3) -- so this is a known, accepted limitation affecting fee fairness among competing solvers, not chain security.

## 3. Block Production

Every node assembles candidate blocks from its local mempool and broadcasts them to peers. There is no designated block producer and no barrier to participation.

**Timing.** Each block must include a valid VDF proof computed over `sha256(previous_block_hash || builder_address)`. The VDF targets approximately 120 seconds of sequential computation. Because VDF evaluation is strictly sequential, no amount of parallelism shortens it. A faster node finishes sooner and broadcasts immediately; fork choice resolves any competition. Block timestamps are checked only against the near future (more than 30 seconds ahead is rejected); the VDF proof is the sole enforcer of elapsed time. As hardware improves, the iteration count adjusts upward, never downward, to keep average block time near the target.

Binding the builder address into the challenge is what stops a VDF output from being a bearer token. Were the challenge the previous hash alone, every builder would evaluate the same VDF, so any node receiving a broadcast block could keep its proof, substitute its own builder address, and rebroadcast a block that verifies identically. The node that spent the 120 seconds would lose the reward to one that spent nothing. With the builder folded in, each builder evaluates a distinct VDF and a copied proof verifies against nobody else's challenge.

The transaction list is deliberately excluded from the VDF challenge. A proof therefore stays valid under any transaction list, so a block rejected for its contents can be corrected and rebroadcast against the same proof rather than costing the network a fresh 120 seconds.

**Gapless front-of-queue resolution.** Every confirmed ciphertext receives a canonical position the instant it lands on chain -- height plus index within the block, the same style as ordinary transaction ordering -- forming one global, ever-growing queue. A block is valid only if it resolves *at least* the current front of that queue, and any additional resolutions in the same block form a *gapless* continuation from the front: no skipping ahead to a more convenient position. This is a pure, positional rule with no deadline or window. A deadline was considered and rejected during design for two reasons: it either gates block cadence on how fast the solving backlog clears (throughput becomes hostage to how much dedicated solving hardware exists), or it opens a self-dealing exploit, since whoever sets a puzzle already knows its answer and could game any rule that rewards "how much backlog you personally cleared." Pure ordering avoids both.

The effect of this rule is what earlier proof-of-work and proof-of-burn designs could not offer directly: a builder facing a transaction it wants to suppress cannot simply drop it while continuing to operate normally on everything else. Because content is encrypted before resolution, the builder cannot even identify the transaction it wants to suppress until it is already the mandatory front of the queue. At that point the builder's only two options are to resolve it (or wait for someone else's resolution to arrive and include that) and keep building, or stop building blocks entirely. There is no third option that discriminates selectively.

**Puzzle difficulty.** `T` is a fixed constant, identical for every transaction -- never chosen by the sender. A sender-chosen `T` would itself be a visible, unencrypted metadata signal (a bigger `T` might mean a more sensitive transaction, for instance), defeating content-blindness even before anything is decrypted. `T` is recalibrated in lockstep with the existing VDF iteration-count adjustment: both primitives are sequential modular squaring, so hardware improvements should roughly track together, scaled by a safety margin because dedicated RSA/modular-squaring acceleration is far more mature and widely deployed in the wild (TLS accelerators, crypto ASICs) than the class-group arithmetic the VDF uses. This is a reasonable heuristic with a margin, not a guarantee. Difficulty is deliberately not recalibrated from observed confirmation-to-resolution times on chain, since that interval is dominated by how long a ciphertext sits queued before anyone bothers grinding it, not by hardware speed -- a contaminated signal that was considered and rejected during design.

**Block selection.** When a node receives a valid block from a peer, it accepts it immediately as the new tip if it extends the current best chain. A node's own locally-assembled candidate is displaced by any valid peer block that arrives first. There is no scoring race per-slot: the first valid block a node sees wins.

**Fork choice.** When two valid chains compete, nodes prefer the chain with the greater cumulative sum of `vdf_iterations` actually proven across its blocks, not raw block count. A block's `vdf_iterations` is only accepted if its VDF proof verifies for that many iterations, so this sum reflects real, cryptographically-proven sequential work and cannot be inflated by a self-report. Raw height is not used, because each fork's own difficulty-adjustment history is derived only from its own block timestamps: an attacker who pads their own blocks' timestamps toward the near-future bound could otherwise keep their fork's required iteration count artificially low, letting them build a same-height or taller fork in less real time than the honest chain took. Weighing cumulative proven iterations closes that gap, the same way Bitcoin's cumulative-work rule prevents a chain of easier blocks from outweighing one of harder blocks. If two chains are tied on cumulative iterations, the one whose tip block has the lexicographically lower hash wins.

**History rewriting.** Rewriting block N requires recomputing the VDF for every subsequent block sequentially, taking as long in real time as the honest network took. The honest chain keeps advancing during any attempt, widening the gap permanently.

## 4. Fees and Rewards

The block builder mints the full newly-minted block reward for every block it produces, unconditionally -- there is no split with any other class of participant. Confirmation fees, however, do not go to the builder: they are escrowed the instant a ciphertext is confirmed, and paid out to whichever resolver's solution for that specific ciphertext lands first (Section 2). This keeps block production profitable independent of mempool contents while keeping the fee incentive pointed at the people who actually do the solving work.

## 5. Emission

The block reward is drawn from the mintable supply:

```
can_mint      = 21,000,000 LAPSE − total_minted
reward(block) = floor(can_mint × (1 − 0.5^(1/5,000,000)))
```

The halflife of 5,000,000 blocks corresponds to roughly 20 years at 2 minutes per block. This smooth exponential decay is intentional: it avoids the reward-cliff instability a hard halving schedule creates, independent of anything to do with burning coins. At low usage, emission decays smoothly toward zero; there is no burn mechanism to offset it, and none is needed for the supply schedule to be well-behaved.

## 6. Privacy

Transactions propagate via Dandelion routing: each transaction travels a random stem path before full broadcast, making the origin node indeterminate to network observers. This complements the ciphertext format at the network layer: Section 2's encryption hides transaction *content* until resolution and lets the broadcaster differ from the real sender; Dandelion routing separately hides which network peer first announced a given wrapper.

All signatures use FALCON-512, a lattice-based post-quantum scheme standardized by NIST. Addresses are twelve-word phrases derived from the public key hash, resistant to transcription errors.

## 7. Network

Nodes discover peers through the BitTorrent mainline DHT, publishing signed address announcements to slots derived deterministically from the genesis hash. Slot ownership must be renewed hourly; an attacker trying to eclipse honest peers must overwrite every slot continuously, or honest nodes reclaim their positions on the next renewal cycle.

On first contact, a node verifies that a candidate peer shares the same genesis block hash and rejects peers on different chains immediately. The full block history is stored permanently; balance state is always recoverable by replaying from genesis.

## 8. Security Analysis

**Botnet.** An attacker running many nodes gains nothing without producing real, sequential VDF work. Block production rights follow cumulative proven VDF work, not any per-slot scoring. A botnet can try to out-pace the honest network, but doing so requires VDF computation for every block, sequentially, which is no faster on a botnet than on a single node. The honest network accumulates real elapsed time.

**History rewriting.** Rewriting old history requires one sequential VDF per block. The honest chain advances continuously; the gap widens permanently. No parallelism helps.

**Majority VDF.** VDF sequentiality means a majority of nodes produce blocks at the same rate as a single node. More compute does not accelerate history rewriting.

**Proof theft.** A VDF output is bound to the address that will be paid for it, because the builder address is hashed into the challenge (Section 3). Copying a broadcast block's proof under a different builder produces a block whose proof verifies against no valid challenge, so the work cannot be claimed by a node that did not perform it.

**Transaction censorship (content-level).** The gapless front-of-queue rule (Section 3) is what this design adds over Bitcoin here: a builder cannot silently drop one ciphertext while otherwise building normally, because doing so means either resolving it or stopping block production entirely -- there is no selective middle ground. This does not eliminate every censorship exposure, and two residual limitations are stated plainly rather than glossed over:

- A genuinely dominant or majority builder can still stall the entire chain by simply ceasing to build once the front of the queue reaches a target it wants to avoid. This is the same universal limit every longest-chain, first-valid-block-wins system has -- Bitcoin included -- and nothing here claims to fix it.
- The very first ciphertext confirmation, before it has any queue position at all, can still be refused by a builder -- but only indiscriminately, since the wrapper reveals nothing to discriminate on at that stage, and refusing one means refusing all new submissions. This is treated as an accepted residual, not a bug.

**Sender-address targeting.** Because the broadcaster need not be the real sender (Section 2), a builder or network observer that wants to discriminate against a specific person's transactions cannot reliably identify them from the wrapper alone. This is arguably the more realistic threat this design defends against, compared to content-level censorship of a single transaction.

**Post-quantum status of the time-lock puzzle.** This is an explicit, unresolved gap, stated here rather than silently patched: the RSW puzzle in Section 2 is plain RSA-style modular exponentiation, and its security rests on the hardness of factoring `N`. Shor's algorithm breaks factoring on a sufficiently large quantum computer, which is inconsistent with FALCON-512 having been chosen specifically for its quantum resistance elsewhere in this design. No post-quantum time-lock puzzle construction was substituted here, because none was available that had been vetted to the same standard as the rest of this design's cryptographic primitives; inventing one for this purpose was explicitly out of scope. This is a real, currently-unresolved weaker link in the design and should be read as such.

**Economic viability of solving.** Whether real-world puzzle-solving throughput actually keeps pace with real transaction volume is a market question -- does the fee bounty attract enough dedicated solving hardware -- not something protocol parameters alone can guarantee. A persistently under-resourced solving market would show up as growing queue depth, not as a security failure, but it is a real operational risk worth naming.

**Comparison with Bitcoin.** Both systems share the same unsolved problem: a new node syncing from scratch cannot cryptographically distinguish the legitimate chain from an attacker's alternative, and both rely on the attacker having no incentive to destroy the network's value. LapseCoin inherits Bitcoin's battle-tested cumulative-work fork choice and first-valid-block acceptance, eliminating the hardware arms race, and adds a content-blind, gapless resolution-ordering rule at the transaction layer that Bitcoin does not have.

## 9. Conclusion

LapseCoin inherits Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function that binds the chain to real elapsed time, and it replaces plaintext transaction broadcast with time-lock-encrypted ciphertexts resolved in strict, gapless, global order -- the mechanism that makes selective transaction censorship an all-or-nothing choice for any builder, however dominant. Supply is capped at 21 million LAPSE with smooth exponential decay. No halvings, no energy waste, no complex per-block scoring, and no burn mechanism standing in for what the queue-ordering rule now does directly.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. R. Rivest, A. Shamir, D. Wagner, "Time-lock puzzles and timed-release crypto," 1996.
