"""Periodic chain sync over UDP.

Uses UDPTransport.get_info() for lightweight tip comparison and
UDPTransport.request_sync() for fetching chain segments.
"""

import logging

log = logging.getLogger("ec.syncer")

FETCH_CHUNK = 500   # blocks per GETSYNC request


class Syncer:

    def __init__(self, pool, udp):
        self.pool = pool
        self.udp  = udp

    def check_and_sync(self, local_chain, apply_fn, force_compare=False):
        """Pick a random peer, compare tip, sync if they have a better chain.

        force_compare: skip height check and always fetch+compare scores.
        Used when a competing tip at the current height has been detected.
        Returns True if the chain was updated.
        """
        peer = self.pool.random()
        if not peer:
            log.debug("[sync] no peers available")
            return False

        info = self.udp.get_info(peer)
        if info is None:
            log.debug("[sync] info request failed  peer=%s", peer)
            return False

        if not isinstance(info, dict) or "height" not in info:
            log.debug("[sync] unexpected info response  peer=%s", peer)
            return False

        remote_height = info["height"]
        local_height  = len(local_chain) - 1
        local_tip     = local_chain[-1]["hash"] if local_chain else ""

        if not force_compare:
            if remote_height < local_height:
                log.debug("[sync] peer not ahead  peer=%s  remote=%d  local=%d",
                          peer, remote_height, local_height)
                return False
            if remote_height == local_height:
                if info.get("tip_hash", "") == local_tip:
                    log.debug("[sync] already in sync  peer=%s  height=%d", peer, local_height)
                    return False
                log.debug("[sync] same height different tip  peer=%s  height=%d", peer, local_height)
        else:
            log.debug("[sync] force compare  peer=%s  remote=%d  local=%d",
                      peer, remote_height, local_height)

        fork_from = self._find_fork_point(peer, local_chain)
        if fork_from is None:
            log.warning("[sync] fork point search failed  peer=%s", peer)
            return False

        log.info("[sync] peer=%s remote=%d local=%d fork_from=%d fetching",
                 peer, remote_height, local_height, fork_from)

        tail = self._fetch_chain(peer, fork_from, remote_height)
        if not tail:
            log.warning("[sync] fetch returned empty  peer=%s", peer)
            return False

        full_chain = local_chain[:fork_from] + tail
        return apply_fn(full_chain)

    def _find_fork_point(self, peer, local_chain):
        """Binary search for common ancestor. O(log n) round trips."""
        lo, hi = 0, len(local_chain) - 1
        result = None

        while lo <= hi:
            mid = (lo + hi) // 2
            local_hash = local_chain[mid]["hash"]

            resp = self.udp.request_sync(peer, from_h=mid, to_h=mid, timeout=10)
            if resp is None:
                log.debug("[sync] fork search failed  peer=%s  height=%d", peer, mid)
                return None
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                log.debug("[sync] fork search failed  peer=%s  height=%d", peer, mid)
                return None

            if page[0].get("hash") == local_hash:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1

        return (result + 1) if result is not None else 0

    def _fetch_chain(self, peer, from_h, remote_height):
        """Fetch chain in FETCH_CHUNK-block pages."""
        chain = []
        h = from_h
        while h <= remote_height:
            to_h = min(h + FETCH_CHUNK - 1, remote_height)
            resp = self.udp.request_sync(peer, from_h=h, to_h=to_h, timeout=30)
            if resp is None:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                break
            page = resp.get("chain") if isinstance(resp, dict) else None
            if not isinstance(page, list) or not page:
                log.warning("[sync] fetch page empty  peer=%s  from_h=%d", peer, h)
                break
            chain.extend(page)
            if len(page) < FETCH_CHUNK:
                break
            h += FETCH_CHUNK
        return chain or None
