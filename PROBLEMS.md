# Known Problems and Bugs


### `syncer.check_and_sync()` missing argument in `main.py`
Fixed.


### Genesis hash torrent seeding not implemented
Partially implemented. `_torrent_announce` and `_torrent_get_peers` are wired
into the main loop and `_process_alerts` now handles `dht_get_peers_reply_alert`
to enqueue candidates. The torrent port used is the node's API port, which is
what peers will connect back to -- this needs verifying against how libtorrent
reports peers from `get_peers` replies (they may arrive with the DHT port, not
the application port). The three mechanisms (BEP44, genesis-hash torrent, crawl)
now all feed into a unified candidate pipeline.


### All discovery sources now go through a unified candidate pipeline
BEP44 alerts, torrent DHT peers, `receive_block` senders, and the peer-list
crawl all call `enqueue_candidate()` instead of hitting `_validate_and_add`
directly. Every 15 s the staging set is drained: `/api/peers` is fetched from
each reachable candidate in parallel to build a one-hop graph, candidates are
ranked by ascending nomination count (lightly-nominated = bridge to
under-connected parts of the network), and only then does genesis validation
run in ranked order. Unreachable candidates are silently discarded before any
genesis work is wasted on them. No source has special trust.
