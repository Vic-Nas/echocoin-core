# PYTHON_ARGCOMPLETE_OK
"""Entry point. Creates PeerPool, UDPTransport, Discovery, Gossip, Syncer, Node, API."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "src"))

import argparse
import getpass
import logging
import os
import queue
import sys
import threading

import argcomplete
from argcomplete.completers import FilesCompleter

import block as block_mod
import crypto
import http_probe
import params
from api import create_app, create_private_app
from discovery import Discovery
from gossip import Gossip
from node import Node
from params import DB_PATH
from peer_udp import UDPTransport
from peerpool import PeerPool
from syncer import Syncer
from update_check import DEFAULT_RELEASES_URL, DEFAULT_VERSION_URL, UpdateChecker
from version import LOCAL_VERSION

LOG_FILE = "lapsecoin.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logging.getLogger("ec").setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


class _WerkzeugFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return ("BitTorrent" not in msg and
                "Bad request version" not in msg and
                "Bad HTTP" not in msg)


logging.getLogger("werkzeug").addFilter(_WerkzeugFilter())
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("ec.main")


def _resolve_passphrase(prompt):
    env_pass = os.environ.get("LAPSECOIN_PASSPHRASE")
    if env_pass:
        return env_pass
    return getpass.getpass(prompt)


def _load_or_create_key(keyfile):
    if not os.path.exists(keyfile):
        print("No key file found. Creating new FALCON-512 keypair.")
        passphrase = _resolve_passphrase("New passphrase: ")
        if not os.environ.get("LAPSECOIN_PASSPHRASE"):
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
    log.info("[startup] key loaded  file=%s", keyfile)
    del passphrase
    return pk, kek


def main():
    parser = argparse.ArgumentParser(description="LapseCoin node")
    parser.add_argument("--host",    default="0.0.0.0")
    parser.add_argument("--port",    type=int, default=8333)
    parser.add_argument("--keyfile", default="lapsecoin_key.json"
                        ).completer = FilesCompleter()
    parser.add_argument("--db",      default=DB_PATH
                        ).completer = FilesCompleter()
    parser.add_argument("--peer",       action="append", default=[])
    parser.add_argument(
        "--private-port", type=int, default=None,
        help="Port for private API (send/burn). Defaults to --port+2.",
    )
    parser.add_argument(
        "--max-peers", type=int, default=params.MAX_PEERS,
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--update-check-url", default=DEFAULT_VERSION_URL,
        help="Raw VERSION file URL to poll for newer releases. "
             "Point this at your own fork's VERSION file if you maintain one.",
    )
    parser.add_argument(
        "--releases-url", default=DEFAULT_RELEASES_URL,
        help="Where the UI's 'new version available' link points.",
    )
    parser.add_argument(
        "--no-update-check", action="store_true",
        help="Disable the periodic check for newer releases entirely.",
    )
    parser.add_argument(
        "--no-gui", action="store_true",
        help="Always use the console (passphrase prompt, Ctrl+C to stop) "
             "instead of the desktop status window. Implied automatically "
             "when LAPSECOIN_PASSPHRASE is set (headless/server runs).",
    )
    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    logging.getLogger("ec").setLevel(getattr(logging, args.log_level))

    use_gui = not args.no_gui and not os.environ.get("LAPSECOIN_PASSPHRASE")
    gui = None
    if use_gui:
        try:
            import gui
        except ImportError as e:
            # tkinter isn't guaranteed present on every Python (e.g. Ubuntu's
            # system python3 needs python3-tk installed separately) -- fall
            # back to the console rather than failing to start at all.
            log.warning("[startup] GUI unavailable (%s), falling back to console", e)
            use_gui = False
    if use_gui:
        pk, kek = gui.load_or_create_key_gui(args.keyfile)
    else:
        pk, kek = _load_or_create_key(args.keyfile)
    genesis  = block_mod.create_genesis()
    pk_hex   = pk.hex()

    pool     = PeerPool(args.host, args.port, max_peers=args.max_peers)
    net_in_q = queue.Queue()

    # ------------------------------------------------------------------
    # UDP transport: single socket for all peer communication
    # ------------------------------------------------------------------
    def on_block(block, sender_addr):
        # Best-effort placeholder wallet for the peers page, shown as
        # unconfirmed until/unless a GETINFO exchange confirms one directly --
        # sender_addr may just be relaying someone else's block.
        pool.note_relayed_builder(sender_addr, block.get("builder"))
        net_in_q.put({"type": "block", "block": block, "sender": sender_addr})

    def on_tx(tx, sender_addr, msg_id, remaining_hops=0, relay_type="tx_fluff"):
        net_in_q.put({"type": "tx", "tx": tx,
                      "relay_type": relay_type, "remaining_hops": remaining_hops})

    def on_peers(peer_list, sender_addr):
        for p in peer_list:
            if isinstance(p, str) and ":" in p:
                discovery.enqueue_candidate(p)
        pool.touch(sender_addr)

    udp = UDPTransport(
        port=args.port,
        genesis_hash=genesis["hash"],
        on_block=on_block,
        on_tx=on_tx,
        on_peers=on_peers,
        pool=pool,
    )
    udp.start()
    log.info("[startup] UDP transport on port %d", args.port)

    # ------------------------------------------------------------------
    # Gossip, Syncer, Discovery, Node
    # ------------------------------------------------------------------
    gossip    = Gossip(pool, udp)
    syncer    = Syncer(pool, udp)
    discovery = Discovery(udp, pool, genesis["hash"], args.port, pk_hex)
    node      = Node(args.keyfile, pk, gossip, syncer, pool, net_in_q, db_path=args.db)

    def _chain_provider(from_h, to_h):
        chain = node.view.chain
        end   = (to_h + 1) if to_h is not None else None
        return chain[from_h:end]

    def _tip_provider():
        chain = node.view.chain
        tip   = chain[-1]
        return tip.get("height", 0), tip.get("hash", ""), node.addr, LOCAL_VERSION

    udp.set_chain_provider(_chain_provider)
    udp.set_tip_provider(_tip_provider)

    for peer in args.peer:
        parts = peer.split(":")
        if len(parts) == 2:
            discovery.add_bootstrap_peer(f"{parts[0]}:{parts[1]}")

    threading.Thread(target=discovery.run, daemon=True).start()
    threading.Thread(target=http_probe.run, args=(pool,), daemon=True).start()

    update_checker = UpdateChecker(
        local_version=LOCAL_VERSION,
        version_url="" if args.no_update_check else args.update_check_url,
        releases_url=args.releases_url,
    )
    update_checker.start()

    # ------------------------------------------------------------------
    # HTTP servers: browser UI only, no peer routes
    # ------------------------------------------------------------------
    private_port = args.private_port if args.private_port else args.port + 2

    app = create_app(node, pool, private_port=private_port,
                     public_port=args.port, update_checker=update_checker)
    threading.Thread(
        target=lambda: app.run(host=args.host, port=args.port, threaded=True),
        daemon=True,
    ).start()
    log.info("[startup] public API on http://%s:%d", args.host, args.port)

    private_app = create_private_app(node, pool, private_port=private_port,
                                     public_port=args.port, update_checker=update_checker)
    threading.Thread(
        target=lambda: private_app.run(host="127.0.0.1", port=private_port, threaded=True),
        daemon=True,
    ).start()
    log.info("[startup] private API on http://127.0.0.1:%d (send/burn)", private_port)
    log.info("[startup] genesis=%s", genesis["hash"][:12])

    if pool.count() > 0:
        syncer.check_and_sync(
            node.cs.chain,
            lambda chain: node.apply_better_chain(chain)[0],
        )

    if use_gui:
        threading.Thread(target=node.start, kwargs={"kek": kek}, daemon=True).start()
        gui.run_status_window(node, udp, private_port, LOG_FILE)
    else:
        try:
            node.start(kek=kek)
        except KeyboardInterrupt:
            log.info("[shutdown] stopped")
            node.stop()
            udp.stop()


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
