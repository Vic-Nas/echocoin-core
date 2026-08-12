"""Periodic chain sync. Compares local height with a random peer,
fetches their chain if they are ahead.

No threads of its own. Called by the node loop on a timer.
"""

import logging

import requests

log = logging.getLogger("pc.syncer")

# Hard cap on blocks fetched in a single sync to bound memory usage.
# At one block per 2 minutes, 50,000 blocks is ~70 days of chain history.
# A node that far behind should sync in multiple passes regardless.
FETCH_CHAIN_MAX_BLOCKS = 50_000


class Syncer:

    def __init__(self, pool):
        self.pool = pool

    def check_and_sync(self, local_height, apply_fn):
        """Pick a random peer, compare height and tip hash, sync if they have
        a better chain (longer, or same height with lower tip hash).
        apply_fn(chain) should return True on success.
        Returns True if the chain was updated."""
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
        local_tip_hash  = self._local_tip_hash()
        if remote_height < local_height:
            return False
        # Same height: only sync if the remote tip hash is strictly lower
        # Only sync if remote hash is strictly lower (lowest hash wins).
        if remote_height == local_height and remote_tip_hash >= local_tip_hash:
            return False

        log.debug("[sync] peer %s is at height %d (local %d), fetching chain",
                 peer, remote_height, local_height)
        chain = self.fetch_chain_from(peer)
        if chain:
            return apply_fn(chain)
        return False

    def _local_tip_hash(self):
        """Return the local tip hash for tie-breaking. Override in tests."""
        return ""

    def fetch_chain_from(self, peer_addr):
        """Paginated chain fetch from one peer. Returns list or None."""
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

