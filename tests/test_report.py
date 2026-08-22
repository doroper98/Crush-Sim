"""FR-08 report tests: content, gate verdicts and the UNVERIFIED watermark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crushsim.config import Law2Params, MaterialCard
from crushsim.deck.writer import DECK_VALIDATION_WARNING
from crushsim.meshing.gates import evaluate_mesh_gate
from crushsim.report.builder import TEMPLATE_DIR, TEMPLATE_NAME, build_context, render_report


@pytest.fixture
def verified_card() -> MaterialCard:
    return MaterialCard(
        key="al3104_h19",
        name="Aluminium 3104-H19",
        E=69000.0,
        nu=0.33,
        rho=2.72e-9,
        sigma_y=280.0,
        uts=295.0,
        K=20.0,
        n=0.05,
        t_default=0.1,
        law2=Law2Params(A=280.0, B=20.0, n=0.05),
        verified=True,
        verification="literature",
        source="literature",
    )


def test_template_is_packaged() -> None:
    assert (TEMPLATE_DIR / TEMPLATE_NAME).is_file()


def test_report_renders(make_case, material_card, tmp_path: Path) -> None:
    case = make_case("rep")
    context = build_context(case=case, material=material_card, report_dir=tmp_path)
    path = render_report(context, tmp_path / "report.html")
    assert path.is_file()
    html = path.read_text(encoding="utf-8")
    assert "Crush-Sim" in html
    assert case.name in html


def test_report_writes_its_context_json(make_case, material_card, tmp_path: Path) -> None:
    case = make_case("rep")
    context = build_context(case=case, material=material_card, report_dir=tmp_path)
    render_report(context, tmp_path / "report.html")
    payload = json.loads((tmp_path / "report_context.json").read_text(encoding="utf-8"))
    assert payload["unit_system"] == "mm-s-tonne-N-MPa"


def test_unverified_material_stamps_the_report(make_case, material_card, tmp_path: Path) -> None:
    """Spec §4 FR-10: an unverified card must produce an UNVERIFIED watermark."""
    case = make_case("rep")
    context = build_context(case=case, material=material_card, report_dir=tmp_path)
    assert context.unverified is True
    assert any("UNVERIFIED MATERIAL" in r for r in context.unverified_reasons)
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "UNVERIFIED" in html


def test_verified_material_without_gate_failure_is_clean(
    make_case, verified_card, tmp_path: Path
) -> None:
    case = make_case("rep")
    gate = evaluate_mesh_gate(
        min_sicn=0.5, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    )
    context = build_context(case=case, material=verified_card, gates=[gate], report_dir=tmp_path)
    assert context.unverified is False
    assert context.unverified_reasons == []


def test_failed_gate_stamps_the_report(make_case, verified_card, tmp_path: Path) -> None:
    case = make_case("rep")
    gate = evaluate_mesh_gate(
        min_sicn=0.05, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    )
    context = build_context(case=case, material=verified_card, gates=[gate], report_dir=tmp_path)
    assert context.unverified is True
    assert any("GATE FAILED" in r for r in context.unverified_reasons)


def test_report_renders_gate_table(make_case, material_card, tmp_path: Path) -> None:
    case = make_case("rep")
    gate = evaluate_mesh_gate(
        min_sicn=0.5, max_aspect_ratio=2.0, min_edge_length=0.9, triangle_fraction=0.02
    )
    context = build_context(case=case, material=material_card, gates=[gate], report_dir=tmp_path)
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "min_sicn" in html
    assert "max_aspect_ratio" in html
    assert "PASS" in html


def test_report_includes_mesh_statistics(
    make_case, material_card, can_mesh_fixture, tmp_path: Path
) -> None:
    case = make_case("rep")
    context = build_context(
        case=case,
        material=material_card,
        mesh_summaries=[can_mesh_fixture.summary()],
        report_dir=tmp_path,
    )
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Mesh statistics" in html
    assert str(can_mesh_fixture.n_elements) in html


def test_report_includes_energy_balance_and_metrics(
    make_case, material_card, tmp_path: Path
) -> None:
    case = make_case("rep")
    context = build_context(
        case=case,
        material=material_card,
        metrics={"peak_load_N": 1234.5, "absorbed_energy_mJ": 6789.0},
        energy={"internal_energy_mJ": 12000.0, "kinetic_energy_mJ": 300.0},
        report_dir=tmp_path,
    )
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Energy balance" in html
    assert "1234.5" in html


def test_report_links_artifacts(make_case, material_card, tmp_path: Path) -> None:
    case = make_case("rep")
    png = tmp_path / "force_displacement.png"
    png.write_bytes(b"\x89PNG")
    mp4 = tmp_path / "anim" / "iso.mp4"
    mp4.parent.mkdir(parents=True, exist_ok=True)
    mp4.write_bytes(b"\x00")
    context = build_context(
        case=case,
        material=material_card,
        artifacts={"curve_png": png, "video_iso": mp4},
        report_dir=tmp_path,
    )
    assert context.artifacts["curve_png"] == "force_displacement.png"
    assert context.artifacts["video_iso"] == "anim/iso.mp4"
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert 'src="force_displacement.png"' in html
    assert "anim/iso.mp4" in html


def test_report_carries_the_deck_validation_notice(
    make_case, material_card, tmp_path: Path
) -> None:
    case = make_case("rep")
    context = build_context(case=case, material=material_card, report_dir=tmp_path)
    assert DECK_VALIDATION_WARNING in context.notices
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "validated against the pinned starter" in html
