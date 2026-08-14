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


def _resolve_passphrase(prompt):
    """Return a passphrase string from ECHOCOIN_PASSPHRASE env var or interactive prompt.
    Non-interactive deployments (Docker, systemd) set ECHOCOIN_PASSPHRASE.
    Interactive sessions are prompted via getpass (no shell-history exposure).
    --passphrase CLI flag has been removed; it was visible in process listings.
    """
    env_pass = os.environ.get("ECHOCOIN_PASSPHRASE")
    if env_pass:
        return env_pass
    return getpass.getpass(prompt)


def _load_or_create_key(keyfile):
    """Load or create a FALCON-512 keypair. Returns (public_key_bytes, kek).
    The KEK (key-encryption key) is kept in memory; the passphrase is dropped.
    """
    if not os.path.exists(keyfile):
        print("No key file found. Creating new FALCON-512 keypair.")
        passphrase = _resolve_passphrase("New passphrase: ")
        if not os.environ.get("ECHOCOIN_PASSPHRASE"):
            passphrase = _prompt_new_passphrase(passphrase)
        sk, pk = crypto.generate_keypair()
        crypto.save_key(keyfile, sk, pk, passphrase)
        kek = crypto.derive_kek(keyfile, passphrase)
        addr = crypto.public_key_to_address(pk)
        log.info("[startup] key created  file=%s", keyfile)
        log.info("[startup] address=%s", addr)
        del sk, passphrase
        return pk, kek
    passphrase = _resolve_passphrase("Passphrase: ")
    try:
        pk = crypto.load_pubkey(keyfile)
        kek = crypto.derive_kek(keyfile, passphrase)
        sk_test = crypto.decrypt_secret_key(keyfile, kek=kek)
        del sk_test
    except ValueError as e:
        sys.exit(f"Error: {e}")
    addr = crypto.public_key_to_address(pk)
    log.info("[startup] key loaded  file=%s", keyfile)
    del passphrase
    return pk, kek


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
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    args = parser.parse_args()
    logging.getLogger("ec").setLevel(getattr(logging, args.log_level))

    pk, kek = _load_or_create_key(args.keyfile)

    genesis  = block_mod.create_genesis()
    pk_hex   = pk.hex()

    pool      = PeerPool(args.host, args.port, max_peers=args.max_peers)
    gossip    = Gossip(pool, args.port)
    syncer    = Syncer(pool)
    net_in_q  = queue.Queue()
    discovery = Discovery(pool, genesis["hash"], args.port, pk_hex)
    node      = Node(args.keyfile, pk, gossip, syncer, pool, net_in_q, db_path=args.db)

    for peer in args.peer:
        parts = peer.split(":")
        if len(parts) == 2:
            discovery.add_bootstrap_peer(f"{parts[0]}:{parts[1]}")

    threading.Thread(target=discovery.run, daemon=True).start()

    app = create_app(node, pool, net_in_q, discovery)
    threading.Thread(
        target=lambda: app.run(host=args.host, port=args.port, threaded=True),
        daemon=True,
    ).start()
    log.info("[startup] API on http://%s:%d", args.host, args.port)
    log.info("[startup] genesis=%s", genesis["hash"][:12])

    if pool.count() > 0:
        syncer.check_and_sync(
            node.chain,
            lambda chain: node.sync_chain(chain)[0],
        )

    try:
        node.start(kek=kek)
    except KeyboardInterrupt:
        log.info("[shutdown] stopped")
        node.stop()


def _prompt_new_passphrase(first=None):
    while True:
        p1 = first if first else getpass.getpass("New passphrase: ")
        first = None
        if len(p1) < 8:
            print("Passphrase must be at least 8 characters.")
            continue
        p2 = getpass.getpass("Confirm passphrase: ")
        if p1 == p2:
            return p1
        print("Passphrases do not match.")


if __name__ == "__main__":
    main()
