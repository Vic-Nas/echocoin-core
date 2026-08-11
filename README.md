# PoolCoin

A peer-to-peer electronic cash system where every participating node earns a share of every block reward. There is nothing a pool operator can offer that the protocol does not already provide. Fees are deterministic and burned. Signatures are quantum-resistant.

See [whitepaper.md](whitepaper.md) for the protocol specification.

## Quickstart

```
pip install -r requirements.txt
python main.py --port 8333
```

Open `http://localhost:8333` for the node UI.

```
python main.py --port 8444  # second node, finds first via DHT or peer cache
```

Options: `--host`, `--port`, `--keyfile`, `--db`, `--peer host:port`

## Building a standalone binary

```
pip install pyinstaller
make linux    # on Linux
make windows  # on Windows
```

Produces a single executable in `dist/` with no Python dependency.

## Key storage

Keys are encrypted at rest with Argon2id + NaCl secretbox. Passphrase mandatory at creation. The secret key is decrypted for each signing call and immediately discarded. Key file written at `0o600`.

## Module layout

| Module | Role |
|---|---|
| `params.py` | Protocol constants |
| `crypto.py` | Keys, signing, address derivation |
| `tx.py` | Transaction creation and validation |
| `block.py` | Block creation and validation |
| `state.py` | Balance ledger |
| `mining.py` | Puzzle, difficulty, rewards |
| `mempool.py` | Pending tx pool, candidate lists |
| `storage.py` | SQLite persistence |
| `node.py` | Block cycle orchestrator |
| `network.py` | Peer discovery, broadcast |
| `api.py` | HTTP API and web UI |
| `templates.py` | HTML base template |
