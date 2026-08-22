"""FR-02 geometry tests: parametric can, tool placement, STEP probing."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from crushsim.errors import GeometryError, OptionalDependencyError
from crushsim.geometry.parametric import make_can, make_tool
from crushsim.geometry.step_inspect import inspect_step, ocp_available, read_step_header


def test_can_dimensions() -> None:
    can = make_can(33.0, 115.0, 0.1)
    assert can.diameter == pytest.approx(66.0)
    assert can.mid_surface_radius == pytest.approx(32.95)
    assert can.slenderness == pytest.approx(660.0)


def test_can_bottom_sits_at_z_zero() -> None:
    """Spec §5.2: can axis is +Z, bottom face at Z = 0."""
    lo, hi = make_can(33.0, 115.0, 0.1).bounding_box()
    assert lo[2] == pytest.approx(0.0)
    assert hi[2] == pytest.approx(115.0)


def test_wall_area_matches_analytic_cylinder() -> None:
    can = make_can(33.0, 115.0, 0.1)
    assert can.wall_area == pytest.approx(2 * math.pi * 32.95 * 115.0)


def test_closed_ends_add_discs() -> None:
    open_can = make_can(33.0, 115.0, 0.1)
    closed = make_can(33.0, 115.0, 0.1, closed_bottom=True, closed_top=True)
    disc = math.pi * 32.95**2
    assert closed.surface_area == pytest.approx(open_can.surface_area + 2 * disc)


def test_mass_uses_working_units() -> None:
    """Mass in tonne from a tonne/mm^3 density and a mm^3 volume."""
    can = make_can(33.0, 115.0, 0.1)
    mass = can.mass(2.73e-9)
    assert mass == pytest.approx(can.volume_of_material() * 2.73e-9)
    assert 1e-7 < mass < 1e-5


@pytest.mark.parametrize(
    ("radius", "height", "thickness"),
    [(0.0, 115.0, 0.1), (33.0, -1.0, 0.1), (33.0, 115.0, 0.0), (0.1, 115.0, 0.5)],
)
def test_invalid_geometry_raises(radius: float, height: float, thickness: float) -> None:
    with pytest.raises(GeometryError):
        make_can(radius, height, thickness)


def test_axial_tool_is_placed_above_the_can() -> None:
    can = make_can(33.0, 115.0, 0.1)
    tool = make_tool(can, "platen", (0.0, 0.0, -1.0), gap=0.5)
    assert tool.origin[2] == pytest.approx(115.5)
    assert tool.unit_direction == pytest.approx((0.0, 0.0, -1.0))


def test_radial_tool_is_placed_beside_the_can() -> None:
    can = make_can(33.0, 115.0, 0.1)
    tool = make_tool(can, "jig_plane", (1.0, 0.0, 0.0), gap=0.5)
    assert tool.origin[0] == pytest.approx(-33.5)
    assert tool.origin[2] == pytest.approx(57.5)


def test_indenter_offset_includes_its_radius() -> None:
    can = make_can(33.0, 115.0, 0.1)
    tool = make_tool(can, "indenter", (1.0, 0.0, 0.0), gap=0.5, indenter_radius=10.0)
    assert tool.origin[0] == pytest.approx(-(33.0 + 10.0 + 0.5))
    assert tool.radius == pytest.approx(10.0)


def test_tool_direction_is_normalised() -> None:
    can = make_can(33.0, 115.0, 0.1)
    tool = make_tool(can, "platen", (0.0, 0.0, -4.0))
    assert tool.unit_direction[2] == pytest.approx(-1.0)


def test_tool_rejects_degenerate_direction() -> None:
    can = make_can(33.0, 115.0, 0.1)
    with pytest.raises(GeometryError):
        make_tool(can, "platen", (0.0, 0.0, 0.0))


def test_tool_rejects_negative_gap() -> None:
    can = make_can(33.0, 115.0, 0.1)
    with pytest.raises(GeometryError, match="gap"):
        make_tool(can, "platen", (0.0, 0.0, -1.0), gap=-1.0)


def test_default_tool_size_scales_with_the_can() -> None:
    can = make_can(33.0, 115.0, 0.1)
    assert make_tool(can, "platen", (0.0, 0.0, -1.0)).size == pytest.approx(1.5 * 66.0)


# ---------------------------------------------------------------------------
# STEP
# ---------------------------------------------------------------------------


def test_step_header_reads_without_any_cad_dependency(example_step: Path) -> None:
    header = read_step_header(example_step)
    assert header.schema
    assert header.declared_unit in ("mm", "unknown")


def test_step_header_missing_file_raises() -> None:
    with pytest.raises(GeometryError, match="not found"):
        read_step_header("/nonexistent/part.stp")


def test_step_header_rejects_non_step(tmp_path: Path) -> None:
    bogus = tmp_path / "not_a_step.stp"
    bogus.write_text("hello world", encoding="utf-8")
    with pytest.raises(GeometryError, match="ISO-10303-21"):
        read_step_header(bogus)


def test_inspect_step_reports_missing_extra_clearly(example_step: Path) -> None:
    """Without the cad extra, the error must name the install command."""
    if ocp_available():
        summary = inspect_step(example_step)
        assert summary.is_valid
        return
    with pytest.raises(OptionalDependencyError) as excinfo:
        inspect_step(example_step)
    assert "pip install crushsim[cad]" in str(excinfo.value)


def test_optional_dependency_error_is_an_import_error() -> None:
    error = OptionalDependencyError("cadquery-ocp", "cad")
    assert isinstance(error, ImportError)
