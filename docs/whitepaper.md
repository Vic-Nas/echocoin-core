# LapseCoin: A Peer-to-Peer Electronic Cash System

*Victorio Nascimento*

## Abstract

LapseCoin keeps Bitcoin's consensus model: the chain with the most proven work wins, and the first valid block received is accepted. Proof-of-work is replaced with a Verifiable Delay Function (VDF), tying block production to real elapsed time rather than a race for a lucky hash. The new idea sits at the transaction layer. Every transaction is submitted as an encrypted, time-locked ciphertext, and blocks must resolve these ciphertexts in strict, gapless order, with the front of the queue mandatory every block. A builder cannot skip a transaction it dislikes without either resolving it or stopping block production entirely, and it can't even identify which transaction to target before resolution, since the content is encrypted. That is what gives the chain censorship resistance.

## 1. Consensus: Verifiable Delay Functions

Each block requires a VDF proof computed over the hash of the previous block and the builder's address. The VDF takes about 120 seconds of strictly sequential computation, and no amount of parallel hardware speeds it up. Binding the builder's address into the challenge stops anyone from copying a broadcast proof and claiming it under their own address: each builder evaluates a different VDF, so a stolen proof verifies against nobody else's challenge.

The transaction list is not part of the VDF challenge. A block rejected for a transaction problem can be fixed and rebroadcast without redoing the 120 seconds of work.

When two chains compete, the one with more cumulative proven VDF iterations wins, not the one with more blocks. A block's iteration count only counts if its VDF proof actually verifies for that many iterations, so it can't be inflated by lying. Ties break on the lower tip hash.

Rewriting old history means redoing every VDF since that point, sequentially, in as much real time as the honest chain took to produce them. The honest chain keeps advancing the whole time, so the gap only grows.

## 2. What a VDF alone does not solve

A VDF secures the chain against history rewriting, but it does nothing to stop whoever is building the current block from simply leaving out one transaction it dislikes while building normally on everything else. Closing that gap needs a rule at the transaction layer, described next.

## 3. Encrypted transactions and the resolution queue

The base unit is the tick. One LAPSE equals 100,000,000 ticks.

A transaction has two parts:

- **The inner payload**: the real sender, recipient, amount, and a nonce. It is encrypted using a time-lock puzzle (Rivest, Shamir, Wagner, 1996): a disposable RSA modulus `N`, a random base `x`, and a fixed iteration count `T`. The sender computes the answer instantly using the factorization of `N`, encrypts the payload with a key derived from that answer, then discards the factorization. Only `N`, `x`, and the ciphertext are published.
- **The wrapper**: a broadcaster address, signature, and fee. This part is visible immediately and checked like an ordinary transaction, but against the broadcaster, not necessarily the real sender.

The broadcaster does not have to be the real sender. A wallet uses the sender's own key by default, but the protocol allows any address to broadcast on someone else's behalf. This protects against an attacker targeting a specific known address, since the visible wrapper does not have to reveal who is actually sending.

Fees are fixed by the protocol (transaction size times a rate that adjusts with network load), never bid by the sender. A sender-chosen fee would itself be a visible signal even before decryption, and letting a higher fee jump the queue would contradict the ordering rule below.

Once confirmed on chain, a ciphertext gets a permanent position in a single global queue, ordered by block height and position within the block. Anyone can solve a pending puzzle by performing the `T` sequential squarings needed to recover the key, then publish the answer along with the decrypted payload. Verifying a published answer is cheap, a single decryption check rather than a repeat of the squaring. The first valid resolution to land in a block collects that transaction's fee. There is no way to prove who solved a puzzle first, so this affects fee fairness among solvers, not the chain's security.

A block is valid only if it resolves at least the current front of the queue, and any further resolutions in that block continue gaplessly from the front. There is no deadline and no partial credit for clearing part of the backlog. Since content stays hidden until resolution, a builder cannot identify which transaction to suppress until that transaction is already the mandatory front of the queue. At that point it has exactly two options: resolve it and keep building, or stop building blocks. There is no way to selectively skip just one.

Puzzle difficulty (`T`) is fixed and identical for every transaction, never chosen by the sender, since a variable difficulty would itself leak information before decryption. Difficulty adjusts in step with the VDF's own iteration count, with an added safety margin, since dedicated hardware for this kind of arithmetic is more mature than for the VDF's.

## 4. Fees and block rewards

The builder receives the full block reward for every block, unconditionally. Confirmation fees are separate: they are held from the moment a transaction is confirmed and paid out to whoever resolves it first.

## 5. Supply

```
reward(block) = floor((21,000,000 LAPSE - total minted) * (1 - 0.5^(1/5,000,000)))
```

The halflife is about 5,000,000 blocks, roughly 20 years at 2 minutes per block. This smooth curve avoids the instability a hard halving schedule can cause.

## 6. Privacy and networking

Transactions propagate through Dandelion routing, so no observer can reliably tell which peer first broadcast a given transaction. Signatures use FALCON-512, a lattice-based scheme designed to resist quantum computers. Addresses are twelve-word phrases derived from the public key.

Peers find each other through the BitTorrent DHT. A node only connects to peers sharing its genesis block hash. The full chain is kept forever, so balances can always be recomputed from scratch.

## 7. Known limitations

This design does not claim to solve every problem:

- A dominant builder can still stall the whole chain by refusing to build once the queue reaches a transaction it wants to avoid. Every longest-chain system has this limit, Bitcoin included.
- The very first confirmation of a new ciphertext, before it has a queue position, can still be refused by a builder. But refusing one means refusing all new submissions, since nothing about the wrapper reveals what to target.
- The time-lock puzzle is plain RSA and would break on a sufficiently large quantum computer, unlike the FALCON-512 signatures used elsewhere in this design. No vetted quantum-resistant time-lock construction exists yet, so this is a real, open weakness.
- Whether real-world solving hardware keeps pace with transaction volume is a market question the protocol cannot guarantee. A shortfall would show up as a growing queue, not a security failure.
- As with Bitcoin, a node syncing from scratch cannot cryptographically distinguish the honest chain from an attacker's alternative on its own.

## 8. Conclusion

LapseCoin keeps Bitcoin's core guarantee: no trust required, everything verifiable, no authority can reverse a transaction. It replaces proof-of-work with a Verifiable Delay Function, and replaces plaintext transactions with encrypted ones resolved in strict, gapless order, turning selective censorship into an all-or-nothing choice for any builder. Supply is capped at 21 million LAPSE with smooth decay and no halvings.

## References

1. S. Nakamoto, "Bitcoin: A Peer-to-Peer Electronic Cash System," 2008.
2. NIST, "FALCON Post-Quantum Signature Standard," 2024.
3. G. Fanti et al., "Dandelion: Redesigning the Bitcoin Network for Anonymity," 2018.
4. A. Loewenstern et al., "BEP 44: Storing arbitrary data in the DHT," 2014.
5. D. Boneh et al., "Verifiable Delay Functions," 2018.
6. R. Rivest, A. Shamir, D. Wagner, "Time-lock puzzles and timed-release crypto," 1996.
