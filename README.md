# LapseCoin

A peer-to-peer electronic cash system. Most cumulative proven work wins, first valid block received: Bitcoin-style consensus. Block timing is enforced by a Verifiable Delay Function anchored to real elapsed time, believed to have a much smaller hardware-advantage gap than proof-of-work mining. Transactions are ordinary and plaintext, with sender-bid fees, much like Bitcoin's own. Signatures are quantum-resistant (FALCON-512).

See [docs/whitepaper.md](docs/whitepaper.md) for the full protocol specification.

**Live node:** https://lapsenode.cyprus-draco.ts.net/

**Bitcoin donations:** `bc1q8qxvr5zuws78650wz9rgzpqxfx7dqzl38rdtsw`

## Installation

**Recommended: use a pre-built release.** Building from source requires native libraries (liboqs, chiavdf) with complex C/C++ compilation. The binaries on the [releases page](https://github.com/Vic-Nas/scorchcoin-core/releases) are self-contained and need no dependencies.

```
# Linux
chmod +x lapsecoin
./lapsecoin

# Windows
lapsecoin.exe
```

**Running from source** requires Python 3.11+ and the native build dependencies for your platform:

```
pip install -r requirements.txt
python main.py
```

## Ports

LapseCoin runs two HTTP servers:

| Port | Interface | Purpose |
|---|---|---|
| `8333` (or `--port`) | `0.0.0.0` | Public node UI and peer API. Safe to expose. Send is disabled. |
| `port+2` (or `--private-port`) | `127.0.0.1` | Private wallet UI. Never expose this. Full access including Send. |

Open `http://localhost:8335` for your wallet, `http://localhost:8333` for the block explorer. The private port is always public port + 2 unless overridden. Note: port+3 is reserved for the DHT subsystem (libtorrent) — don't bind other services to it.

## Passphrase

The signing passphrase is required to start the node.

- **Interactive** (default): prompted via `getpass` on startup, never stored in shell history or visible to `ps`.
- **Non-interactive** (Docker, systemd, CI): set the `LAPSECOIN_PASSPHRASE` environment variable.

```bash
export LAPSECOIN_PASSPHRASE="your passphrase"
python main.py
```

The `--passphrase` CLI flag has been removed since it was visible in `ps aux` and shell history.

## CLI options

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind for the public port |
| `--port` | `8333` | Public port for HTTP API and peer connections |
| `--private-port` | `port+2` | Private port for wallet UI, always bound to 127.0.0.1 |
| `--keyfile` | `lapsecoin_key.json` | Path to encrypted keypair |
| `--db` | `lapsecoin_chain.db` | Path to SQLite chain database |
| `--peer host:port` | - | Bootstrap peer (repeatable) |
| `--max-peers` | `125` | Hard cap on peer table size |
| `--log-level` | `INFO` | Verbosity: DEBUG, INFO, WARNING, ERROR |

## Building from source

```
pip install pyinstaller cairosvg Pillow
make linux    # on Linux
make windows  # on Windows
```

Produces a self-contained binary in `dist/`. Requires cmake, ninja, and a C compiler for the native dependencies (on Windows, also liboqs and MSVC redistributables).

## Requirements

- Python 3.11+
- chiavdf (VDF computation and verification)
- liboqs-python (FALCON-512 signatures)
- See `requirements.txt` for the full list
