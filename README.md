# Scorchcoin

A peer-to-peer electronic cash system. Most cumulative proven work wins, first valid block received: Bitcoin-style consensus. Block timing is enforced by a Verifiable Delay Function anchored to real elapsed time. Fees are deterministic and go to the block builder. Signatures are quantum-resistant (FALCON-512). Voluntary Proof-of-Burn lets any participant earn a proportional share of every block's reward by burning coins.

See [docs/whitepaper.md](docs/whitepaper.md) for the full protocol specification.

## Installation

**Recommended: use a pre-built release.** Building from source requires native libraries (liboqs, chiavdf) that involve complex C/C++ compilation and can produce DLL or shared library errors depending on your platform. The release binaries on the [releases page](https://github.com/Vic-Nas/scorchcoin-core/releases) are self-contained and require no dependencies.

Download the binary for your platform and run it directly:

```
# Linux
chmod +x scorchcoin
./scorchcoin

# Windows
scorchcoin.exe
```

### Running from source

If you need to run from source, Python 3.11+ is required along with the native build dependencies for your platform.

```
pip install -r requirements.txt
python main.py
```

## Ports

Scorchcoin runs two HTTP servers:

| Port | Interface | Purpose |
|---|---|---|
| `8333` (or `--port`) | `0.0.0.0` | Public node UI and peer API. Safe to expose. Send and Burn are disabled. |
| `port+2` (or `--private-port`) | `127.0.0.1` | Private wallet UI. Never expose this. Full access including Send and Burn. |

Open `http://localhost:8335` for your personal wallet interface. Open `http://localhost:8333` (or your public address) for the block explorer. The private port is always two above the public port unless overridden with `--private-port`.

Note: port+3 is reserved by the DHT subsystem (libtorrent) for its internal use. Do not bind other services to it.

## Passphrase

The signing passphrase is required to start the node. Two ways to supply it:

**Interactive** (default): you are prompted via `getpass` on startup. Nothing is stored in shell history or visible to `ps`.

**Non-interactive** (Docker, systemd, CI): set the `SCORCHCOIN_PASSPHRASE` environment variable before starting the process.

```bash
export SCORCHCOIN_PASSPHRASE="your passphrase"
python main.py
```

Or inline with systemd:

```ini
[Service]
Environment=SCORCHCOIN_PASSPHRASE=your passphrase
ExecStart=/usr/local/bin/scorchcoin
```

The `--passphrase` CLI flag has been removed. It was visible in process listings (`ps aux`) and shell history, making it unsafe for any deployment.

## CLI options

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind for the public port |
| `--port` | `8333` | Public port for HTTP API and peer connections |
| `--private-port` | `port+2` | Private port for wallet UI. Always bound to 127.0.0.1. |
| `--keyfile` | `scorchcoin_key.json` | Path to encrypted keypair |
| `--db` | `scorchcoin_chain.db` | Path to SQLite chain database |
| `--peer host:port` | - | Bootstrap peer (repeatable) |
| `--max-peers` | `125` | Hard cap on peer table size |
| `--log-level` | `INFO` | Verbosity: DEBUG, INFO, WARNING, ERROR |

## Building from source

Pre-built releases are strongly preferred. If you must build from source:

```
pip install pyinstaller cairosvg Pillow
make linux    # on Linux
make windows  # on Windows
```

Produces a self-contained binary in `dist/`. Building requires cmake, ninja, and a C compiler for the native dependencies. On Windows, liboqs and MSVC redistributables must be present.

## Requirements

- Python 3.11+
- chiavdf (VDF computation and verification)
- liboqs-python (FALCON-512 signatures)
- See `requirements.txt` for the full list
