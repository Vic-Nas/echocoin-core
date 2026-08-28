"""
Chain and state persistence via SQLite + peewee ORM.

Schema:
  Block      full block JSON, indexed by height
  State      per-address balance and nonce
  Emission   total_minted singleton
  TxIndex    tx_hash -> block_height lookup
  AddrIndex  addr -> (tx_hash, block_height) lookup

Blocks are the source of truth. State is rebuilt from blocks on open
if the stored state is missing or the chain was extended offline.
The node calls storage after every block is validated and applied.
"""

import json
import logging

import tx as tx_mod
from peewee import (
    SqliteDatabase, Model,
    IntegerField, TextField, CompositeKey,
)

log = logging.getLogger("ec.storage")

db = SqliteDatabase(None, pragmas={"journal_mode": "wal", "synchronous": "normal"},
                    check_same_thread=False)


class _Base(Model):
    class Meta:
        database = db


class Block(_Base):
    height = IntegerField(primary_key=True)
    hash   = TextField()
    data   = TextField()          # full JSON blob


class State(_Base):
    addr    = TextField(primary_key=True)
    balance = IntegerField(default=0)
    nonce   = IntegerField(default=0)


class Emission(_Base):
    key   = TextField(primary_key=True)
    value = IntegerField(default=0)


class Meta(_Base):
    key   = TextField(primary_key=True)
    value = TextField()


class TxIndex(_Base):
    tx_hash      = TextField(primary_key=True)
    block_height = IntegerField()


class AddrIndex(_Base):
    addr         = TextField()
    tx_hash      = TextField()
    block_height = IntegerField()

    class Meta:
        primary_key = CompositeKey("addr", "tx_hash")
        indexes = ((("addr",), False),)


_TABLES = [Block, State, Emission, Meta, TxIndex, AddrIndex]


class Storage:
    def __init__(self, path):
        self.path = path
        db.init(path)
        db.connect(reuse_if_open=True)
        db.create_tables(_TABLES, safe=True)

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------

    def _index_block(self, blk):
        """Insert TxIndex and AddrIndex rows for all transactions in blk."""
        height = blk["height"]
        txs = blk.get("transactions", [])
        tx_rows, addr_rows = [], []
        for t in txs:
            h = tx_mod.tx_hash(t)
            tx_rows.append({"tx_hash": h, "block_height": height})
            addrs = {t["from"]} | {o["to"] for o in t.get("outputs", [])}
            for addr in addrs:
                addr_rows.append({"addr": addr, "tx_hash": h, "block_height": height})
        if tx_rows:
            TxIndex.insert_many(tx_rows).on_conflict_ignore().execute()
        if addr_rows:
            AddrIndex.insert_many(addr_rows).on_conflict_ignore().execute()

    def save_block(self, blk):
        with db.atomic():
            Block.insert(height=blk["height"], hash=blk["hash"],
                         data=json.dumps(blk)).on_conflict_replace().execute()
            self._index_block(blk)

    def get_tx_height(self, tx_hash):
        row = TxIndex.get_or_none(TxIndex.tx_hash == tx_hash)
        return row.block_height if row else None

    def get_tx_heights_for_addr(self, addr):
        rows = (AddrIndex
                .select(AddrIndex.block_height, AddrIndex.tx_hash)
                .where(AddrIndex.addr == addr)
                .order_by(AddrIndex.block_height.desc()))
        return [(r.block_height, r.tx_hash) for r in rows]

    def load_block(self, height):
        row = Block.get_or_none(Block.height == height)
        return json.loads(row.data) if row else None

    def load_all_blocks(self):
        """Load and return the full chain as a list. The entire chain is kept
        in memory by design; this is called once at startup."""
        return [json.loads(r.data) for r in Block.select().order_by(Block.height)]

    def chain_height(self):
        row = Block.select(Block.height).order_by(Block.height.desc()).first()
        return row.height if row else -1

    # ------------------------------------------------------------------
    # State snapshots
    # ------------------------------------------------------------------

    def _save_state_inner(self, state):
        """Write state rows; must be called inside an existing db.atomic()."""
        balances = state.all_balances()
        nonces   = state.all_nonces()
        rows = [
            {"addr": addr, "balance": balances.get(addr, 0), "nonce": nonces.get(addr, 0)}
            for addr in balances.keys() | nonces.keys()
        ]
        State.delete().execute()
        if rows:
            State.insert_many(rows).execute()
        Emission.insert(key="total_minted", value=state.total_minted).on_conflict_replace().execute()

    def save_state(self, state):
        with db.atomic():
            self._save_state_inner(state)

    def load_state(self):
        rows     = State.select()
        balances = {r.addr: r.balance for r in rows}
        nonces   = {r.addr: r.nonce   for r in rows}
        em       = {r.key: r.value for r in Emission.select()}
        return balances, nonces, em.get("total_minted", 0)

    def state_exists(self):
        return State.select().exists()

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def save_block_and_state(self, blk, state):
        """Save block and state in one atomic transaction.
        Calls inner logic directly to avoid nested db.atomic() savepoints.
        Exceptions from peewee propagate to the caller (node._commit),
        which is wrapped in the cycle's unhandled-error handler.
        """
        log.debug("[storage] save block+state  height=%d  hash=%s",
                  blk["height"], blk.get("hash", "?")[:12])
        with db.atomic():
            Block.insert(height=blk["height"], hash=blk["hash"],
                         data=json.dumps(blk)).on_conflict_replace().execute()
            self._index_block(blk)
            self._save_state_inner(state)

    def replace_chain_and_state(self, fork_point, blocks, state):
        """Replace chain tail and state in one atomic transaction."""
        log.debug("[storage] replace chain  fork_point=%d  new_blocks=%d",
                  fork_point, len(blocks))
        with db.atomic():
            Block.delete().where(Block.height >= fork_point).execute()
            TxIndex.delete().where(TxIndex.block_height >= fork_point).execute()
            AddrIndex.delete().where(AddrIndex.block_height >= fork_point).execute()
            Block.insert_many([
                {"height": b["height"], "hash": b["hash"], "data": json.dumps(b)}
                for b in blocks
            ]).on_conflict_replace().execute()
            for blk in blocks:
                self._index_block(blk)
            self._save_state_inner(state)

    def get_meta(self, key, default=None):
        row = Meta.get_or_none(Meta.key == key)
        return row.value if row else default

    def set_meta(self, key, value):
        Meta.insert(key=key, value=str(value)).on_conflict_replace().execute()

    def close(self):
        db.close()
