"""VDF (Verifiable Delay Function) wrapper.

Wraps chiavdf's Wesolowski VDF over imaginary class groups.
This module is the only place chiavdf is imported -- all other modules
call evaluate() and verify() here.

Public interface:
  evaluate(challenge: bytes) -> (output_hex: str, proof_hex: str)
  verify(challenge: bytes, output_hex: str, proof_hex: str) -> bool

Both functions are blocking. evaluate() takes ~BLOCK_CYCLE_SECONDS of
wall time (sequential computation). verify() returns in milliseconds
regardless of iteration count (that is the VDF guarantee).

Calibration note:
  VDF_ITERATIONS in params.py must be measured empirically on the
  target reference machine before mainnet launch and cannot be changed
  afterward without breaking chain identity. The goal is ~120 seconds
  of sequential squaring on commodity hardware. Because the computation
  is strictly sequential, parallelism gives no advantage.

Discriminant:
  A 1024-bit discriminant is derived from the challenge via
  chiavdf.create_discriminant(). This is deterministic and domain-
  separated per block. No trusted setup is required; the discriminant
  changes every block.

Element encoding:
  chiavdf encodes group elements as raw bytes. For a 1024-bit
  discriminant the element serialization is 100 bytes (Chia's constant).
  The prove() output blob is element_bytes || proof_bytes; we store
  both as hex in the block and split on verify.
"""

import chiavdf
from params import VDF_ITERATIONS

# chiavdf constants for 1024-bit discriminants.
# ELEMENT_BYTES is the serialized size of a class group element.
# N_WESOLOWSKI is the number of Wesolowski proof segments (Chia uses 2).
DISC_SIZE_BITS = 1024
ELEMENT_BYTES  = 100   # fixed for 1024-bit discriminant
N_WESOLOWSKI   = 2


def _discriminant(challenge: bytes) -> str:
    """Derive a 1024-bit discriminant from challenge bytes."""
    return chiavdf.create_discriminant(challenge, DISC_SIZE_BITS)


def evaluate(challenge: bytes) -> tuple[str, str]:
    """Run the VDF. Blocks for approximately BLOCK_CYCLE_SECONDS.

    Returns (output_hex, proof_hex) where both are hex strings suitable
    for storing in a block dict. The caller recomputes block_hash after
    attaching these fields.

    challenge should be the raw bytes of the previous block hash.
    """
    disc        = _discriminant(challenge)
    result_blob = chiavdf.prove(challenge, disc, VDF_ITERATIONS,
                                DISC_SIZE_BITS, "n_wesolowski")
    output_hex = result_blob[:ELEMENT_BYTES].hex()
    proof_hex  = result_blob.hex()  # full blob; verify needs the whole thing
    return output_hex, proof_hex


def verify(challenge: bytes, output_hex: str, proof_hex: str) -> bool:
    """Verify a VDF proof. Returns in milliseconds.

    challenge: raw bytes of the previous block hash.
    output_hex: the output element hex from evaluate().
    proof_hex: the full proof blob hex from evaluate().

    Returns True if the proof is valid, False otherwise.
    """
    try:
        disc = _discriminant(challenge)
        return chiavdf.verify_n_wesolowski(
            disc,
            output_hex,
            proof_hex,
            VDF_ITERATIONS,
            DISC_SIZE_BITS,
            N_WESOLOWSKI,
        )
    except Exception:
        return False
