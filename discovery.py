"""DHT peer discovery using libtorrent BEP44 mutable items.

Runs in its own daemon thread. Writes peers into PeerPool.
Reads nothing else. No queues, no callbacks, no side effects
beyond PeerPool.add().
"""

import json
import time
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
import nacl.signing

from params import BEP44_SLOT_COUNT

log = logging.getLogger("pc.discovery")

PEER_CACHE_FILE  = "poolcoin_peers.json"
DHT_STATE_FILE   = "poolcoin_lt_dht.dat"
SAVE_INTERVAL    = 300
GET_INTERVAL          = 60
PUT_DELAY             = 30
PUT_REFRESH_INTERVAL  = 3600  # re-announce our slot at least this often

# Peer-of-peer crawl: fetch peer lists from current peers, rank candidates
# by nomination count (how many peers listed them), try best-connected first.
CRAWL_INTERVAL = 600   # crawl every 10 minutes when at max peers
GET_INTERVAL_FAST = GET_INTERVAL  # same rate when below max


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
        self._external_ip = None  # set by UPnP in run()
        if not node_pubkey_hex:
            log.warning("[dht] no node_pubkey_hex provided -- using slot 0 and offset 0; "
                        "all nodes without a pubkey will collide on the same slot")

    # ------------------------------------------------------------------
    # Main loop (called as thread target)
    # ------------------------------------------------------------------

    def run(self):
        import libtorrent as lt

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

        last_get   = 0
        last_put   = 0
        last_save  = 0
        last_crawl = 0
        start_time = time.monotonic()

        # Initial get immediately after bootstrap
        self._bep44_get_all(ses)
        last_get = time.monotonic()

        while True:
            alert_event.wait(timeout=1)
            alert_event.clear()

            self._process_alerts(ses)

            now = time.monotonic()

            # Fast when below max peers, slow when full.
            peer_count = self.pool.count()
            at_max = peer_count >= self.pool._max_peers
            get_interval = 300 if at_max else GET_INTERVAL
            if now - last_get > get_interval:
                self._bep44_get_all(ses)
                last_get = now

            crawl_interval = CRAWL_INTERVAL if at_max else GET_INTERVAL
            if now - last_crawl > crawl_interval:
                self._validate_executor.submit(self._crawl_peer_lists)
                last_crawl = now

            # Periodic put: refresh our slot on a fixed cadence, staggered
            # by my_offset (seconds) so nodes don't all put at once.
            first_put_ready = (last_put == 0 and now - start_time >= PUT_DELAY + my_offset % 300)
            refresh_ready = (last_put != 0 and now - last_put >= PUT_REFRESH_INTERVAL)
            if first_put_ready or refresh_ready:
                my_addr = f"{self._my_ip()}:{self.port}"
                self._bep44_put(ses, my_slot, my_addr)
                last_put = now

            # Evict stale, save state
            self.pool.evict_stale()
            if now - last_save > SAVE_INTERVAL:
                self._save_peer_cache()
                self._save_lt_state(ses)
                new_ip = self._upnp_map_port()
                if new_ip and new_ip != self._external_ip:
                    log.info("[upnp] external IP changed: %s -> %s", self._external_ip, new_ip)
                    self._external_ip = new_ip
                last_save = now

    # ------------------------------------------------------------------
    # Alert processing
    # ------------------------------------------------------------------

    def _process_alerts(self, ses):
        import libtorrent as lt
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
                        log.debug("[dht] BEP44 found node  addr=%s", addr)
                        self._validate_executor.submit(self._validate_and_add, addr)
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
    # Peer validation (genesis check + height plausibility)
    # ------------------------------------------------------------------

    def _validate_and_add(self, peer_addr):
        """Check a candidate peer's /api/info, add to pool if valid.
        On successful connection, also fetch their peer list so we learn
        about the wider network one hop at a time."""
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
                # Immediately fetch this peer's peer list so we learn one hop further.
                self._validate_executor.submit(self._fetch_and_queue_peers, peer_addr)
        except requests.exceptions.Timeout:
            log.debug("[peer] validation timed out  addr=%s", peer_addr)
        except Exception:
            log.debug("[peer] validation failed  addr=%s", peer_addr, exc_info=True)
            self.pool.strike(peer_addr)

    def _fetch_and_queue_peers(self, peer_addr):
        """Fetch one peer's peer list and submit each unknown address for validation."""
        try:
            r = requests.get(f"http://{peer_addr}/api/peers", timeout=5)
            if r.status_code != 200:
                return
            data = r.json()
            candidates = data.get("peers", [])
            known = set(self.pool.all_addrs())
            new_candidates = [c for c in candidates if isinstance(c, str) and c not in known]
            if new_candidates:
                log.debug("[peer] %s offered %d candidates (%d new)",
                          peer_addr, len(candidates), len(new_candidates))
            for addr in new_candidates:
                self._validate_executor.submit(self._validate_and_add, addr)
        except Exception:
            log.debug("[peer] peer list fetch failed  addr=%s", peer_addr, exc_info=True)

    def _crawl_peer_lists(self):
        """One-depth crawl: fetch peer lists from all current peers in parallel,
        then try candidates sorted by *ascending* nomination count.

        Low nomination count = few of your peers already know this node.
        That means it's likely a gateway to a part of the network you're not
        well-connected to, which improves path diversity. High-nomination
        candidates are already well-known to your peers and add little new
        reach -- prioritizing them would push all nodes toward the same hubs
        and recreate centralization.

        The entire fetch runs in the background. Results are only acted on
        once all peers have been queried (or timed out), so the crawl has no
        impact on the main loop or the validate thread pool while in progress.
        """
        current_peers = self.pool.get_all()
        if not current_peers:
            return

        known = set(self.pool.all_addrs())
        nominations: dict[str, int] = {}

        # Fetch all peer lists in parallel with a short per-peer timeout.
        def fetch_one(peer_addr):
            try:
                r = requests.get(f"http://{peer_addr}/api/peers", timeout=5)
                if r.status_code == 200:
                    return r.json().get("peers", [])
            except Exception:
                pass
            return []

        with ThreadPoolExecutor(max_workers=min(len(current_peers), 32)) as ex:
            results = ex.map(fetch_one, current_peers)

        for peer_list in results:
            for addr in peer_list:
                if isinstance(addr, str) and addr not in known:
                    nominations[addr] = nominations.get(addr, 0) + 1

        if not nominations:
            return

        # Sort ascending: low-nomination candidates first (highest path diversity value).
        # Cap at max_peers -- no point queuing more than the pool can hold.
        ranked = sorted(nominations, key=lambda a: nominations[a])[:self.pool._max_peers]
        log.info("[peer] crawl found %d candidates  unique=%d  multi_nominated=%d",
                 len(ranked),
                 sum(1 for c in nominations if nominations[c] == 1),
                 sum(1 for c in nominations if nominations[c] > 1))

        for addr in ranked:
            self._validate_executor.submit(self._validate_and_add, addr)

    # ------------------------------------------------------------------
    # BEP44 helpers (moved verbatim from old network.py)
    # ------------------------------------------------------------------

    _PUBLIC_IP_SERVICES = [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://checkip.amazonaws.com",
    ]

    def _my_ip(self):
        """Return the best IP to advertise in the DHT: external (UPnP) if available,
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
            b"poolcoin-peers-v1:"
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
            ses.dht_put_mutable_item(sk_64, pk, value, b"poolcoin-v1")
            log.info("[dht] BEP44 put  slot=%d  addr=%s",
                     slot_index, my_addr)
        except Exception:
            log.error("[dht] BEP44 put error", exc_info=True)

    def _bep44_get_all(self, ses):
        my_slot = getattr(self, "_my_slot", None)
        for i in range(BEP44_SLOT_COUNT):
            if i == my_slot:
                continue   # never read our own slot -- we already know our address
            _, pk = self._bep44_slot_keypair(i)
            try:
                ses.dht_get_mutable_item(pk, b"poolcoin-v1")
            except Exception:
                pass
        log.debug("[dht] BEP44 get  slots=%d", BEP44_SLOT_COUNT - (1 if my_slot is not None else 0))

    def _make_lt_session(self):
        import libtorrent as lt
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
            import libtorrent as lt
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
            if not peers:
                return
            with ThreadPoolExecutor(max_workers=min(len(peers), 16)) as ex:
                for addr in peers:
                    ex.submit(self._validate_and_add, addr)
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
            if u.addportmapping(self.port, "TCP", u.lanaddr, self.port, "PoolCoin node", ""):
                log.debug("[upnp] mapped %s:%d -> %s:%d", ext, self.port, u.lanaddr, self.port)
                return ext
        except Exception:
            pass
        return None
