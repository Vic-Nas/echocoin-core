"""Protocol constants. No logic, no I/O."""

# Denomination: 1 SCH = 100_000_000 embers (same precision as BTC/satoshis).
# All balances, amounts, fees, and rewards are integers in embers.
EMBERS_PER_SCH = 100_000_000

# Emission. Supply is bounded at 21M SCH with smooth exponential decay
# over a 20-year half-life. No halvings, no supply shock.
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

# VDF iteration count targeting ~120 seconds of sequential computation on
# target testnet hardware. Calibrated from real benchmark runs: median of
# three 500k-iteration timed runs measured ~6,100,000 iterations/61s, then
# doubled to reach the full ~120s target (see commits 34c65ab, d8616f3,
# a62e4bb). Re-measure if target/mainnet hardware differs from what was
# benchmarked for the testnet.
VDF_ITERATIONS = 12_200_000  # calibrated: ~120s on target hardware

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

# Time-lock puzzle (RSW construction, see timelock.py) difficulty. This is a
# protocol-wide constant: every transaction uses the same T. A sender-chosen
# T would leak a visible metadata signal even before decryption, defeating
# the content-blindness the ciphertext format is meant to provide.
#
# Calibration mirrors vdf.py's methodology (see that module's docstring),
# but the underlying operation here is RSA-style modular squaring, not
# class-group arithmetic, so the throughput numbers differ. Published
# benchmarks (a 2023 time-lock-puzzle paper measuring 2048-bit modular
# squaring) show roughly 0.6-0.85 million squarings/second on modern
# single-core hardware: about 0.85M/s on an Apple M1 Pro, 0.6-0.7M/s on
# server-class Xeon/EPYC parts. Targeting the same ~120 s floor used for
# VDF_ITERATIONS at the slower, more conservative end of that range gives
# roughly 80-100 million iterations; 90,000,000 is chosen as the midpoint.
TIMELOCK_ITERATIONS = 90_000_000  # calibrated: ~120-150s on target hardware

# RSA modulus size for each disposable puzzle. 2048 bits (two ~1024-bit
# safe-prime-derived factors) matches conventional RSA security margins.
TIMELOCK_MODULUS_BITS = 2048

# Safety margin applied when TIMELOCK_ITERATIONS is bumped in lockstep with
# a VDF_ADJUST_FACTOR increase (see timelock.get_timelock_iterations). RSA
# modular squaring has far more mature, widely-deployed dedicated hardware
# acceleration in the wild (TLS/crypto accelerators, ASICs) than class-group
# arithmetic does, so tracking VDF hardware improvements 1:1 would be
# optimistic. This multiplier is a heuristic margin, not a guarantee.
TIMELOCK_MARGIN_MULTIPLIER = 1.5

# Genesis message. Embedded in block 0 and hashed into the genesis block hash.
# Cannot change after launch without breaking network identity.
GENESIS_MESSAGE = (
    "Scorchcoin genesis. No premine. No authority. Every node earns. "
    "The chain is its own clock: one VDF per block, real elapsed time."
)

DB_PATH = "scorchcoin_chain.db"

# Genesis timestamp: unix time when the chain was launched. Set once manually
# before the first release and never changed.
GENESIS_TIMESTAMP = 1787580863

# Number of BEP44 DHT slots used for peer discovery.
BEP44_SLOT_COUNT = 256

# TESTNET = True: GitHub Actions updates GENESIS_TIMESTAMP on every release,
# letting the chain restart fresh. Set to False for mainnet; at that point
# GENESIS_TIMESTAMP is fixed manually once and the workflow never touches it.
TESTNET      = True
NETWORK_NAME = "Scorchcoin Testnet" if TESTNET else "Scorchcoin"
