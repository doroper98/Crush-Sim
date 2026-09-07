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
    assert "메쉬·수치 조건" in html
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
    assert "에너지 항목" in html
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


# ---------------------------------------------------------------------------
# UI_001 §11 - the report as the document that preserves the evidence
# ---------------------------------------------------------------------------

_RESULT = {
    "schema_version": 1,
    "execution_id": "box_vent_a1b2c3",
    "case_name": "box_vent",
    "executed_at": "2026-09-07T14:17:23+00:00",
    "snapshot_hash": "sha256:abc123",
    "graph_revision": 7,
    "judgement": {
        "sentence": "개방 압력은 0.385 MPa로, 목표 0.30–0.50 MPa 안에 있습니다.",
        "status": "within",
        "caveat": "재료 모델이 검증되지 않아 참고용으로 표시합니다.",
    },
    "target": {"metric_key": "vent_opening_pressure", "lower": 0.3, "upper": 0.5, "unit": "MPa"},
    "target_status": "within",
    "metrics": [
        {
            "key": "vent_opening_pressure",
            "label": "개방 압력",
            "value": 0.38533333333333336,
            "unit": "MPa",
            "kind": "scalar",
            "definition_id": "vent_open_area_25pct_v1",
            "criterion": "개구 면적 / 벤트 면적 >= 25%",
            "unavailable_reason": None,
        },
        {
            "key": "pressure_time_curve",
            "label": "압력–시간",
            "value": None,
            "unit": "MPa vs s",
            "kind": "curve",
            "definition_id": "pressure_ramp_v1",
            "points": [[0.0, 0.0], [0.003, 0.5]],
        },
    ],
    "validation": {
        "model_validity": "valid",
        "scope_status": "unverified",
        "diagnostics": [
            {"code": "MATERIAL_NOT_VALIDATED", "severity": "review", "message": "재료 미검증"}
        ],
        "references": [{"id": "B-3", "title": "메쉬 수렴", "path": "docs/VENT_BURST.md"}],
    },
    "timing": {"queue_seconds": 12.0, "execution_seconds": 864.0, "threads": 4},
    "policy_version": "2026-09-07",
}

_EXECUTION = {
    "exec_id": "box_vent_a1b2c3",
    "input_hash": "sha256:abc123",
    "graph_revision": 7,
    "purpose": "vent_burst",
    "preset_id": "lc6_preset_30min",
    "estimate": {"per_run_min": [12, 17], "basis": "lc6_preset_30min 실측 14.4분 ×0.85~1.2"},
    "provenance": {"n3.thickness": "geometry"},
    "confirmations": {"material": True},
    "assets": [
        {
            "node_id": "n1",
            "asset_id": "abc123def456",
            "display_name": "Honda_Can.stp",
            "content_hash": "sha256:feedface",
            "units": {"declared": "mm", "confirmed": True},
            "dimensions_mm": [120.5, 12.56, 65.0],
        }
    ],
}

_SECTION_ORDER = [
    "1. 판단 요약",
    "2. 입력·가정",
    "3. 형상 처리",
    "4. 재료",
    "5. 메쉬·수치 조건",
    "6. 진단·게이트",
    "7. 검증 범위",
    "8. 지표·곡선",
    "9. 산출물",
]


def _full_report(make_case, material_card, tmp_path: Path) -> str:
    context = build_context(
        case=make_case("rep"),
        material=material_card,
        report_dir=tmp_path,
        result=_RESULT,
        execution=_EXECUTION,
        timings={"geometry": 2.0, "meshing": 30.0, "deck": 4.0, "solver": 813.3,
                 "post": 40.0, "total": 890.0},
    )
    return render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")


def test_report_sections_are_in_the_mandatory_order(make_case, material_card, tmp_path: Path) -> None:
    """UI_001 §11: 판단 요약 first, 산출물 last, nothing reordered."""
    html = _full_report(make_case, material_card, tmp_path)
    positions = [html.index(heading) for heading in _SECTION_ORDER]
    assert positions == sorted(positions)


def test_report_shows_the_judgement_and_the_three_axes(
    make_case, material_card, tmp_path: Path
) -> None:
    """The sentence comes from results.py; target and trust stay separate (§10.2)."""
    html = _full_report(make_case, material_card, tmp_path)
    assert _RESULT["judgement"]["sentence"] in html
    assert _RESULT["judgement"]["caveat"] in html
    assert "목표 범위 내" in html
    assert "유효" in html
    assert "일부 조건 미검증" in html


def test_report_records_geometry_material_and_reproduction_fields(
    make_case, material_card, tmp_path: Path
) -> None:
    html = _full_report(make_case, material_card, tmp_path)
    assert "Honda_Can.stp" in html and "sha256:feedface" in html  # 형상: 표시명 + 해시
    assert "n3.thickness" in html and "geometry" in html  # 조건 출처
    assert "vent_open_area_25pct_v1" in html  # 지표 정의 id
    assert "0.38533333333333336" in html  # 원값 (반올림 저장 금지, §10.4)
    assert "sha256:abc123" in html and "box_vent_a1b2c3" in html  # 재현 정보
    assert "lc6_preset_30min" in html  # 메쉬 프리셋


def test_report_reports_prep_solver_post_and_estimate_vs_actual(
    make_case, material_card, tmp_path: Path
) -> None:
    """§11 시간: 준비/솔버/후처리/총/대기 and estimate against measured."""
    context = build_context(
        case=make_case("rep"),
        material=material_card,
        report_dir=tmp_path,
        result=_RESULT,
        execution=_EXECUTION,
        timings={"geometry": 2.0, "meshing": 30.0, "deck": 4.0, "solver": 813.3,
                 "post": 40.0, "total": 890.0},
    )
    stages = context.stage_timings
    assert stages["prep_s"] == 36.0  # geometry + meshing + deck
    assert stages["solver_s"] == 813.3
    assert stages["post_s"] == 40.0
    assert stages["queue_s"] == 12.0
    assert stages["estimate_min"] == [12, 17]
    assert stages["actual_min"] == 14.8
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "813.3" in html and "14.8" in html


def test_a_run_without_a_snapshot_says_정보_없음(make_case, material_card, tmp_path: Path) -> None:
    """U43/§16 P2: an old run shows what is missing, it does not invent it."""
    context = build_context(case=make_case("rep"), material=material_card, report_dir=tmp_path)
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "정보 없음" in html
    assert "이 실행에는 결과 계약이 없습니다" in html
    # No fabricated verdict anywhere.
    assert "목표 범위 내" not in html
    assert context.stage_timings["solver_s"] is None


def test_report_escapes_a_hostile_case_name(make_case, material_card, tmp_path: Path) -> None:
    """U42: a name is text, never markup."""
    context = build_context(
        case=make_case("rep"),
        material=material_card,
        report_dir=tmp_path,
        execution={"provenance": {"<script>alert(1)</script>": "user"}},
    )
    html = render_report(context, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
