"""Balance ledger and nonce tracking. In-memory dict operations, no disk I/O."""


class State:
    def __init__(self):
        self._balances = {}   # addr -> int
        self._nonces = {}     # addr -> int (last used nonce, 0 = never transacted)

    def get_balance(self, addr):
        return self._balances.get(addr, 0)

    def get_nonce(self, addr):
        return self._nonces.get(addr, 0)

    def credit(self, addr, amount):
        """Add amount to address. Amount must be positive."""
        if amount <= 0:
            raise ValueError(f"credit amount must be positive, got {amount}")
        self._balances[addr] = self.get_balance(addr) + amount

    def debit(self, addr, amount):
        """Subtract amount from address. Amount must be positive. Balance must not go negative."""
        if amount <= 0:
            raise ValueError(f"debit amount must be positive, got {amount}")
        bal = self.get_balance(addr)
        if bal < amount:
            raise ValueError(f"debit would make balance negative: {bal} - {amount}")
        self._balances[addr] = bal - amount

    def set_nonce(self, addr, nonce):
        self._nonces[addr] = nonce

    def apply_tx(self, tx_dict):
        """
        Apply a validated transaction to state.
        Debits sender (outputs + fee), credits recipients, updates nonce.
        Fee is burned (debited but not credited anywhere).
        """
        sender = tx_dict["from"]
        total_out = sum(o["amount"] for o in tx_dict["outputs"])
        fee = tx_dict["fee"]

        self.debit(sender, total_out + fee)
        for out in tx_dict["outputs"]:
            self.credit(out["to"], out["amount"])
        self.set_nonce(sender, tx_dict["nonce"])

    def apply_rewards(self, reward_map):
        """
        Apply block rewards. reward_map: {addr: amount}.
        """
        for addr, amount in reward_map.items():
            if amount > 0:
                self.credit(addr, amount)

    def snapshot(self):
        """Return a copy for use as a rollback probe. Shallow copy is safe
        because keys are interned strings and values are ints -- both immutable."""
        s = State()
        s._balances = self._balances.copy()
        s._nonces   = self._nonces.copy()
        return s

    def restore(self, snapshot):
        """Restore from a snapshot."""
        self._balances = snapshot._balances.copy()
        self._nonces   = snapshot._nonces.copy()

    def all_balances(self):
        """Return a copy of all nonzero balances."""
        return dict(self._balances)

    def all_nonces(self):
        """Return a copy of all nonces (including zero-balance addresses)."""
        return dict(self._nonces)
