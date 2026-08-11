"""Entry point. Creates PeerPool, Discovery, Gossip, Syncer, Node, API."""

import os
import sys
import queue
import logging
import argparse
import getpass
import threading

import crypto
import block as block_mod
from peerpool import PeerPool
from discovery import Discovery
from gossip import Gossip
from syncer import Syncer
from node import Node
from api import create_app
from params import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logging.getLogger("pc").setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

class _WerkzeugFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "BitTorrent" not in msg and "Bad request version" not in msg and "Bad HTTP" not in msg

logging.getLogger("werkzeug").addFilter(_WerkzeugFilter())
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("pc.main")


def main():
    parser = argparse.ArgumentParser(description="PoolCoin node")
    parser.add_argument("--host",    default="0.0.0.0")
    parser.add_argument("--port",    type=int, default=8333)
    parser.add_argument("--keyfile", default="poolcoin_key.json")
    parser.add_argument("--db",      default=DB_PATH)
    parser.add_argument("--peer",    action="append", default=[])
    args = parser.parse_args()

    # Key setup
    if not os.path.exists(args.keyfile):
        print("No key file found. Creating new FALCON-512 keypair.")
        passphrase = _prompt_new_passphrase()
        sk, pk = crypto.generate_keypair()
        crypto.save_key(args.keyfile, sk, pk, passphrase)
        kek = crypto.derive_kek(args.keyfile, passphrase)
        addr = crypto.public_key_to_address(pk)
        log.info("[startup] key created  file=%s", args.keyfile)
        log.info("[startup] address=%s", addr)
        del sk, passphrase
    else:
        passphrase = getpass.getpass("Passphrase: ")
        try:
            pk = crypto.load_pubkey(args.keyfile)
            kek = crypto.derive_kek(args.keyfile, passphrase)
            sk_test = crypto.decrypt_secret_key(args.keyfile, kek=kek)
            del sk_test
        except ValueError as e:
            sys.exit(f"Error: {e}")
        addr = crypto.public_key_to_address(pk)
        log.info("[startup] key loaded  file=%s", args.keyfile)
        del passphrase

    genesis  = block_mod.create_genesis()
    pk_hex   = pk.hex()

    # Compose the four modules
    pool      = PeerPool(args.host, args.port)
    gossip    = Gossip(pool, args.port)
    syncer    = Syncer(pool)
    net_in_q  = queue.Queue()
    discovery = Discovery(pool, genesis["hash"], args.port, pk_hex)
    node      = Node(args.keyfile, pk, gossip, syncer, pool, net_in_q, db_path=args.db)

    # Manual --peer flags: just add to pool (discovery will validate later,
    # and the periodic syncer will fetch the chain if needed)
    for peer in args.peer:
        parts = peer.split(":")
        if len(parts) == 2:
            # Validate inline so the node can sync before first cycle
            discovery._validate_and_add(f"{parts[0]}:{parts[1]}")

    # Start background threads
    threading.Thread(target=discovery.run, daemon=True).start()

    app = create_app(node, pool, net_in_q)
    threading.Thread(
        target=lambda: app.run(host=args.host, port=args.port, threaded=True),
        daemon=True,
    ).start()
    log.info("[startup] API on http://%s:%d", args.host, args.port)
    log.info("[startup] genesis=%s", genesis["hash"][:12])

    # Initial sync from any peers added via --peer
    if pool.count() > 0:
        syncer.check_and_sync(
            len(node.chain) - 1,
            lambda chain: node.sync_chain(chain)[0],
        )

    try:
        node.start(kek=kek)
    except KeyboardInterrupt:
        log.info("[shutdown] stopped")
        node.stop()


def _prompt_new_passphrase():
    while True:
        p1 = getpass.getpass("New passphrase: ")
        if len(p1) < 8:
            print("Passphrase must be at least 8 characters.")
            continue
        p2 = getpass.getpass("Confirm passphrase: ")
        if p1 == p2:
            return p1
        print("Passphrases do not match.")


if __name__ == "__main__":
    main()
