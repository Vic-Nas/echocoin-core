# PoolCoin

A peer-to-peer electronic cash system where every participating node earns a share of every block reward. There is nothing a pool operator can offer that the protocol does not already provide. Fees are deterministic and burned. Signatures are quantum-resistant.

See [whitepaper.md](whitepaper.md) for the protocol specification.

## Quickstart

```
pip install -r requirements.txt
python main.py
```

Open `http://localhost:8333` for the node UI. On first run a FALCON-512 keypair is generated and you are prompted for a passphrase to encrypt it.

## CLI options

```
python main.py [options]
```

| Option | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8333` | Port for HTTP API and peer connections |
| `--keyfile` | `poolcoin_key.json` | Path to encrypted keypair |
| `--db` | `poolcoin_chain.db` | Path to SQLite chain database |
| `--peer host:port` | - | Bootstrap peer (repeatable) |
| `--max-peers` | `125` | Hard cap on peer table size |
| `--passphrase` | - | Key passphrase. **Visible in shell history and process list. Only use in non-interactive environments.** |

## Building a standalone binary

```
pip install pyinstaller
make linux    # on Linux
make windows  # on Windows
```

Produces a single executable in `dist/` with no Python dependency.

## Key storage

Keys are encrypted at rest with Argon2id + NaCl secretbox. Passphrase is required at creation. The secret key is decrypted per signing call and immediately discarded. Key file is written at `0o600`.

## Protocol constants

Defined in `params.py`. Notable values:

- Block cycle: 120 seconds (60s puzzle phase, 60s build phase)
- Block reward: 10 PC (fixed, no halving)
- Block size: 500 KB target, 10 MB hard limit
- Denomination: 1 PC = 100,000,000 seeds
- Difficulty window: 100 blocks, clamped to 0.5x–2.0x per adjustment
- Target solutions per block: 100
- Peer discovery: 256 BEP44 DHT slots

## Module layout

`params.py` - Protocol constants. `crypto.py` - Keys, signing, address derivation. `tx.py` - Transaction creation and validation. `block.py` - Block creation and validation. `state.py` - Balance ledger. `mining.py` - Puzzle, difficulty, rewards. `mempool.py` - Pending transaction pool. `storage.py` - SQLite persistence. `node.py` - Block cycle orchestrator. `gossip.py` - Solution and transaction broadcast. `syncer.py` - Chain sync. `peerpool.py` - Peer table. `discovery.py` - DHT peer discovery. `api.py` - HTTP API and web UI. `templates.py` - HTML templates.
