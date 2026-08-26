"""FR-03 meshing tests plus the §7 mesh gate.

Meshes are kept deliberately coarse: the point is to exercise the Gmsh path and
the gate logic headlessly and fast, not to produce a solver-grade mesh.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from crushsim.errors import GateFailure, MeshingError
from crushsim.geometry.parametric import make_can, make_tool
from crushsim.meshing.gates import evaluate_mesh_gate, evaluate_solution_gate
from crushsim.meshing.mesh_data import ShellMesh, read_mesh_npz, write_mesh_npz
from crushsim.meshing.mesher import mesh_parametric_can, mesh_step_surfaces, mesh_tool
from crushsim.units import (
    MESH_MAX_ASPECT_RATIO,
    MESH_MAX_TRIANGLE_FRACTION,
    MESH_MIN_EDGE_LENGTH_MM,
    MESH_MIN_SICN,
)

COARSE = 6.0
"""Target element size [mm] used throughout: keeps the tests to ~0.2 s each."""


@pytest.fixture(scope="module")
def coarse_can_mesh():
    """A coarse but gate-passing can mesh, shared by the tests in this module."""
    return mesh_parametric_can(make_can(33.0, 115.0, 0.1), target_size=COARSE, name="CAN")


# ---------------------------------------------------------------------------
# Gate logic (pure, no Gmsh)
# ---------------------------------------------------------------------------


def test_mesh_gate_passes_on_good_statistics() -> None:
    gate = evaluate_mesh_gate(
        min_sicn=0.5, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    )
    assert gate.passed
    assert gate.failures == []


def test_mesh_gate_uses_the_limits_from_units() -> None:
    gate = evaluate_mesh_gate(
        min_sicn=0.5, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    )
    limits = {m.name: m.limit for m in gate.metrics}
    assert limits["min_sicn"] == MESH_MIN_SICN
    assert limits["max_aspect_ratio"] == MESH_MAX_ASPECT_RATIO
    assert limits["min_edge_length"] == MESH_MIN_EDGE_LENGTH_MM
    assert limits["triangle_fraction"] == MESH_MAX_TRIANGLE_FRACTION


@pytest.mark.parametrize(
    ("kwargs", "failing"),
    [
        ({"min_sicn": 0.1}, "min_sicn"),
        ({"max_aspect_ratio": 9.0}, "max_aspect_ratio"),
        ({"min_edge_length": 0.05}, "min_edge_length"),
        ({"triangle_fraction": 0.4}, "triangle_fraction"),
        ({"non_manifold_edges": 3}, "non_manifold_edges"),
    ],
)
def test_mesh_gate_flags_each_violation(kwargs: dict, failing: str) -> None:
    base = {
        "min_sicn": 0.5,
        "max_aspect_ratio": 2.0,
        "min_edge_length": 0.9,
        "triangle_fraction": 0.02,
    }
    gate = evaluate_mesh_gate(**{**base, **kwargs})
    assert not gate.passed
    assert [m.name for m in gate.failures] == [failing]
    assert gate.recommendations()


def test_solution_gate_thresholds() -> None:
    ok = evaluate_solution_gate(
        energy_error=0.01, hourglass_ratio=0.02, kinetic_ratio=0.01, added_mass_ratio=0.005
    )
    assert ok.passed
    bad = evaluate_solution_gate(
        energy_error=0.09, hourglass_ratio=0.2, kinetic_ratio=0.3, added_mass_ratio=0.5
    )
    assert not bad.passed
    assert len(bad.failures) == 4


def test_gate_describe_is_human_readable() -> None:
    gate = evaluate_mesh_gate(
        min_sicn=0.1, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    )
    text = gate.describe()
    assert "FAIL" in text
    assert "min_sicn" in text


def test_gate_serialises() -> None:
    payload = evaluate_mesh_gate(
        min_sicn=0.5, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    ).to_dict()
    assert payload["passed"] is True
    assert len(payload["metrics"]) == 5


# ---------------------------------------------------------------------------
# ShellMesh container
# ---------------------------------------------------------------------------


def test_shell_mesh_rejects_empty_mesh() -> None:
    with pytest.raises(MeshingError):
        ShellMesh(
            node_ids=np.array([], dtype=np.int64),
            nodes=np.zeros((0, 3)),
            quads=np.zeros((0, 4), dtype=np.int64),
            tris=np.zeros((0, 3), dtype=np.int64),
        )


def test_shell_mesh_counts_edges(can_mesh_fixture: ShellMesh) -> None:
    assert can_mesh_fixture.n_quads == 4
    assert can_mesh_fixture.non_manifold_edge_count() == 0
    assert can_mesh_fixture.free_edge_count() == 8


def test_seat_on_floor_centres_and_grounds_the_mesh() -> None:
    mesh = ShellMesh(
        node_ids=np.array([1, 2, 3, 4], dtype=np.int64),
        nodes=np.array(
            [
                [100.0, -30.0, -7.0],
                [140.0, -30.0, -7.0],
                [140.0, -10.0, 3.0],
                [100.0, -10.0, 3.0],
            ]
        ),
        quads=np.array([[1, 2, 3, 4]], dtype=np.int64),
        tris=np.zeros((0, 3), dtype=np.int64),
        name="CAN",
    )
    offset = mesh.seat_on_floor()
    assert offset == pytest.approx((-120.0, 20.0, 7.0))
    lo, hi = mesh.bounding_box()
    assert (lo[0] + hi[0]) / 2.0 == pytest.approx(0.0)
    assert (lo[1] + hi[1]) / 2.0 == pytest.approx(0.0)
    assert lo[2] == pytest.approx(0.0)
    assert mesh.metadata["seated_offset_mm"] == pytest.approx([-120.0, 20.0, 7.0])


def test_seated_can_proxy_follows_the_lying_orientation() -> None:
    """A can modelled lying down must yield a wide, low reference proxy."""
    from crushsim.config import load_case
    from crushsim.pipeline import _seated_can_proxy

    mesh = ShellMesh(
        node_ids=np.array([1, 2, 3, 4], dtype=np.int64),
        nodes=np.array(
            [
                [-57.5, -33.0, 0.0],
                [57.5, -33.0, 0.0],
                [57.5, 33.0, 66.0],
                [-57.5, 33.0, 66.0],
            ]
        ),
        quads=np.array([[1, 2, 3, 4]], dtype=np.int64),
        tris=np.zeros((0, 3), dtype=np.int64),
        name="CAN",
    )
    case = load_case(Path("configs/cases/lc2_step_example.yaml"))
    proxy = _seated_can_proxy(mesh, case)
    assert proxy.radius == pytest.approx(57.5)  # half the largest in-plane extent
    assert proxy.height == pytest.approx(66.0)  # actual seated height, not nominal 115
    assert proxy.thickness == pytest.approx(case.geometry.thickness)


def test_shell_mesh_renumber_offsets_ids(can_mesh_fixture: ShellMesh) -> None:
    shifted = can_mesh_fixture.renumber(100)
    assert int(shifted.node_ids.min()) == 101
    assert int(shifted.quads.min()) >= 101
    assert shifted.n_quads == can_mesh_fixture.n_quads


def test_shell_mesh_npz_round_trip(can_mesh_fixture: ShellMesh, tmp_path: Path) -> None:
    path = write_mesh_npz(can_mesh_fixture, tmp_path / "mesh.npz")
    back = read_mesh_npz(path)
    assert back.n_nodes == can_mesh_fixture.n_nodes
    assert np.array_equal(back.quads, can_mesh_fixture.quads)


def test_read_mesh_npz_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(MeshingError, match="not found"):
        read_mesh_npz(tmp_path / "missing.npz")


# ---------------------------------------------------------------------------
# Real Gmsh meshing
# ---------------------------------------------------------------------------


def test_can_mesh_is_quad_dominant(coarse_can_mesh) -> None:
    assert coarse_can_mesh.mesh.triangle_fraction <= MESH_MAX_TRIANGLE_FRACTION
    assert coarse_can_mesh.mesh.n_quads > 0


def test_can_mesh_passes_the_gate(coarse_can_mesh) -> None:
    assert coarse_can_mesh.passed, coarse_can_mesh.gate.describe()
    assert coarse_can_mesh.attempts == 1


def test_can_mesh_lies_on_the_mid_surface(coarse_can_mesh) -> None:
    nodes = coarse_can_mesh.mesh.nodes
    radii = np.hypot(nodes[:, 0], nodes[:, 1])
    assert radii.min() == pytest.approx(32.95, rel=2e-3)
    assert radii.max() == pytest.approx(32.95, rel=2e-3)
    assert nodes[:, 2].min() == pytest.approx(0.0, abs=1e-6)
    assert nodes[:, 2].max() == pytest.approx(115.0, abs=1e-6)


def test_can_mesh_is_manifold(coarse_can_mesh) -> None:
    assert coarse_can_mesh.mesh.non_manifold_edge_count() == 0


def test_finer_target_size_produces_more_elements() -> None:
    coarse = mesh_parametric_can(make_can(20.0, 40.0, 0.2), target_size=8.0)
    fine = mesh_parametric_can(make_can(20.0, 40.0, 0.2), target_size=4.0)
    assert fine.mesh.n_elements > coarse.mesh.n_elements


def test_mesh_writes_msh_file(tmp_path: Path) -> None:
    result = mesh_parametric_can(
        make_can(20.0, 40.0, 0.2), target_size=8.0, out_path=tmp_path / "can.msh"
    )
    assert result.msh_path is not None
    assert result.msh_path.is_file()
    assert result.msh_path.stat().st_size > 0


def test_gate_failure_is_raised_and_remeshing_was_attempted() -> None:
    """An impossible edge-length limit must abort after the retry budget."""
    with pytest.raises(GateFailure) as excinfo:
        mesh_parametric_can(
            make_can(2.0, 3.0, 0.05),
            target_size=0.25,
            min_size=0.05,
            max_attempts=3,
        )
    assert "Mesh gate failed" in str(excinfo.value)
    assert "min_edge_length" in str(excinfo.value)


def test_gate_can_be_reported_without_enforcement() -> None:
    result = mesh_parametric_can(
        make_can(2.0, 3.0, 0.05), target_size=0.25, min_size=0.05, max_attempts=1, enforce=False
    )
    assert not result.passed
    assert result.quality.worst_elements


def test_worst_elements_are_reported_for_the_defect_report(coarse_can_mesh) -> None:
    worst = coarse_can_mesh.quality.worst_elements
    assert worst
    assert {"element_tag", "sicn", "x", "y", "z"} <= set(worst[0])
    assert worst[0]["sicn"] == pytest.approx(coarse_can_mesh.quality.min_sicn)


@pytest.mark.parametrize("kind", ["platen", "jig_plane", "v_block", "indenter"])
def test_every_parametric_tool_meshes(kind: str) -> None:
    can = make_can(33.0, 115.0, 0.1)
    direction = (0.0, 0.0, -1.0) if kind == "platen" else (1.0, 0.0, 0.0)
    tool = make_tool(can, kind, direction)
    result = mesh_tool(tool, can_height=can.height, target_size=10.0)
    assert result.mesh.n_elements > 0


def test_unknown_tool_kind_raises() -> None:
    can = make_can(33.0, 115.0, 0.1)
    tool = make_tool(can, "step", (1.0, 0.0, 0.0))
    with pytest.raises(MeshingError, match="step_path"):
        mesh_tool(tool, can_height=can.height)


def test_step_meshing_produces_shells(example_step: Path) -> None:
    result = mesh_step_surfaces(example_step, target_size=5.0, enforce=False, name="STEP")
    assert result.mesh.n_elements > 0
    assert result.mesh.n_quads > 0


def test_step_meshing_missing_file_raises() -> None:
    with pytest.raises(MeshingError, match="not found"):
        mesh_step_surfaces("/nonexistent/part.stp")


def test_mesh_result_serialises(coarse_can_mesh) -> None:
    payload = coarse_can_mesh.summary()
    assert payload["gate"]["passed"] is True
    assert payload["mesh"]["elements"] == coarse_can_mesh.mesh.n_elements
    assert payload["quality"]["min_sicn"] >= MESH_MIN_SICN


def test_orient_outward_flips_inward_shells():
    import numpy as np

    from crushsim.meshing.mesh_data import ShellMesh

    # A unit square face above the origin, wound so its normal points DOWN
    # (toward the centre below) - orient_outward must flip it upward.
    mesh = ShellMesh(
        name="patch",
        node_ids=np.array([1, 2, 3, 4]),
        nodes=np.array([[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=float),
        quads=np.array([[1, 4, 3, 2]]),
        tris=np.zeros((0, 3), dtype=int),
    )
    flipped = mesh.orient_outward((0.5, 0.5, 0.0))
    assert flipped == 1
    pts = mesh.nodes
    quad = [int(n) - 1 for n in mesh.quads[0]]
    normal = np.cross(pts[quad[1]] - pts[quad[0]], pts[quad[2]] - pts[quad[0]])
    assert normal[2] > 0
    # Already-outward meshes are untouched.
    assert mesh.orient_outward((0.5, 0.5, 0.0)) == 0


def test_resume_mesh_and_quality(tmp_path):
    import json

    import numpy as np
    import pyvista as pv

    from crushsim.meshing.resume import load_resume_mesh, quality_of_mesh

    # Source run: one frame, a deformable quad (part 1) + a rigid tri (part 9).
    points = np.array(
        [[0, 0, 2], [10, 0, 2], [10, 10, 2], [0, 10, 2], [30, 0, 0], [40, 0, 0], [40, 10, 0]],
        dtype=float,
    )
    cells = np.array([4, 0, 1, 2, 3, 3, 4, 5, 6])
    grid = pv.UnstructuredGrid(cells, np.array([9, 5], dtype=np.uint8), points)
    grid.cell_data["PART_ID"] = np.array([1, 9])
    run = tmp_path / "src_run"
    (run / "vtk").mkdir(parents=True)
    grid.save(run / "vtk" / "f_A001.vtk")
    (run / "pipeline_summary.json").write_text(
        json.dumps({"deck": {"parts": [
            {"name": "CAN", "part_id": 1, "role": "deformable", "thickness_mm": 0.3},
            {"name": "REF_TOOL", "part_id": 9, "role": "tool", "thickness_mm": 0.5},
        ]}}),
        encoding="utf-8",
    )

    mesh, thickness = load_resume_mesh(run, frame=-1)
    assert thickness == 0.3
    # Only the deformable part survives, nodes compacted to 1..4.
    assert mesh.n_quads == 1 and mesh.n_tris == 0 and mesh.n_nodes == 4
    assert mesh.nodes[:, 2].max() == 2.0

    stats = quality_of_mesh(mesh)
    assert stats["min_sicn"] == pytest.approx(1.0)  # a perfect square
    assert stats["min_edge_length"] == pytest.approx(10.0)
    assert stats["max_aspect_ratio"] == pytest.approx(1.0)
