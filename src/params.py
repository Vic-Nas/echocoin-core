"""Protocol constants. No logic, no I/O."""

# Denomination: 1 SCH = 100_000_000 embers (same precision as BTC/satoshis).
# All balances, amounts, fees, and rewards are integers in embers.
EMBERS_PER_SCH = 100_000_000

# Emission. Supply is bounded at 21M SCH with smooth exponential decay
# over a 20-year half-life. Burnt fees replenish can_mint, sustaining
# rewards indefinitely. No halvings, no supply shock.
SUPPLY_CAP        = 21_000_000 * EMBERS_PER_SCH
EMISSION_HALFLIFE = 5_000_000  # blocks (~20 years at 2 min/block)
EMISSION_RATE     = 0.5 ** (1 / EMISSION_HALFLIFE)  # per-block decay factor

BLOCK_CYCLE_SECONDS = 120

FEE_RATE_WINDOW    = 100      # blocks used for median volume signal
FEE_HEIGHT_MAX_AGE = 20       # max age of fee_height field in a tx

BLOCK_SIZE_TARGET_BYTES = 200_000   # 200 KB soft target for vol_ratio in fee formula
BLOCK_SIZE_LIMIT        = 10_000_000  # 10 MB hard cap, raised only by network upgrade

MAX_PEERS           = 125
PEER_CHECK_INTERVAL = 60

ADDRESS_BITS       = 132
ADDRESS_WORD_COUNT = 12
WORD_BITS          = 11

INITIAL_FEE_RATE = 10     # embers/byte; low start, fee formula rises under load

# Proof-of-Burn sliding window. Burns older than this many blocks no longer
# count toward a sender's reward share. 500 blocks ~ 17 hours at 2 min/block:
# long enough for daily participation cycles, short enough to prevent
# permanent whale dominance from a one-time burn.
POB_WINDOW = 500

# Fraction of the newly-minted block reward paid unconditionally to the
# block builder, regardless of burn activity. Keeps block production
# profitable even with an empty mempool and no burns in the PoB window,
# and removes any incentive to suppress burn transactions -- the
# builder's cut is constant whether burns exist or not. The remainder
# splits proportionally among burners in the window, or stays unminted
# in can_mint if none exist.
BUILDER_REWARD_SHARE = 0.02

# VDF iteration count targeting ~120 seconds of sequential computation on
# commodity hardware. NOT YET CALIBRATED: this is a placeholder value, not
# a measured one -- no benchmark has been run against real target hardware.
# Must be replaced with an empirically measured value before genesis.
VDF_ITERATIONS = 12_200_000  # placeholder -- unmeasured

# VDF difficulty adjustment. The iteration count can only increase over time
# as hardware gets faster. Adjustment happens every VDF_ADJUST_INTERVAL blocks
# using the median real block-to-block timestamp delta across that window
# (not a self-reported figure -- every node computes this identically from
# chain data alone). If median < VDF_ADJUST_MIN_SECONDS, iterations increase
# by VDF_ADJUST_FACTOR. Iterations never decrease; faster hardware means
# shorter block times until the next upward adjustment, never a security
# regression.
#
# Window size matches Bitcoin's actual real-time retarget window (2 weeks),
# not its block count -- block count alone isn't the right basis, since
# what resists manipulation is how long an attacker must sustain outsized
# influence over the window's median, not how many blocks it spans. At our
# 2-minute cadence that's 2 weeks / 2 min = 10,080 blocks.
VDF_ADJUST_INTERVAL    = 10_080  # blocks between adjustments (~2 weeks)
VDF_ADJUST_MIN_SECONDS = 100    # trigger increase if median falls below this
VDF_ADJUST_FACTOR      = 1.02   # max 2% increase per adjustment period

# Genesis message. Embedded in block 0 and hashed into the genesis block hash.
# Cannot change after launch without breaking network identity.
GENESIS_MESSAGE = (
    "Scorchcoin genesis. No premine. No authority. Every node earns. "
    "The chain is its own clock: one VDF per block, real elapsed time."
)

DB_PATH = "scorchcoin_chain.db"

# Genesis timestamp: unix time when the chain was launched. Set once manually
# before the first release and never changed.
GENESIS_TIMESTAMP = 1787456696

# Number of BEP44 DHT slots used for peer discovery.
BEP44_SLOT_COUNT = 256

# TESTNET = True: GitHub Actions updates GENESIS_TIMESTAMP on every release,
# letting the chain restart fresh. Set to False for mainnet; at that point
# GENESIS_TIMESTAMP is fixed manually once and the workflow never touches it.
TESTNET      = True
NETWORK_NAME = "Scorchcoin Testnet" if TESTNET else "Scorchcoin"
