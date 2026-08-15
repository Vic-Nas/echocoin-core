"""Transaction creation, serialization, validation. Pure functions on dicts."""

import crypto
from crypto import canonical_json
from pob import BURN_ADDRESS
from params import FEE_HEIGHT_MAX_AGE


def create(from_addr, pubkey_hex, outputs, nonce, fee_height, fee, secret_key_bytes):
    """Build and sign a transaction. Returns tx dict with signature."""
    tx = {
        "from":       from_addr,
        "pubkey":     pubkey_hex,
        "outputs":    outputs,
        "nonce":      nonce,
        "fee_height": fee_height,
        "fee":        fee,
    }
    msg = crypto.serialize_for_signing(tx)
    sig = crypto.sign(msg, secret_key_bytes)
    tx["signature"] = sig.hex()
    return tx


def tx_hash(tx_dict):
    """Deterministic hash of the full tx including signature."""
    return crypto.sha256_hex(canonical_json(tx_dict))


def tx_size(tx_dict):
    """Fee-basis size: serialized body excluding the signature field.
    The signature is not under the sender's control so is not priced."""
    fields = {k: v for k, v in tx_dict.items() if k != "signature"}
    return len(canonical_json(fields).encode())


def tx_size_in_block(tx_dict, position=0):
    """Size of tx_dict as it appears serialized inside a block's JSON array.
    Position 0 = first element (no leading comma). Position > 0 adds 1 byte
    for the comma separator between elements.
    Used by block.assemble() to track running block size without re-serializing
    the entire block on every candidate tx.
    """
    size = len(canonical_json(tx_dict).encode())
    return size + (1 if position > 0 else 0)


def compute_fee(from_addr, pubkey_hex, outputs, nonce, fee_height, fee_rate):
    """Compute fee = body_size * fee_rate.

    The fee field itself contributes to body_size (its digit count is priced).
    Two passes always suffice: pass 1 uses fee=0 to get a baseline size, pass 2
    uses that fee to get the exact size. A third pass is only needed if the fee
    value crosses a digit-count boundary (e.g. 9999 -> 10000), which is handled
    by the loop. Convergence beyond 3 iterations is not possible for any
    realistic fee_rate; ValueError indicates a bug in the caller.
    """
    skeleton = {
        "from": from_addr, "pubkey": pubkey_hex, "outputs": outputs,
        "nonce": nonce, "fee_height": fee_height, "fee": 0,
    }
    fee = 0
    for _ in range(4):
        skeleton["fee"] = fee
        new_fee = tx_size(skeleton) * fee_rate
        if new_fee == fee:
            return fee
        fee = new_fee
    raise ValueError(
        f"compute_fee did not converge after 4 iterations "
        f"(last fee={fee}, fee_rate={fee_rate}) -- this is a bug"
    )



_REQUIRED_FIELDS = ["from", "pubkey", "outputs", "nonce", "fee_height", "fee", "signature"]


def _check_fields_and_outputs(tx_dict):
    for field in _REQUIRED_FIELDS:
        if field not in tx_dict:
            return False, f"missing field: {field}"
    outputs = tx_dict["outputs"]
    if not isinstance(outputs, list) or not outputs:
        return False, "outputs must be a non-empty list"
    for out in outputs:
        if "to" not in out or "amount" not in out:
            return False, "each output must have 'to' and 'amount'"
        if not isinstance(out["amount"], int) or out["amount"] <= 0:
            return False, "output amounts must be positive integers"
        if out["to"] != BURN_ADDRESS and not crypto.is_valid_address(out["to"]):
            return False, f"invalid address format: {out['to']!r}"
        if out["to"] == BURN_ADDRESS:
            beneficiary = out.get("beneficiary")
            if beneficiary is not None and not crypto.is_valid_address(beneficiary):
                return False, f"burn output has invalid beneficiary: {beneficiary!r}"
    fee = tx_dict["fee"]
    if not isinstance(fee, int) or fee < 0:
        return False, "fee must be a non-negative integer"
    return True, None


def _check_signature(tx_dict):
    pubkey_hex = tx_dict["pubkey"]
    sig_hex    = tx_dict["signature"]
    if not isinstance(pubkey_hex, str) or not isinstance(sig_hex, str):
        return False, "pubkey and signature must be hex strings"
    try:
        pubkey_bytes = bytes.fromhex(pubkey_hex)
        sig_bytes    = bytes.fromhex(sig_hex)
    except ValueError:
        return False, "pubkey or signature is not valid hex"
    if crypto.public_key_to_address(pubkey_bytes) != tx_dict["from"]:
        return False, "pubkey does not match from address"
    if not crypto.verify(crypto.serialize_for_signing(tx_dict), sig_bytes, pubkey_bytes):
        return False, "invalid signature"
    return True, None


def _check_nonce(tx_dict, state):
    current = state.get_nonce(tx_dict["from"])
    if tx_dict["nonce"] != current + 1:
        return False, f"bad nonce: expected {current + 1}, got {tx_dict['nonce']}"
    return True, None


def _check_fee(tx_dict, chain_tip_height, get_fee_rate_at_height):
    fh = tx_dict["fee_height"]
    if not isinstance(fh, int):
        return False, "fee_height must be an integer"
    if fh > chain_tip_height:
        return False, "fee_height is in the future"
    if fh < chain_tip_height - (FEE_HEIGHT_MAX_AGE - 1):
        return False, "fee_height is too old"
    fee_rate = get_fee_rate_at_height(fh)
    if fee_rate is None:
        return False, f"no fee rate at height {fh}"
    try:
        expected = compute_fee(
            tx_dict["from"], tx_dict["pubkey"], tx_dict["outputs"],
            tx_dict["nonce"], fh, fee_rate,
        )
    except ValueError as e:
        return False, f"fee computation error: {e}"
    if tx_dict["fee"] != expected:
        return False, f"fee mismatch: expected {expected}, got {tx_dict['fee']}"
    return True, None


def _check_balance(tx_dict, state):
    total_out = sum(o["amount"] for o in tx_dict["outputs"])
    if total_out + tx_dict["fee"] > state.get_balance(tx_dict["from"]):
        return False, "insufficient balance"
    return True, None


def validate(tx_dict, state, chain_tip_height, get_fee_rate_at_height):
    """Validate a transaction. Returns (True, None) or (False, error_string).

    state: object with .get_balance(addr), .get_nonce(addr)
    get_fee_rate_at_height: callable(height) -> fee_rate or None
    """
    for check, args in (
        (_check_fields_and_outputs, (tx_dict,)),
        (_check_signature,          (tx_dict,)),
        (_check_nonce,              (tx_dict, state)),
        (_check_fee,                (tx_dict, chain_tip_height, get_fee_rate_at_height)),
        (_check_balance,            (tx_dict, state)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
    return True, None


def sort_key(tx_dict):
    """Sort key: (fee_height asc, nonce asc, tx_hash lex)."""
    return (tx_dict["fee_height"], tx_dict["nonce"], tx_hash(tx_dict))


def sort_txs(tx_list):
    """Sort transactions by the deterministic ordering rule."""
    return sorted(tx_list, key=sort_key)
