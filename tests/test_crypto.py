"""Crypto module tests: key gen, signing, verification, address, key storage."""
import os
import tempfile

import pytest
from helpers import *


def test_keypair_unique():
    _, pk1, _, _ = make_keypair()
    _, pk2, _, _ = make_keypair()
    assert pk1 != pk2


def test_sign_verify():
    sk, pk, _, _ = make_keypair()
    msg = b"test message"
    sig = crypto.sign(msg, sk)
    assert crypto.verify(msg, sig, pk)


def test_wrong_key_rejects():
    sk, _pk, _, _ = make_keypair()
    _, pk2, _, _ = make_keypair()
    msg = b"test message"
    sig = crypto.sign(msg, sk)
    assert not crypto.verify(msg, sig, pk2)


def test_tampered_message_rejects():
    sk, pk, _, _ = make_keypair()
    msg = b"original"
    sig = crypto.sign(msg, sk)
    assert not crypto.verify(b"tampered", sig, pk)


def test_address_deterministic():
    _, pk, _, _ = make_keypair()
    assert crypto.public_key_to_address(pk) == crypto.public_key_to_address(pk)


def test_address_12_words():
    _, pk, _, _ = make_keypair()
    addr = crypto.public_key_to_address(pk)
    assert len(addr.split(".")) == 12


def test_different_keys_different_addresses():
    _, pk1, _, _ = make_keypair()
    _, pk2, _, _ = make_keypair()
    assert crypto.public_key_to_address(pk1) != crypto.public_key_to_address(pk2)


def test_serialize_for_signing_excludes_signature():
    data = {"from": "alice", "to": "bob", "amount": 100, "signature": "excluded"}
    s = crypto.serialize_for_signing(data)
    assert b"signature" not in s
    assert b"alice" in s


def test_serialize_deterministic():
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert crypto.serialize_for_signing(d1) == crypto.serialize_for_signing(d2)


# --- Key storage ---

def test_save_and_load_key():
    sk, pk, _, _ = make_keypair()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        crypto.save_key(path, sk, pk, "testpassphrase123")
        pk_loaded = crypto.load_pubkey(path)
        assert pk_loaded == pk

        sk_loaded = crypto.decrypt_secret_key(path, "testpassphrase123")
        assert sk_loaded == sk
    finally:
        os.unlink(path)


def test_wrong_passphrase_raises():
    sk, pk, _, _ = make_keypair()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        crypto.save_key(path, sk, pk, "correct")
        with pytest.raises(ValueError, match="wrong passphrase"):
            crypto.decrypt_secret_key(path, "wrong")
    finally:
        os.unlink(path)


def test_empty_passphrase_raises():
    sk, pk, _, _ = make_keypair()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        with pytest.raises(ValueError, match="mandatory"):
            crypto.save_key(path, sk, pk, "")
    finally:
        os.unlink(path)


def test_keyfile_is_0600():
    sk, pk, _, _ = make_keypair()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        crypto.save_key(path, sk, pk, "testpassphrase123")
        mode = oct(os.stat(path).st_mode)[-3:]
        assert mode == "600"
    finally:
        os.unlink(path)


def test_sign_with_keyfile():
    sk, pk, _, _ = make_keypair()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        crypto.save_key(path, sk, pk, "pw123456")
        msg = b"sign this"
        kek = crypto.derive_kek(path, "pw123456")
        sig = crypto.sign_with_keyfile(msg, path, kek)
        assert crypto.verify(msg, sig, pk)
    finally:
        os.unlink(path)
