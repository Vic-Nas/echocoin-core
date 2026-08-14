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

    def check_and_sync(self, local_chain, apply_fn):
        """Pick a random peer, compare tip, sync if they have a better chain.

        local_chain:  list of block dicts (the node's current chain)
        apply_fn:     callable(chain) -> bool, called with the fetched chain

        Finds the common ancestor by binary-searching the peer's chain using
        the local tip hash, so only the differing tail is fetched rather than
        the entire chain from block 0.

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

        remote_height = info.get("height", 0)
        local_height  = len(local_chain) - 1

        if remote_height < local_height:
            return False

        # Find the fork point: walk back until we find a hash the peer also has.
        fork_from = self._find_fork_point(peer, local_chain)
        if fork_from is None:
            return False

        log.info("[sync] peer %s at height=%d  local=%d  fork_from=%d  fetching",
                 peer, remote_height, local_height, fork_from)

        tail = self._fetch_chain(peer, from_h=fork_from)
        if not tail:
            return False

        # Reconstruct the full chain: trusted local prefix + fetched tail.
        full_chain = local_chain[:fork_from] + tail
        return apply_fn(full_chain)

    def _find_fork_point(self, peer_addr, local_chain):
        """Return the height at which our chain and the peer's diverge.

        Walks back from the local tip one step at a time -- this is fast in
        the common case (peer is just a few blocks ahead) and bounded by the
        chain length in the worst case (a full reorg from genesis).
        Returns the height of the first block to fetch (the fork point + 1),
        or 0 if genesis is the common ancestor.
        Returns None on network error.
        """
        for height in range(len(local_chain) - 1, -1, -1):
            local_hash = local_chain[height]["hash"]
            try:
                r = requests.get(
                    f"http://{peer_addr}/api/chain",
                    params={"from": height, "to": height},
                    timeout=10,
                )
                if r.status_code != 200:
                    return None
                page = r.json()
                if not isinstance(page, list) or not page:
                    return None
                if page[0].get("hash") == local_hash:
                    return height + 1   # diverges at the next block
            except Exception:
                return None
        return 0

    def _fetch_chain(self, peer_addr, from_h=0):
        """Paginated chain fetch starting from from_h. Returns list of block dicts or None."""
        try:
            chain = []
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
