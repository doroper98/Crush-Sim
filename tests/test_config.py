"""Configuration loading and validation tests (ADR-05, spec §11)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from crushsim.config import (
    LOAD_CASES,
    MaterialCard,
    find_material_card,
    load_case,
    load_material_card,
)
from crushsim.errors import ConfigError
from crushsim.units import HARDENING_REFERENCE_STRAIN


def _write(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Material cards
# ---------------------------------------------------------------------------


def test_material_card_derives_K_when_absent(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "m.yaml",
        {"name": "M", "E": 69000, "nu": 0.33, "rho": 2.7e-9, "sigma_y": 100, "uts": 200, "n": 0.2},
    )
    card = load_material_card(path)
    assert card.K == pytest.approx(100.0 / HARDENING_REFERENCE_STRAIN**0.2)
    assert card.law2.B == pytest.approx(card.K)


def test_material_card_flow_stress_is_ludwik_hollomon(material_card: MaterialCard) -> None:
    assert material_card.flow_stress(0.0) == pytest.approx(material_card.sigma_y)
    assert material_card.flow_stress(0.3) == pytest.approx(
        material_card.sigma_y + material_card.K * 0.3**material_card.n
    )


def test_unverified_card_is_not_verified(material_card: MaterialCard) -> None:
    assert material_card.is_verified is False


def test_verified_flag_requires_a_verification_state(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "m.yaml",
        {
            "name": "M",
            "E": 69000,
            "nu": 0.33,
            "rho": 2.7e-9,
            "sigma_y": 100,
            "uts": 200,
            "n": 0.2,
            "verified": True,
            "verification": "literature",
        },
    )
    assert load_material_card(path).is_verified is True


def test_unknown_verification_state_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "m.yaml",
        {
            "name": "M",
            "E": 1,
            "nu": 0.3,
            "rho": 1e-9,
            "sigma_y": 1,
            "uts": 2,
            "verification": "vibes",
        },
    )
    with pytest.raises(ConfigError, match="verification"):
        load_material_card(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "M", "nu": 0.3, "rho": 1e-9, "sigma_y": 1, "uts": 2},  # no E
        {"name": "M", "E": 1, "rho": 1e-9, "sigma_y": 1, "uts": 2},  # no nu
        {"name": "M", "E": 1, "nu": 0.3, "sigma_y": 1, "uts": 2},  # no rho
    ],
)
def test_missing_required_material_field_raises(tmp_path: Path, payload: dict) -> None:
    with pytest.raises(ConfigError, match="Missing required key"):
        load_material_card(_write(tmp_path / "m.yaml", payload))


def test_uts_below_yield_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "m.yaml",
        {"name": "M", "E": 1000, "nu": 0.3, "rho": 1e-9, "sigma_y": 300, "uts": 100},
    )
    with pytest.raises(ConfigError, match="uts"):
        load_material_card(path)


def test_invalid_poisson_ratio_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "m.yaml",
        {"name": "M", "E": 1000, "nu": 0.7, "rho": 1e-9, "sigma_y": 100, "uts": 200},
    )
    with pytest.raises(ConfigError, match="nu"):
        load_material_card(path)


def test_verified_cards_take_precedence(tmp_path: Path) -> None:
    verified = tmp_path / "verified"
    harvested = tmp_path / "harvested"
    verified.mkdir()
    harvested.mkdir()
    (verified / "al.yaml").write_text("x", encoding="utf-8")
    (harvested / "al.yaml").write_text("x", encoding="utf-8")
    found = find_material_card("al", [verified, harvested])
    assert found.parent == verified


def test_unknown_material_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        find_material_card("does_not_exist", [tmp_path])


def test_repository_harvested_card_loads(repo_root: Path) -> None:
    card = load_material_card(
        repo_root / "configs" / "materials" / "harvested" / "aluminum_3003.yaml"
    )
    assert card.name.startswith("Aluminum 3003")
    assert card.rho == pytest.approx(2.73e-9)
    assert card.is_verified is False


# ---------------------------------------------------------------------------
# Case files
# ---------------------------------------------------------------------------


def test_shipped_cases_load(repo_root: Path) -> None:
    for name in ("lc1_default", "lc2_default", "lc2_step_example"):
        case = load_case(repo_root / "configs" / "cases" / f"{name}.yaml")
        assert case.name == name
        assert case.load_case in LOAD_CASES


def test_shipped_cases_resolve_their_material(repo_root: Path) -> None:
    case = load_case(repo_root / "configs" / "cases" / "lc1_default.yaml")
    roots = [
        repo_root / "configs" / "materials" / "verified",
        repo_root / "configs" / "materials" / "harvested",
    ]
    assert case.material(roots).key == "aluminum_3003"


def test_step_case_points_at_an_existing_file(repo_root: Path) -> None:
    case = load_case(repo_root / "configs" / "cases" / "lc2_step_example.yaml")
    assert case.geometry.kind == "step"
    assert case.geometry.step_path is not None
    assert case.geometry.step_path.is_file()


def test_direction_is_normalised(make_case) -> None:
    case = make_case("dir", loading={"direction": [0.0, 0.0, -3.0]})
    assert case.loading.direction == pytest.approx((0.0, 0.0, -1.0))
    assert math.isclose(sum(c * c for c in case.loading.direction), 1.0)


def test_unknown_load_case_raises(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.yaml", {"name": "c", "load_case": "LC-9"})
    with pytest.raises(ConfigError, match="load_case"):
        load_case(path)


def test_step_kind_without_path_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "c.yaml",
        {"name": "c", "load_case": "LC-2", "geometry": {"kind": "step"}},
    )
    with pytest.raises(ConfigError, match="step_path"):
        load_case(path)


def test_zero_direction_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "c.yaml",
        {"name": "c", "load_case": "LC-1", "loading": {"direction": [0, 0, 0]}},
    )
    with pytest.raises(ConfigError, match="non-zero"):
        load_case(path)


def test_negative_stroke_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "c.yaml", {"name": "c", "load_case": "LC-1", "loading": {"stroke": -5.0}}
    )
    with pytest.raises(ConfigError, match="stroke"):
        load_case(path)


def test_non_positive_geometry_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "c.yaml", {"name": "c", "load_case": "LC-1", "geometry": {"radius": 0.0}}
    )
    with pytest.raises(ConfigError, match="radius"):
        load_case(path)


def test_missing_case_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_case(tmp_path / "nope.yaml")


def test_non_mapping_case_raises(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_case(path)


def test_end_time_auto_is_none(make_case) -> None:
    case = make_case("auto", solver={"end_time": "auto"})
    assert case.solver.end_time is None


def test_extra_tools_parse_and_validate(tmp_path: Path) -> None:
    payload = {
        "name": "multi",
        "load_case": "LC-3",
        "geometry": {"kind": "parametric_can", "radius": 10.5, "height": 70.0, "thickness": 0.3},
        "material": {"key": "aluminum_3003"},
        "loading": {
            "tool": "platen",
            "stroke": 3.5,
            "stage": 1,
            "extra_tools": [
                {"tool": "cylinder", "direction": [1, 0, 0], "stroke": 2.0,
                 "indenter_radius": 1.5, "height_frac": 0.9, "stage": 0},
            ],
        },
        "output": {"dir": "runs/multi"},
    }
    case = load_case(_write(tmp_path / "multi.yaml", payload))
    (extra,) = case.loading.extra_tools
    assert extra.tool == "cylinder"
    assert extra.stage == 0
    assert extra.height_frac == 0.9
    assert case.loading.stage == 1

    payload["loading"]["extra_tools"][0]["tool"] = "banana"
    with pytest.raises(ConfigError, match="extra_tools"):
        load_case(_write(tmp_path / "bad.yaml", payload))


def test_vent_eps_p_max_must_be_positive(tmp_path: Path) -> None:
    payload = {
        "name": "foil",
        "load_case": "LC-3",
        "geometry": {
            "kind": "box_can", "width": 40.0, "depth": 10.0, "height": 30.0,
            "thickness": 0.4, "closed_top": True,
            "vent": {"length": 8.0, "width": 4.0, "membrane_thickness": 0.1,
                     "score_thickness": 0.03, "pattern": "petal_x", "eps_p_max": 0.0},
        },
        "material": {"key": "aluminum_3003"},
        "loading": {"tool": "none"},
        "pressure": {"peak": 1.0},
        "output": {"dir": "runs/foil"},
    }
    with pytest.raises(ConfigError, match="eps_p_max"):
        load_case(_write(tmp_path / "foil.yaml", payload))
    payload["geometry"]["vent"]["eps_p_max"] = 0.4
    assert load_case(_write(tmp_path / "ok.yaml", payload)).geometry.vent["eps_p_max"] == 0.4
