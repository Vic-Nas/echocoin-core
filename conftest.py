import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))
sys.path.insert(0, os.path.dirname(__file__))

# block.validate rejects timestamps more than 30s in the future using
# time.time(). On CI the system clock may be behind the genesis timestamp,
# causing spurious failures. Patch block._time.time to a fixed value well
# after genesis so tests are clock-independent.
from params import GENESIS_TIMESTAMP
import block as _block_mod

_TEST_NOW = GENESIS_TIMESTAMP + 365 * 24 * 3600  # one year after genesis
patch.object(_block_mod._time, "time", return_value=_TEST_NOW).start()
