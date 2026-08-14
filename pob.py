"""Proof-of-Burn score engine.

Public interface:

  score(chain, address)        -> int   (lower = more committed)
  cumulative_score(chain)      -> int   (sum of all block scores)
  best_builder(chain, addrs)   -> str   (address with lowest score)

The score formula for a builder at a given chain tip:

  numerator   = Hash(vdf_output_of_tip + builder_pubkey_address)  [as int]
  denominator = max(1, sum of intentional burns by builder
                       in the last POB_WINDOW blocks)

Lower score = more burns = more block-building priority.

Fork choice: when two valid chains of equal height compete, the one
with the lower cumulative_score wins. Honest burners always beat
unburnished botnets whose denominator stays at the floor of 1.

Intentional burns are tx outputs with {"to": BURN_ADDRESS, "amount": N}.
Fee burns are NOT counted -- they flow into emission but not PoB weight.
"""

import hashlib
import struct

from params import POB_WINDOW

BURN_ADDRESS = "burn"   # sentinel; validated as the one non-BIP39 address


def _tip_hash_int(chain):
    """Return the VDF output of the chain tip as an integer seed."""
    tip = chain[-1]
    vdf_out = tip.get("vdf_output") or tip["hash"]
    return int(vdf_out[:64], 16)      # 256-bit int from first 32 bytes of hex


def _builder_burn(chain, address):
    """Sum intentional burns by address over the last POB_WINDOW blocks."""
    window = chain[-POB_WINDOW:] if len(chain) > POB_WINDOW else chain
    total = 0
    for blk in window:
        for tx in blk.get("transactions", []):
            if tx.get("from") == address:
                for out in tx.get("outputs", []):
                    if out.get("to") == BURN_ADDRESS:
                        total += out["amount"]
    return total


def _addr_int(address):
    """Deterministic integer derived from an address string."""
    h = hashlib.sha256(address.encode()).digest()
    return struct.unpack(">Q", h[:8])[0]   # 64-bit, good enough for ordering


def score(chain, address):
    """Compute PoB score for address at the current chain tip.

    Lower is better. Unburnished builders score (tip_seed XOR addr_int),
    which can be large. Heavy burners divide that by their burn total,
    yielding a small score.
    """
    seed      = _tip_hash_int(chain) ^ _addr_int(address)
    burn      = _builder_burn(chain, address)
    denom     = max(1, burn)
    return seed // denom


def cumulative_score(chain):
    """Sum of scores for each block's builder over the whole chain.

    Used as the fork-choice weight: lower cumulative score = heavier chain.
    Genesis (height 0, no builder) contributes 0.
    """
    total = 0
    for i, blk in enumerate(chain):
        builder = blk.get("builder")
        if builder and i > 0:
            total += score(chain[:i], builder)
    return total


def best_builder(candidates, chain):
    """Return the address with the lowest PoB score among candidates."""
    return min(candidates, key=lambda a: score(chain, a))
