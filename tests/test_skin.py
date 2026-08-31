"""EXP-004 - shell idealisation of imported STEP solids.

The defect these tests pin down: a thick-walled STEP solid's boundary is
two-sided, so meshing it whole produces two shells where the part has one.
Measured on the Honda can before the fix, the shell model carried 83 % more
mass than the solid it stands for, and no benchmark caught it because B-1,
B-2 and B-3 all run on parametric cans that are mid-surfaces by construction.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from crushsim.errors import GeometryError
from crushsim.geometry.skin import (
    HOLLOW_AREA_SHARE_MAX,
    extract_shell_skins,
    offset_to_mid_surface,
    shell_mass_error,
)
from crushsim.meshing.mesh_data import ShellMesh

HONDA = Path("examples/step/Honda_Can.stp")
ASSEMBLY = Path("can/test.stp")

MASS_ERROR_LIMIT = 0.10
"""EXP-004 criterion 1: a correct mid-surface reproduces the solid's volume."""


class TestMassError:
    def test_two_sided_shell_is_caught(self) -> None:
        """The defect must be visible in the metric, not just in the mesh.

        Honda numbers: 35 925 mm2 meshed against a 7 447 mm3 solid at the
        0.38 mm wall the geometry actually has.
        """
        err = shell_mass_error(
            meshed_area_mm2=35925.0, thickness_mm=0.38, solid_volume_mm3=7447.2
        )
        assert err > MASS_ERROR_LIMIT
        assert err == pytest.approx(0.833, abs=0.01)

    def test_single_skin_passes(self) -> None:
        err = shell_mass_error(
            meshed_area_mm2=19323.3, thickness_mm=0.38, solid_volume_mm3=7447.2
        )
        assert abs(err) <= MASS_ERROR_LIMIT

    def test_sign_says_which_way(self) -> None:
        """Positive means the shell is heavier than the part."""
        assert shell_mass_error(meshed_area_mm2=200.0, thickness_mm=1.0, solid_volume_mm3=100.0) > 0
        assert shell_mass_error(meshed_area_mm2=50.0, thickness_mm=1.0, solid_volume_mm3=100.0) < 0

    def test_zero_volume_raises(self) -> None:
        with pytest.raises(GeometryError, match="must be > 0"):
            shell_mass_error(meshed_area_mm2=10.0, thickness_mm=1.0, solid_volume_mm3=0.0)


class TestOffset:
    def _plate(self, size: float = 10.0) -> ShellMesh:
        """One flat quad in the XY plane, centred on the origin."""
        return ShellMesh(
            node_ids=np.array([1, 2, 3, 4], dtype=np.int64),
            nodes=np.array(
                [[-size, -size, 0.0], [size, -size, 0.0], [size, size, 0.0], [-size, size, 0.0]]
            ),
            quads=np.array([[1, 2, 3, 4]], dtype=np.int64),
            tris=np.zeros((0, 3), dtype=np.int64),
            name="plate",
        )

    def test_offset_moves_by_half_thickness(self) -> None:
        mesh = self._plate()
        offset_to_mid_surface(mesh, 0.8)
        assert np.allclose(np.abs(mesh.nodes[:, 2]), 0.4)
        assert mesh.metadata["mid_surface_offset_mm"] == pytest.approx(0.4)

    def test_offset_records_both_areas(self) -> None:
        mesh = self._plate()
        offset_to_mid_surface(mesh, 0.8)
        # A flat plate translating along its own normal keeps its area.
        assert mesh.metadata["area_after_offset_mm2"] == pytest.approx(
            mesh.metadata["area_before_offset_mm2"], rel=1e-9
        )

    def test_offset_recomputes_area_after_moving(self) -> None:
        """Guards a bug that made the offset look like a no-op.

        The area check closed over the pre-move coordinates, so it reported the
        old area and the ratio came back exactly 1.0000 however far the nodes
        had travelled - the metric could never have shown the offset failing.
        A closed box shrinks when offset inwards, so the ratio must drop.
        """
        s = 10.0
        nodes = np.array(
            [
                [-s, -s, -s], [s, -s, -s], [s, s, -s], [-s, s, -s],
                [-s, -s, s], [s, -s, s], [s, s, s], [-s, s, s],
            ],
            dtype=float,
        )
        quads = np.array(
            [
                [1, 2, 3, 4], [5, 6, 7, 8], [1, 2, 6, 5],
                [2, 3, 7, 6], [3, 4, 8, 7], [4, 1, 5, 8],
            ],
            dtype=np.int64,
        )
        mesh = ShellMesh(
            node_ids=np.arange(1, 9, dtype=np.int64),
            nodes=nodes,
            quads=quads,
            tris=np.zeros((0, 3), dtype=np.int64),
            name="box",
        )
        ratio = offset_to_mid_surface(mesh, 2.0)
        assert ratio < 0.999, "an inward offset on a closed box must shrink its area"
        assert np.abs(mesh.nodes).max() < s

    def test_zero_thickness_raises(self) -> None:
        with pytest.raises(GeometryError, match="must be > 0"):
            offset_to_mid_surface(self._plate(), 0.0)


@pytest.mark.skipif(not HONDA.is_file(), reason="Honda STEP not present")
class TestHondaCan:
    def test_hollow_can_is_split_in_half(self, tmp_path: Path) -> None:
        skins = extract_shell_skins(HONDA, tmp_path / "skin.brep")
        assert len(skins) == 1
        skin = skins[0]
        assert skin.kind == "hollow"
        # A wall around a cavity splits its boundary roughly evenly; a value
        # near 1.0 would mean the cavity lining was never found.
        assert 0.4 < skin.outer_share < HOLLOW_AREA_SHARE_MAX

    def test_gauged_thickness_matches_the_wall(self, tmp_path: Path) -> None:
        """0.38 mm, cross-checked against the node spacing of the raw mesh.

        The area-inferred value (0.412) is 8 % high because the cavity lining
        carries fewer faces than a pure offset would, so the gauge has to come
        from a ray across the wall rather than from an area ratio.
        """
        skin = extract_shell_skins(HONDA, tmp_path / "skin.brep")[0]
        assert skin.wall_thickness_mm == pytest.approx(0.38, abs=0.02)

    def test_meshed_skin_conserves_mass(self, tmp_path: Path) -> None:
        """EXP-004 criterion 1, end to end."""
        from crushsim.meshing.mesher import mesh_step_surfaces

        brep = tmp_path / "skin.brep"
        skin = extract_shell_skins(HONDA, brep)[0]
        result = mesh_step_surfaces(
            brep, target_size=1.5, out_path=tmp_path / "skin.msh", enforce=False, name="skin"
        )
        offset_to_mid_surface(result.mesh, skin.wall_thickness_mm)
        area = result.mesh.metadata["area_before_offset_mm2"]
        err = shell_mass_error(
            meshed_area_mm2=area,
            thickness_mm=skin.wall_thickness_mm,
            solid_volume_mm3=skin.volume_mm3,
        )
        assert abs(err) <= MASS_ERROR_LIMIT, f"mass error {err:+.1%}"

    def test_meshed_skin_is_one_layer(self, tmp_path: Path) -> None:
        """EXP-004 criterion 2: the wall must not appear twice.

        Before the fix this slice read four distinct depths (-6.28, -5.90,
        +5.90, +6.28) - the outer wall and its lining, 0.38 mm apart.
        """
        from crushsim.meshing.mesher import mesh_step_surfaces

        brep = tmp_path / "skin.brep"
        extract_shell_skins(HONDA, brep)
        mesh = mesh_step_surfaces(
            brep, target_size=1.5, out_path=tmp_path / "skin.msh", enforce=False, name="skin"
        ).mesh
        lo, hi = mesh.bounding_box()
        pts = mesh.nodes
        mid = pts[np.abs(pts[:, 2] - (lo[2] + hi[2]) / 2) < 0.8]
        centred = mid[np.abs(mid[:, 0] - (lo[0] + hi[0]) / 2) < 3.0]
        depths = np.unique(np.round(centred[:, 1], 2))
        assert len(depths) == 2, f"expected one wall per side, got depths {depths}"


@pytest.mark.skipif(not ASSEMBLY.is_file(), reason="validation assembly not present")
class TestAssembly:
    def test_each_solid_is_classified(self, tmp_path: Path) -> None:
        """Can, cap and vent - a hollow body and two plates."""
        skins = extract_shell_skins(ASSEMBLY, tmp_path / "skin.brep")
        assert len(skins) == 3
        kinds = [s.kind for s in skins]
        assert kinds.count("hollow") == 1
        assert kinds.count("plate") == 2

    def test_plates_keep_a_single_face(self, tmp_path: Path) -> None:
        """A plate has no cavity, so keeping its whole boundary is two-sided.

        This is the case the ray test alone does not solve: every face of a
        solid plate is an outer face.
        """
        skins = extract_shell_skins(ASSEMBLY, tmp_path / "skin.brep")
        for skin in skins:
            if skin.kind == "plate":
                assert skin.kept_faces == 1, skin.summary()

    def test_gauged_thicknesses_are_design_values(self, tmp_path: Path) -> None:
        skins = extract_shell_skins(ASSEMBLY, tmp_path / "skin.brep")
        by_kind = {s.kind: s for s in skins}
        assert by_kind["hollow"].wall_thickness_mm == pytest.approx(0.65, abs=0.05)
