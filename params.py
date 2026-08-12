"""Protocol constants. No logic, no I/O."""

# Denomination: 1 PC = 100_000_000 seeds (same precision as BTC/satoshis).
# All balances, amounts, fees, and rewards are integers in seeds.
SEEDS_PER_PC               = 100_000_000
BLOCK_REWARD               = 10 * SEEDS_PER_PC   # 10 PC, expressed in seeds
BLOCK_CYCLE_SECONDS        = 120
PUZZLE_PHASE_SECONDS       = 60
BUILD_PHASE_SECONDS        = 60

DIFFICULTY_WINDOW          = 100
DIFFICULTY_CLAMP_LOW       = 0.5
DIFFICULTY_CLAMP_HIGH      = 2.0
TARGET_SOLUTIONS_PER_BLOCK = 100

FEE_RATE_WINDOW    = 100
FEE_HEIGHT_MAX_AGE = 5

# Target byte volume per block for fee rate computation.
# Fee rate targets this fixed volume rather than solver count.
BLOCK_SIZE_TARGET_BYTES = 500_000  # 500 KB
BLOCK_SIZE_LIMIT = 10_000_000   # 10 MB, fixed

MAX_PEERS           = 125  # hard cap on peer table size
PEER_CHECK_INTERVAL = 60

ADDRESS_BITS       = 132
ADDRESS_WORD_COUNT = 12
WORD_BITS          = 11

INITIAL_DIFFICULTY_TARGET = 2**240
INITIAL_FEE_RATE          = 1

# Genesis message. Embedded in block 0 and hashed into the genesis block hash.
# Cannot change after launch without breaking network identity.
GENESIS_MESSAGE = "PoolCoin genesis. No premine. No authority. Every node earns. The chain is its own clock: one block per two minutes, chain length tells the age."

DB_PATH = "poolcoin_chain.db"

# Genesis timestamp: unix time when the chain was launched. Set once manually
# before the first release and never changed. Stored in the genesis block and
# used as the parent timestamp for validating block 1's minimum interval.
GENESIS_TIMESTAMP = 1786535647

# Number of BEP44 DHT slots used for peer discovery.
BEP44_SLOT_COUNT = 256

# Set by build_config.py at build time. True = testnet (default), False = production.
TESTNET = True
NETWORK_NAME = "PoolCoin Testnet" if TESTNET else "PoolCoin"
