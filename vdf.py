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

import chiavdf

from params import VDF_ITERATIONS

# chiavdf constants for 1024-bit discriminant.
DISC_SIZE_BITS = 1024
FORM_SIZE      = 100        # BQFC_FORM_SIZE = (1024+31)//32*3+4
N_WESOLOWSKI   = 0          # 0 = standard single Wesolowski proof, what prove() produces.
                            # Values > 0 expect a recursively composed proof that prove()
                            # does not emit -- using 2 was the bug causing "invalid VDF proof".

# Identity element in BQFC compressed form:
# first byte = 0x04 (BQFC_IS_1 flag), remaining 99 bytes = 0x00.
# Passed as raw bytes to both prove() and verify_n_wesolowski().
_IDENTITY = b"\x04" + b"\x00" * (FORM_SIZE - 1)


def evaluate(challenge: bytes) -> tuple[bytes, bytes]:
    """Run the VDF. Blocks for approximately BLOCK_CYCLE_SECONDS.

    challenge: raw bytes of the previous block hash (32 bytes).
    Returns (output_hex, proof_hex) where each encodes FORM_SIZE=100 bytes.
    Store both in the block; pass both to verify().
    """
    result = chiavdf.prove(
        challenge,
        _IDENTITY,
        DISC_SIZE_BITS,
        VDF_ITERATIONS,
        "",             # shutdown_file: "" = run to completion
    )
    # result = SerializeForm(y) || SerializeForm(proof) = 200 bytes
    output = result[:FORM_SIZE]
    proof  = result[FORM_SIZE:]
    return output.hex(), proof.hex()


def verify(challenge: bytes, output: str, proof: str) -> bool:
    """Verify a VDF proof. Returns in milliseconds.

    challenge: raw bytes of the previous block hash.
    output, proof: hex strings from evaluate() stored in the block.
    Returns True if valid, False otherwise.
    """
    try:
        disc       = chiavdf.create_discriminant(challenge, DISC_SIZE_BITS)
        proof_blob = bytes.fromhex(output) + bytes.fromhex(proof)   # must stay as bytes
        return chiavdf.verify_n_wesolowski(
            disc,
            _IDENTITY,
            proof_blob,
            VDF_ITERATIONS,
            DISC_SIZE_BITS,
            N_WESOLOWSKI,
        )
    except Exception:
        return False
