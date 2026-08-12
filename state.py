"""Balance ledger, nonce tracking, and emission accounting. No disk I/O."""

from params import SUPPLY_CAP, EMISSION_RATE


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
        credits recipients, advances nonce. Fee is burned: debited from
        sender but credited to no one, increasing total_burnt."""
        sender    = tx_dict["from"]
        total_out = sum(o["amount"] for o in tx_dict["outputs"])
        fee       = tx_dict["fee"]

        self.debit(sender, total_out + fee)
        for out in tx_dict["outputs"]:
            self.credit(out["to"], out["amount"])
        self.set_nonce(sender, tx_dict["nonce"])
        self.total_burnt += fee

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def compute_block_reward(self):
        """Compute the reward for the next accepted block.

        can_mint = SUPPLY_CAP - total_minted + total_burnt
        reward   = int(can_mint * (1 - EMISSION_RATE))

        Burnt fees flow back into can_mint, sustaining rewards indefinitely
        at high network usage. At low usage, emission decays toward zero.
        """
        can_mint = SUPPLY_CAP - self.total_minted + self.total_burnt
        if can_mint <= 0:
            return 0
        return int(can_mint * (1 - EMISSION_RATE))

    def apply_reward(self, builder_addr, reward):
        """Credit block reward to the builder. Increments total_minted."""
        if reward <= 0:
            return
        self.credit(builder_addr, reward)
        self.total_minted += reward

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

    def restore(self, snapshot):
        self._balances    = snapshot._balances.copy()
        self._nonces      = snapshot._nonces.copy()
        self.total_minted = snapshot.total_minted
        self.total_burnt  = snapshot.total_burnt

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def all_balances(self):
        return dict(self._balances)

    def all_nonces(self):
        return dict(self._nonces)
