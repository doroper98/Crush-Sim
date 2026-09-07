"""FR-08 - one self-contained HTML report per run (Jinja2).

The report is the document that preserves *why* a number may be believed, so
it follows the fixed order of UI_001 §11: 판단 요약 → 입력·가정 → 형상 처리 →
재료 → 메쉬·수치 조건 → 진단·게이트 → 검증 범위 → 지표·곡선 → 산출물. Any
field a run does not carry (an old run with no execution snapshot, a case
with no design target) is printed as ``정보 없음`` - never filled in by
guessing (UI_001 §16 P2).

The judgement sentence, the target verdict and the validity/scope axes are
NOT computed here: they come from :mod:`crushsim.ui.results`, so the report,
the viewer and the workshop all say the same thing about the same run.

The report carries an ``UNVERIFIED`` watermark whenever the material card is
not verified or any quality gate failed (spec §4 FR-10, §7): a reader must
never mistake an ungated result for a validated one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import CaseConfig, MaterialCard
from ..deck.writer import DECK_VALIDATION_WARNING
from ..errors import ReportError
from ..meshing.gates import GateResult
from ..units import UNIT_SYSTEM

TEMPLATE_DIR: Path = Path(__file__).parent / "templates"
"""Directory holding the Jinja2 templates shipped with the package."""

TEMPLATE_NAME: str = "report.html.j2"


@dataclass(slots=True)
class ReportContext:
    """Everything the HTML report renders."""

    title: str
    case: dict[str, Any] = field(default_factory=dict)
    material: dict[str, Any] = field(default_factory=dict)
    geometry: dict[str, Any] = field(default_factory=dict)
    meshes: list[dict[str, Any]] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] | None = None
    energy: dict[str, Any] | None = None
    deck: dict[str, Any] | None = None
    solver: dict[str, Any] | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    unverified_reasons: list[str] = field(default_factory=list)
    generated_at: str = ""
    unit_system: str = UNIT_SYSTEM
    result: dict[str, Any] | None = None
    """The §2.6 result contract (judgement, target, validation, metrics)."""
    execution: dict[str, Any] = field(default_factory=dict)
    """Execution snapshot: exec id, input hash, graph revision, provenance."""
    timings: dict[str, float] = field(default_factory=dict)
    """Wall-clock seconds per pipeline stage (UI_001 §11 시간)."""
    stages_completed: list[str] = field(default_factory=list)
    """Stages this run actually finished - a skipped solver must be visible."""

    @property
    def unverified(self) -> bool:
        """Whether the report must carry the ``UNVERIFIED`` watermark."""
        return bool(self.unverified_reasons)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form, also written as ``report_context.json``."""
        return {
            "title": self.title,
            "case": self.case,
            "material": self.material,
            "geometry": self.geometry,
            "meshes": self.meshes,
            "gates": self.gates,
            "metrics": self.metrics,
            "energy": self.energy,
            "deck": self.deck,
            "solver": self.solver,
            "artifacts": self.artifacts,
            "notices": self.notices,
            "unverified": self.unverified,
            "unverified_reasons": self.unverified_reasons,
            "generated_at": self.generated_at,
            "unit_system": self.unit_system,
            "result": self.result,
            "execution": self.execution,
            "timings": self.timings,
            "stages_completed": self.stages_completed,
            "stage_timings": self.stage_timings,
        }

    @property
    def stage_timings(self) -> dict[str, Any]:
        """준비 / 솔버 / 후처리 / 총 실행 / 대기, in seconds (UI_001 §11).

        준비 is geometry + meshing + deck: three stages the reader thinks of
        as one ("before the solver"). Missing stages stay missing - a skipped
        solver must not read as a zero-second solver.
        """
        timings = self.timings or {}
        prep_parts = [timings[k] for k in ("geometry", "meshing", "deck") if k in timings]
        stages: dict[str, Any] = {
            "prep_s": round(sum(prep_parts), 1) if prep_parts else None,
            "solver_s": timings.get("solver"),
            "post_s": timings.get("post"),
            "report_s": timings.get("report"),
            "total_s": timings.get("total"),
        }
        exec_timing = ((self.result or {}).get("timing")) or {}
        stages["queue_s"] = exec_timing.get("queue_seconds")
        if stages["total_s"] is None:
            stages["total_s"] = exec_timing.get("execution_seconds")
        estimate = (self.execution or {}).get("estimate") or {}
        span = estimate.get("per_run_min")
        stages["estimate_min"] = list(span) if isinstance(span, (list, tuple)) else None
        stages["estimate_basis"] = estimate.get("basis")
        total = stages["total_s"]
        stages["actual_min"] = round(total / 60.0, 1) if total else None
        return stages


UNKNOWN = "정보 없음"
"""What an absent field prints as. Never a zero, never a guess (UI_001 §16)."""


def _or_unknown(value: Any, suffix: str = "") -> str:
    """``정보 없음`` for anything the run did not record."""
    if value is None or value == "" or value == [] or value == {}:
        return UNKNOWN
    if isinstance(value, bool):
        return "예" if value else "아니오"
    return f"{value}{suffix}"


def _sig(value: Any, digits: int = 3) -> str:
    """Display precision: ~3 significant digits (UI_001 §10.4), raw kept intact."""
    if value is None:
        return UNKNOWN
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:.{digits}g}"
    return text


def _relative(path: Path, base: Path) -> str:
    """Best-effort relative link from the report to an artefact.

    Always POSIX-style: these strings become href/src values in the HTML
    report, and URLs use forward slashes on every platform.
    """
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def build_context(
    *,
    case: CaseConfig,
    material: MaterialCard,
    geometry: dict[str, Any] | None = None,
    mesh_summaries: list[dict[str, Any]] | None = None,
    gates: list[GateResult] | None = None,
    metrics: dict[str, Any] | None = None,
    energy: dict[str, Any] | None = None,
    deck: dict[str, Any] | None = None,
    solver: dict[str, Any] | None = None,
    artifacts: dict[str, Path] | None = None,
    report_dir: Path | None = None,
    notices: list[str] | None = None,
    result: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    timings: dict[str, float] | None = None,
    stages_completed: list[str] | None = None,
) -> ReportContext:
    """Assemble the report context from the pipeline's stage outputs."""
    gate_results = list(gates or [])
    reasons: list[str] = []
    if not material.is_verified:
        reasons.append(
            f"UNVERIFIED MATERIAL: card '{material.key}' is tagged "
            f"verified: false (source: {material.source})."
        )
    for gate in gate_results:
        if not gate.passed:
            failed = ", ".join(m.name for m in gate.failures)
            reasons.append(f"GATE FAILED: {gate.name} ({failed}).")

    base = report_dir or case.run_dir
    links = {name: _relative(Path(p), Path(base)) for name, p in (artifacts or {}).items()}

    return ReportContext(
        title=f"{case.name} - {case.load_case}",
        case={
            "name": case.name,
            "load_case": case.load_case,
            "description": case.description,
            "path": str(case.path) if case.path else None,
            "stroke_mm": case.loading.stroke,
            "velocity_m_s": case.loading.velocity_m_s,
            "direction": list(case.loading.direction),
            "tool": case.loading.tool,
            "friction": case.contact.friction,
            "target_mesh_size_mm": case.mesh.target_size,
        },
        material={
            **material.to_dict(),
            "key": material.key,
            "is_verified": material.is_verified,
        },
        geometry=geometry or {},
        meshes=list(mesh_summaries or []),
        gates=[g.to_dict() for g in gate_results],
        metrics=metrics,
        energy=energy,
        deck=deck,
        solver=solver,
        artifacts=links,
        notices=list(notices or []) + [DECK_VALIDATION_WARNING],
        unverified_reasons=reasons,
        result=result,
        execution=dict(execution or {}),
        timings=dict(timings or {}),
        stages_completed=list(stages_completed or []),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def render_report(context: ReportContext, out_path: str | Path) -> Path:
    """Render the HTML report to ``out_path``.

    Raises:
        ReportError: If Jinja2 is missing or the template cannot be rendered.
    """
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - hard dependency
        raise ReportError(f"Jinja2 is required to render reports: {exc}") from exc

    if not (TEMPLATE_DIR / TEMPLATE_NAME).is_file():
        raise ReportError(f"Report template missing: {TEMPLATE_DIR / TEMPLATE_NAME}")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # ".j2" has to be in the list: select_autoescape decides on the file
        # NAME, and "report.html.j2" ends in ".j2", so the previous
        # ["html", "xml"] left autoescape OFF for every template we ship - a
        # part name or a case description containing markup went into the
        # report as markup (U42). Verified by rendering a hostile provenance
        # key in tests/test_report.py.
        autoescape=select_autoescape(
            enabled_extensions=("html", "xml", "j2"), default_for_string=True, default=True
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["prettyjson"] = lambda value: json.dumps(value, indent=2, default=str)
    env.filters["orunknown"] = _or_unknown
    env.filters["sig"] = _sig
    template = env.get_template(TEMPLATE_NAME)
    html = template.render(ctx=context, **context.to_dict())

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    (target.parent / "report_context.json").write_text(
        json.dumps(context.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    return target
