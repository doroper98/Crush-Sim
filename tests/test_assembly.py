"""EXP-004 - welded multi-part assembly meshing from a STEP file.

A weld is shared nodes, so the parts have to come out of one mesh. These tests
pin the two things that make that work and are easy to lose: the parts really
do share nodes where they meet, and every part carries the full node array the
deck writer's ``host`` mechanism requires.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from crushsim.errors import MeshingError
from crushsim.meshing.assembly import mesh_step_assembly

ASSEMBLY = Path("can/test.stp")

pytestmark = pytest.mark.skipif(
    not ASSEMBLY.is_file(), reason="validation assembly not present"
)


@pytest.fixture(scope="module")
def welded(tmp_path_factory: pytest.TempPathFactory):
    return mesh_step_assembly(
        ASSEMBLY,
        names=["CAN", "CAP", "VENT"],
        workdir=tmp_path_factory.mktemp("assy"),
        target_size=3.0,
        local_sizes={"VENT": 0.8},
        coplanar={"VENT": "CAP"},
    )


def _nodes_of(part) -> set[int]:
    blocks = [part.mesh.quads.ravel(), part.mesh.tris.ravel()]
    return set(np.unique(np.concatenate(blocks)).tolist())


class TestWelds:
    def test_every_part_has_elements(self, welded) -> None:
        assert [p.name for p in welded.parts] == ["CAN", "CAP", "VENT"]
        for part in welded.parts:
            assert part.mesh.n_elements > 0, part.name

    def test_cap_is_welded_to_the_can(self, welded) -> None:
        shared = _nodes_of(welded.part("CAN")) & _nodes_of(welded.part("CAP"))
        assert shared, "the cap must share the can's mouth nodes"

    def test_vent_is_welded_to_the_cap(self, welded) -> None:
        """The case this fails without ``coplanar``.

        Each plate idealises to its own mid-plane, and the cap's is 2 mm above
        the vent's, so the two shells float apart and share nothing - the vent
        would be an unattached island under pressure.
        """
        shared = _nodes_of(welded.part("CAP")) & _nodes_of(welded.part("VENT"))
        assert shared, "the vent must share the cap's stadium outline"

    def test_parts_share_one_node_array(self, welded) -> None:
        """Required by the deck writer to host one part on another."""
        reference = welded.parts[0].mesh.node_ids
        for part in welded.parts[1:]:
            assert np.array_equal(part.mesh.node_ids, reference)
            assert np.allclose(part.mesh.nodes, welded.parts[0].mesh.nodes)

    def test_thicknesses_are_per_part(self, welded) -> None:
        """Can, cap and foil are three different gauges, not one."""
        thicknesses = {p.name: p.thickness_mm for p in welded.parts}
        assert thicknesses["CAN"] == pytest.approx(0.65, abs=0.05)
        assert thicknesses["CAP"] > thicknesses["CAN"]
        assert thicknesses["VENT"] < thicknesses["CAN"]


class TestFailsLoudly:
    def test_name_count_must_match(self, tmp_path: Path) -> None:
        with pytest.raises(MeshingError, match="3 solid"):
            mesh_step_assembly(
                ASSEMBLY, names=["CAN"], workdir=tmp_path, target_size=5.0
            )

    def test_unknown_coplanar_host_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MeshingError, match="coplanar"):
            mesh_step_assembly(
                ASSEMBLY,
                names=["CAN", "CAP", "VENT"],
                workdir=tmp_path,
                target_size=5.0,
                coplanar={"VENT": "LID"},
            )


class TestPerElementThickness:
    """The reason a single thickness per part is not enough.

    Pockets milled for weight, pads left on the load path and a vent score are
    all places where the designer deliberately left a different thickness. One
    number per part erases exactly those - which is to say, it erases the
    design intent the analysis exists to check.
    """

    def test_thickness_varies_within_a_part(self, welded) -> None:
        can = welded.part("CAN")
        assert can.mesh.element_thickness is not None
        spread = np.unique(np.round(can.mesh.element_thickness, 2))
        assert len(spread) > 1, f"a single gauge would hide the wall steps: {spread}"

    def test_score_survives_as_thin_elements(self, welded) -> None:
        """Measured: 0.100 mm residual inside a 0.400 mm foil.

        Before per-element gauging the whole foil came back at one value - the
        score was either everywhere or nowhere depending on where the single
        per-face sample happened to land.
        """
        vent = welded.part("VENT")
        thickness = vent.mesh.element_thickness
        assert thickness is not None
        assert thickness.min() == pytest.approx(0.10, abs=0.02)
        assert thickness.max() == pytest.approx(0.40, abs=0.02)
        scored = int((thickness <= 0.15).sum())
        assert 0 < scored < vent.mesh.n_elements * 0.2, (
            f"{scored} scored elements of {vent.mesh.n_elements} - a score is a "
            "narrow band, not the whole foil"
        )

    def test_outliers_do_not_reach_the_deck(self, welded) -> None:
        """A ray that finds no opposite wall runs the length of the part.

        Measured on this can: 218 mm readings on 6 961 of 18 117 elements
        against a 0.65 mm wall. Unclamped they would go straight into the
        /SHELL cards as element thicknesses.
        """
        for part in welded.parts:
            thickness = part.mesh.element_thickness
            assert thickness is not None
            assert thickness.max() < part.thickness_mm * 4.0, part.name

    def test_thickness_array_matches_element_count(self, welded) -> None:
        for part in welded.parts:
            assert part.mesh.element_thickness.shape[0] == part.mesh.n_elements

    def test_can_be_turned_off(self, tmp_path: Path) -> None:
        plain = mesh_step_assembly(
            ASSEMBLY,
            names=["CAN", "CAP", "VENT"],
            workdir=tmp_path,
            target_size=5.0,
            coplanar={"VENT": "CAP"},
            per_element_thickness=False,
        )
        assert plain.part("CAN").mesh.element_thickness is None


class TestUniformityDiagnostic:
    def test_uniformity_flags_a_varying_part(self, welded) -> None:
        """The number to look at before trusting one thickness.

        Nothing here is uniform: 79 % of the can's area, 55 % of the cap's and
        44 % of the vent's sits within 5 % of its own median.
        """
        by_name = {p.name: p.skin.uniformity for p in welded.parts}
        assert by_name["VENT"] < by_name["CAN"], by_name
        assert all(0.0 <= v <= 1.0 for v in by_name.values())
