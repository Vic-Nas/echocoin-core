# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

LapseCoin keeps Bitcoin's consensus model: the chain with the most proven work wins, and the first valid block received is accepted. Proof-of-work is replaced with a Verifiable Delay Function (VDF), tying block production to real elapsed time rather than a race for a lucky hash. Transactions are ordinary and plaintext, with sender-bid fees, much like Bitcoin's own. The main practical difference from Bitcoin is the VDF itself: it is believed to have a much smaller hardware advantage gap than proof-of-work does, so it does not push the network toward the same resource-consumption spiral that proof-of-work mining did.

## 1. Consensus: Verifiable Delay Functions

Each block requires a VDF proof computed over the hash of the previous block and the builder's address. The VDF takes about 120 seconds of strictly sequential computation, and no amount of parallel hardware speeds it up. Binding the builder's address into the challenge stops anyone from copying a broadcast proof and claiming it under their own address: each builder evaluates a different VDF, so a stolen proof verifies against nobody else's challenge.

The transaction list is not part of the VDF challenge. A block rejected for a transaction problem can be fixed and rebroadcast without redoing the 120 seconds of work.

When two chains compete, the one with more cumulative proven VDF iterations wins, not the one with more blocks. A block's iteration count only counts if its VDF proof actually verifies for that many iterations, so it can't be inflated by lying. Ties are routine, not rare: any two builders finishing at the same height require the same protocol-set iteration count, so a simple same-height fork ties exactly. Ties break on the lower VDF output, not the block hash. The block hash includes the transaction list, and since the transaction list is not part of the VDF challenge (see below), a builder can swap it for free after finishing the real work. Breaking ties on block hash would let that same builder grind transaction-list variants after the fact, searching for a lower hash at nearly zero cost, which would undermine the property the VDF exists to enforce in the first place. The VDF output cannot be changed without redoing the actual 120 seconds under a different builder address, so it keeps the tie-break's cost real.

Rewriting old history means redoing every VDF since that point, sequentially, in as much real time as the honest chain took to produce them. The honest chain keeps advancing the whole time, so the gap only grows.

## 2. Why a VDF instead of proof-of-work

Proof-of-work ties a miner's expected reward directly to hash rate, with no ceiling: doubling your hash rate doubles your expected share of every block, forever. That unbounded incentive is what drove Bitcoin mining from CPUs to GPUs to ASICs, a hardware gap that reached 10,000x or more over commodity hardware, and an enormous, ever-growing energy bill along with it.

A VDF does not have that shape. Each block's challenge is a single sequential computation with a fixed floor, about 120 seconds here, that more hardware cannot shrink below. Racing to build a block is not a matter of buying more machines and running more attempts in parallel; it is a matter of how fast one chain of sequential steps can be evaluated, and that has a hard floor set by the arithmetic itself.

This does not mean all VDF hardware is equal. Chia Network's 2019 public hardware competition for the same class-group-based VDF construction used here found real, specialized implementations beating commodity software by something like 3 to 10 times, not nothing. But that gap is far narrower than the proof-of-work gap, and more importantly it does not grow the same way: there is no unbounded reward for adding more of that hardware in parallel, because a single VDF evaluation cannot be split across machines. The result is a design that avoids proof-of-work's resource-consumption spiral structurally, from the shape of the problem, rather than by adding a separate "green" rule on top.

That gap also changes the shape of competition, not just its size. A VDF round is a race to finish first, not a per-attempt lottery: a core outside that top few-times band does not win a proportionally smaller share the way lower hash rate does under proof-of-work, it simply loses whenever a faster builder is competing. So the practical cost of a competitive identity is not just any core, it is a core within that top few-times band, and since the arithmetic floor caps how much further specialization can go, it is a cost paid once rather than chased generation after generation the way proof-of-work hardware was. What that cost does not do is cap how many such cores one operator runs. Nothing in the protocol limits the number of independent builder addresses a single operator holds, so total participation, and the energy that comes with it, still scales with how much an operator is willing to spend, the same way Bitcoin's hash rate does. A VDF narrows the hardware advantage within one identity; it does not, and cannot by itself, limit the number of identities.

## 3. Transactions

The base unit is the tick. One LAPSE equals 100,000,000 ticks.

A transaction is a plain, visible dict: a sender address, a public key, a list of outputs (recipient and amount), a sequential per-sender nonce, a fee, and a signature. Nothing about it is encrypted or hidden.

Nonces are sequential per sender, starting from zero: a transaction's nonce must be exactly one more than the sender's last confirmed nonce. This is the standard replay-protection scheme, the same one Bitcoin-style account models use.

Fees are chosen by the sender, not fixed by the protocol. A transaction is valid as long as the sender's balance covers every output plus the fee. Builders are free to prioritize whichever pending transactions pay the most per byte, the same market-based mechanism Bitcoin uses to clear its mempool under load.

Blocks apply their listed transactions in order, checking each one against the state as it stands after the transactions before it in the same block. There is no required canonical ordering across transactions; a block's builder can list them however it likes, as long as each one is individually valid at the point it is applied.

## 4. Fees and block rewards

The builder receives the full block reward for every block, unconditionally, plus every transaction fee in that block. There is no split, and no separate party to pay out to.

## 5. Supply

```
reward(block) = floor((21,000,000 LAPSE - total minted) * (1 - 0.5^(1/5,000,000)))
```

The halflife is about 5,000,000 blocks, roughly 20 years at 2 minutes per block. This smooth curve avoids the instability a hard halving schedule can cause.

## 6. Privacy and networking

Transactions propagate through Dandelion routing, so no observer can reliably tell which peer first broadcast a given transaction. Signatures use FALCON-512, a lattice-based scheme designed to resist quantum computers. Addresses are twelve-word phrases derived from the public key.

Peers find each other through the BitTorrent DHT. A node only connects to peers sharing its genesis block hash. The full chain is kept forever, so balances can always be recomputed from scratch.

## 7. Security and censorship: what this design does and does not solve

Consensus here is longest-chain, exactly like Bitcoin's, just measured in proven VDF iterations instead of hashes. That inherits Bitcoin's security model in full, including its limits.

Ordinary transaction censorship, a single non-majority actor refusing to include some transaction, is defeated the same way it always has been: any other willing participant can include it instead, and ordinary confirmation-depth economics protect against a brief refusal turning into a permanent one.

A genuine, sustained majority attacker is a different matter, and here this design does not claim to do better than Bitcoin. Fork choice cares only about cumulative proven work; it has no way to look at what a chain contains. A majority attacker can always choose to fork from a point before some transaction was ever confirmed, and build an alternative history that simply never confirms it, at exactly the cost of an ordinary reorg attack. No rule enforced at the level of individual blocks or individual transactions can stop this, because the attacker is not breaking any such rule; it is simply choosing not to extend the branch that contains the disliked content. This is a universal property of longest-chain consensus, not a gap specific to this design.

Other inherited limitations:

- As with Bitcoin, a node syncing from scratch cannot cryptographically distinguish the honest chain from an attacker's alternative on its own; it has to trust the network it connects to at least once.
- Whether VDF-solving hardware availability keeps pace with the network's needs is a market question the protocol cannot guarantee, the same as mining hardware availability is for Bitcoin.

## 8. Conclusion

LapseCoin keeps Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function, which is believed to narrow the hardware-advantage gap that drove proof-of-work's runaway energy use, without needing a separate rule bolted on to achieve that. Transactions stay ordinary and plaintext, with sender-bid fees. Supply is capped at 21 million LAPSE with smooth decay and no halvings. Censorship resistance beyond ordinary confirmation-depth security is not claimed, because no rule at the block or transaction level can give it against a genuine majority attacker in a longest-chain system.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FIPS 206 (Draft): FN-DSA (FALCON)," 2025.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. Chia Network, "Chia Network's Proof of Space and Time VDF Competition Results," 2019.
