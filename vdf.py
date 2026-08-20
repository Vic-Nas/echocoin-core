"""VDF (Verifiable Delay Function) wrapper around chiavdf.

Public interface -- the only two functions any other module calls:

  evaluate(challenge: bytes) -> (output: bytes, proof: bytes)
  verify(challenge: bytes, output: bytes, proof: bytes) -> bool

evaluate() blocks for ~BLOCK_CYCLE_SECONDS of wall time (sequential
squarings). verify() returns in milliseconds regardless of iteration
count -- that is the VDF guarantee.

Internals
---------
chiavdf uses a Wesolowski VDF over imaginary class groups. The
discriminant is derived fresh from each challenge so no trusted setup
is needed. All class group elements are serialized in chiavdf's
compressed BQFC format, which is exactly BQFC_FORM_SIZE=100 bytes for
a 1024-bit discriminant.

prove() signature (from fastvdf.cpp):
  prove(challenge: bytes, x: bytes, disc_size_bits: int,
        num_iterations: int, shutdown_file: str) -> bytes
  Returns SerializeForm(y) + SerializeForm(proof) = 200 bytes.
  x is the starting element as raw bytes.
  shutdown_file: if non-empty, prove() polls that path and exits early
  if it disappears. We pass "" to run to completion.

verify_n_wesolowski() signature:
  verify_n_wesolowski(discriminant: str, x: bytes, proof_blob: bytes,
                      num_iterations: int, disc_size_bits: int,
                      recursion: int) -> bool
  x and proof_blob must be raw bytes (not latin-1 decoded strings).
  recursion must be 0 for a standard single Wesolowski proof as
  produced by prove(); higher values expect a recursively composed
  proof that prove() does not emit.

Calibration
-----------
VDF_ITERATIONS must be measured on the target hardware before genesis:

  import chiavdf, hashlib, time
  challenge  = hashlib.sha256(b"echocoin_genesis").digest()
  initial_el = b"\x04" + b"\x00" * 99
  t0         = time.time()
  chiavdf.prove(challenge, initial_el, 1024, 10_000, "")
  elapsed    = time.time() - t0
  print(int(120 * 10_000 / elapsed))

Cannot change after genesis without breaking chain identity.
"""

import time

import chiavdf

from params import VDF_ITERATIONS

# chiavdf constants for 1024-bit discriminant.
DISC_SIZE_BITS = 1024
FORM_SIZE      = 100
N_WESOLOWSKI   = 0

_IDENTITY = b"\\x04" + b"\\x00" * (FORM_SIZE - 1)


def evaluate(challenge: bytes,
             iterations: int = VDF_ITERATIONS) -> tuple[bytes, bytes, float]:
    """Run the VDF. Blocks for approximately BLOCK_CYCLE_SECONDS.

    challenge:  raw bytes of the previous block hash (32 bytes).
    iterations: number of sequential squarings. Defaults to VDF_ITERATIONS
                but callers should pass the chain-determined value from
                block.get_vdf_iterations(chain).
    Returns (output_hex, proof_hex, elapsed_seconds).
    Store output_hex and proof_hex in the block; elapsed_seconds goes into
    vdf_seconds (informational, used for difficulty adjustment).
    """
    t0     = time.monotonic()
    result = chiavdf.prove(
        challenge,
        _IDENTITY,
        DISC_SIZE_BITS,
        iterations,
        "",
    )
    elapsed = time.monotonic() - t0
    output  = result[:FORM_SIZE]
    proof   = result[FORM_SIZE:]
    return output.hex(), proof.hex(), elapsed


def verify(challenge: bytes, output: str, proof: str,
           iterations: int = VDF_ITERATIONS) -> bool:
    """Verify a VDF proof. Returns in milliseconds.

    challenge:  raw bytes of the previous block hash.
    output, proof: hex strings from evaluate() stored in the block.
    iterations: must match what was used during evaluate().
    """
    try:
        disc       = chiavdf.create_discriminant(challenge, DISC_SIZE_BITS)
        proof_blob = bytes.fromhex(output) + bytes.fromhex(proof)
        return chiavdf.verify_n_wesolowski(
            disc,
            _IDENTITY,
            proof_blob,
            iterations,
            DISC_SIZE_BITS,
            N_WESOLOWSKI,
        )
    except Exception:
        return False
