"""FR-10 harvester tests: a small fixture plus the real legacy library."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crushsim.config import load_material_card
from crushsim.errors import HarvestError
from crushsim.harvest.legacy_ts import (
    SOURCE_TAG,
    harvest_file,
    parse_material_source,
    write_cards,
)
from crushsim.units import HARDENING_REFERENCE_STRAIN, kg_per_m3_to_tonne_per_mm3

FIXTURE = """
/** Base implementation with Ludwik-Hollomon hardening */
function createMaterial(props: Omit<MaterialModel, 'flowStress' | 'hardeningK'>): MaterialModel {
  const K = (props.uts - props.yieldStress) / Math.pow(0.3, props.hardeningExponent)
  return { ...props }
}

export const MATERIALS: Record<string, MaterialModel> = {
  aluminum_3003: createMaterial({
    name: 'Aluminum 3003-H14',
    youngsModulus: 69000,
    poissonRatio: 0.33,
    yieldStress: 145,
    uts: 150,
    density: 2730,
    hardeningExponent: 0.25,
    wallThickness: 0.1,
  }),
  steel_mild: createMaterial({
    name: "Mild Steel (AISI 1018)",
    youngsModulus: 200000,
    poissonRatio: 0.3,
    yieldStress: 370,
    uts: 440,
    density: 7850,
    hardeningExponent: 0.15,
    wallThickness: 0.5,
  }),
}
"""


def test_parse_fixture_extracts_both_materials() -> None:
    materials = parse_material_source(FIXTURE, source="fixture")
    assert [m.key for m in materials] == ["aluminum_3003", "steel_mild"]


def test_parse_fixture_converts_density_to_tonne_per_mm3() -> None:
    al = parse_material_source(FIXTURE)[0]
    assert al.rho == pytest.approx(kg_per_m3_to_tonne_per_mm3(2730.0))
    assert al.rho_kg_m3 == pytest.approx(2730.0)


def test_hardening_coefficient_matches_legacy_definition() -> None:
    al = parse_material_source(FIXTURE)[0]
    expected = (150.0 - 145.0) / (HARDENING_REFERENCE_STRAIN**0.25)
    assert al.K == pytest.approx(expected)


def test_law2_mapping_is_lossless() -> None:
    """(sigma_y, K, n) maps onto /MAT/LAW2 (A, B, n) without loss (spec §1.1)."""
    for material in parse_material_source(FIXTURE):
        assert material.law2_A == material.sigma_y
        assert material.law2_B == material.K
        assert material.law2_n == material.n


def test_names_with_double_quotes_are_parsed() -> None:
    steel = parse_material_source(FIXTURE)[1]
    assert steel.name == "Mild Steel (AISI 1018)"


def test_empty_source_raises_loudly() -> None:
    """A silent empty result is forbidden (spec §13.5)."""
    with pytest.raises(HarvestError, match="no createMaterial"):
        parse_material_source("export const MATERIALS = {}", source="empty")


def test_missing_field_raises() -> None:
    broken = "x: createMaterial({ name: 'X', youngsModulus: 1, poissonRatio: 0.3 })"
    with pytest.raises(HarvestError, match="missing field"):
        parse_material_source(broken, source="broken")


def test_missing_file_raises() -> None:
    with pytest.raises(HarvestError, match="not found"):
        harvest_file("/nonexistent/MaterialModel.ts", "/tmp/out")


def test_write_cards_produces_loadable_yaml(tmp_path: Path) -> None:
    materials = parse_material_source(FIXTURE)
    written = write_cards(materials, tmp_path)
    assert len(written) == 2
    card = load_material_card(tmp_path / "aluminum_3003.yaml")
    assert card.key == "aluminum_3003"
    assert card.E == pytest.approx(69000.0)
    assert card.law2.A == pytest.approx(145.0)


def test_written_cards_are_all_unverified(tmp_path: Path) -> None:
    write_cards(parse_material_source(FIXTURE), tmp_path)
    for path in tmp_path.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["verified"] is False
        assert data["verification"] == "unverified"
        assert data["source"] == SOURCE_TAG


def test_write_cards_refuses_overwrite(tmp_path: Path) -> None:
    materials = parse_material_source(FIXTURE)
    write_cards(materials, tmp_path)
    with pytest.raises(HarvestError, match="Refusing to overwrite"):
        write_cards(materials, tmp_path, overwrite=False)


# ---------------------------------------------------------------------------
# Against the real legacy library
# ---------------------------------------------------------------------------


def test_real_legacy_file_yields_the_expected_scale(legacy_material_file: Path) -> None:
    """The legacy DB holds roughly 240 materials (spec §0.1)."""
    materials = parse_material_source(
        legacy_material_file.read_text(encoding="utf-8"), source=legacy_material_file
    )
    assert 200 <= len(materials) <= 300
    assert len({m.key for m in materials}) == len(materials)


def test_real_legacy_values_are_physically_sane(legacy_material_file: Path) -> None:
    materials = parse_material_source(legacy_material_file.read_text(encoding="utf-8"))
    for material in materials:
        # Polymers (UHMWPE, PTFE, ...) sit at the low end of the modulus range.
        assert 100.0 < material.E < 1_500_000.0, material.key
        assert 0.0 <= material.nu < 0.5, material.key
        assert 0.0 < material.rho < 1.0e-7, material.key
        assert material.uts >= material.sigma_y, material.key
        assert 0.0 <= material.n <= 1.0, material.key
        assert material.t_default > 0.0, material.key


def test_real_harvest_writes_cards(legacy_material_file: Path, tmp_path: Path) -> None:
    report = harvest_file(legacy_material_file, tmp_path)
    assert report.count == len(report.written)
    assert report.count > 200
    assert (tmp_path / "aluminum_3003.yaml").is_file()


def test_committed_harvested_cards_are_present_and_unverified(repo_root: Path) -> None:
    """The committed harvest output must exist and stay tagged unverified."""
    outdir = repo_root / "configs" / "materials" / "harvested"
    cards = sorted(outdir.glob("*.yaml"))
    assert len(cards) > 200, f"only {len(cards)} harvested cards found in {outdir}"
    for path in cards:
        card = load_material_card(path)
        assert card.verified is False, path
        assert card.source == SOURCE_TAG, path
        assert card.is_verified is False, path
