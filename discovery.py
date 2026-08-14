"""DHT peer discovery coordinator.

Runs in its own daemon thread. Owns three discovery strategies and a unified
candidate pipeline that all sources feed into:

  DHTDiscovery    -- BEP44 mutable items + genesis-hash torrent DHT peers
  CrawlDiscovery  -- crawls /api/peers from current pool members
  receive_block   -- senders registered via enqueue_candidate() from api.py

All sources call enqueue_candidate(), which adds to a thread-safe staging
set. Every STAGE_FLUSH_INTERVAL seconds the coordinator drains that set,
fetches /api/info from each candidate in parallel (genesis-hash check +
peer-list nomination in one trip), ranks by ascending nomination count
(lightly-nominated = gateway to under-connected parts of the network),
and admits valid candidates into PeerPool. No source gets special trust.

External public interface (called from main.py and api.py):
  Discovery(pool, genesis_hash, port, node_pubkey_hex)
  .enqueue_candidate(addr)   -- thread-safe, called from any thread
  .add_bootstrap_peer(addr)  -- admit a --peer CLI address immediately
  .run()                     -- blocking loop, run as daemon thread
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from discovery_crawl import CrawlDiscovery
from discovery_dht   import DHTDiscovery, PUT_REFRESH_INTERVAL

log = logging.getLogger("ec.discovery")

PEER_CACHE_FILE      = "echocoin_peers.json"
SAVE_INTERVAL        = 300
GET_INTERVAL         = 60
CRAWL_INTERVAL       = 600
STAGE_FLUSH_INTERVAL = 15

_PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)


class Discovery:

    def __init__(self, pool, genesis_hash, port, node_pubkey_hex=""):
        self.pool            = pool
        self.genesis_hash    = genesis_hash
        self.port            = port
        self._candidates     = set()
        self._candidates_lock = threading.Lock()

        if not node_pubkey_hex:
            log.warning("[dht] no node_pubkey_hex -- all such nodes share slot 0")

        self._dht   = DHTDiscovery(self.enqueue_candidate, genesis_hash, port, node_pubkey_hex)
        self._crawl = CrawlDiscovery(pool, self.enqueue_candidate)

    # ------------------------------------------------------------------
    # Public: enqueue from any source
    # ------------------------------------------------------------------

    def enqueue_candidate(self, addr):
        """Add a raw address to the staging set. Thread-safe. No I/O."""
        if isinstance(addr, str) and ":" in addr:
            with self._candidates_lock:
                self._candidates.add(addr)

    def add_bootstrap_peer(self, addr):
        """Admit a CLI --peer address immediately, bypassing the pipeline.
        Called once at startup before the discovery thread starts.
        """
        if not (isinstance(addr, str) and ":" in addr):
            return
        try:
            r = requests.get(f"http://{addr}/api/info", timeout=5)
            if r.status_code == 200 and r.json().get("genesis_hash") == self.genesis_hash:
                if self.pool.add(addr):
                    log.info("[peer] bootstrap peer admitted  addr=%s", addr)
                return
        except Exception:
            pass
        log.warning("[peer] bootstrap peer unreachable or genesis mismatch  addr=%s", addr)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        external_ip = self._upnp_map_port()
        if external_ip:
            log.info("[upnp] external IP mapped: %s", external_ip)

        self._load_peer_cache()

        ses, my_slot, my_offset = self._dht.start()
        alert_event = threading.Event()
        ses.set_alert_notify(alert_event.set)

        log.info("[dht] started, bootstrapping")
        time.sleep(15)

        put_delay = PUT_DELAY_LOCAL + my_offset % 300
        now = time.monotonic()

        last_flush = now - STAGE_FLUSH_INTERVAL   # fire immediately
        last_get   = now
        last_crawl = now
        last_put   = now - PUT_REFRESH_INTERVAL + put_delay
        last_save  = now

        # Initial BEP44 + torrent lookup right after bootstrap
        self._dht.get_all(ses, my_slot)
        self._dht.torrent_get_peers(ses)
        last_get = time.monotonic()

        while True:
            alert_event.wait(timeout=1)
            alert_event.clear()
            self._dht.process_alerts(ses)

            now    = time.monotonic()
            at_max = self.pool.count() >= self.pool._max_peers

            if now - last_flush >= STAGE_FLUSH_INTERVAL:
                self._flush_candidates()
                last_flush = now

            get_interval = 300 if at_max else GET_INTERVAL
            if now - last_get >= get_interval:
                self._dht.get_all(ses, my_slot)
                self._dht.torrent_get_peers(ses)
                last_get = now

            crawl_interval = CRAWL_INTERVAL if at_max else GET_INTERVAL
            if now - last_crawl >= crawl_interval:
                self._crawl.crawl_and_enqueue()
                last_crawl = now

            if now - last_put >= PUT_REFRESH_INTERVAL:
                my_addr = f"{self._my_ip(external_ip)}:{self.port}"
                self._dht.put(ses, my_slot, my_addr)
                self._dht.torrent_announce(ses)
                last_put = now

            self.pool.evict_stale()

            if now - last_save >= SAVE_INTERVAL:
                self._save_peer_cache()
                self._dht.save_state(ses)
                new_ip = self._upnp_map_port()
                if new_ip and new_ip != external_ip:
                    log.info("[upnp] external IP changed: %s -> %s", external_ip, new_ip)
                    external_ip = new_ip
                last_save = now

    # ------------------------------------------------------------------
    # Candidate pipeline
    # ------------------------------------------------------------------

    def _flush_candidates(self):
        """Drain staging set, probe each via /api/info, rank, admit to pool."""
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

        def probe(addr):
            try:
                r = requests.get(f"http://{addr}/api/info", timeout=3)
                if r.status_code == 200:
                    return addr, r.json()
            except Exception:
                pass
            return addr, None

        nominations: dict[str, int] = {}
        valid: dict[str, dict]      = {}

        with ThreadPoolExecutor(max_workers=min(len(fresh), 32)) as ex:
            for addr, info in ex.map(probe, fresh):
                if info is None:
                    continue
                if info.get("genesis_hash") != self.genesis_hash:
                    log.debug("[peer] rejected %s, genesis mismatch", addr)
                    continue
                valid[addr] = info
                for p in info.get("peers", []):
                    if isinstance(p, str) and p in fresh and p != addr:
                        nominations[p] = nominations.get(p, 0) + 1

        if not valid:
            return

        ranked = sorted(valid, key=lambda a: nominations.get(a, 0))
        log.info("[peer] admitting %d valid candidates", len(ranked))
        for addr in ranked:
            if self.pool.add(addr):
                log.info("[peer] connected  addr=%s  pool=%d", addr, self.pool.count())

    # ------------------------------------------------------------------
    # IP helpers
    # ------------------------------------------------------------------

    def _my_ip(self, external_ip):
        """Return the best IP to advertise."""
        if external_ip:
            return external_ip
        for url in _PUBLIC_IP_SERVICES:
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


# PUT_DELAY_LOCAL is the per-node stagger before first announcement.
# Kept here (not in params) because it's a discovery-internal timing detail.
PUT_DELAY_LOCAL = 30
