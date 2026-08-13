"""
Chain and state persistence via SQLite. All disk I/O lives here.

Schema:
  blocks(height INTEGER PK, hash TEXT, data TEXT)  -- full block JSON
  state(addr TEXT PK, balance INTEGER, nonce INTEGER)
  emission(key TEXT PK, value INTEGER)  -- total_minted, total_burnt
  meta(key TEXT PK, value TEXT)

Blocks are the source of truth. State is rebuilt from blocks on open
if the stored state is missing or the chain was extended offline.
The node calls storage after every block is validated and applied.
"""

import json
import logging
import sqlite3

log = logging.getLogger("ec.storage")


def _conn(path):
    c = sqlite3.connect(path, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


class Storage:
    def __init__(self, path):
        self.path = path
        self.db   = _conn(path)
        self._init_schema()

    def _init_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                hash   TEXT NOT NULL,
                data   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                addr    TEXT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                nonce   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS emission (
                key   TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tx_index (
                tx_hash      TEXT PRIMARY KEY,
                block_height INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS addr_index (
                addr         TEXT NOT NULL,
                tx_hash      TEXT NOT NULL,
                block_height INTEGER NOT NULL,
                PRIMARY KEY (addr, tx_hash)
            );
            CREATE INDEX IF NOT EXISTS addr_index_addr ON addr_index(addr);
        """)
        self.db.commit()

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------

    def _index_block(self, blk, tx_hashes=None):
        """Insert tx_index and addr_index rows for all transactions in blk.
        tx_hashes: optional list of pre-computed hashes (same order as blk["transactions"]).
        If omitted, hashes are computed here using json+sha256 directly to avoid
        importing tx_mod (keeping storage.py free of domain-logic dependencies).
        Must be called inside a transaction."""
        import hashlib, json as _json
        height = blk["height"]
        txs = blk.get("transactions", [])
        if tx_hashes is None:
            tx_hashes = [
                hashlib.sha256(
                    _json.dumps(t, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                for t in txs
            ]
        for t, h in zip(txs, tx_hashes):
            self.db.execute(
                "INSERT OR IGNORE INTO tx_index(tx_hash, block_height) VALUES(?,?)",
                (h, height)
            )
            addrs = {t["from"]} | {o["to"] for o in t.get("outputs", [])}
            for addr in addrs:
                self.db.execute(
                    "INSERT OR IGNORE INTO addr_index(addr, tx_hash, block_height) VALUES(?,?,?)",
                    (addr, h, height)
                )

    def save_block(self, blk):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO blocks(height, hash, data) VALUES(?,?,?)",
                (blk["height"], blk["hash"], json.dumps(blk))
            )
            self._index_block(blk)

    def get_tx_height(self, tx_hash):
        row = self.db.execute(
            "SELECT block_height FROM tx_index WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        return row[0] if row else None

    def get_tx_heights_for_addr(self, addr):
        rows = self.db.execute(
            "SELECT block_height, tx_hash FROM addr_index WHERE addr=? ORDER BY block_height DESC",
            (addr,)
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def load_block(self, height):
        row = self.db.execute(
            "SELECT data FROM blocks WHERE height=?", (height,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def load_all_blocks(self):
        rows = self.db.execute("SELECT data FROM blocks ORDER BY height").fetchall()
        return [json.loads(r[0]) for r in rows]

    def chain_height(self):
        row = self.db.execute("SELECT MAX(height) FROM blocks").fetchone()
        return row[0] if row and row[0] is not None else -1

    def replace_chain(self, from_height, blocks):
        """Truncate from from_height and save replacement blocks atomically."""
        with self.db:
            self.db.execute("DELETE FROM blocks WHERE height >= ?", (from_height,))
            self.db.execute("DELETE FROM tx_index WHERE block_height >= ?", (from_height,))
            self.db.execute("DELETE FROM addr_index WHERE block_height >= ?", (from_height,))
            self.db.executemany(
                "INSERT OR REPLACE INTO blocks(height, hash, data) VALUES(?,?,?)",
                [(blk["height"], blk["hash"], json.dumps(blk)) for blk in blocks]
            )
            for blk in blocks:
                self._index_block(blk)

    # ------------------------------------------------------------------
    # State snapshots
    # ------------------------------------------------------------------

    def save_state(self, state):
        """Persist full state including emission counters.
        DELETE + INSERT is simpler and faster than a scan-diff-upsert:
        state is small (one row per funded address) and the WAL transaction
        makes it atomic. A crash mid-write leaves a partial state table,
        but the node rebuilds state from blocks on next start if the table
        is empty, so partial writes are safe.
        """
        balances = state.all_balances()
        nonces   = state.all_nonces()
        addrs    = set(balances) | set(nonces)
        rows     = [(addr, balances.get(addr, 0), nonces.get(addr, 0)) for addr in addrs]
        with self.db:
            self.db.execute("DELETE FROM state")
            if rows:
                self.db.executemany(
                    "INSERT INTO state(addr, balance, nonce) VALUES(?,?,?)",
                    rows
                )
            self.db.execute(
                "INSERT OR REPLACE INTO emission(key, value) VALUES('total_minted', ?)",
                (state.total_minted,)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO emission(key, value) VALUES('total_burnt', ?)",
                (state.total_burnt,)
            )

    def load_state(self):
        """Return (balances, nonces, total_minted, total_burnt)."""
        rows     = self.db.execute("SELECT addr, balance, nonce FROM state").fetchall()
        balances = {r[0]: r[1] for r in rows}
        nonces   = {r[0]: r[2] for r in rows}
        em       = {
            r[0]: r[1] for r in
            self.db.execute("SELECT key, value FROM emission").fetchall()
        }
        total_minted = em.get("total_minted", 0)
        total_burnt  = em.get("total_burnt",  0)
        return balances, nonces, total_minted, total_burnt

    def state_exists(self):
        return self.db.execute("SELECT COUNT(*) FROM state").fetchone()[0] > 0

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
        self.db.commit()

    def close(self):
        self.db.close()
