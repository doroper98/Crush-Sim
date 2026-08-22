"""§8 validation benchmarks B-1 and B-2.

Two layers live here:

* **Formula tests** (unmarked, always run) - pin the analytic references in
  :mod:`bench.analytic` so a typo in a coefficient cannot slip through.
* **Comparison tests** (``@pytest.mark.bench``) - compare a real solver result
  against those references. They skip when no result file is present, which is
  the normal state on a machine without OpenRadioss.

Run the comparisons with ``pytest -m bench`` once a run exists; CI runs
``-m "not bench"``.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from analytic import (
    ALEXANDER_K1,
    ALEXANDER_K2,
    RING_DEFLECTION_COEFFICIENT,
    BenchmarkVerdict,
    alexander_fold_wavelength,
    alexander_mean_crush_load,
    flow_stress,
    relative_error,
    ring_lateral_deflection,
    ring_lateral_stiffness,
    ring_second_moment,
)
from crushsim.post.curves import compute_metrics, extract_force_displacement, read_th_csv
from crushsim.units import BENCH_B1_TOLERANCE, BENCH_B2_TOLERANCE

# Reference specimen: the beverage-can-like shell used by configs/cases.
CAN_RADIUS_MM = 33.0
CAN_DIAMETER_MM = 66.0
CAN_THICKNESS_MM = 0.1
CAN_HEIGHT_MM = 115.0
AL3003_E_MPA = 69000.0
AL3003_NU = 0.33
AL3003_SIGMA_Y_MPA = 145.0
AL3003_UTS_MPA = 150.0


# ---------------------------------------------------------------------------
# B-1 formula: Alexander mean crush load
# ---------------------------------------------------------------------------


def test_flow_stress_is_the_mean_of_yield_and_uts() -> None:
    assert flow_stress(145.0, 150.0) == pytest.approx(147.5)


def test_flow_stress_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        flow_stress(0.0, 150.0)
    with pytest.raises(ValueError):
        flow_stress(200.0, 150.0)


def test_alexander_matches_its_closed_form() -> None:
    sigma_0 = flow_stress(AL3003_SIGMA_Y_MPA, AL3003_UTS_MPA)
    expected = (
        sigma_0
        * CAN_THICKNESS_MM**2
        * (ALEXANDER_K1 * math.sqrt(CAN_DIAMETER_MM / CAN_THICKNESS_MM) + ALEXANDER_K2)
    )
    assert alexander_mean_crush_load(
        sigma_0, CAN_THICKNESS_MM, CAN_DIAMETER_MM
    ) == pytest.approx(expected)


def test_alexander_is_order_of_magnitude_sane_for_a_beverage_can() -> None:
    """A 0.1 mm Al can wall crushes at O(100 N) mean load, not kN or mN."""
    sigma_0 = flow_stress(AL3003_SIGMA_Y_MPA, AL3003_UTS_MPA)
    load = alexander_mean_crush_load(sigma_0, CAN_THICKNESS_MM, CAN_DIAMETER_MM)
    assert 50.0 < load < 2000.0


def test_alexander_scales_with_thickness_faster_than_linearly() -> None:
    sigma_0 = 150.0
    thin = alexander_mean_crush_load(sigma_0, 0.1, 66.0)
    thick = alexander_mean_crush_load(sigma_0, 0.2, 66.0)
    assert thick / thin > 2.0


def test_alexander_grows_with_diameter() -> None:
    sigma_0 = 150.0
    assert alexander_mean_crush_load(sigma_0, 0.1, 100.0) > alexander_mean_crush_load(
        sigma_0, 0.1, 66.0
    )


def test_alexander_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        alexander_mean_crush_load(0.0, 0.1, 66.0)
    with pytest.raises(ValueError):
        alexander_mean_crush_load(150.0, 0.0, 66.0)


def test_fold_wavelength_is_a_meshable_length() -> None:
    """Several elements must fit inside one fold for the mesh to resolve it."""
    wavelength = alexander_fold_wavelength(CAN_THICKNESS_MM, CAN_DIAMETER_MM)
    assert 1.0 < wavelength < 5.0


# ---------------------------------------------------------------------------
# B-2 formula: ring lateral stiffness
# ---------------------------------------------------------------------------


def test_ring_deflection_coefficient() -> None:
    assert RING_DEFLECTION_COEFFICIENT == pytest.approx(0.1487784, rel=1e-6)


def test_ring_second_moment() -> None:
    assert ring_second_moment(0.1, 115.0) == pytest.approx(115.0 * 0.1**3 / 12.0)


def test_ring_stiffness_matches_its_closed_form() -> None:
    inertia = ring_second_moment(CAN_THICKNESS_MM, CAN_HEIGHT_MM)
    expected = AL3003_E_MPA * inertia / (RING_DEFLECTION_COEFFICIENT * CAN_RADIUS_MM**3)
    assert ring_lateral_stiffness(
        AL3003_E_MPA, CAN_THICKNESS_MM, CAN_RADIUS_MM, CAN_HEIGHT_MM
    ) == pytest.approx(expected)


def test_ring_stiffness_scales_with_thickness_cubed() -> None:
    thin = ring_lateral_stiffness(AL3003_E_MPA, 0.1, 33.0, 115.0)
    thick = ring_lateral_stiffness(AL3003_E_MPA, 0.2, 33.0, 115.0)
    assert thick / thin == pytest.approx(8.0, rel=1e-9)


def test_ring_stiffness_scales_inversely_with_radius_cubed() -> None:
    small = ring_lateral_stiffness(AL3003_E_MPA, 0.1, 33.0, 115.0)
    large = ring_lateral_stiffness(AL3003_E_MPA, 0.1, 66.0, 115.0)
    assert small / large == pytest.approx(8.0, rel=1e-9)


def test_plane_strain_stiffens_the_ring() -> None:
    plane_stress = ring_lateral_stiffness(AL3003_E_MPA, 0.1, 33.0, 115.0)
    plane_strain = ring_lateral_stiffness(
        AL3003_E_MPA, 0.1, 33.0, 115.0, plane_strain=True, nu=AL3003_NU
    )
    assert plane_strain > plane_stress
    assert plane_strain / plane_stress == pytest.approx(1.0 / (1.0 - AL3003_NU**2))


def test_ring_deflection_is_the_inverse_of_the_stiffness() -> None:
    k = ring_lateral_stiffness(AL3003_E_MPA, 0.1, 33.0, 115.0)
    assert ring_lateral_deflection(10.0, AL3003_E_MPA, 0.1, 33.0, 115.0) == pytest.approx(10.0 / k)


def test_ring_stiffness_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        ring_lateral_stiffness(0.0, 0.1, 33.0, 115.0)
    with pytest.raises(ValueError):
        ring_lateral_stiffness(AL3003_E_MPA, 0.1, 0.0, 115.0)


# ---------------------------------------------------------------------------
# Verdict helper
# ---------------------------------------------------------------------------


def test_relative_error() -> None:
    assert relative_error(110.0, 100.0) == pytest.approx(0.1)
    with pytest.raises(ValueError):
        relative_error(1.0, 0.0)


def test_verdict_applies_the_tolerance() -> None:
    inside = BenchmarkVerdict("B-1", measured=110.0, reference=100.0, tolerance=0.25)
    outside = BenchmarkVerdict("B-1", measured=200.0, reference=100.0, tolerance=0.25)
    assert inside.passed
    assert not outside.passed
    assert "PASS" in inside.describe()
    assert "FAIL" in outside.describe()


def test_tolerances_come_from_units() -> None:
    """Spec §8: B-1 is +/-25%, B-2 is +/-10%."""
    assert BENCH_B1_TOLERANCE == pytest.approx(0.25)
    assert BENCH_B2_TOLERANCE == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Solver comparisons (skipped without a result file)
# ---------------------------------------------------------------------------


@pytest.mark.bench
def test_b1_mean_crush_load_matches_alexander(b1_result: Path) -> None:
    """B-1: simulated mean crush load within +/-25% of the Alexander reference."""
    curve = extract_force_displacement(read_th_csv(b1_result))
    metrics = compute_metrics(curve)
    reference = alexander_mean_crush_load(
        flow_stress(AL3003_SIGMA_Y_MPA, AL3003_UTS_MPA), CAN_THICKNESS_MM, CAN_DIAMETER_MM
    )
    verdict = BenchmarkVerdict(
        benchmark="B-1",
        measured=metrics.mean_crush_load,
        reference=reference,
        tolerance=BENCH_B1_TOLERANCE,
        unit=" N",
    )
    assert verdict.passed, verdict.describe()


@pytest.mark.bench
def test_b2_lateral_stiffness_matches_curved_beam_theory(b2_result: Path) -> None:
    """B-2: simulated elastic lateral stiffness within +/-10% of ring theory."""
    curve = extract_force_displacement(read_th_csv(b2_result))
    # Take the initial, still-elastic part of the curve: the first 2% of stroke.
    elastic = curve[curve["displacement"] <= 0.02 * curve["displacement"].max()]
    assert len(elastic) >= 3, "Not enough points in the elastic range to fit a stiffness"
    slope = float(
        (elastic["force"].iloc[-1] - elastic["force"].iloc[0])
        / (elastic["displacement"].iloc[-1] - elastic["displacement"].iloc[0])
    )
    reference = ring_lateral_stiffness(
        AL3003_E_MPA,
        CAN_THICKNESS_MM,
        CAN_RADIUS_MM,
        CAN_HEIGHT_MM,
        plane_strain=True,
        nu=AL3003_NU,
    )
    verdict = BenchmarkVerdict(
        benchmark="B-2",
        measured=slope,
        reference=reference,
        tolerance=BENCH_B2_TOLERANCE,
        unit=" N/mm",
    )
    assert verdict.passed, verdict.describe()
