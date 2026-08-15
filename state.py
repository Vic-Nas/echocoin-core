"""Balance ledger, nonce tracking, and emission accounting. No disk I/O."""

from params import EMISSION_RATE, SUPPLY_CAP
from pob import BURN_ADDRESS


def compute_reward(total_minted: int, total_burnt: int) -> int:
    """Single source of truth for block reward. Used by State and NodeView stats."""
    can_mint = SUPPLY_CAP - total_minted + total_burnt
    if can_mint <= 0:
        return 0
    return int(can_mint * (1 - EMISSION_RATE))


class State:
    def __init__(self):
        self._balances    = {}  # addr -> int (rings)
        self._nonces      = {}  # addr -> int (last used nonce, 0 = never transacted)
        self.total_minted = 0   # rings minted via block rewards since genesis
        self.total_burnt  = 0   # rings destroyed via fee burns since genesis

    # ------------------------------------------------------------------
    # Balance and nonce access
    # ------------------------------------------------------------------

    def get_balance(self, addr):
        return self._balances.get(addr, 0)

    def get_nonce(self, addr):
        return self._nonces.get(addr, 0)

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

    def set_nonce(self, addr, nonce):
        self._nonces[addr] = nonce

    # ------------------------------------------------------------------
    # Transaction application
    # ------------------------------------------------------------------

    def apply_tx(self, tx_dict):
        """Apply a validated transaction. Debits sender (outputs + fee),
        credits recipients, advances nonce.

        Fee burns and intentional PoB burns (outputs to BURN_ADDRESS) both
        increase total_burnt, which feeds back into the emission formula.
        """
        sender    = tx_dict["from"]
        total_out = sum(o["amount"] for o in tx_dict["outputs"])
        fee       = tx_dict["fee"]

        self.debit(sender, total_out + fee)
        for out in tx_dict["outputs"]:
            if out["to"] == BURN_ADDRESS:
                self.total_burnt += out["amount"]   # intentional PoB burn
            else:
                self.credit(out["to"], out["amount"])
        self.set_nonce(sender, tx_dict["nonce"])
        self.total_burnt += fee                     # fee burn

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def compute_block_reward(self) -> int:
        """Compute the reward for the next accepted block."""
        return compute_reward(self.total_minted, self.total_burnt)

    def apply_reward_distribution(self, distribution):
        """Credit a pre-computed reward distribution from pob.reward_distribution().

        distribution: list of (address, amount) pairs.
        Each amount is credited independently. total_minted is incremented
        by the sum actually distributed (may be slightly less than the full
        reward due to integer rounding -- remainder stays in can_mint).
        """
        for addr, amount in distribution:
            if amount >= 1:
                self.credit(addr, amount)
                self.total_minted += amount

    # ------------------------------------------------------------------
    # Construction from persisted data
    # ------------------------------------------------------------------

    @classmethod
    def from_snapshot(cls, balances: dict, nonces: dict,
                      total_minted: int, total_burnt: int) -> "State":
        """Restore a State from persisted data. Replaces direct field assignment."""
        s = cls()
        s._balances    = balances
        s._nonces      = nonces
        s.total_minted = total_minted
        s.total_burnt  = total_burnt
        return s

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self):
        """Return a copy for use as a rollback probe. Safe because keys are
        interned strings and values are ints -- both immutable."""
        s = State()
        s._balances    = self._balances.copy()
        s._nonces      = self._nonces.copy()
        s.total_minted = self.total_minted
        s.total_burnt  = self.total_burnt
        return s

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def all_balances(self):
        return dict(self._balances)

    def all_nonces(self):
        return dict(self._nonces)
