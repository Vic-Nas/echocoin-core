"""
Keys, signing, verification, address derivation, key storage.

Signing: FALCON-512 (NIST PQC standard) via pqcrypto.
Key storage: Argon2id + NaCl secretbox. Passphrase mandatory.
             Secret key decrypted per signing call, reference dropped immediately.
"""

import hashlib
import json
import os
import base64

import nacl.secret
import nacl.utils
import nacl.pwhash
from pqcrypto.sign.falcon_512 import (
    generate_keypair as _falcon_keygen,
    sign as _falcon_sign,
    verify as _falcon_verify,
    PUBLIC_KEY_SIZE,
    SECRET_KEY_SIZE,
    SIGNATURE_SIZE,
)

from params import ADDRESS_WORD_COUNT, WORD_BITS

_WORDLIST = None

def _load_wordlist():
    global _WORDLIST
    if _WORDLIST is not None:
        return _WORDLIST
    base = getattr(__import__("sys"), "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "bip39_english.txt")
    with open(path) as f:
        _WORDLIST = [w.strip() for w in f if w.strip()]
    assert len(_WORDLIST) == 2048, f"BIP39 wordlist must have 2048 words, got {len(_WORDLIST)}"
    return _WORDLIST


# ---------------------------------------------------------------------------
# Key generation and signing (FALCON-512)
# ---------------------------------------------------------------------------

def generate_keypair():
    """Returns (secret_key_bytes, public_key_bytes). FALCON-512."""
    pk, sk = _falcon_keygen()
    return sk, pk   # our convention: (sk, pk) everywhere


def sign(message_bytes, secret_key_bytes):
    """Sign with FALCON-512. Returns signature bytes (variable length, max ~752 bytes)."""
    return _falcon_sign(secret_key_bytes, message_bytes)


def verify(message_bytes, signature_bytes, public_key_bytes):
    """Verify FALCON-512 signature. Returns True/False."""
    if isinstance(signature_bytes, str):
        signature_bytes = bytes.fromhex(signature_bytes)
    return _falcon_verify(public_key_bytes, message_bytes, signature_bytes)


def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).digest()


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def public_key_to_address(public_key_bytes):
    """sha256(pubkey) -> 132 bits -> twelve 11-bit BIP39 indices -> dotted words."""
    h = sha256(public_key_bytes)
    bits = int.from_bytes(h[:17], "big") >> (17 * 8 - ADDRESS_WORD_COUNT * WORD_BITS)
    wordlist = _load_wordlist()
    words = []
    for _ in range(ADDRESS_WORD_COUNT):
        words.append(wordlist[bits & ((1 << WORD_BITS) - 1)])
        bits >>= WORD_BITS
    words.reverse()
    return ".".join(words)


def is_valid_address(addr):
    """True if addr is exactly ADDRESS_WORD_COUNT dot-separated words, all
    drawn from the BIP39 wordlist. Does not (and cannot) verify the address
    was actually derived from a real public key -- addresses are one-way
    hashes, so any syntactically valid word sequence passes here. This only
    rejects malformed input (wrong shape, words not in the list), which is
    exactly what's needed to keep non-address strings out of 'to' fields:
    without it, arbitrary text -- including markup -- ends up stored
    on-chain and rendered by the block explorer."""
    if not isinstance(addr, str):
        return False
    words = addr.split(".")
    if len(words) != ADDRESS_WORD_COUNT:
        return False
    wordlist_set = set(_load_wordlist())
    return all(w in wordlist_set for w in words)


def serialize_for_signing(tx_dict):
    """Canonical deterministic serialization excluding 'signature'."""
    fields = {k: v for k, v in tx_dict.items() if k != "signature"}
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Key storage: Argon2id + NaCl secretbox (whitepaper Section 10)
# ---------------------------------------------------------------------------

_OPS  = nacl.pwhash.argon2id.OPSLIMIT_MODERATE
_MEM  = nacl.pwhash.argon2id.MEMLIMIT_MODERATE
_KLEN = nacl.secret.SecretBox.KEY_SIZE  # 32 bytes


def save_key(path, secret_key_bytes, public_key_bytes, passphrase):
    """Encrypt keypair to disk. Passphrase mandatory. File written 0o600."""
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    key  = nacl.pwhash.argon2id.kdf(_KLEN, passphrase.encode(), salt,
                                     opslimit=_OPS, memlimit=_MEM)
    box        = nacl.secret.SecretBox(key)
    ciphertext = box.encrypt(secret_key_bytes)
    data = {
        "public_key": public_key_bytes.hex(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "salt":       base64.b64encode(salt).decode(),
        "ops": _OPS, "mem": _MEM,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def load_pubkey(path):
    """Load public key from key file (no passphrase needed)."""
    with open(path) as f:
        return bytes.fromhex(json.load(f)["public_key"])


def derive_kek(path, passphrase):
    """Derive and return a key-encryption key (KEK) from the passphrase and
    the salt stored in the key file. The KEK is a 32-byte value suitable for
    use with NaCl secretbox. Store this instead of the passphrase; discard
    the passphrase string immediately after calling this function.

    The KEK has the same cryptographic strength as the Argon2id output and
    cannot be used to recover the original passphrase. An attacker with a
    memory dump gets a key-encryption key, not a reusable secret.
    """
    if not passphrase:
        raise ValueError("passphrase is mandatory")
    with open(path) as f:
        data = json.load(f)
    salt = base64.b64decode(data["salt"])
    return nacl.pwhash.argon2id.kdf(
        _KLEN, passphrase.encode(), salt,
        opslimit=data.get("ops", _OPS),
        memlimit=data.get("mem", _MEM),
    )


def decrypt_secret_key(path, passphrase=None, kek=None):
    """Decrypt and return secret key bytes. Never store the result.
    Supply exactly one of passphrase or kek."""
    with open(path) as f:
        data = json.load(f)
    salt       = base64.b64decode(data["salt"])
    ciphertext = base64.b64decode(data["ciphertext"])
    if kek is not None:
        derived = kek
    elif passphrase:
        derived = nacl.pwhash.argon2id.kdf(
            _KLEN, passphrase.encode(), salt,
            opslimit=data.get("ops", _OPS),
            memlimit=data.get("mem", _MEM),
        )
    else:
        raise ValueError("passphrase or kek is required")
    box = nacl.secret.SecretBox(derived)
    try:
        return bytes(box.decrypt(ciphertext))
    except nacl.exceptions.CryptoError:
        raise ValueError("wrong passphrase or corrupted key file")


def sign_with_keyfile(message_bytes, keyfile_path, kek):
    """Decrypt, sign, discard. The only way the node signs.
    kek must be a derived key-encryption key from derive_kek(), never a
    raw passphrase string."""
    sk  = decrypt_secret_key(keyfile_path, kek=kek)
    sig = sign(message_bytes, sk)
    del sk
    return sig
