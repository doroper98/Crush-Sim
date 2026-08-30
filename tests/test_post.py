"""FR-06 / FR-07 post-processing tests on synthetic data.

The curve mathematics is checked against closed-form answers; the converter
wrappers are checked for their missing-tool error paths (no solver is installed
here and writing an in-house binary parser is forbidden, spec §13.4).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from crushsim.errors import PostProcessError
from crushsim.post.convert import convert_animation, convert_time_history, find_result_files
from crushsim.post.curves import (
    balance_energy_error,
    compute_metrics,
    extract_energy_balance,
    extract_force_displacement,
    plot_force_displacement,
    read_th_csv,
)
from crushsim.post.render import CAMERA_PRESETS, ffmpeg_available, render_sequence, rendering_available
from crushsim.units import ENERGY_ERROR_MAX


def _solver_config(tmp_path: Path) -> Path:
    path = tmp_path / "solver.yaml"
    path.write_text(
        yaml.safe_dump({"version_tag": "openradioss-test", "executables": {}}), encoding="utf-8"
    )
    return path


@pytest.fixture
def triangular_curve() -> pd.DataFrame:
    """Load ramps to 1000 N at 10 mm, then unloads to zero at 8 mm.

    Loading work  = 0.5 * 10 * 1000 = 5000 mJ
    Unloading work = -0.5 * 2 * 1000 = -1000 mJ
    Net absorbed  = 4000 mJ, residual set = 8 mm.
    """
    up_d = np.linspace(0.0, 10.0, 101)
    up_f = up_d * 100.0
    down_d = np.linspace(10.0, 8.0, 21)[1:]
    down_f = (down_d - 8.0) * 500.0
    disp = np.concatenate([up_d, down_d])
    force = np.concatenate([up_f, down_f])
    time = np.linspace(0.0, 1.0, disp.size)
    return pd.DataFrame({"time": time, "displacement": disp, "force": force})


# ---------------------------------------------------------------------------
# Curve metrics
# ---------------------------------------------------------------------------


def test_peak_load_and_location(triangular_curve: pd.DataFrame) -> None:
    metrics = compute_metrics(triangular_curve)
    assert metrics.peak_load == pytest.approx(1000.0)
    assert metrics.peak_displacement == pytest.approx(10.0)
    assert metrics.max_displacement == pytest.approx(10.0)


def test_absorbed_energy_matches_the_analytic_area(triangular_curve: pd.DataFrame) -> None:
    metrics = compute_metrics(triangular_curve)
    assert metrics.absorbed_energy == pytest.approx(4000.0, rel=1e-3)


def test_mean_crush_load_is_energy_over_stroke(triangular_curve: pd.DataFrame) -> None:
    metrics = compute_metrics(triangular_curve)
    assert metrics.mean_crush_load == pytest.approx(metrics.absorbed_energy / 10.0, rel=1e-9)


def test_residual_dent_depth_is_the_permanent_set(triangular_curve: pd.DataFrame) -> None:
    metrics = compute_metrics(triangular_curve)
    assert metrics.unloaded is True
    assert metrics.residual_dent_depth == pytest.approx(8.0, abs=0.05)


def test_monotonic_curve_reports_no_unloading() -> None:
    disp = np.linspace(0.0, 10.0, 51)
    curve = pd.DataFrame({"displacement": disp, "force": disp * 50.0})
    metrics = compute_metrics(curve)
    assert metrics.unloaded is False
    assert metrics.residual_dent_depth == pytest.approx(10.0)


def test_metrics_serialise(triangular_curve: pd.DataFrame) -> None:
    payload = compute_metrics(triangular_curve).to_dict()
    assert set(payload) >= {
        "peak_load_N",
        "absorbed_energy_mJ",
        "mean_crush_load_N",
        "residual_dent_depth_mm",
    }


def test_compute_metrics_requires_columns() -> None:
    with pytest.raises(PostProcessError, match="displacement"):
        compute_metrics(pd.DataFrame({"force": [1.0, 2.0]}))


def test_compute_metrics_rejects_empty_curve() -> None:
    with pytest.raises(PostProcessError, match="no points"):
        compute_metrics(pd.DataFrame({"displacement": [], "force": []}))


# ---------------------------------------------------------------------------
# CSV ingestion
# ---------------------------------------------------------------------------


def _write_th_csv(path: Path) -> Path:
    time = np.linspace(0.0, 0.02, 51)
    disp = -np.linspace(0.0, 40.0, 51)  # solver reports -Z motion
    force = -np.concatenate([np.linspace(0.0, 900.0, 26), np.linspace(900.0, 400.0, 25)])
    frame = pd.DataFrame(
        {
            "TIME": time,
            "RBODY_1_DZ": disp,
            "RBODY_1_FZ": force,
            "INTERNAL ENERGY": np.linspace(0.0, 12000.0, 51),
            "KINETIC ENERGY": np.linspace(0.0, 300.0, 51),
            "HOURGLASS ENERGY": np.linspace(0.0, 500.0, 51),
            "CONTACT ENERGY": np.linspace(0.0, 100.0, 51),
            "EXTERNAL WORK": np.linspace(0.0, 12300.0, 51),
        }
    )
    frame.to_csv(path, index=False)
    return path


def test_read_th_csv_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PostProcessError, match="not found"):
        read_th_csv(tmp_path / "missing.csv")


def test_extract_force_displacement_finds_columns(tmp_path: Path) -> None:
    frame = read_th_csv(_write_th_csv(tmp_path / "th.csv"))
    curve = extract_force_displacement(frame)
    assert list(curve.columns) == ["time", "displacement", "force"]
    assert curve["displacement"].min() >= 0.0
    assert curve["force"].max() == pytest.approx(900.0)


def test_extract_force_displacement_reports_unknown_columns() -> None:
    frame = pd.DataFrame({"alpha": [1.0], "beta": [2.0]})
    with pytest.raises(PostProcessError, match="Could not identify"):
        extract_force_displacement(frame)


def test_explicit_column_names_win(tmp_path: Path) -> None:
    frame = read_th_csv(_write_th_csv(tmp_path / "th.csv"))
    curve = extract_force_displacement(
        frame, time_column="TIME", displacement_column="RBODY_1_DZ", force_column="RBODY_1_FZ"
    )
    assert len(curve) == 51


# ---------------------------------------------------------------------------
# Energy balance and the solution gate
# ---------------------------------------------------------------------------


def test_energy_balance_ratios(tmp_path: Path) -> None:
    frame = read_th_csv(_write_th_csv(tmp_path / "th.csv"))
    energy = extract_energy_balance(frame, added_mass_ratio=0.005)
    assert energy.internal == pytest.approx(12000.0)
    assert energy.hourglass_ratio == pytest.approx(500.0 / 12000.0)
    assert energy.kinetic_ratio == pytest.approx(300.0 / 12000.0)


def test_energy_balance_gate_passes_on_good_data(tmp_path: Path) -> None:
    frame = read_th_csv(_write_th_csv(tmp_path / "th.csv"))
    energy = extract_energy_balance(frame, added_mass_ratio=0.005)
    assert energy.gate.passed, energy.gate.describe()
    assert energy.energy_error <= ENERGY_ERROR_MAX


def test_energy_balance_gate_fails_on_bad_data(tmp_path: Path) -> None:
    frame = read_th_csv(_write_th_csv(tmp_path / "th.csv"))
    energy = extract_energy_balance(frame, added_mass_ratio=0.2, energy_error=0.4)
    assert not energy.gate.passed
    names = {m.name for m in energy.gate.failures}
    assert {"energy_error", "added_mass_ratio"} <= names


def test_energy_balance_requires_internal_energy() -> None:
    with pytest.raises(PostProcessError, match="internal-energy"):
        extract_energy_balance(pd.DataFrame({"TIME": [0.0, 1.0]}))


def _frictional_crush_frame() -> pd.DataFrame:
    """A healthy frictional crush: every tracked energy sums to the external work."""
    n = 21
    internal = np.linspace(0.0, 8000.0, n)
    kinetic = np.concatenate([[0.0], np.full(n - 1, 50.0)])
    contact = np.linspace(0.0, 700.0, n)  # friction-dominated, ~8% of the work
    return pd.DataFrame(
        {
            "TIME": np.linspace(0.0, 0.02, n),
            "INTERNAL ENERGY": internal,
            "KINETIC ENERGY": kinetic,
            "CONTACT ENERGY": contact,
            "HOURGLASS ENERGY": np.zeros(n),
            "EXTERNAL WORK": internal + kinetic + contact,
        }
    )


def test_balance_energy_error_credits_friction_dissipation() -> None:
    """Friction energy is part of the balance, not an error (§7)."""
    frame = _frictional_crush_frame()
    err = balance_energy_error(frame)
    assert err == pytest.approx(0.0, abs=1e-9)
    # Sanity: an accounting that drops contact energy (the engine console's
    # ERROR column) would report well over the 5% limit on this run.
    contact_share = 700.0 / float(frame["EXTERNAL WORK"].iloc[-1])
    assert contact_share > ENERGY_ERROR_MAX


def test_balance_energy_error_needs_internal_and_external() -> None:
    assert balance_energy_error(pd.DataFrame({"INTERNAL ENERGY": [1.0, 2.0]})) is None
    assert balance_energy_error(pd.DataFrame({"EXTERNAL WORK": [1.0, 2.0]})) is None


def test_extract_energy_balance_gates_on_the_full_balance() -> None:
    energy = extract_energy_balance(_frictional_crush_frame(), added_mass_ratio=0.0)
    assert energy.energy_error == pytest.approx(0.0, abs=1e-9)
    assert energy.gate.passed, energy.gate.describe()


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------


def test_curve_png_is_written(triangular_curve: pd.DataFrame, tmp_path: Path) -> None:
    path = plot_force_displacement(triangular_curve, tmp_path / "curve.png")
    assert path.is_file()
    assert path.stat().st_size > 1000


def test_curve_png_supports_the_unverified_watermark(
    triangular_curve: pd.DataFrame, tmp_path: Path
) -> None:
    plain = plot_force_displacement(triangular_curve, tmp_path / "a.png")
    stamped = plot_force_displacement(triangular_curve, tmp_path / "b.png", unverified=True)
    assert plain.read_bytes() != stamped.read_bytes()


# ---------------------------------------------------------------------------
# Official converters
# ---------------------------------------------------------------------------


def test_convert_time_history_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(PostProcessError, match="not found"):
        convert_time_history(tmp_path / "runT01", solver_config=_solver_config(tmp_path))


def test_convert_time_history_missing_converter_is_actionable(tmp_path: Path) -> None:
    th = tmp_path / "runT01"
    th.write_bytes(b"\x00\x01")
    with pytest.raises(PostProcessError) as excinfo:
        convert_time_history(th, solver_config=_solver_config(tmp_path))
    message = str(excinfo.value)
    assert "th_to_csv" in message
    assert "executables.th_to_csv" in message
    assert "forbidden" in message


def test_convert_animation_missing_converter_is_actionable(tmp_path: Path) -> None:
    anim = tmp_path / "runA001"
    anim.write_bytes(b"\x00\x01")
    with pytest.raises(PostProcessError) as excinfo:
        convert_animation([anim], outdir=tmp_path / "vtk", solver_config=_solver_config(tmp_path))
    assert "anim_to_vtk" in str(excinfo.value)


def test_convert_animation_rejects_an_empty_list(tmp_path: Path) -> None:
    with pytest.raises(PostProcessError, match="No animation files"):
        convert_animation([], outdir=tmp_path, solver_config=_solver_config(tmp_path))


def test_find_result_files_sorts_and_filters(tmp_path: Path) -> None:
    for name in ("caseT01", "caseA001", "caseA002", "case_0000.rad", "caseA001.vtk"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    found = find_result_files(tmp_path, "case")
    assert [p.name for p in found["time_history"]] == ["caseT01"]
    assert [p.name for p in found["animation"]] == ["caseA001", "caseA002"]


def test_find_result_files_is_empty_when_nothing_ran(tmp_path: Path) -> None:
    found = find_result_files(tmp_path, "case")
    assert found == {"time_history": [], "animation": []}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_three_camera_presets_exist() -> None:
    """Spec §4 FR-07: three fixed camera presets."""
    assert set(CAMERA_PRESETS) == {"iso", "front", "section"}


def test_rendering_probe_never_raises() -> None:
    ok, reason = rendering_available()
    assert isinstance(ok, bool)
    assert reason


def test_ffmpeg_probe_never_raises() -> None:
    ok, reason = ffmpeg_available()
    assert isinstance(ok, bool)
    assert reason


def test_render_rejects_empty_input(tmp_path: Path) -> None:
    with pytest.raises(PostProcessError, match="No VTK files"):
        render_sequence([], tmp_path)


def test_render_rejects_unknown_preset(tmp_path: Path) -> None:
    vtk = tmp_path / "f.vtk"
    vtk.write_text("x", encoding="utf-8")
    with pytest.raises(PostProcessError, match="preset"):
        render_sequence([vtk], tmp_path, presets=("orbit",))


def test_render_sequence_produces_media(tmp_path: Path) -> None:
    """End-to-end offscreen render; skipped when no software OpenGL is present."""
    ok, reason = rendering_available()
    if not ok:
        pytest.skip(f"offscreen rendering unavailable: {reason}")
    pv = pytest.importorskip("pyvista")

    files: list[Path] = []
    for index in range(3):
        mesh = pv.Sphere(radius=10.0 + index)
        mesh.point_data["von Mises"] = np.linspace(0.0, 100.0 + index, mesh.n_points)
        path = tmp_path / f"frame{index:03d}.vtk"
        mesh.save(path)
        files.append(path)

    result = render_sequence(
        files, tmp_path / "anim", presets=("iso",), fps=4, window_size=(160, 120)
    )
    assert result.rendered
    assert result.scalar == "von Mises"
    assert result.scalar_range is not None
    assert result.stills["iso"].is_file()
    assert result.gifs["iso"].is_file()


def test_render_sequence_composites_the_curve_panel(tmp_path: Path) -> None:
    """With a curve, every frame carries the synced force-displacement panel."""
    ok, reason = rendering_available()
    if not ok:
        pytest.skip(f"offscreen rendering unavailable: {reason}")
    pv = pytest.importorskip("pyvista")
    pd = pytest.importorskip("pandas")
    import imageio.v2 as imageio

    from crushsim.post.render import CURVE_PANEL_WIDTH_PX

    files: list[Path] = []
    for index in range(3):
        mesh = pv.Sphere(radius=10.0 + index)
        mesh.point_data["von Mises"] = np.linspace(0.0, 100.0, mesh.n_points)
        path = tmp_path / f"c{index}.vtk"
        mesh.save(path)
        files.append(path)

    curve = pd.DataFrame(
        {
            "time": np.linspace(0.0, 0.04, 50),
            "displacement": np.linspace(0.0, 20.0, 50),
            "force": np.linspace(0.0, 5.0, 50) ** 2,
        }
    )
    window = (160, 120)
    result = render_sequence(
        files, tmp_path / "anim3", presets=("iso",), window_size=window, curve=curve
    )
    assert result.rendered
    assert result.skipped_reason is None or "panel" not in result.skipped_reason
    still = imageio.imread(result.stills["iso"])
    assert still.shape[1] == window[0] + CURVE_PANEL_WIDTH_PX
    assert still.shape[0] >= window[1]


def test_render_colormap_range_spans_the_whole_sequence(tmp_path: Path) -> None:
    """Frame-fixed colour scale: the range covers every frame (spec §4 FR-07)."""
    ok, reason = rendering_available()
    if not ok:
        pytest.skip(f"offscreen rendering unavailable: {reason}")
    pv = pytest.importorskip("pyvista")

    files: list[Path] = []
    for index, top in enumerate((10.0, 250.0)):
        mesh = pv.Sphere(radius=5.0)
        mesh.point_data["von Mises"] = np.linspace(0.0, top, mesh.n_points)
        path = tmp_path / f"g{index}.vtk"
        mesh.save(path)
        files.append(path)

    result = render_sequence(
        files, tmp_path / "anim2", presets=("front",), window_size=(120, 90)
    )
    assert result.scalar_range is not None
    assert result.scalar_range[1] == pytest.approx(250.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Deformed-shape STEP export (issue #26)
# ---------------------------------------------------------------------------


def test_export_deformed_step_roundtrip(tmp_path):
    pytest.importorskip("OCP")
    import json

    import numpy as np
    import pyvista as pv

    from crushsim.post.export_step import export_deformed_step

    # A 2-element "deformed" mesh: one quad (part 1, the can) + one tri (part 2).
    points = np.array(
        [[0, 0, 0], [10, 0, 0], [10, 10, 1.5], [0, 10, 1.5], [20, 0, 0], [20, 10, 0]],
        dtype=float,
    )
    cells = np.array([4, 0, 1, 2, 3, 3, 1, 4, 5])
    celltypes = np.array([9, 5], dtype=np.uint8)  # quad, tri
    grid = pv.UnstructuredGrid(cells, celltypes, points)
    grid.cell_data["PART_ID"] = np.array([1, 2])
    run = tmp_path / "run"
    (run / "vtk").mkdir(parents=True)
    grid.save(run / "vtk" / "frame_A001.vtk")
    (run / "pipeline_summary.json").write_text(
        json.dumps(
            {
                "deck": {
                    "parts": [
                        {"name": "CAN", "part_id": 1, "role": "deformable"},
                        {"name": "FLOOR", "part_id": 2, "role": "floor"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    out = export_deformed_step(run, frame=-1, part="can")
    assert out.is_file() and out.suffix == ".stp"
    text = out.read_text(errors="replace")
    assert "ISO-10303-21" in text  # a real STEP file

    # The can quad became two faces; the floor tri must NOT be exported.
    import gmsh

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    try:
        gmsh.open(str(out))
        surfaces = gmsh.model.getEntities(2)
        assert len(surfaces) == 2
        bb = gmsh.model.getBoundingBox(-1, -1)
        assert bb[3] < 15.0  # floor tri at x=20 excluded
    finally:
        gmsh.finalize()


def test_vent_metrics_parses_sh3n_thickness() -> None:
    """/SH3N lines are 80 chars (3 node ids), /SHELL 90 - both must yield
    their trailing thickness field (a scored triangle miscounted the score
    set and made t_opening report early)."""
    from crushsim.deck.format import f20, i10
    from crushsim.post.vent_metrics import _element_areas

    deck = "\n".join([
        "/NODE",
        i10(1) + f20(0.0) + f20(0.0) + f20(0.0),
        i10(2) + f20(1.0) + f20(0.0) + f20(0.0),
        i10(3) + f20(1.0) + f20(1.0) + f20(0.0),
        i10(4) + f20(0.0) + f20(1.0) + f20(0.0),
        "/SHELL/2",
        i10(10) + i10(1) + i10(2) + i10(3) + i10(4) + f20(0.0) + f20(0.1),
        "/SH3N/2",
        i10(11) + i10(1) + i10(2) + i10(3) + f20(0.0) + f20(0.03),
        "/END",
    ])
    areas, thickness = _element_areas(deck, 2)
    assert set(areas) == {10, 11}
    assert thickness[10] == 0.1
    assert thickness[11] == 0.03
