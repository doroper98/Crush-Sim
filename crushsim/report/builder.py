"""FR-08 - one self-contained HTML report per run (Jinja2).

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
        }


def _relative(path: Path, base: Path) -> str:
    """Best-effort relative link from the report to an artefact."""
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


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
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["prettyjson"] = lambda value: json.dumps(value, indent=2, default=str)
    template = env.get_template(TEMPLATE_NAME)
    html = template.render(ctx=context, **context.to_dict())

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    (target.parent / "report_context.json").write_text(
        json.dumps(context.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    return target
