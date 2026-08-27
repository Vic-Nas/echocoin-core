"""Time-lock puzzle (RSW construction) wrapper.

Public interface:
  generate_puzzle(payload, iterations=TIMELOCK_ITERATIONS) -> dict(N, x, ciphertext)
  solve_puzzle(N, x, ciphertext, iterations=TIMELOCK_ITERATIONS) -> payload bytes
  get_timelock_iterations(chain) -> int

This is a separate primitive from vdf.py. vdf.py wraps chiavdf's Wesolowski
VDF over imaginary class groups; this module is plain RSA-style modular
exponentiation, per Rivest-Shamir-Wagner, "Time-lock puzzles and
timed-release crypto" (1996):

  N = p*q, p and q prime, similar bit length.
  x is random in Z_N*.
  K = x^(2^T) mod N.
  The puzzle setter, knowing phi(N) = (p-1)(q-1), computes
  e = 2^T mod phi(N) and then K = x^e mod N in a single fast modexp
  (Euler's theorem shortcut), then discards p, q, phi(N).
  A solver without the factorization has no such shortcut and must perform
  T sequential squarings: y_0 = x, y_{i+1} = y_i^2 mod N, y_T == K.

The symmetric payload key is derived from K via SHA-256 and used with
AES-256-GCM (authenticated encryption) to encrypt the actual transaction
payload, following the standard RSW-derived construction used by existing
time-lock-puzzle implementations (e.g. the "timelock" reference tooling
built on top of RSA key generation for the modulus).

Modulus generation deliberately reuses `cryptography`'s RSA key generation
(OpenSSL-backed, well-reviewed primality testing) rather than hand-rolling
safe-prime search: RSW does not strictly require safe primes, only that the
setter knows phi(N) and that the modulus resists factorization for the
puzzle's public lifetime, both of which standard RSA keygen provides.

Difficulty
----------
TIMELOCK_ITERATIONS is a fixed, protocol-wide constant (see params.py) --
never chosen per-transaction. A sender-chosen T would leak a visible
metadata signal even before any decryption happens, undermining the
content-blindness the ciphertext transaction format exists to provide.

Difficulty recalibration deliberately does NOT try to measure real puzzle
solve times directly: confirmation-to-resolution height is contaminated by
how long a puzzle sat at the back of the queue before anyone bothered
solving it, not just hardware speed, so it is not a usable signal. Instead,
get_timelock_iterations() derives puzzle difficulty from the existing VDF
adjustment mechanism in block.py: both primitives are sequential modular
squaring, so hardware improvements should roughly track together. Every
time VDF_ITERATIONS is bumped by VDF_ADJUST_FACTOR at a boundary, puzzle T
is bumped by the same proportional factor, scaled by TIMELOCK_MARGIN_MULTIPLIER
as a safety margin (see params.py for why 1:1 tracking would be optimistic:
RSA squaring has much more mature dedicated hardware acceleration in the
wild than class-group arithmetic does).

Calibration methodology (mirrors vdf.py's docstring)
-----------------------------------------------------
Measure on target hardware before genesis. Use at least 1,000,000
iterations (~1-2s) so OS scheduling jitter doesn't dominate. Run 3 times,
take the median:

  import time
  from cryptography.hazmat.primitives.asymmetric import rsa
  priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
  N = priv.private_numbers().public_numbers.n
  x = 123456789
  ITERS = 1_000_000
  t0 = time.monotonic()
  y = x
  for _ in range(ITERS):
      y = pow(y, 2, N)
  elapsed = time.monotonic() - t0
  print(f"elapsed {elapsed:.2f}s  -> {int(120 * ITERS / elapsed)} iterations for ~120s")

Published reference figures (2023 time-lock puzzle benchmarking): roughly
0.6-0.85 million 2048-bit modular squarings/second on modern single-core
hardware (Apple M1 Pro highest at ~0.85M/s, server Xeon/EPYC ~0.6-0.7M/s).
"""

import hashlib
import secrets

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from params import (
    TIMELOCK_ITERATIONS,
    TIMELOCK_MODULUS_BITS,
    TIMELOCK_MARGIN_MULTIPLIER,
    VDF_ITERATIONS,
)

_NONCE_LEN = 12  # AES-GCM standard nonce length


def _int_to_fixed_bytes(value: int, byte_len: int) -> bytes:
    return value.to_bytes(byte_len, "big")


def _derive_key(K: int, modulus_bit_len: int) -> bytes:
    """SHA-256(K) with K encoded at a fixed width (the modulus byte length)
    so puzzles never leak information via variable-length encoding."""
    byte_len = (modulus_bit_len + 7) // 8
    return hashlib.sha256(_int_to_fixed_bytes(K, byte_len)).digest()


def generate_puzzle(payload: bytes, iterations: int = TIMELOCK_ITERATIONS) -> dict:
    """Create a disposable RSW time-lock puzzle encrypting `payload`.

    Returns {"N": int, "x": int, "iterations": int, "ciphertext": hex str}.
    The setter computes the answer instantly via phi(N), uses it to derive
    an AES-256-GCM key, encrypts payload, and then discards p, q, and phi(N)
    entirely -- only N and x are ever returned.
    """
    priv = rsa.generate_private_key(public_exponent=65537, key_size=TIMELOCK_MODULUS_BITS)
    numbers = priv.private_numbers()
    p, q = numbers.p, numbers.q
    N = p * q
    phi = (p - 1) * (q - 1)

    x = secrets.randbelow(N - 3) + 2  # random in [2, N-2]

    exponent = pow(2, iterations, phi)
    K = pow(x, exponent, N)
    key = _derive_key(K, N.bit_length())

    nonce = secrets.token_bytes(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, payload, None)

    # Discard the factorization; only N and x are protocol-visible.
    del p, q, phi, priv, numbers, K, key

    return {
        "N": N,
        "x": x,
        "iterations": iterations,
        "ciphertext": (nonce + ct).hex(),
    }


def solve_puzzle(N: int, x: int, ciphertext_hex: str,
                  iterations: int = TIMELOCK_ITERATIONS) -> bytes:
    """Solve a puzzle by brute-force sequential squaring, then decrypt.

    Public and reproducible: anyone with (N, x, ciphertext, iterations) can
    run this. No shortcut exists without the factorization of N. Raises
    ValueError (via AESGCM) if the derived key is wrong, which cannot
    happen for a correctly-solved puzzle.
    """
    y = x
    for _ in range(iterations):
        y = pow(y, 2, N)
    key = _derive_key(y, N.bit_length())

    blob = bytes.fromhex(ciphertext_hex)
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


def get_timelock_iterations(chain) -> int:
    """Puzzle difficulty T for the next block, derived from the VDF
    adjustment mechanism rather than measured directly.

    Confirmation-to-resolution height is not a usable difficulty signal:
    it is dominated by how long a puzzle sat at the back of the queue
    before anyone solved it, not by hardware speed. Instead, this tracks
    block.get_vdf_iterations proportionally -- both primitives are
    sequential modular squaring, so hardware improvements should roughly
    track together -- scaled by TIMELOCK_MARGIN_MULTIPLIER as a safety
    margin, since RSA squaring has more mature dedicated hardware
    acceleration in the wild than class-group arithmetic.
    """
    import block as block_mod  # local import: avoid a hard cycle at module load

    vdf_iterations = block_mod.get_vdf_iterations(chain)
    growth = vdf_iterations / VDF_ITERATIONS  # >= 1.0; VDF iterations only rise
    # Apply the margin multiplier to the *increase* only, so the baseline
    # (growth == 1, no adjustment has fired yet) stays exactly
    # TIMELOCK_ITERATIONS rather than always inflated by the margin.
    scaled_growth = 1 + (growth - 1) * TIMELOCK_MARGIN_MULTIPLIER
    return int(TIMELOCK_ITERATIONS * scaled_growth)
