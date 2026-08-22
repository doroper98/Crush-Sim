"""Unit-system and quality-limit tests (ADR-04, spec §5.1/§7)."""

from __future__ import annotations

import math

import pytest

from crushsim import units


def test_density_conversion_matches_spec_example() -> None:
    """Steel at 7850 kg/m^3 must become 7.85e-9 tonne/mm^3 (spec §5.1)."""
    assert units.kg_per_m3_to_tonne_per_mm3(7850.0) == pytest.approx(7.85e-9, rel=1e-12)


def test_density_conversion_factor_is_1e_minus_12() -> None:
    assert units.KG_PER_M3_TO_TONNE_PER_MM3 == 1.0e-12


def test_density_round_trip() -> None:
    for rho in (2700.0, 7850.0, 8960.0, 1820.0):
        back = units.tonne_per_mm3_to_kg_per_m3(units.kg_per_m3_to_tonne_per_mm3(rho))
        assert back == pytest.approx(rho, rel=1e-12)


def test_g_per_cm3_conversion() -> None:
    assert units.g_per_cm3_to_tonne_per_mm3(7.85) == pytest.approx(7.85e-9, rel=1e-12)


def test_velocity_conversion() -> None:
    assert units.m_per_s_to_mm_per_s(2.0) == pytest.approx(2000.0)
    assert units.mm_per_s_to_m_per_s(2000.0) == pytest.approx(2.0)


def test_length_and_modulus_conversions() -> None:
    assert units.m_to_mm(0.115) == pytest.approx(115.0)
    assert units.mm_to_m(115.0) == pytest.approx(0.115)
    assert units.gpa_to_mpa(69.0) == pytest.approx(69000.0)
    assert units.joule_to_mj(1.0) == pytest.approx(1000.0)


def test_gravity_is_expressed_in_mm_per_s2() -> None:
    assert units.GRAVITY_MM_S2 == pytest.approx(9810.0)
    assert units.GRAVITY_MM_S2 == pytest.approx(units.m_per_s_to_mm_per_s(9.81))


def test_unit_system_tag() -> None:
    assert units.UNIT_SYSTEM == "mm-s-tonne-N-MPa"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MESH_MIN_SICN", 0.3),
        ("MESH_MAX_ASPECT_RATIO", 5.0),
        ("MESH_MIN_EDGE_LENGTH_MM", 0.3),
        ("MESH_MAX_TRIANGLE_FRACTION", 0.15),
        ("ENERGY_ERROR_MAX", 0.05),
        ("HOURGLASS_TO_INTERNAL_MAX", 0.10),
        ("KINETIC_TO_INTERNAL_MAX", 0.05),
        ("ADDED_MASS_MAX", 0.02),
    ],
)
def test_gate_limits_match_spec_section_7(name: str, expected: float) -> None:
    """Every §7 limit lives here with the value the spec states."""
    assert getattr(units, name) == pytest.approx(expected)


def test_deck_defaults_match_spec_section_4() -> None:
    assert units.CONTACT_FRICTION_DEFAULT == pytest.approx(0.15)
    assert units.SHELL_ISHELL_FORMULATION == 24
    assert units.SHELL_INTEGRATION_POINTS == 5
    assert units.MESH_REMESH_MAX_ATTEMPTS == 3
    assert units.HARDENING_REFERENCE_STRAIN == pytest.approx(0.3)


def test_target_size_band() -> None:
    """The default target size sits inside the 0.8-1.5 mm band (spec §4 FR-03)."""
    assert (
        units.MESH_TARGET_SIZE_MIN_MM
        <= units.MESH_TARGET_SIZE_DEFAULT_MM
        <= units.MESH_TARGET_SIZE_MAX_MM
    )


def test_within_helper() -> None:
    assert units.within(0.4, units.MESH_MIN_SICN, mode="min")
    assert not units.within(0.2, units.MESH_MIN_SICN, mode="min")
    assert units.within(4.0, units.MESH_MAX_ASPECT_RATIO, mode="max")
    assert not units.within(6.0, units.MESH_MAX_ASPECT_RATIO, mode="max")


def test_within_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        units.within(1.0, 2.0, mode="between")


def test_all_limits_are_finite_positive() -> None:
    for name in dir(units):
        if name.isupper() and name.endswith(("_MAX", "_MIN", "_MM")):
            value = getattr(units, name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert math.isfinite(value)
                assert value >= 0.0
