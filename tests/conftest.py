"""Shared pytest fixtures for the whole suite.

Autouse fixtures here keep individual test modules from having to
remember to patch the same cross-cutting things (e.g. puzzle difficulty)
every time they build a confirmation.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import timelock as timelock_mod
from fixtures import TEST_ITERATIONS


@pytest.fixture(autouse=True)
def _tiny_timelock_difficulty(monkeypatch):
    """Real TIMELOCK_ITERATIONS (params.py) is ~90 million: fine for
    production, far too slow for a test suite. Tests build confirmations
    with fixtures.TEST_ITERATIONS instead (see fixtures.make_confirmation),
    so block.py's own difficulty expectation (timelock.get_timelock_iterations,
    used to validate a confirmation's recorded "iterations" field) must be
    patched to agree, or every block-validation test involving a
    confirmation would fail on an iterations mismatch."""
    monkeypatch.setattr(timelock_mod, "TIMELOCK_ITERATIONS", TEST_ITERATIONS)
