"""Protocol constants. No logic, no I/O."""

# Denomination: 1 ECH = 100_000_000 rings (same precision as BTC/satoshis).
# All balances, amounts, fees, and rewards are integers in rings.
RINGS_PER_ECH = 100_000_000

# Emission. Supply is bounded at 21M ECH with smooth exponential decay
# over a 20-year half-life. Burnt fees replenish can_mint, sustaining
# rewards indefinitely. No halvings, no supply shock.
SUPPLY_CAP        = 21_000_000 * RINGS_PER_ECH
EMISSION_HALFLIFE = 5_000_000  # blocks (~20 years at 2 min/block)
EMISSION_RATE     = 0.5 ** (1 / EMISSION_HALFLIFE)  # per-block decay factor

BLOCK_CYCLE_SECONDS = 120

FEE_RATE_WINDOW    = 100      # blocks used for median volume signal
FEE_HEIGHT_MAX_AGE = 5        # max age of fee_height field in a tx

BLOCK_SIZE_TARGET_BYTES = 200_000   # 200 KB soft target for vol_ratio in fee formula
BLOCK_SIZE_LIMIT        = 10_000_000  # 10 MB hard cap, raised only by network upgrade

MAX_PEERS           = 125
PEER_CHECK_INTERVAL = 60

ADDRESS_BITS       = 132
ADDRESS_WORD_COUNT = 12
WORD_BITS          = 11

INITIAL_FEE_RATE = 1_000  # rings/byte; gives fee formula room above the integer floor

# VDF iteration count tuned to ~120 seconds of sequential computation on
# commodity hardware. Must be calibrated empirically before genesis and
# cannot change after launch without breaking chain identity.
# Placeholder: replace with measured value before mainnet.
VDF_ITERATIONS = 2_000_000_000

# Genesis message. Embedded in block 0 and hashed into the genesis block hash.
# Cannot change after launch without breaking network identity.
GENESIS_MESSAGE = (
    "Echocoin genesis. No premine. No authority. Every node earns. "
    "The chain is its own clock: one VDF per block, real elapsed time."
)

DB_PATH = "echocoin_chain.db"

# Genesis timestamp: unix time when the chain was launched. Set once manually
# before the first release and never changed.
GENESIS_TIMESTAMP = 1786537835

# Number of BEP44 DHT slots used for peer discovery.
BEP44_SLOT_COUNT = 256

# Set by build_config.py at build time. True = testnet, False = production.
TESTNET      = True
NETWORK_NAME = "Echocoin Testnet" if TESTNET else "Echocoin"
