"""Fixtures for the §8 validation benchmarks.

The benchmark comparisons need a solver result, which exists only after a real
OpenRadioss run. They are marked ``bench`` and skip themselves when no result
file is present, so the suite stays green on a machine without a solver.

Point the fixtures at a run with::

    CRUSHSIM_BENCH_B1=runs/bench_b1/force_displacement.csv pytest -m bench
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_RESULT_PATHS: dict[str, Path] = {
    "B1": REPO_ROOT / "runs" / "bench_b1" / "force_displacement.csv",
    "B2": REPO_ROOT / "runs" / "bench_b2" / "force_displacement.csv",
    "B3_COARSE": REPO_ROOT / "runs" / "bench_b3_coarse" / "force_displacement.csv",
    "B3_MEDIUM": REPO_ROOT / "runs" / "bench_b3_medium" / "force_displacement.csv",
    "B3_FINE": REPO_ROOT / "runs" / "bench_b3_fine" / "force_displacement.csv",
}
"""Conventional locations of each benchmark's converted force-displacement CSV."""


def _resolve(benchmark: str) -> Path | None:
    """Locate a benchmark result, honouring ``CRUSHSIM_BENCH_<ID>``."""
    override = os.environ.get(f"CRUSHSIM_BENCH_{benchmark}")
    candidate = Path(override) if override else DEFAULT_RESULT_PATHS[benchmark]
    return candidate if candidate.is_file() else None


def _require(benchmark: str) -> Path:
    path = _resolve(benchmark)
    if path is None:
        pytest.skip(
            f"No {benchmark} solver result available. Run the case, then set "
            f"CRUSHSIM_BENCH_{benchmark} to the converted force_displacement.csv "
            f"(default: {DEFAULT_RESULT_PATHS[benchmark]})."
        )
    return path


@pytest.fixture
def b1_result() -> Path:
    """Converted force-displacement CSV of the B-1 axial crush run."""
    return _require("B1")


@pytest.fixture
def b2_result() -> Path:
    """Converted force-displacement CSV of the B-2 ring compression run."""
    return _require("B2")


@pytest.fixture
def b3_results() -> dict[str, Path]:
    """The B-3 mesh-sweep curves keyed ``coarse`` / ``medium`` / ``fine``.

    The convergence verdict needs the whole sweep, so the test skips unless
    all three runs exist.
    """
    return {
        label: _require(f"B3_{label.upper()}")
        for label in ("coarse", "medium", "fine")
    }
