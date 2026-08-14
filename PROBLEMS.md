# Known Problems and Bugs


### `syncer.check_and_sync()` missing argument in `main.py`
Fixed.


### All discovery sources now go through a unified candidate pipeline
BEP44 alerts, torrent DHT peers, `receive_block` senders, and the peer-list
crawl all call `enqueue_candidate()` instead of hitting `_validate_and_add`
directly. Every 15 s the staging set is drained: `/api/info` is fetched from
each reachable candidate in parallel to build a one-hop graph, candidates are
ranked by ascending nomination count (lightly-nominated = bridge to
under-connected parts of the network), and only then does genesis validation
run in ranked order. Unreachable candidates are silently discarded before any
genesis work is wasted on them. No source has special trust.
