"""UDP peer transport — the single socket that replaces all HTTP peer communication.

One UDP socket per node handles everything:
  - PING/PONG    : reachability check + NAT-observed public IP discovery
  - PEERS        : peer list exchange
  - BLOCK        : block gossip
  - TX           : transaction gossip (Dandelion stem/fluff)
  - GETSYNC      : request chain segment
  - SYNC         : chain segment response (chunked for large syncs)
  - PUNCH_REQ    : ask relay to coordinate a hole punch to a third peer
  - PUNCH_GO     : relay telling us to punch toward a peer right now

Reliability layer
-----------------
Large messages (SYNC, full block) are chunked into MAX_CHUNK_SIZE UDP datagrams.
Each chunk is numbered. The receiver reassembles and ACKs the whole message.
Small messages (PING, PEERS, TX, small blocks) fit in one datagram and are
fire-and-forget with application-level retry handled by the caller.

Wire format
-----------
All datagrams: [1 byte msg_type][2 byte chunk_id][2 byte chunk_total][payload]
If chunk_total == 1 the message is not chunked (fire-and-forget).
Payload is msgpack-encoded for compactness (falls back to json).

Public interface
----------------
  UDPTransport(host, port, genesis_hash, on_block, on_tx, on_peers, pool)
  .start()                    -- bind socket, start recv loop thread
  .stop()
  .ping(addr)                 -- fire-and-forget
  .send_block(block)          -- broadcast to all peers
  .send_tx(tx, peers=None)    -- send tx (dandelion stem or broadcast)
  .request_sync(addr, from_h) -- request chain from peer, returns list|None
  .send_peers(addr, peers)    -- send peer list to addr
  .punch_via(relay, target)   -- ask relay to coordinate punch to target
  .our_external_addr          -- best-known external ip:port (str or None)
"""

import json
import logging
import queue
import secrets
import socket
import struct
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("ec.udp")

# Message type constants
MT_PING      = 0x01
MT_PONG      = 0x02
MT_PEERS     = 0x03
MT_BLOCK     = 0x04
MT_TX        = 0x05
MT_GETSYNC   = 0x06
MT_SYNC      = 0x07
MT_ACK       = 0x08
MT_PUNCH_REQ = 0x09
MT_PUNCH_GO  = 0x0A
MT_GETINFO   = 0x0B   # request peer tip info (height + hash)
MT_INFO      = 0x0C   # response: {"height": N, "tip_hash": "..."}

MAX_CHUNK_SIZE   = 1400   # bytes, safe below MTU
RECV_TIMEOUT     = 2.0    # seconds select/recvfrom timeout
SYNC_TIMEOUT     = 30.0   # seconds to wait for a full sync response
PING_TIMEOUT     = 3.0    # seconds to wait for PONG

# Header: 1 (type) + 4 (msg_id) + 2 (chunk_idx) + 2 (chunk_total) = 9 bytes
HDR_FMT  = "!BIHh"  # note: chunk_total signed so -1 = ACK special
HDR_SIZE = struct.calcsize(HDR_FMT)


def _encode(data: dict) -> bytes:
    try:
        import msgpack
        return msgpack.packb(data, use_bin_type=True)
    except ImportError:
        return json.dumps(data).encode()


def _decode(raw: bytes) -> dict:
    try:
        import msgpack
        return msgpack.unpackb(raw, raw=False)
    except ImportError:
        return json.loads(raw.decode())


def _pack(msg_type: int, msg_id: int, chunk_idx: int,
          chunk_total: int, payload: bytes) -> bytes:
    hdr = struct.pack(HDR_FMT, msg_type, msg_id, chunk_idx, chunk_total)
    return hdr + payload


def _unpack(data: bytes):
    if len(data) < HDR_SIZE:
        return None
    msg_type, msg_id, chunk_idx, chunk_total = struct.unpack_from(HDR_FMT, data)
    payload = data[HDR_SIZE:]
    return msg_type, msg_id, chunk_idx, chunk_total, payload


def _split(payload: bytes):
    """Split payload into chunks. Returns list of bytes."""
    chunks = []
    for i in range(0, max(len(payload), 1), MAX_CHUNK_SIZE):
        chunks.append(payload[i:i + MAX_CHUNK_SIZE])
    return chunks


class _Reassembler:
    """Reassemble chunked messages per (sender_addr, msg_id)."""

    def __init__(self):
        self._pending = {}   # (addr, msg_id) -> {idx: payload_bytes, "total": int, "ts": float}
        self._lock    = threading.Lock()

    def feed(self, addr, msg_id, chunk_idx, chunk_total, payload):
        """Return complete payload bytes when all chunks arrive, else None."""
        if chunk_total == 1:
            return payload  # single-chunk, no reassembly needed

        key = (addr, msg_id)
        with self._lock:
            if key not in self._pending:
                self._pending[key] = {"total": chunk_total, "ts": time.monotonic()}
            rec = self._pending[key]
            rec[chunk_idx] = payload
            if len(rec) - 2 == rec["total"]:   # -2 for "total" and "ts" keys
                full = b"".join(rec[i] for i in range(rec["total"]))
                del self._pending[key]
                return full
        return None

    def evict_stale(self, max_age=60.0):
        cutoff = time.monotonic() - max_age
        with self._lock:
            stale = [k for k, v in self._pending.items() if v["ts"] < cutoff]
            for k in stale:
                del self._pending[k]


class _PendingSync:
    """Collects SYNC chunks for a specific GETSYNC request."""

    def __init__(self):
        self.chunks  = {}   # chunk_idx -> payload
        self.total   = None
        self.event   = threading.Event()
        self.result  = None   # set when complete

    def feed(self, chunk_idx, chunk_total, payload):
        self.total = chunk_total
        self.chunks[chunk_idx] = payload
        if len(self.chunks) == chunk_total:
            full = b"".join(self.chunks[i] for i in range(chunk_total))
            try:
                self.result = _decode(full)
            except Exception:
                self.result = None
            self.event.set()


class UDPTransport:

    def __init__(self, port, genesis_hash, on_block, on_tx, on_peers, pool):
        self.port         = port
        self.genesis_hash = genesis_hash
        self._on_block    = on_block
        self._on_tx       = on_tx
        self._on_peers    = on_peers
        self._pool        = pool

        self._sock        = None
        self._running     = False
        self._reassembler = _Reassembler()
        self._pending_sync: dict[int, _PendingSync] = {}  # msg_id -> _PendingSync
        self._sync_lock   = threading.Lock()
        self._pong_events: dict[tuple, threading.Event] = {}
        self._pong_addrs: dict[tuple, str] = {}  # (addr, msg_id) -> observed addr
        self._pong_lock   = threading.Lock()
        self._info_events: dict[int, threading.Event] = {}  # msg_id -> event
        self._info_results: dict[int, dict] = {}            # msg_id -> info dict
        self._info_lock   = threading.Lock()

        self.our_external_addr: str | None = None  # set from PONG responses
        self._ext_addr_votes: dict[str, int] = {}  # addr -> vote count
        self._seen_msg: dict[int, float] = {}      # msg_id -> ts for dedup
        self._seen_lock = threading.Lock()
        self._executor  = ThreadPoolExecutor(max_workers=16, thread_name_prefix="udp-cb")
        self._on_punch_go = None  # set by discovery after init
        self._get_tip_fn  = None  # set by main after node init

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.settimeout(RECV_TIMEOUT)
        self._running = True
        t = threading.Thread(target=self._recv_loop, daemon=True, name="udp-recv")
        t.start()
        log.info("[udp] listening on 0.0.0.0:%d", self.port)

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Public send operations
    # ------------------------------------------------------------------

    def ping(self, addr: str, timeout: float = PING_TIMEOUT) -> str | None:
        """Send PING, wait for PONG. Returns observed external addr or None."""
        host, port = addr.rsplit(":", 1)
        target = (host, int(port))
        msg_id = self._new_msg_id()
        ev = threading.Event()
        with self._pong_lock:
            self._pong_events[(target, msg_id)] = ev
        self._send_one(MT_PING, msg_id, {"genesis": self.genesis_hash}, target)
        if ev.wait(timeout):
            with self._pong_lock:
                result = self._pong_addrs.pop((target, msg_id), None)
                self._pong_events.pop((target, msg_id), None)
            # Vote on our external address — require 2 agreements before committing
            if result:
                self._ext_addr_votes[result] = self._ext_addr_votes.get(result, 0) + 1
                if self._ext_addr_votes[result] >= 2:
                    self.our_external_addr = result
                elif self.our_external_addr is None:
                    self.our_external_addr = result  # tentative until confirmed
            return result
        with self._pong_lock:
            self._pong_events.pop((target, msg_id), None)
        return None

    def send_block(self, block: dict):
        """Broadcast block to all peers."""
        peers = self._pool.get_all()
        if not peers:
            return
        log.debug("[udp] send_block height=%s to %d peers", block.get("height"), len(peers))
        payload = _encode({"genesis": self.genesis_hash, "block": block})
        msg_id = self._new_msg_id()
        self._mark_seen(msg_id)
        for addr in peers:
            self._send_chunked(MT_BLOCK, msg_id, payload,
                               self._addr_tuple(addr))

    def send_tx(self, tx: dict, peers=None):
        """Send TX to specific peers or broadcast."""
        if peers is None:
            peers = self._pool.get_all()
        if not peers:
            return
        payload = _encode({"genesis": self.genesis_hash, "tx": tx})
        msg_id = self._new_msg_id()
        self._mark_seen(msg_id)
        for addr in peers:
            self._send_one(MT_TX, msg_id, None, self._addr_tuple(addr),
                           raw_payload=payload)

    def send_peers(self, addr: str, peers: list[str]):
        """Send peer list to addr."""
        self._send_one(MT_PEERS, self._new_msg_id(),
                       {"genesis": self.genesis_hash, "peers": peers},
                       self._addr_tuple(addr))

    def request_sync(self, addr: str, from_h: int,
                     to_h: int = None, timeout: float = SYNC_TIMEOUT):
        """Ask addr for chain[from_h:to_h]. Returns list of blocks or None."""
        msg_id = self._new_msg_id()
        pending = _PendingSync()
        with self._sync_lock:
            self._pending_sync[msg_id] = pending
        self._send_one(MT_GETSYNC, msg_id,
                       {"genesis": self.genesis_hash,
                        "from_h": from_h,
                        "to_h": to_h},
                       self._addr_tuple(addr))
        if pending.event.wait(timeout):
            with self._sync_lock:
                self._pending_sync.pop(msg_id, None)
            return pending.result
        with self._sync_lock:
            self._pending_sync.pop(msg_id, None)
        return None

    def get_info(self, addr: str, timeout: float = 8.0) -> dict | None:
        """Request tip info from peer. Returns {"height": N, "tip_hash": "..."} or None."""
        msg_id = self._new_msg_id()
        ev     = threading.Event()
        with self._info_lock:
            self._info_events[msg_id] = ev
        self._send_one(MT_GETINFO, msg_id,
                       {"genesis": self.genesis_hash},
                       self._addr_tuple(addr))
        if ev.wait(timeout):
            with self._info_lock:
                result = self._info_results.pop(msg_id, None)
                self._info_events.pop(msg_id, None)
            return result
        with self._info_lock:
            self._info_events.pop(msg_id, None)
        return None

    def punch_via(self, relay_addr: str, target_addr: str):
        """Ask relay to coordinate a hole punch toward target.
        Simultaneously fire UDP packets toward target to open our NAT hole
        before the relay tells the target to do the same."""
        self._send_one(MT_PUNCH_REQ, self._new_msg_id(),
                       {"genesis": self.genesis_hash,
                        "target": target_addr},
                       self._addr_tuple(relay_addr))
        # Fire simultaneously from our side — this is the key to hole punching:
        # both sides must send toward each other at roughly the same time.
        target = self._addr_tuple(target_addr)
        for _ in range(8):
            try:
                self._send_one(MT_PING, self._new_msg_id(),
                               {"genesis": self.genesis_hash}, target)
            except Exception:
                pass
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    def _recv_loop(self):
        while self._running:
            try:
                data, sender = self._sock.recvfrom(65535)
            except socket.timeout:
                self._reassembler.evict_stale()
                continue
            except OSError:
                break
            self._executor.submit(self._handle_datagram, data, sender)

    def _handle_datagram(self, data: bytes, sender: tuple):
        unpacked = _unpack(data)
        if unpacked is None:
            return
        msg_type, msg_id, chunk_idx, chunk_total, payload_bytes = unpacked

        # Reassemble chunked messages
        if msg_type in (MT_SYNC,):
            with self._sync_lock:
                pending = self._pending_sync.get(msg_id)
            if pending:
                pending.feed(chunk_idx, chunk_total, payload_bytes)
            return

        # For everything else, reassemble then dispatch
        complete = self._reassembler.feed(
            sender, msg_id, chunk_idx, chunk_total, payload_bytes
        )
        if complete is None:
            return

        try:
            parsed = _decode(complete)
        except Exception:
            return

        # Genesis check for peer messages
        if msg_type not in (MT_PING, MT_PONG, MT_ACK):
            if parsed.get("genesis") != self.genesis_hash:
                return

        self._dispatch(msg_type, msg_id, parsed, sender)

    def _dispatch(self, msg_type: int, msg_id: int, data: dict, sender: tuple):
        sender_addr = f"{sender[0]}:{sender[1]}"

        if msg_type == MT_PING:
            # Reply with PONG including sender's observed address
            if data.get("genesis") == self.genesis_hash:
                self._send_one(MT_PONG, msg_id,
                               {"observed": sender_addr,
                                "genesis": self.genesis_hash},
                               sender)

        elif msg_type == MT_PONG:
            observed = data.get("observed", "")
            with self._pong_lock:
                key = (sender, msg_id)
                if key in self._pong_events:
                    self._pong_addrs[key] = observed
                    self._pong_events[key].set()

        elif msg_type == MT_PEERS:
            peers = data.get("peers", [])
            self._on_peers(peers, sender_addr)

        elif msg_type == MT_BLOCK:
            if self._is_new(msg_id):
                self._pool.touch(sender_addr)
                block = data.get("block")
                if block:
                    log.debug("[udp] recv_block height=%s from=%s",
                              block.get("height"), sender_addr)
                    self._on_block(block, sender_addr)
                    self._rebroadcast(MT_BLOCK, msg_id, data, exclude=sender_addr)

        elif msg_type == MT_TX:
            if self._is_new(msg_id):
                self._pool.touch(sender_addr)
                tx = data.get("tx")
                if tx:
                    self._on_tx(tx, sender_addr, msg_id)

        elif msg_type == MT_GETSYNC:
            self._handle_getsync(msg_id, data, sender)

        elif msg_type == MT_PUNCH_REQ:
            target = data.get("target", "")
            if target:
                self._handle_punch_req(sender_addr, target)

        elif msg_type == MT_PUNCH_GO:
            target = data.get("target", "")
            if target:
                log.debug("[udp] punch_go -> %s", target)
                tgt = self._addr_tuple(target)
                for _ in range(8):
                    try:
                        self._send_one(MT_PING, self._new_msg_id(),
                                       {"genesis": self.genesis_hash}, tgt)
                    except Exception:
                        pass
                    time.sleep(0.05)
                if self._on_punch_go:
                    self._on_punch_go(target)

        elif msg_type == MT_GETINFO:
            # Peer requesting our tip info — respond with height + tip hash
            if self._get_tip_fn:
                height, tip_hash = self._get_tip_fn()
                self._send_one(MT_INFO, msg_id,
                               {"genesis": self.genesis_hash,
                                "height":   height,
                                "tip_hash": tip_hash},
                               sender)

        elif msg_type == MT_INFO:
            with self._info_lock:
                if msg_id in self._info_events:
                    self._info_results[msg_id] = {
                        "height":   data.get("height"),
                        "tip_hash": data.get("tip_hash", ""),
                    }
                    self._info_events[msg_id].set()

    def _handle_getsync(self, msg_id: int, data: dict, sender: tuple):
        """Serve a chain segment request. Calls back on_sync_request if set."""
        from_h = data.get("from_h", 0)
        to_h   = data.get("to_h")
        chain  = self._get_chain_fn(from_h, to_h) if self._get_chain_fn else []
        payload = _encode({"genesis": self.genesis_hash, "chain": chain})
        self._send_chunked(MT_SYNC, msg_id, payload, sender)

    def _handle_punch_req(self, requester_addr: str, target_addr: str):
        """Relay: tell both peers to punch toward each other."""
        log.debug("[udp] punch relay %s <-> %s", requester_addr, target_addr)
        # Tell target to punch toward requester
        self._send_one(MT_PUNCH_GO, self._new_msg_id(),
                       {"genesis": self.genesis_hash, "target": requester_addr},
                       self._addr_tuple(target_addr))
        # Tell requester to punch toward target
        self._send_one(MT_PUNCH_GO, self._new_msg_id(),
                       {"genesis": self.genesis_hash, "target": target_addr},
                       self._addr_tuple(requester_addr))

    # ------------------------------------------------------------------
    # Low-level send helpers
    # ------------------------------------------------------------------

    def _send_one(self, msg_type: int, msg_id: int, data: dict | None,
                  target: tuple, raw_payload: bytes = None):
        if raw_payload is None:
            raw_payload = _encode(data or {})
        pkt = _pack(msg_type, msg_id, 0, 1, raw_payload)
        try:
            self._sock.sendto(pkt, target)
        except Exception as e:
            log.debug("[udp] send error to %s: %s", target, e)

    def _send_chunked(self, msg_type: int, msg_id: int,
                      payload: bytes, target: tuple):
        chunks = _split(payload)
        total  = len(chunks)
        for i, chunk in enumerate(chunks):
            pkt = _pack(msg_type, msg_id, i, total, chunk)
            try:
                self._sock.sendto(pkt, target)
            except Exception as e:
                log.debug("[udp] send_chunked error to %s: %s", target, e)
                break
            if total > 1:
                time.sleep(0.001)  # gentle pacing to avoid local buffer drops

    def _rebroadcast(self, msg_type: int, msg_id: int,
                     data: dict, exclude: str):
        peers = [p for p in self._pool.get_all() if p != exclude]
        if not peers:
            return
        payload = _encode(data)
        for addr in peers:
            self._send_chunked(msg_type, msg_id, payload, self._addr_tuple(addr))

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def set_chain_provider(self, fn):
        """fn(from_h, to_h) -> list[block_dict]. Set by Node after init."""
        self._get_chain_fn = fn

    def set_tip_provider(self, fn):
        """fn() -> (height, tip_hash). Used for lightweight MT_GETINFO responses."""
        self._get_tip_fn = fn

    def set_punch_go_callback(self, fn):
        """fn(addr) called when PUNCH_GO received — discovery should ping immediately."""
        self._on_punch_go = fn

    def _get_chain_fn(self, from_h, to_h):  # default no-op before Node sets it
        return []

    @staticmethod
    def _addr_tuple(addr: str) -> tuple:
        host, port = addr.rsplit(":", 1)
        return host, int(port)

    @staticmethod
    def _new_msg_id() -> int:
        return secrets.randbits(32)

    def _mark_seen(self, msg_id: int):
        with self._seen_lock:
            self._seen_msg[msg_id] = time.monotonic()
            # Evict old entries
            if len(self._seen_msg) > 50_000:
                cutoff = time.monotonic() - 300
                self._seen_msg = {k: v for k, v in self._seen_msg.items()
                                  if v > cutoff}

    def _is_new(self, msg_id: int) -> bool:
        with self._seen_lock:
            if msg_id in self._seen_msg:
                return False
            self._seen_msg[msg_id] = time.monotonic()
            return True
