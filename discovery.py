"""DHT peer discovery using libtorrent BEP44 mutable items.

Runs in its own daemon thread. Writes peers into PeerPool.
Reads nothing else. No queues, no callbacks, no side effects
beyond PeerPool.add().

Candidate pipeline
------------------
Every source (BEP44, genesis-hash torrent DHT, receive_block senders,
peer-list crawl) feeds raw addresses into a thread-safe staging set
(_candidates). Once per STAGE_FLUSH_INTERVAL the main loop drains that
set, fetches /api/peers from each candidate in parallel (building a
one-hop graph), ranks by ascending nomination count (lightly-nominated =
gateway to under-connected parts of the network), and submits the ranked
list to _validate_and_add. Genesis validation only happens at that final
step, so no source gets special trust and the nomination sort always has
the widest possible picture before it commits any HTTP work.
"""

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import nacl.signing
import requests

from params import BEP44_SLOT_COUNT

try:
    import libtorrent as lt
except ImportError:
    lt = None  # type: ignore[assignment]

log = logging.getLogger("ec.discovery")


class _Ticker:
    """Minimal interval timer. Avoids five scattered last_X variables in run()."""
    __slots__ = ("_interval", "_last")

    def __init__(self, interval: float, *, fire_immediately: bool = False) -> None:
        self._interval = interval
        self._last = 0.0 if fire_immediately else float("inf")

    def ready(self, now: float) -> bool:
        return now - self._last >= self._interval

    def reset(self, now: float) -> None:
        self._last = now

PEER_CACHE_FILE      = "echocoin_peers.json"
DHT_STATE_FILE       = "echocoin_lt_dht.dat"
SAVE_INTERVAL        = 300
GET_INTERVAL         = 60
PUT_DELAY            = 30
PUT_REFRESH_INTERVAL = 3600   # re-announce our slot at least this often
CRAWL_INTERVAL       = 600    # full crawl of current peers every 10 min when at max
STAGE_FLUSH_INTERVAL = 15     # drain candidates and rank every 15 s


class Discovery:

    # Bounded thread pool for peer validation. Cap at 32 so a burst of
    # 256 DHT alerts doesn't spawn 256 simultaneous HTTP connections.
    _VALIDATE_WORKERS = 32

    def __init__(self, pool, genesis_hash, port, node_pubkey_hex=""):
        self.pool               = pool
        self.genesis_hash       = genesis_hash
        self.port               = port
        self.node_pubkey_hex    = node_pubkey_hex
        self._validate_executor = ThreadPoolExecutor(max_workers=self._VALIDATE_WORKERS)
        self._external_ip       = None   # set by UPnP in run()
        # Staging set: raw addresses from all sources, merged before ranking.
        self._candidates        = set()
        self._candidates_lock   = threading.Lock()
        if not node_pubkey_hex:
            log.warning("[dht] no node_pubkey_hex provided -- using slot 0 and offset 0; "
                        "all nodes without a pubkey will collide on the same slot")

    # ------------------------------------------------------------------
    # Public: enqueue a raw candidate address from any source
    # ------------------------------------------------------------------

    def enqueue_candidate(self, addr):
        """Add a raw address to the staging set. Thread-safe. No I/O."""
        if isinstance(addr, str) and ":" in addr:
            with self._candidates_lock:
                self._candidates.add(addr)

    # ------------------------------------------------------------------
    # Main loop (called as thread target)
    # ------------------------------------------------------------------

    def run(self):

        self._external_ip = self._upnp_map_port()
        if self._external_ip:
            log.info("[upnp] external IP mapped: %s", self._external_ip)

        self._load_peer_cache()

        ses = self._make_lt_session()
        alert_event = threading.Event()
        ses.set_alert_notify(alert_event.set)

        log.info("[dht] started, bootstrapping")
        time.sleep(15)

        my_slot   = self._my_slot_index() if self.node_pubkey_hex else 0
        my_offset = self._my_write_offset() if self.node_pubkey_hex else 0
        self._my_slot = my_slot  # stored so _bep44_get_all can skip it

        # Stagger the first put by my_offset so all nodes don't announce at once.
        put_delay   = PUT_DELAY + my_offset % 300
        tick_flush  = _Ticker(STAGE_FLUSH_INTERVAL,  fire_immediately=True)
        tick_get    = _Ticker(GET_INTERVAL,           fire_immediately=True)
        tick_crawl  = _Ticker(GET_INTERVAL,           fire_immediately=False)
        tick_put    = _Ticker(PUT_REFRESH_INTERVAL,   fire_immediately=False)
        tick_save   = _Ticker(SAVE_INTERVAL,          fire_immediately=False)
        start_time  = time.monotonic()

        # Initial BEP44 + genesis-hash torrent lookup immediately after bootstrap.
        self._bep44_get_all(ses)
        self._torrent_get_peers(ses)
        tick_get.reset(time.monotonic())

        while True:
            alert_event.wait(timeout=1)
            alert_event.clear()

            self._process_alerts(ses)

            now    = time.monotonic()
            at_max = self.pool.count() >= self.pool._max_peers

            if tick_flush.ready(now):
                self._validate_executor.submit(self._flush_candidates)
                tick_flush.reset(now)

            tick_get._interval = 300 if at_max else GET_INTERVAL
            if tick_get.ready(now):
                self._bep44_get_all(ses)
                self._torrent_get_peers(ses)
                tick_get.reset(now)

            tick_crawl._interval = CRAWL_INTERVAL if at_max else GET_INTERVAL
            if tick_crawl.ready(now):
                self._validate_executor.submit(self._crawl_and_enqueue)
                tick_crawl.reset(now)

            put_ready = (not tick_put._last and now - start_time >= put_delay) or tick_put.ready(now)
            if put_ready:
                my_addr = f"{self._my_ip()}:{self.port}"
                self._bep44_put(ses, my_slot, my_addr)
                self._torrent_announce(ses)
                tick_put.reset(now)

            self.pool.evict_stale()
            if tick_save.ready(now):
                self._save_peer_cache()
                self._save_lt_state(ses)
                new_ip = self._upnp_map_port()
                if new_ip and new_ip != self._external_ip:
                    log.info("[upnp] external IP changed: %s -> %s", self._external_ip, new_ip)
                    self._external_ip = new_ip
                tick_save.reset(now)

    # ------------------------------------------------------------------
    # Alert processing
    # ------------------------------------------------------------------

    def _process_alerts(self, ses):
        for a in ses.pop_alerts():
            if isinstance(a, lt.dht_mutable_item_alert):
                try:
                    item = a.item
                    if isinstance(item, dict):
                        raw = item.get("value") or item.get(b"value") or b"{}"
                    else:
                        raw = item or b"{}"
                    data = json.loads(raw)
                    addr = data.get("addr", "")
                    if addr and ":" in addr:
                        log.debug("[dht] BEP44 candidate  addr=%s", addr)
                        self.enqueue_candidate(addr)
                except Exception:
                    pass
            elif isinstance(a, lt.dht_get_peers_reply_alert):
                # Genesis-hash torrent peers -- raw (ip, port) tuples from DHT
                try:
                    for ep in a.peers():
                        addr = f"{ep.address()}:{ep.port()}"
                        log.debug("[dht] torrent candidate  addr=%s", addr)
                        self.enqueue_candidate(addr)
                except Exception:
                    pass
            elif isinstance(a, lt.dht_put_alert):
                if a.num_success > 0:
                    log.info("[dht] BEP44 put accepted by %d node(s)", a.num_success)
                else:
                    log.debug("[dht] BEP44 put num_success=0")
            elif isinstance(a, lt.dht_bootstrap_alert):
                log.info("[dht] bootstrap complete")

    # ------------------------------------------------------------------
    # Candidate pipeline: drain -> fetch peer lists -> rank -> validate
    # ------------------------------------------------------------------

    def _flush_candidates(self):
        """Drain the staging set, fetch /api/peers from each candidate in
        parallel to build a one-hop nomination graph, then rank by ascending
        nomination count and submit the best candidates to _validate_and_add.

        All three sources (BEP44, torrent DHT, receive_block) are mixed in
        the staging set before this runs, so the nomination sort always has
        the widest possible picture. No source gets priority or special trust.
        """
        with self._candidates_lock:
            if not self._candidates:
                return
            batch = self._candidates.copy()
            self._candidates.clear()

        known = set(self.pool.all_addrs())
        fresh = [a for a in batch if a not in known]
        if not fresh:
            return

        log.debug("[peer] flushing %d candidates", len(fresh))

        # Fetch /api/peers from every candidate in parallel.
        # This gives us one-hop graph data without doing any genesis work yet.
        def fetch_peers(addr):
            try:
                r = requests.get(f"http://{addr}/api/peers", timeout=3)
                if r.status_code == 200:
                    return addr, r.json().get("peers", [])
            except Exception:
                pass
            return addr, None   # None = unreachable

        nominations: dict[str, int] = {}
        reachable: set[str] = set()

        with ThreadPoolExecutor(max_workers=min(len(fresh), 32)) as ex:
            for addr, peers in ex.map(fetch_peers, fresh):
                if peers is None:
                    # Candidate didn't respond at all -- don't waste a
                    # _validate_and_add call, just discard silently.
                    continue
                reachable.add(addr)
                # Count cross-nominations among the candidates themselves.
                # A candidate nominated by many of its peers' peers is
                # already well-connected; one nominated by few is a potential
                # bridge to a different part of the graph.
                for p in peers:
                    if isinstance(p, str) and p in fresh and p != addr:
                        nominations[p] = nominations.get(p, 0) + 1

        if not reachable:
            return

        # Sort reachable candidates by ascending nomination count.
        # Ties broken arbitrarily. Lightly-nominated first = path diversity.
        ranked = sorted(reachable, key=lambda a: nominations.get(a, 0))

        log.info("[peer] ranked %d reachable candidates  unique=%d  cross_nominated=%d",
                 len(ranked),
                 sum(1 for a in ranked if nominations.get(a, 0) == 0),
                 sum(1 for a in ranked if nominations.get(a, 0) > 0))

        for addr in ranked:
            self._validate_executor.submit(self._validate_and_add, addr)

    # ------------------------------------------------------------------
    # Crawl current pool to enqueue deeper candidates
    # ------------------------------------------------------------------

    def _crawl_and_enqueue(self):
        """Fetch peer lists from all current pool members in parallel and
        enqueue their peers as candidates. The nomination ranking in
        _flush_candidates handles prioritisation -- this just widens the set.
        """
        current_peers = self.pool.get_all()
        if not current_peers:
            return

        known = set(self.pool.all_addrs())

        def fetch_one(peer_addr):
            try:
                r = requests.get(f"http://{peer_addr}/api/peers", timeout=5)
                if r.status_code == 200:
                    return r.json().get("peers", [])
            except Exception:
                pass
            return []

        with ThreadPoolExecutor(max_workers=min(len(current_peers), 32)) as ex:
            results = list(ex.map(fetch_one, current_peers))

        count = 0
        for peer_list in results:
            for addr in peer_list:
                if isinstance(addr, str) and addr not in known:
                    self.enqueue_candidate(addr)
                    count += 1

        if count:
            log.debug("[peer] crawl enqueued %d candidates", count)

    # ------------------------------------------------------------------
    # Genesis check + pool admission
    # ------------------------------------------------------------------

    def _validate_and_add(self, peer_addr):
        """Final step: genesis check via /api/info, then pool admission.
        Only called after the candidate has already responded to /api/peers
        (in _flush_candidates), so timeouts here are genuinely unexpected."""
        try:
            r = requests.get(f"http://{peer_addr}/api/info", timeout=3)
            if r.status_code != 200:
                self.pool.strike(peer_addr)
                return
            info = r.json()
            if info.get("genesis_hash") != self.genesis_hash:
                log.debug("[peer] rejected %s, genesis mismatch", peer_addr)
                self.pool.remove(peer_addr)
                return
            if self.pool.add(peer_addr):
                log.info("[peer] connected  addr=%s  pool=%d", peer_addr, self.pool.count())
        except requests.exceptions.Timeout:
            log.debug("[peer] validation timed out  addr=%s", peer_addr)
        except Exception:
            log.debug("[peer] validation failed  addr=%s", peer_addr, exc_info=True)
            self.pool.strike(peer_addr)

    # ------------------------------------------------------------------
    # BEP44 helpers
    # ------------------------------------------------------------------

    _PUBLIC_IP_SERVICES: ClassVar[tuple[str, ...]] = (
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://checkip.amazonaws.com",
    )

    def _my_ip(self):
        """Return the best IP to advertise: external (UPnP) if available,
        otherwise query a public IP service, falling back to 127.0.0.1."""
        if self._external_ip:
            return self._external_ip
        for url in self._PUBLIC_IP_SERVICES:
            try:
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    ip = r.text.strip()
                    if ip:
                        log.debug("[ip] public IP via %s: %s", url, ip)
                        return ip
            except Exception:
                pass
        log.warning("[ip] could not determine public IP, falling back to 127.0.0.1")
        return "127.0.0.1"

    def _bep44_slot_keypair(self, slot_index):
        seed = hashlib.sha256(
            b"echocoin-peers-v1:"
            + self.genesis_hash.encode()
            + b":"
            + str(slot_index).encode()
        ).digest()
        sk_obj = nacl.signing.SigningKey(seed)
        pk     = bytes(sk_obj.verify_key)
        h      = bytearray(hashlib.sha512(seed).digest())
        h[0]  &= 248
        h[31] &= 127
        h[31] |= 64
        sk_64  = bytes(h)
        return sk_64, pk

    def _my_slot_index(self):
        return int(hashlib.sha256(
            self.node_pubkey_hex.encode()
        ).hexdigest()[:8], 16) % BEP44_SLOT_COUNT

    def _my_write_offset(self):
        return int(hashlib.sha256(
            (self.node_pubkey_hex + "-offset").encode()
        ).hexdigest()[:8], 16) % 3600

    def _bep44_put(self, ses, slot_index, my_addr):
        sk_64, pk = self._bep44_slot_keypair(slot_index)
        value = json.dumps({"addr": my_addr, "ts": int(time.time())}).encode()
        try:
            ses.dht_put_mutable_item(sk_64, pk, value, b"echocoin-v1")
            log.info("[dht] BEP44 put  slot=%d  addr=%s", slot_index, my_addr)
        except Exception:
            log.exception("[dht] BEP44 put error")

    def _bep44_get_all(self, ses):
        my_slot = getattr(self, "_my_slot", None)
        for i in range(BEP44_SLOT_COUNT):
            if i == my_slot:
                continue   # we already know our own address
            _, pk = self._bep44_slot_keypair(i)
            try:
                ses.dht_get_mutable_item(pk, b"echocoin-v1")
            except Exception:
                pass
        log.debug("[dht] BEP44 get  slots=%d",
                  BEP44_SLOT_COUNT - (1 if my_slot is not None else 0))

    # ------------------------------------------------------------------
    # Genesis-hash torrent seeding (BEP 5 standard get_peers / announce)
    # ------------------------------------------------------------------

    def _genesis_info_hash(self):
        """SHA-1 of the genesis hash bytes -- used as the torrent info-hash
        so Echocoin nodes appear as peers on a well-known info-hash that any
        BitTorrent client in the DHT can stumble across organically."""
        raw = hashlib.sha1(bytes.fromhex(self.genesis_hash)).digest()
        return lt.sha1_hash(raw)

    def _torrent_announce(self, ses):
        """Announce ourselves as a seeder of the genesis-hash torrent."""
        try:
            ses.dht_announce(self._genesis_info_hash(), self.port)
            log.debug("[dht] torrent announced  port=%d", self.port)
        except Exception:
            log.debug("[dht] torrent announce failed", exc_info=True)

    def _torrent_get_peers(self, ses):
        """Ask the DHT for peers of the genesis-hash torrent.
        Results arrive as dht_get_peers_reply_alert events."""
        try:
            ses.dht_get_peers(self._genesis_info_hash())
            log.debug("[dht] torrent get_peers issued")
        except Exception:
            log.debug("[dht] torrent get_peers failed", exc_info=True)

    def _make_lt_session(self):
        settings = lt.default_settings()
        settings["enable_dht"]    = True
        settings["enable_lsd"]    = False
        settings["enable_upnp"]   = False
        settings["enable_natpmp"] = False
        settings["listen_interfaces"] = f"0.0.0.0:{self.port + 1}"
        settings["alert_mask"] = (
            lt.alert.category_t.dht_notification
            | lt.alert.category_t.status_notification
        )
        settings["dht_bootstrap_nodes"] = (
            "router.bittorrent.com:6881,"
            "router.utorrent.com:6881,"
            "dht.transmissionbt.com:6881,"
            "dht.aelitis.com:6881"
        )
        ses = lt.session(settings)
        try:
            with open(DHT_STATE_FILE, "rb") as f:
                state = lt.bdecode(f.read())
            ses.load_state(state)
            log.info("[dht] routing table loaded")
        except Exception:
            pass
        return ses

    def _save_lt_state(self, ses):
        try:
            state = ses.save_state()
            with open(DHT_STATE_FILE, "wb") as f:
                f.write(lt.bencode(state))
        except Exception:
            log.debug("[dht] state save failed", exc_info=True)

    # ------------------------------------------------------------------
    # Peer cache
    # ------------------------------------------------------------------

    def _load_peer_cache(self):
        try:
            with open(PEER_CACHE_FILE) as f:
                peers = json.load(f)
            log.info("[peer] cache loaded  count=%d", len(peers))
            for addr in peers:
                self.enqueue_candidate(addr)
        except FileNotFoundError:
            pass
        except Exception:
            log.debug("[peer] cache load failed", exc_info=True)

    def _save_peer_cache(self):
        peers = self.pool.all_addrs()
        try:
            with open(PEER_CACHE_FILE, "w") as f:
                json.dump(peers, f)
        except Exception:
            log.debug("[peer] cache save failed", exc_info=True)

    # ------------------------------------------------------------------
    # UPnP
    # ------------------------------------------------------------------

    def _upnp_map_port(self):
        try:
            import miniupnpc
        except ImportError:
            return None
        try:
            u = miniupnpc.UPnP()
            u.discoverdelay = 1000
            if u.discover() == 0:
                return None
            u.selectigd()
            ext = u.externalipaddress()
            try:
                u.deleteportmapping(self.port, "TCP")
            except Exception:
                pass
            if u.addportmapping(self.port, "TCP", u.lanaddr, self.port, "Echocoin node", ""):
                log.debug("[upnp] mapped %s:%d -> %s:%d", ext, self.port, u.lanaddr, self.port)
                return ext
        except Exception:
            pass
        return None
