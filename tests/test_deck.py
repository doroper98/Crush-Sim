"""FR-04 deck writer tests, including golden-file snapshots for LC-1 and LC-2.

The golden files pin the writer's output byte for byte so an accidental change
to field order or column width shows up as a diff. They do **not** certify the
syntax against OpenRadioss - see the warning in
:mod:`crushsim.deck.writer`. Regenerate them with::

    pytest tests/test_deck.py --update-golden
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crushsim.config import MaterialCard
from crushsim.deck.format import RULER, f20, i10, reals
from crushsim.deck.writer import (
    DECK_VALIDATION_WARNING,
    DeckPart,
    DriveDefinition,
    RadiossDeckWriter,
    build_deck,
)
from crushsim.errors import DeckError
from crushsim.meshing.mesh_data import ShellMesh
from crushsim.units import (
    CONTACT_FRICTION_DEFAULT,
    SHELL_INTEGRATION_POINTS,
    SHELL_ISHELL_FORMULATION,
)

GOLDEN_DIR = Path(__file__).parent / "golden"


def pytest_addoption(parser) -> None:  # pragma: no cover - hook lives in conftest
    """Declared for documentation; the real option is registered in conftest."""


def _writer(
    material: MaterialCard,
    can: ShellMesh,
    floor: ShellMesh,
    tool: ShellMesh,
    *,
    direction: tuple[float, float, float] = (0.0, 0.0, -1.0),
    stroke: float = 40.0,
    run_name: str = "lc1_golden",
    load_case: str = "LC-1",
) -> RadiossDeckWriter:
    return RadiossDeckWriter(
        run_name=run_name,
        parts=[
            DeckPart(name="CAN", mesh=can, thickness=0.1, role="deformable", material=material),
            DeckPart(name="FLOOR", mesh=floor, thickness=2.0, role="floor"),
            DeckPart(name="REF_TOOL", mesh=tool, thickness=2.0, role="tool"),
        ],
        drive=DriveDefinition(
            direction=direction, stroke=stroke, velocity_mm_s=2000.0, ramp_fraction=0.1
        ),
        material=material,
        load_case=load_case,
        description="golden snapshot",
        solver_version_tag="openradioss-test",
    )


# ---------------------------------------------------------------------------
# Field formatting
# ---------------------------------------------------------------------------


def test_integer_and_real_field_widths() -> None:
    assert len(i10(7)) == 10
    assert len(f20(1.0)) == 20
    assert len(reals((1.0, 2.0, 3.0))) == 60
    assert len(RULER) == 100


def test_real_field_never_overflows_its_column() -> None:
    for value in (0.0, -1.234567891234e-13, 1.0e18, 2.73e-9, 123456.789):
        assert len(f20(value)) == 20


# ---------------------------------------------------------------------------
# Drive definition
# ---------------------------------------------------------------------------


def test_drive_end_time_delivers_the_requested_stroke() -> None:
    drive = DriveDefinition(
        direction=(0, 0, -1), stroke=40.0, velocity_mm_s=2000.0, ramp_fraction=0.1
    )
    table = drive.table()
    assert table[0] == (0.0, 0.0)
    assert table[-1][0] == pytest.approx(drive.end_time)
    assert table[-1][1] == pytest.approx(40.0)


def test_drive_table_is_monotonic() -> None:
    drive = DriveDefinition(
        direction=(1, 0, 0), stroke=20.0, velocity_mm_s=2000.0, ramp_fraction=0.1
    )
    times = [t for t, _ in drive.table()]
    displacements = [s for _, s in drive.table()]
    assert times == sorted(times)
    assert displacements == sorted(displacements)


def test_drive_ramp_is_smooth_at_the_start() -> None:
    """The ease-in must travel less than a step start over the ramp window."""
    drive = DriveDefinition(
        direction=(0, 0, -1), stroke=40.0, velocity_mm_s=2000.0, ramp_fraction=0.2, points=41
    )
    ramp = drive.ramp_time
    for t, s in drive.table():
        if 0.0 < t < ramp:
            assert s < drive.velocity_mm_s * t


def test_zero_velocity_raises() -> None:
    with pytest.raises(DeckError, match="velocity"):
        _ = DriveDefinition(direction=(0, 0, -1), stroke=40.0, velocity_mm_s=0.0).end_time


# ---------------------------------------------------------------------------
# Deck assembly
# ---------------------------------------------------------------------------


def test_ids_are_unique_across_parts(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture) -> None:
    writer = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture)
    ids: list[int] = []
    for part in writer.parts:
        ids.extend(int(n) for n in part.mesh.node_ids)
    assert len(ids) == len(set(ids))
    assert writer.node_count == len(ids) + 2  # two rigid-body master nodes


def test_rigid_master_nodes_follow_the_mesh(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture) -> None:
    writer = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture)
    masters = [p.rbody_master_node for p in writer.parts if p.rigid]
    assert masters == sorted(masters)
    assert min(masters) > max(int(n) for p in writer.parts for n in p.mesh.node_ids)


def test_deck_requires_a_deformable_part(material_card, tool_mesh_fixture) -> None:
    with pytest.raises(DeckError, match="deformable"):
        RadiossDeckWriter(
            run_name="x",
            parts=[DeckPart(name="REF_TOOL", mesh=tool_mesh_fixture, thickness=2.0, role="tool")],
            drive=DriveDefinition(direction=(0, 0, -1), stroke=1.0, velocity_mm_s=1000.0),
            material=material_card,
        )


def test_deck_requires_one_tool_part_per_drive(
    material_card, can_mesh_fixture, tool_mesh_fixture
) -> None:
    # Two tool parts but a single drive: the writer must refuse the mismatch.
    with pytest.raises(DeckError, match="drive"):
        RadiossDeckWriter(
            run_name="x",
            parts=[
                DeckPart(name="CAN", mesh=can_mesh_fixture, thickness=0.1, role="deformable"),
                DeckPart(name="T1", mesh=tool_mesh_fixture, thickness=2.0, role="tool"),
                DeckPart(name="T2", mesh=tool_mesh_fixture, thickness=2.0, role="tool"),
            ],
            drive=DriveDefinition(direction=(0, 0, -1), stroke=1.0, velocity_mm_s=1000.0),
            material=material_card,
        )


# ---------------------------------------------------------------------------
# Starter content
# ---------------------------------------------------------------------------


@pytest.fixture
def lc1_starter(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture) -> str:
    return _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture).starter_text()


def test_starter_carries_the_validation_warning(lc1_starter: str) -> None:
    assert DECK_VALIDATION_WARNING in lc1_starter


def test_starter_declares_the_working_unit_system(lc1_starter: str) -> None:
    assert "mm-s-tonne-N-MPa" in lc1_starter
    # Unit codes are 20-character fields (begin.cfg %20s%20s%20s).
    assert "                  Mg                  mm                   s" in lc1_starter


def test_starter_contains_every_required_keyword(lc1_starter: str) -> None:
    for keyword in (
        "/BEGIN",
        "/NODE",
        "/SHELL/1",
        "/PART/1",
        "/PROP/SHELL/1",
        "/MAT/LAW2/1",
        "/MAT/LAW1/2",
        "/INTER/TYPE7/1",
        "/RBODY/1",
        "/BCS/1",
        "/FUNCT/1",
        "/IMPVEL/1",
        "/TH/RBODY/1",
        "/END",
    ):
        assert keyword in lc1_starter, keyword


def test_shell_property_uses_the_spec_values(lc1_starter: str) -> None:
    """Ishell=24 (QEPH) with 5 through-thickness integration points."""
    block = lc1_starter.split("/PROP/SHELL/1")[1].split("/MAT/")[0]
    assert i10(SHELL_ISHELL_FORMULATION) in block
    assert i10(SHELL_INTEGRATION_POINTS) in block


def test_material_block_carries_law2_terms(lc1_starter: str, material_card) -> None:
    block = lc1_starter.split("/MAT/LAW2/1")[1].split("/PART/2")[0]
    assert f20(material_card.law2.A).strip() in block
    assert f20(material_card.rho).strip() in block
    assert "verified=false" in block


def test_contact_uses_the_default_friction(lc1_starter: str) -> None:
    block = lc1_starter.split("/INTER/TYPE7/1")[1]
    assert f20(CONTACT_FRICTION_DEFAULT).strip() in block


def test_floor_is_fully_fixed(lc1_starter: str) -> None:
    """FLOOR is fixed in all six DOF at Z = 0 (spec §5.2)."""
    block = lc1_starter.split("/BCS/1")[1].split("/GRNOD/NODE/95")[0]
    assert "111 111" in block


def test_axial_drive_uses_the_z_axis_without_a_skew(lc1_starter: str) -> None:
    block = lc1_starter.split("/IMPVEL/1")[1]
    assert "         Z" in block
    assert "/SKEW/FIX" not in lc1_starter


def test_radial_drive_uses_the_x_axis(
    material_card, can_mesh_fixture, floor_mesh_fixture, radial_tool_mesh_fixture
) -> None:
    text = _writer(
        material_card,
        can_mesh_fixture,
        floor_mesh_fixture,
        radial_tool_mesh_fixture,
        direction=(1.0, 0.0, 0.0),
        stroke=20.0,
        run_name="lc2_golden",
        load_case="LC-2",
    ).starter_text()
    block = text.split("/IMPVEL/1")[1]
    assert "         X" in block
    assert "/SKEW/FIX" not in text


def test_oblique_drive_creates_a_skew(
    material_card, can_mesh_fixture, floor_mesh_fixture, radial_tool_mesh_fixture
) -> None:
    text = _writer(
        material_card,
        can_mesh_fixture,
        floor_mesh_fixture,
        radial_tool_mesh_fixture,
        direction=(1.0, 1.0, 0.0),
    ).starter_text()
    assert "/SKEW/FIX/1" in text


def test_node_lines_are_fixed_width(lc1_starter: str) -> None:
    node_block = lc1_starter.split("/NODE\n")[1].split("/SHELL/")[0]
    data_lines = [
        line for line in node_block.splitlines() if line and not line.startswith("#")
    ]
    assert data_lines
    for line in data_lines:
        assert len(line) == 70, line


# ---------------------------------------------------------------------------
# Engine content
# ---------------------------------------------------------------------------


def test_engine_deck_contents(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture) -> None:
    writer = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture)
    text = writer.engine_text()
    for keyword in ("/RUN/lc1_golden/1", "/DT/NODA/CST", "/ANIM/DT", "/TFILE/0", "/END"):
        assert keyword in text, keyword
    assert DECK_VALIDATION_WARNING in text


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------


def test_build_deck_writes_both_files(
    make_case, material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture, tmp_path
) -> None:
    case = make_case("deckcase")
    result = build_deck(
        case,
        material_card,
        can_mesh=can_mesh_fixture,
        tool_mesh=tool_mesh_fixture,
        floor_mesh=floor_mesh_fixture,
        outdir=tmp_path / "deck",
    )
    assert result.starter_path.is_file()
    assert result.engine_path.is_file()
    assert result.starter_path.name == "deckcase_0000.rad"
    assert result.engine_path.name == "deckcase_0001.rad"
    assert result.summary()["material_verified"] is False


def test_deck_generation_is_deterministic(
    material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture
) -> None:
    first = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture).starter_text()
    second = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture).starter_text()
    assert first == second


# ---------------------------------------------------------------------------
# Golden snapshots
# ---------------------------------------------------------------------------


def _check_golden(name: str, text: str, update: bool) -> None:
    path = GOLDEN_DIR / name
    if update or not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if not update:
            pytest.fail(f"Golden file {path} was missing; it has been created. Re-run.")
        return
    expected = path.read_text(encoding="utf-8")
    assert text == expected, (
        f"Deck output drifted from {path}. Review the change, then regenerate with "
        "'pytest tests/test_deck.py --update-golden'."
    )


def test_lc1_starter_matches_golden(
    material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture, update_golden: bool
) -> None:
    writer = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture)
    _check_golden("lc1_starter.rad", writer.starter_text(), update_golden)


def test_lc1_engine_matches_golden(
    material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture, update_golden: bool
) -> None:
    writer = _writer(material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture)
    _check_golden("lc1_engine.rad", writer.engine_text(), update_golden)


def test_lc2_starter_matches_golden(
    material_card, can_mesh_fixture, floor_mesh_fixture, radial_tool_mesh_fixture, update_golden: bool
) -> None:
    writer = _writer(
        material_card,
        can_mesh_fixture,
        floor_mesh_fixture,
        radial_tool_mesh_fixture,
        direction=(1.0, 0.0, 0.0),
        stroke=20.0,
        run_name="lc2_golden",
        load_case="LC-2",
    )
    _check_golden("lc2_starter.rad", writer.starter_text(), update_golden)


def test_lc2_engine_matches_golden(
    material_card, can_mesh_fixture, floor_mesh_fixture, radial_tool_mesh_fixture, update_golden: bool
) -> None:
    writer = _writer(
        material_card,
        can_mesh_fixture,
        floor_mesh_fixture,
        radial_tool_mesh_fixture,
        direction=(1.0, 0.0, 0.0),
        stroke=20.0,
        run_name="lc2_golden",
        load_case="LC-2",
    )
    _check_golden("lc2_engine.rad", writer.engine_text(), update_golden)


# ---------------------------------------------------------------------------
# Multi-tool decks (staged drives: beading + crimping)
# ---------------------------------------------------------------------------


def _multi_tool_writer(material_card, can, floor, tool) -> RadiossDeckWriter:
    main = DriveDefinition(
        direction=(0.0, 0.0, -1.0), stroke=4.0, velocity_mm_s=1000.0, ramp_fraction=0.1
    )
    extra = DriveDefinition(
        direction=(1.0, 0.0, 0.0), stroke=2.0, velocity_mm_s=1000.0, ramp_fraction=0.1
    )
    # The extra tool moves first (stage 0); the main tool starts afterwards.
    main.t_start = extra.end_time
    return RadiossDeckWriter(
        run_name="multi",
        parts=[
            DeckPart(name="CAN", mesh=can, thickness=0.1, role="deformable", material=material_card),
            DeckPart(name="FLOOR", mesh=floor, thickness=2.0, role="floor"),
            DeckPart(name="REF_TOOL", mesh=tool, thickness=2.0, role="tool"),
            DeckPart(name="TOOL2", mesh=tool, thickness=2.0, role="tool"),
        ],
        drive=main,
        material=material_card,
        extra_drives=[extra],
    )


def test_multi_tool_deck_blocks(
    material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture
) -> None:
    writer = _multi_tool_writer(
        material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture
    )
    text = writer.starter_text()
    # Each driven tool gets its own guide, function and imposed velocity.
    for keyword in (
        "/IMPVEL/1",
        "/IMPVEL/2",
        "/FUNCT/2",
        "TOOL2_MASTER",
        "TOOL2_GUIDE",
        "/SURF/PART/4",
        "EXTRA_TOOL_SURFACE",
        "/INTER/TYPE7/4",
    ):
        assert keyword in text, keyword
    # Interface 1 (the force curve) carries the REF_TOOL part alone.
    surf1 = text.split("/SURF/PART/1")[1].splitlines()[2]
    assert surf1.strip() == "3"  # REF_TOOL is part 3 (CAN, FLOOR, REF_TOOL, TOOL2)


def test_staged_drive_holds_still_outside_window() -> None:
    drive = DriveDefinition(
        direction=(0.0, 0.0, -1.0), stroke=4.0, velocity_mm_s=1000.0, ramp_fraction=0.1
    )
    drive.t_start = 2.0e-3
    total = drive.t_start + drive.end_time + 1.0e-3
    table = drive.padded_velocity_table(total)
    assert table[0] == (0.0, 0.0)
    # Zero until the window opens...
    assert all(v == 0.0 for t, v in table if t <= drive.t_start)
    # ...moving inside it, and zero again after the stroke completes.
    assert any(v > 0.0 for _, v in table)
    assert table[-1] == (total, 0.0)
    assert table[-2][1] == 0.0


def test_multi_tool_end_time_covers_both_stages(
    material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture
) -> None:
    writer = _multi_tool_writer(
        material_card, can_mesh_fixture, floor_mesh_fixture, tool_mesh_fixture
    )
    stage0 = DriveDefinition(
        direction=(1.0, 0.0, 0.0), stroke=2.0, velocity_mm_s=1000.0, ramp_fraction=0.1
    ).end_time
    stage1 = DriveDefinition(
        direction=(0.0, 0.0, -1.0), stroke=4.0, velocity_mm_s=1000.0, ramp_fraction=0.1
    ).end_time
    assert writer.end_time == pytest.approx(stage0 + stage1)
