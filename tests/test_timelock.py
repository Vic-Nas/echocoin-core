"""
Unit tests for timelock.py (RSW time-lock puzzles).

Uses a small iteration count so tests run fast; TIMELOCK_ITERATIONS itself
is never exercised directly (that would take minutes).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import timelock as tl_mod

SMALL_T = 50  # small enough to solve instantly in tests


class TestGenerateAndSolve:
    def test_solver_recovers_payload(self):
        payload = b"real sender, real recipient, real amount"
        puzzle = tl_mod.generate_puzzle(payload, iterations=SMALL_T)
        recovered = tl_mod.solve_puzzle(
            puzzle["N"], puzzle["x"], puzzle["ciphertext"], iterations=SMALL_T
        )
        assert recovered == payload

    def test_puzzle_does_not_expose_factorization(self):
        puzzle = tl_mod.generate_puzzle(b"secret", iterations=SMALL_T)
        assert set(puzzle.keys()) == {"N", "x", "iterations", "ciphertext"}

    def test_n_and_x_are_ints(self):
        puzzle = tl_mod.generate_puzzle(b"secret", iterations=SMALL_T)
        assert isinstance(puzzle["N"], int)
        assert isinstance(puzzle["x"], int)
        assert 2 <= puzzle["x"] < puzzle["N"]

    def test_ciphertext_is_hex(self):
        puzzle = tl_mod.generate_puzzle(b"secret", iterations=SMALL_T)
        bytes.fromhex(puzzle["ciphertext"])  # must not raise

    def test_two_puzzles_use_different_moduli(self):
        """Each puzzle gets a disposable modulus -- no shared setup."""
        p1 = tl_mod.generate_puzzle(b"a", iterations=SMALL_T)
        p2 = tl_mod.generate_puzzle(b"b", iterations=SMALL_T)
        assert p1["N"] != p2["N"]

    def test_wrong_iteration_count_fails_to_decrypt(self):
        puzzle = tl_mod.generate_puzzle(b"secret payload", iterations=SMALL_T)
        with pytest.raises(Exception):
            tl_mod.solve_puzzle(
                puzzle["N"], puzzle["x"], puzzle["ciphertext"], iterations=SMALL_T + 1
            )

    def test_solving_is_the_only_way_in(self):
        """Without solving (i.e. skipping the squaring loop), the raw x
        does not decrypt the payload."""
        puzzle = tl_mod.generate_puzzle(b"secret payload", iterations=SMALL_T)
        with pytest.raises(Exception):
            tl_mod.solve_puzzle(
                puzzle["N"], puzzle["x"], puzzle["ciphertext"], iterations=0
            )

    def test_empty_payload_roundtrips(self):
        puzzle = tl_mod.generate_puzzle(b"", iterations=SMALL_T)
        assert tl_mod.solve_puzzle(
            puzzle["N"], puzzle["x"], puzzle["ciphertext"], iterations=SMALL_T
        ) == b""

    def test_larger_payload_roundtrips(self):
        payload = os.urandom(512)
        puzzle = tl_mod.generate_puzzle(payload, iterations=SMALL_T)
        assert tl_mod.solve_puzzle(
            puzzle["N"], puzzle["x"], puzzle["ciphertext"], iterations=SMALL_T
        ) == payload


class TestGetTimelockIterations:
    """Compares against tl_mod.TIMELOCK_ITERATIONS (the module attribute
    get_timelock_iterations actually reads at call time), not params'
    constant directly: the suite-wide autouse fixture in conftest.py
    patches the module attribute to a tiny value so other tests don't
    have to solve real ~90-million-iteration puzzles."""

    def test_baseline_matches_constant_before_any_adjustment(self):
        from tests.fixtures import genesis
        chain = [genesis()]
        assert tl_mod.get_timelock_iterations(chain) == tl_mod.TIMELOCK_ITERATIONS

    def test_scales_with_vdf_growth(self, monkeypatch):
        # A larger baseline than the suite-wide tiny test value: at 8
        # iterations, a 2-3% bump rounds away to nothing (int(8*1.03) == 8),
        # which would make this test about integer truncation, not scaling.
        monkeypatch.setattr(tl_mod, "TIMELOCK_ITERATIONS", 1_000_000)
        from params import VDF_ITERATIONS
        monkeypatch.setattr(
            "block.get_vdf_iterations", lambda chain: int(VDF_ITERATIONS * 1.02)
        )
        result = tl_mod.get_timelock_iterations([])
        assert result > tl_mod.TIMELOCK_ITERATIONS

    def test_margin_multiplier_amplifies_growth(self, monkeypatch):
        """The bump applied to puzzle T is >= the raw VDF growth bump,
        reflecting the safety margin for RSA hardware acceleration."""
        monkeypatch.setattr(tl_mod, "TIMELOCK_ITERATIONS", 1_000_000)
        from params import VDF_ITERATIONS
        bumped = int(VDF_ITERATIONS * 1.02)
        monkeypatch.setattr("block.get_vdf_iterations", lambda chain: bumped)
        result = tl_mod.get_timelock_iterations([])
        naive_scale = tl_mod.TIMELOCK_ITERATIONS * (bumped / VDF_ITERATIONS)
        assert result >= naive_scale
