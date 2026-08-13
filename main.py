"""Entry point. Creates PeerPool, Discovery, Gossip, Syncer, Node, API."""

import argparse
import getpass
import logging
import os
import queue
import sys
import threading

import block as block_mod
import crypto
import params
from api import create_app
from discovery import Discovery
from gossip import Gossip
from node import Node
from params import DB_PATH
from peerpool import PeerPool
from syncer import Syncer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logging.getLogger("ec").setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

class _WerkzeugFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "BitTorrent" not in msg and "Bad request version" not in msg and "Bad HTTP" not in msg

logging.getLogger("werkzeug").addFilter(_WerkzeugFilter())
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("ec.main")


def main():
    parser = argparse.ArgumentParser(description="Echocoin node")
    parser.add_argument("--host",    default="0.0.0.0")
    parser.add_argument("--port",    type=int, default=8333)
    parser.add_argument("--keyfile", default="echocoin_key.json")
    parser.add_argument("--db",      default=DB_PATH)
    parser.add_argument("--peer",       action="append", default=[])
    parser.add_argument(
        "--max-peers", type=int, default=params.MAX_PEERS,
        help="Hard cap on peer table size (default %(default)s).",
    )
    parser.add_argument(
        "--passphrase", default=None,
        help=(
            "Key passphrase. WARNING: visible in shell history and process list. "
            "Only use in non-interactive environments where you accept that risk."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    args = parser.parse_args()
    logging.getLogger("ec").setLevel(getattr(logging, args.log_level))

    # Key setup
    if not os.path.exists(args.keyfile):
        print("No key file found. Creating new FALCON-512 keypair.")
        passphrase = args.passphrase or _prompt_new_passphrase()
        sk, pk = crypto.generate_keypair()
        crypto.save_key(args.keyfile, sk, pk, passphrase)
        kek = crypto.derive_kek(args.keyfile, passphrase)
        addr = crypto.public_key_to_address(pk)
        log.info("[startup] key created  file=%s", args.keyfile)
        log.info("[startup] address=%s", addr)
        del sk, passphrase
    else:
        passphrase = args.passphrase or getpass.getpass("Passphrase: ")
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
    pool      = PeerPool(args.host, args.port, max_peers=args.max_peers)
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

    app = create_app(node, pool, net_in_q, discovery)
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
            node.chain[-1]["hash"],
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
