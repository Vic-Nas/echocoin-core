"""VDF (Verifiable Delay Function) wrapper around chiavdf.

Public interface:
  evaluate(challenge, iterations) -> (output_hex, proof_hex, elapsed_seconds)
  verify(challenge, output, proof, iterations) -> bool

evaluate() blocks for ~BLOCK_CYCLE_SECONDS. verify() returns in milliseconds.

The iteration count is now a protocol parameter that adjusts upward over time
as hardware improves (see block.get_vdf_iterations). It is passed explicitly
so all nodes agree on the same value for each block height.

Internals
---------
chiavdf uses a Wesolowski VDF over imaginary class groups. The discriminant
is derived fresh from each challenge so no trusted setup is needed. All class
group elements are serialized in chiavdf's compressed BQFC format, which is
exactly BQFC_FORM_SIZE=100 bytes for a 1024-bit discriminant.

prove() signature (from fastvdf.cpp):
  prove(challenge, x, disc_size_bits, num_iterations, shutdown_file) -> bytes
  Returns SerializeForm(y) + SerializeForm(proof) = 200 bytes.

verify_n_wesolowski() signature:
  verify_n_wesolowski(discriminant, x, proof_blob, num_iterations,
                      disc_size_bits, recursion) -> bool
  recursion must be 0 for a standard single Wesolowski proof.

Calibration
-----------
VDF_ITERATIONS in params.py must be measured on target hardware before genesis:

  import chiavdf, hashlib, time
  challenge  = hashlib.sha256(b"echocoin_genesis").digest()
  initial_el = b"\\x04" + b"\\x00" * 99
  t0         = time.time()
  chiavdf.prove(challenge, initial_el, 1024, 10_000, "")
  elapsed    = time.time() - t0
  print(int(120 * 10_000 / elapsed))
"""

import time

import chiavdf

from params import VDF_ITERATIONS

DISC_SIZE_BITS = 1024
FORM_SIZE      = 100
N_WESOLOWSKI   = 0

# Identity element: first byte 0x04 (BQFC_IS_1), remaining 99 bytes 0x00.
_IDENTITY = bytes([0x04]) + bytes(FORM_SIZE - 1)


def evaluate(challenge: bytes,
             iterations: int = VDF_ITERATIONS) -> tuple[str, str, float]:
    """Run the VDF. Blocks for approximately BLOCK_CYCLE_SECONDS.

    challenge:  raw bytes of the previous block hash (32 bytes).
    iterations: number of sequential squarings. Pass block.get_vdf_iterations(chain).
    Returns (output_hex, proof_hex, elapsed_seconds).
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
