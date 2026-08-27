"""Ciphertext transaction format: creation, serialization, validation.

Every transaction is submitted as an opaque, time-lock-encrypted ciphertext
instead of plaintext. There are two dict shapes on chain, both tagged with
a "kind" field:

  kind == "confirm"  A puzzle submission (wrapper). Visible fields: puzzle
                      parameters (N, x -- T is the fixed protocol constant,
                      not repeated per-tx), the ciphertext blob, and an
                      ordinary wrapper: broadcaster address, signature, fee.
                      Checked exactly like the old fee/signature/balance
                      checks, but against the broadcaster, who need not be
                      the real sender -- the real sender/receiver/amount/
                      nonce live inside the encrypted payload, invisible
                      until resolution. This is what protects against
                      sender-address-level targeting.

  kind == "resolve"   A published solution to a previously-confirmed
                      puzzle: the confirmed tx's hash, the resolver's
                      address (paid the confirmation's fee if this is the
                      one that lands first), the solved key K (hex), and
                      the decrypted inner payload. Verifying a resolution
                      is O(1) (one AEAD decrypt via timelock.verify_
                      resolution) -- it does not require redoing the T
                      squarings, even though finding K did.

Fees stay deterministic (compute_fee, unchanged in spirit): a variable
sender-bid fee would be a discriminable metadata signal on the wrapper
even when the wrapper's signer isn't the real sender, and paying more to
jump the queue would directly contradict the gapless front-of-queue
ordering rule that delivers censorship resistance.
"""

import secrets

import crypto
from crypto import canonical_json
import timelock as timelock_mod
from params import FEE_HEIGHT_MAX_AGE, TIMELOCK_ITERATIONS

# Nonces only need to be unique per sender, not sequential: the gapless
# front-of-queue block validity rule already forces resolution order to
# equal confirmation order, so a sender's own transactions are already
# applied in a deterministic order without any help from the nonce. A
# fixed-width random value gives replay protection (state.has_used_nonce)
# without requiring the sender to track a running counter across restarts
# or across still-unresolved, in-flight sends.
NONCE_BYTES = 16


def generate_nonce() -> str:
    """A fresh, effectively-unique nonce for a new inner payload."""
    return secrets.token_hex(NONCE_BYTES)


def _valid_nonce_format(nonce) -> bool:
    if not isinstance(nonce, str) or len(nonce) != NONCE_BYTES * 2:
        return False
    try:
        bytes.fromhex(nonce)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Inner payload: the real transfer, invisible until resolution
# ---------------------------------------------------------------------------

def create_inner_payload(from_addr, pubkey_hex, outputs, nonce, secret_key_bytes):
    """Build and sign the real transfer. This dict is what gets encrypted
    into a confirmation's ciphertext; it never appears on chain directly.

    nonce: a unique value for this sender, e.g. from generate_nonce().
    """
    payload = {
        "from":    from_addr,
        "pubkey":  pubkey_hex,
        "outputs": outputs,
        "nonce":   nonce,
    }
    msg = crypto.serialize_for_signing(payload)
    sig = crypto.sign(msg, secret_key_bytes)
    payload["signature"] = sig.hex()
    return payload


_INNER_REQUIRED_FIELDS = ["from", "pubkey", "outputs", "nonce", "signature"]


def _check_inner_fields_and_outputs(payload):
    if not isinstance(payload, dict):
        return False, "inner payload is not a dict"
    for field in _INNER_REQUIRED_FIELDS:
        if field not in payload:
            return False, f"inner payload missing field: {field}"
    outputs = payload["outputs"]
    if not isinstance(outputs, list) or not outputs:
        return False, "inner outputs must be a non-empty list"
    for out in outputs:
        if "to" not in out or "amount" not in out:
            return False, "each inner output must have 'to' and 'amount'"
        if not isinstance(out["amount"], int) or out["amount"] <= 0:
            return False, "inner output amounts must be positive integers"
        if not crypto.is_valid_address(out["to"]):
            return False, f"invalid inner address format: {out['to']!r}"
    if not _valid_nonce_format(payload["nonce"]):
        return False, "inner nonce must be a fixed-width hex string"
    return True, None


def _check_signature_generic(d, addr_field):
    """Shared by confirmation and inner-payload signature checks: both
    verify pubkey/signature against the same fields, just under a
    different name for the signer's address ('broadcaster' vs 'from')."""
    pubkey_hex = d["pubkey"]
    sig_hex    = d["signature"]
    if not isinstance(pubkey_hex, str) or not isinstance(sig_hex, str):
        return False, "pubkey and signature must be hex strings"
    try:
        pubkey_bytes = bytes.fromhex(pubkey_hex)
        sig_bytes    = bytes.fromhex(sig_hex)
        if crypto.public_key_to_address(pubkey_bytes) != d[addr_field]:
            return False, f"pubkey does not match {addr_field} address"
        if not crypto.verify(crypto.serialize_for_signing(d), sig_bytes, pubkey_bytes):
            return False, "invalid signature"
    except (ValueError, Exception):
        return False, "malformed pubkey or signature"
    return True, None


def _check_inner_signature(payload):
    return _check_signature_generic(payload, "from")


def _check_inner_nonce(payload, state):
    if state.has_used_nonce(payload["from"], payload["nonce"]):
        return False, f"nonce already used: {payload['nonce']}"
    return True, None


def _check_inner_balance(payload, state):
    total_out = sum(o["amount"] for o in payload["outputs"])
    available = state.get_balance(payload["from"])
    if total_out > available:
        return False, f"insufficient balance: have {available}, need {total_out}"
    return True, None


def validate_inner_payload(payload, state):
    """Validate the decrypted inner payload at resolution time. The fee
    was already collected from the broadcaster at confirmation time, so
    there is no fee field or fee check here."""
    for check, args in (
        (_check_inner_fields_and_outputs, (payload,)),
        (_check_inner_signature,          (payload,)),
        (_check_inner_nonce,              (payload, state)),
        (_check_inner_balance,            (payload, state)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
    return True, None


# ---------------------------------------------------------------------------
# Confirmation: the on-chain ciphertext wrapper
# ---------------------------------------------------------------------------

_CONFIRM_REQUIRED_FIELDS = ["kind", "broadcaster", "pubkey", "fee_height",
                            "fee", "puzzle", "iterations", "signature"]


def create_confirmation(broadcaster_addr, broadcaster_pubkey_hex, inner_payload,
                         fee_height, fee, secret_key_bytes,
                         iterations=TIMELOCK_ITERATIONS):
    """Build and sign a confirmation. The broadcaster need not be the real
    sender inside inner_payload -- the wallet/app layer defaults to using
    the sender's own key for simplicity, but the protocol does not require
    broadcaster == sender.

    iterations is recorded on the tx itself (not secretly per-sender-chosen:
    every validator independently derives the same expected value from
    chain state, mirroring how block.py records and validates
    vdf_iterations). This is what lets a solver know how many squarings a
    given puzzle actually needs even after a later difficulty adjustment
    changes TIMELOCK_ITERATIONS going forward -- without it, an old,
    unresolved puzzle would become ambiguous to solve (see
    timelock.get_timelock_iterations)."""
    payload_bytes = canonical_json(inner_payload)
    puzzle = timelock_mod.generate_puzzle(payload_bytes, iterations=iterations)
    tx = {
        "kind":        "confirm",
        "broadcaster": broadcaster_addr,
        "pubkey":      broadcaster_pubkey_hex,
        "fee_height":  fee_height,
        "fee":         fee,
        "iterations":  iterations,
        # N and x are RSA-scale integers (2048-bit modulus): stored as hex
        # strings on the wire since JSON (and orjson in particular) does
        # not support integers of that size.
        "puzzle": {
            "N":          format(puzzle["N"], "x"),
            "x":          format(puzzle["x"], "x"),
            "ciphertext": puzzle["ciphertext"],
        },
    }
    msg = crypto.serialize_for_signing(tx)
    sig = crypto.sign(msg, secret_key_bytes)
    tx["signature"] = sig.hex()
    return tx


def tx_hash(tx_dict):
    """Deterministic hash of the full tx (confirmation or resolution)
    including signature, matching the existing block/tx ordering pattern."""
    return crypto.sha256_hex(canonical_json(tx_dict))


def tx_size(tx_dict):
    """Fee-basis size: serialized body excluding the signature field."""
    fields = {k: v for k, v in tx_dict.items() if k != "signature"}
    return len(canonical_json(fields))


def tx_size_in_block(tx_dict, position=0):
    """Size of tx_dict as it appears serialized inside a block's JSON array."""
    size = len(canonical_json(tx_dict))
    return size + (1 if position > 0 else 0)


def compute_fee(broadcaster_addr, pubkey_hex, puzzle, fee_height, fee_rate,
                 iterations=TIMELOCK_ITERATIONS):
    """Compute fee = body_size * fee_rate for a confirmation. Deterministic:
    the sender does not choose the fee (see module docstring)."""
    skeleton = {
        "kind": "confirm", "broadcaster": broadcaster_addr, "pubkey": pubkey_hex,
        "fee_height": fee_height, "fee": 0, "iterations": iterations, "puzzle": puzzle,
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
        f"(last fee={fee}, fee_rate={fee_rate}): this is a bug"
    )


def _check_confirm_fields(tx_dict):
    for field in _CONFIRM_REQUIRED_FIELDS:
        if field not in tx_dict:
            return False, f"missing field: {field}"
    if tx_dict["kind"] != "confirm":
        return False, "kind must be 'confirm'"
    puzzle = tx_dict["puzzle"]
    if not isinstance(puzzle, dict) or not all(k in puzzle for k in ("N", "x", "ciphertext")):
        return False, "puzzle must contain N, x, ciphertext"
    if not isinstance(puzzle["N"], str) or not isinstance(puzzle["x"], str):
        return False, "puzzle N and x must be hex strings"
    try:
        int(puzzle["N"], 16)
        int(puzzle["x"], 16)
    except ValueError:
        return False, "puzzle N and x must be valid hex"
    if not isinstance(puzzle["ciphertext"], str):
        return False, "puzzle ciphertext must be a hex string"
    try:
        bytes.fromhex(puzzle["ciphertext"])
    except ValueError:
        return False, "puzzle ciphertext is not valid hex"
    fee = tx_dict["fee"]
    if not isinstance(fee, int) or fee < 0:
        return False, "fee must be a non-negative integer"
    if not isinstance(tx_dict["iterations"], int) or tx_dict["iterations"] <= 0:
        return False, "iterations must be a positive integer"
    return True, None


def _check_confirm_signature(tx_dict):
    return _check_signature_generic(tx_dict, "broadcaster")


def _check_confirm_iterations(tx_dict, expected_iterations):
    """iterations is not sender-chosen: it must match what every validator
    independently derives from chain state for the current difficulty
    epoch (timelock.get_timelock_iterations), the same way block.py
    validates vdf_iterations. expected_iterations is None when the caller
    doesn't have chain context (e.g. some unit tests); skip in that case
    rather than force every test to wire up a real chain."""
    if expected_iterations is None:
        return True, None
    if tx_dict["iterations"] != expected_iterations:
        return False, (f"iterations mismatch: tx has {tx_dict['iterations']}, "
                       f"chain expects {expected_iterations}")
    return True, None


def _check_confirm_fee(tx_dict, chain_tip_height, get_fee_rate_at_height):
    fh = tx_dict["fee_height"]
    if not isinstance(fh, int):
        return False, "fee_height must be an integer"
    if fh > chain_tip_height:
        return False, f"fee_height {fh} is in the future (tip={chain_tip_height})"
    if fh < chain_tip_height - (FEE_HEIGHT_MAX_AGE - 1):
        return False, f"fee_height {fh} is too old (tip={chain_tip_height}, max_age={FEE_HEIGHT_MAX_AGE})"
    fee_rate = get_fee_rate_at_height(fh)
    if fee_rate is None:
        return False, f"no fee rate at height {fh}"
    try:
        expected = compute_fee(tx_dict["broadcaster"], tx_dict["pubkey"],
                                tx_dict["puzzle"], fh, fee_rate,
                                iterations=tx_dict["iterations"])
    except ValueError as e:
        return False, f"fee computation error: {e}"
    if tx_dict["fee"] != expected:
        return False, f"fee mismatch: expected {expected}, got {tx_dict['fee']}"
    return True, None


def _check_confirm_balance(tx_dict, state):
    """The broadcaster only needs to cover the fee -- the real transfer
    amount is inside the encrypted payload and is checked against the real
    sender's balance at resolution time, not here."""
    available = state.get_balance(tx_dict["broadcaster"])
    if tx_dict["fee"] > available:
        return False, f"insufficient balance: have {available}, need {tx_dict['fee']}"
    return True, None


def validate_confirmation(tx_dict, state, chain_tip_height, get_fee_rate_at_height,
                          expected_iterations=None):
    """Validate a confirmation. Returns (True, None) or (False, error_string).

    expected_iterations: the difficulty this height requires, from
    timelock.get_timelock_iterations(chain). None skips the check (tests
    that don't wire up a real chain)."""
    for check, args in (
        (_check_confirm_fields,     (tx_dict,)),
        (_check_confirm_signature,  (tx_dict,)),
        (_check_confirm_iterations, (tx_dict, expected_iterations)),
        (_check_confirm_fee,        (tx_dict, chain_tip_height, get_fee_rate_at_height)),
        (_check_confirm_balance,    (tx_dict, state)),
    ):
        ok, err = check(*args)
        if not ok:
            return False, err
    return True, None


# ---------------------------------------------------------------------------
# Resolution: a published solution to a confirmed puzzle
# ---------------------------------------------------------------------------

_RESOLVE_REQUIRED_FIELDS = ["kind", "confirmed_tx_hash", "resolver", "K_hex", "payload"]


def create_resolution(confirmed_tx_hash, resolver_addr, K, payload):
    """Anyone can build a resolution once they have solved the puzzle
    (solve K via timelock.solve_for_key, decrypt via decrypt_with_key to
    get payload back). No signature is required or possible here: there is
    no cryptographic way to prove who solved a puzzle first, since the
    answer space has no room for an identity nonce (whitepaper: known,
    accepted fee-fairness limitation, not a security one)."""
    return {
        "kind":              "resolve",
        "confirmed_tx_hash": confirmed_tx_hash,
        "resolver":          resolver_addr,
        "K_hex":             format(K, "x"),
        "payload":           payload,
    }


def _check_resolve_fields(res_dict):
    for field in _RESOLVE_REQUIRED_FIELDS:
        if field not in res_dict:
            return False, f"missing field: {field}"
    if res_dict["kind"] != "resolve":
        return False, "kind must be 'resolve'"
    if not crypto.is_valid_address(res_dict["resolver"]):
        return False, "invalid resolver address"
    try:
        int(res_dict["K_hex"], 16)
    except (ValueError, TypeError):
        return False, "K_hex must be a hex string"
    return True, None


def validate_resolution(res_dict, confirmed_tx, state):
    """Validate a resolution against the confirmation it claims to resolve
    and current chain state.

    confirmed_tx: the "confirm" tx dict this resolution claims to solve,
    looked up by the caller via res_dict["confirmed_tx_hash"].
    """
    ok, err = _check_resolve_fields(res_dict)
    if not ok:
        return False, err

    if confirmed_tx is None:
        return False, "confirmed_tx_hash does not reference a known confirmation"

    K = int(res_dict["K_hex"], 16)
    puzzle = confirmed_tx["puzzle"]
    N = int(puzzle["N"], 16)
    expected_bytes = canonical_json(res_dict["payload"])
    if not timelock_mod.verify_resolution(N, K, puzzle["ciphertext"], expected_bytes):
        return False, "resolution does not decrypt the puzzle's ciphertext"

    ok, err = validate_inner_payload(res_dict["payload"], state)
    if not ok:
        return False, f"invalid inner payload: {err}"
    return True, None


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def sort_key(t):
    """Canonical ordering key for confirmations: (fee_height, tx_hash).
    There is no per-broadcaster nonce at the wrapper level (the real
    sender's nonce lives inside the encrypted payload and only matters at
    resolution), so unlike the old plaintext format, ordering does not
    need a nonce component to keep one sender's txs in sequence."""
    return (t["fee_height"], tx_hash(t))


def sort_txs(tx_list):
    """Sort confirmations by the deterministic ordering rule."""
    keyed = [(sort_key(t), t) for t in tx_list]
    return [t for _, t in sorted(keyed)]
