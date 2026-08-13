"""Periodic chain sync. Compares local tip with a random peer,
fetches their chain if they are ahead.

No threads of its own. Called by the node loop once per N cycles.
"""

import logging
import requests

log = logging.getLogger("ec.syncer")

FETCH_CHAIN_MAX_BLOCKS = 50_000


class Syncer:

    def __init__(self, pool):
        self.pool = pool

    def check_and_sync(self, local_height, local_tip_hash, apply_fn):
        """Pick a random peer, compare tip, sync if they have a better chain.

        local_height:   int, current chain height
        local_tip_hash: str, current tip block hash (for tie-breaking)
        apply_fn:       callable(chain) -> bool, called with the fetched chain

        Returns True if the chain was updated.
        """
        peer = self.pool.random()
        if not peer:
            return False
        try:
            r = requests.get(f"http://{peer}/api/info", timeout=3)
            if r.status_code != 200:
                return False
            info = r.json()
        except Exception:
            self.pool.strike(peer)
            return False

        remote_height   = info.get("height", 0)
        remote_tip_hash = info.get("tip_hash", "")

        if remote_height < local_height:
            return False
        # Same height: only sync if remote tip hash is strictly lower
        # (lowest hash wins; same or higher means we already have the better chain).
        if remote_height == local_height and remote_tip_hash >= local_tip_hash:
            return False

        log.info("[sync] peer %s is ahead  remote=%d  local=%d  fetching",
                 peer, remote_height, local_height)
        chain = self._fetch_chain(peer)
        if chain:
            return apply_fn(chain)
        return False

    def _fetch_chain(self, peer_addr):
        """Paginated chain fetch. Returns list of block dicts or None."""
        try:
            chain, from_h = [], 0
            while True:
                r = requests.get(
                    f"http://{peer_addr}/api/chain",
                    params={"from": from_h, "to": from_h + 499},
                    timeout=30,
                )
                if r.status_code != 200:
                    break
                page = r.json()
                if not isinstance(page, list) or not page:
                    break
                chain.extend(page)
                if len(chain) >= FETCH_CHAIN_MAX_BLOCKS:
                    break
                if len(page) < 500:
                    break
                from_h += 500
            return chain or None
        except Exception:
            return None
