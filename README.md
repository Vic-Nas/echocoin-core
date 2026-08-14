# Echocoin

A peer-to-peer electronic cash system. Every participating node earns the full reward for every block it builds. No pools, no puzzle, no energy waste. Block timing is enforced by a Verifiable Delay Function anchored to real elapsed time. Fees are deterministic and burned. Signatures are quantum-resistant (FALCON-512).

See [docs/whitepaper.md](docs/whitepaper.md) for the protocol specification and [docs/FLOW.md](docs/FLOW.md) for a walkthrough of the node's internals.

## Quickstart

```
pip install -r requirements.txt
python main.py
```

Open `http://localhost:8333` for the node UI. On first run a FALCON-512 keypair is generated and you are prompted for a passphrase to encrypt it.

## Passphrase

The signing passphrase is required to start the node. Two ways to supply it:

**Interactive** (default): you are prompted via `getpass` on startup. Nothing is stored in shell history or visible to `ps`.

**Non-interactive** (Docker, systemd, CI): set the `ECHOCOIN_PASSPHRASE` environment variable before starting the process.

```bash
export ECHOCOIN_PASSPHRASE="your passphrase"
python main.py
```

Or inline with systemd:

```ini
[Service]
Environment=ECHOCOIN_PASSPHRASE=your passphrase
ExecStart=/usr/local/bin/echocoin
```

The `--passphrase` CLI flag has been removed. It was visible in process listings (`ps aux`) and shell history, making it unsafe for any deployment.

## CLI options

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8333` | Port for HTTP API and peer connections |
| `--keyfile` | `echocoin_key.json` | Path to encrypted keypair |
| `--db` | `echocoin_chain.db` | Path to SQLite chain database |
| `--peer host:port` | - | Bootstrap peer (repeatable) |
| `--max-peers` | `125` | Hard cap on peer table size |
| `--log-level` | `INFO` | Verbosity: DEBUG, INFO, WARNING, ERROR |

## Building a standalone binary

```
pip install pyinstaller cairosvg Pillow
make linux    # on Linux
make windows  # on Windows
```

Produces `dist/echocoin` with no Python dependency. Icons are regenerated from `echocoin.svg` before each build.

## Requirements

- Python 3.11+
- chiavdf (VDF computation and verification)
- pqcrypto (FALCON-512 signatures)
- See `requirements.txt` for the full list
