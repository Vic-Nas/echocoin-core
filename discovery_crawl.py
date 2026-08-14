"""Peer-list crawl strategy.

Fetches /api/peers from all current pool members in parallel and enqueues
their neighbours as candidates. The nomination ranking in the coordinator's
_flush_candidates() handles prioritisation -- this just widens the candidate set.

Public interface:
  CrawlDiscovery(pool, enqueue_fn)
  .crawl_and_enqueue()
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("ec.discovery.crawl")


class CrawlDiscovery:

    def __init__(self, pool, enqueue_fn):
        self._pool    = pool
        self._enqueue = enqueue_fn

    def crawl_and_enqueue(self):
        """Fetch peer lists from all current pool members and enqueue neighbours."""
        current_peers = self._pool.get_all()
        if not current_peers:
            return

        known = set(self._pool.all_addrs())

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
                    self._enqueue(addr)
                    count += 1

        if count:
            log.debug("[peer] crawl enqueued %d candidates", count)
