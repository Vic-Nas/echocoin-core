"""Balance ledger, nonce tracking, and emission accounting. No disk I/O."""

from params import EMISSION_RATE, SUPPLY_CAP


def compute_can_mint(total_minted: int) -> int:
    """Ticks still mintable: SUPPLY_CAP - total_minted, floored at 0.

    Single source of truth for the mintable pool.
    """
    return max(0, SUPPLY_CAP - total_minted)


def compute_reward(total_minted: int) -> int:
    """Single source of truth for block reward. Used by State and NodeView stats."""
    return int(compute_can_mint(total_minted) * (1 - EMISSION_RATE))


class State:
    def __init__(self):
        self._balances    = {}  # addr -> int (ticks)
        self._used_nonces = {}  # addr -> set of nonce strings already applied
        self.total_minted = 0   # ticks minted via block rewards since genesis
        self._escrow      = {}  # confirmed_tx_hash -> fee ticks awaiting a resolver

    # ------------------------------------------------------------------
    # Balance and nonce access
    # ------------------------------------------------------------------
    #
    # A nonce here only needs to be unique per sender, not sequential. The
    # gapless front-of-queue block validity rule already forces resolution
    # order to equal confirmation order, so a sender's own transactions are
    # already applied in a fully deterministic order without any help from
    # the nonce. The nonce's only remaining job is replay protection (the
    # same signed inner payload cannot be applied twice), which a used-once
    # check gives for free without requiring the sender to track a running
    # counter (see tx.generate_nonce).

    def get_balance(self, addr):
        return self._balances.get(addr, 0)

    def has_used_nonce(self, addr, nonce):
        return nonce in self._used_nonces.get(addr, ())

    def nonce_count(self, addr):
        """Number of nonces this address has spent so far (display only)."""
        return len(self._used_nonces.get(addr, ()))

    def credit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"credit amount must be positive, got {amount}")
        self._balances[addr] = self.get_balance(addr) + amount

    def debit(self, addr, amount):
        if amount <= 0:
            raise ValueError(f"debit amount must be positive, got {amount}")
        bal = self.get_balance(addr)
        if bal < amount:
            raise ValueError(f"debit would make balance negative: {bal} - {amount}")
        self._balances[addr] = bal - amount

    def mark_nonce_used(self, addr, nonce):
        self._used_nonces.setdefault(addr, set()).add(nonce)

    # ------------------------------------------------------------------
    # Transaction application
    # ------------------------------------------------------------------

    def apply_confirmation(self, confirm_tx, confirmed_tx_hash):
        """Apply a "confirm" ciphertext submission: debits the broadcaster's
        fee into escrow, to be paid out to whichever resolver's solution
        lands first (tx.py module docstring). The real transfer inside the
        encrypted payload is not touched here -- it isn't visible yet."""
        fee = confirm_tx["fee"]
        broadcaster = confirm_tx["broadcaster"]
        if fee > 0:
            self.debit(broadcaster, fee)
            self._escrow[confirmed_tx_hash] = fee

    def apply_inner_payload(self, payload):
        """Apply the real transfer revealed by a resolution. No fee here --
        the fee was already collected from the broadcaster at confirmation
        time (see apply_confirmation)."""
        sender    = payload["from"]
        total_out = sum(o["amount"] for o in payload["outputs"])

        self.debit(sender, total_out)
        for out in payload["outputs"]:
            self.credit(out["to"], out["amount"])
        self.mark_nonce_used(sender, payload["nonce"])

    def apply_resolution(self, res_dict):
        """Apply a validated resolution: releases escrow (if any) to the
        resolver, then applies the revealed inner transfer."""
        confirmed_hash = res_dict["confirmed_tx_hash"]
        fee = self._escrow.pop(confirmed_hash, 0)
        if fee > 0:
            self.credit(res_dict["resolver"], fee)
        self.apply_inner_payload(res_dict["payload"])

    def escrowed_fee(self, confirmed_tx_hash):
        return self._escrow.get(confirmed_tx_hash, 0)

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def compute_can_mint(self) -> int:
        """Ticks still available to mint."""
        return compute_can_mint(self.total_minted)

    def compute_block_reward(self) -> int:
        """Compute the reward for the next accepted block."""
        return compute_reward(self.total_minted)

    def apply_reward_distribution(self, distribution):
        """Credit a pre-computed reward distribution.

        distribution: list of (address, amount) pairs.
        Each amount is credited independently. total_minted is incremented
        by the sum actually distributed (may be slightly less than the full
        reward due to integer rounding; remainder stays in can_mint).
        """
        for addr, amount in distribution:
            if amount >= 1:
                self.credit(addr, amount)
                self.total_minted += amount

    # ------------------------------------------------------------------
    # Construction from persisted data
    # ------------------------------------------------------------------

    @classmethod
    def from_snapshot(cls, balances: dict, used_nonces: dict,
                      total_minted: int) -> "State":
        """Restore a State from persisted data. Replaces direct field assignment.

        used_nonces: addr -> set/list of nonce strings already spent.
        """
        s = cls()
        s._balances    = balances
        s._used_nonces = {addr: set(nonces) for addr, nonces in used_nonces.items()}
        s.total_minted = total_minted
        return s

    def all_escrow(self):
        return dict(self._escrow)

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self):
        """Return a copy for use as a rollback probe. Safe because keys are
        interned strings and values are ints, both immutable."""
        s = State()
        s._balances    = self._balances.copy()
        s._used_nonces = {addr: set(nonces) for addr, nonces in self._used_nonces.items()}
        s.total_minted = self.total_minted
        s._escrow      = self._escrow.copy()
        return s

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def all_balances(self):
        return dict(self._balances)

    def all_used_nonces(self):
        return {addr: set(nonces) for addr, nonces in self._used_nonces.items()}
