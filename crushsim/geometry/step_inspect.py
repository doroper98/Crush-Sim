"""STEP inspection and pre-processing (FR-02, Phase 2).

Real geometry work (topology counts, bounding box, principal-axis alignment)
needs OpenCascade through OCP, which ships in the optional ``cad`` extra. When
OCP is absent every geometric entry point raises
:class:`~crushsim.errors.OptionalDependencyError` with the exact install
command - it never degrades to a guess (spec §13.5).

:func:`read_step_header` is dependency-free: it reads the ISO-10303-21 header
so the CLI can report file provenance and the declared unit before any heavy
import is attempted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ..errors import GeometryError, OptionalDependencyError

_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"HEADER;(.*?)ENDSEC;", re.DOTALL | re.IGNORECASE)
_QUOTED_RE: Final[re.Pattern[str]] = re.compile(r"'((?:[^']|'')*)'")
_UNIT_TOKENS: Final[tuple[tuple[str, str], ...]] = (
    ("MILLI.*METRE", "mm"),
    ("CENTI.*METRE", "cm"),
    ("KILO.*METRE", "km"),
    ("METRE", "m"),
    ("INCH", "in"),
)


@dataclass(slots=True)
class StepHeader:
    """ISO-10303-21 header fields of a STEP file."""

    path: Path
    description: str = ""
    name: str = ""
    originating_system: str = ""
    schema: str = ""
    declared_unit: str = "unknown"

    def summary(self) -> dict[str, str]:
        """Flat dictionary for reports."""
        return {
            "path": str(self.path),
            "description": self.description,
            "name": self.name,
            "originating_system": self.originating_system,
            "schema": self.schema,
            "declared_unit": self.declared_unit,
        }


@dataclass(slots=True)
class StepSummary:
    """Topology and extents of a STEP file, as read by OCP."""

    path: Path
    solids: int
    shells: int
    faces: int
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    header: StepHeader | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def extents(self) -> tuple[float, float, float]:
        """Bounding box size along X, Y, Z [mm]."""
        return tuple(hi - lo for hi, lo in zip(self.bbox_max, self.bbox_min))  # type: ignore[return-value]

    @property
    def is_valid(self) -> bool:
        """FR-01 re-open check: at least one solid or shell and a finite box."""
        return (self.solids + self.shells) >= 1 and all(e > 0.0 for e in self.extents)

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for reports and run summaries."""
        return {
            "path": str(self.path),
            "solids": self.solids,
            "shells": self.shells,
            "faces": self.faces,
            "bbox_min_mm": list(self.bbox_min),
            "bbox_max_mm": list(self.bbox_max),
            "extents_mm": list(self.extents),
            "valid": self.is_valid,
            "warnings": list(self.warnings),
            "header": self.header.summary() if self.header else None,
        }


def read_step_header(path: str | Path) -> StepHeader:
    """Read the STEP header without any CAD dependency.

    Raises:
        GeometryError: If the file is missing or is not an ISO-10303-21 file.
    """
    p = Path(path)
    if not p.is_file():
        raise GeometryError(f"STEP file not found: {p}")
    # The header sits at the top of the file; reading a prefix keeps large
    # assemblies cheap to probe.
    with p.open("r", encoding="utf-8", errors="replace") as handle:
        prefix = handle.read(65536)
    if "ISO-10303-21" not in prefix.upper():
        raise GeometryError(f"Not an ISO-10303-21 STEP file (missing magic): {p}")
    match = _HEADER_RE.search(prefix)
    if match is None:
        raise GeometryError(f"STEP file has no HEADER section: {p}")
    header_text = match.group(1)
    strings = [s.replace("''", "'") for s in _QUOTED_RE.findall(header_text)]

    def _entity(name: str) -> list[str]:
        entity_match = re.search(rf"{name}\s*\((.*?)\)\s*;", header_text, re.DOTALL | re.IGNORECASE)
        if entity_match is None:
            return []
        return [s.replace("''", "'") for s in _QUOTED_RE.findall(entity_match.group(1))]

    description = next(iter(_entity("FILE_DESCRIPTION")), "")
    file_name = _entity("FILE_NAME")
    schema = next(iter(_entity("FILE_SCHEMA")), "")

    declared_unit = "unknown"
    upper = prefix.upper()
    for token, unit in _UNIT_TOKENS:
        if re.search(token, upper):
            declared_unit = unit
            break

    return StepHeader(
        path=p,
        description=description or (strings[0] if strings else ""),
        name=file_name[0] if file_name else "",
        originating_system=file_name[4] if len(file_name) > 4 else "",
        schema=schema,
        declared_unit=declared_unit,
    )


def _require_ocp() -> Any:
    """Import OCP or raise a clear, actionable error.

    Raises:
        OptionalDependencyError: If the ``cad`` extra is not installed.
    """
    try:  # pragma: no cover - depends on the optional extra being installed
        from OCP.BRepBndLib import BRepBndLib
        from OCP.Bnd import Bnd_Box
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
    except ImportError as exc:
        raise OptionalDependencyError(
            "cadquery-ocp", "cad", purpose="STEP import and geometry pre-processing"
        ) from exc
    return {
        "BRepBndLib": BRepBndLib,
        "Bnd_Box": Bnd_Box,
        "IFSelect_ReturnStatus": IFSelect_ReturnStatus,
        "STEPControl_Reader": STEPControl_Reader,
        "TopAbs_FACE": TopAbs_FACE,
        "TopAbs_SHELL": TopAbs_SHELL,
        "TopAbs_SOLID": TopAbs_SOLID,
        "TopExp_Explorer": TopExp_Explorer,
    }


def ocp_available() -> bool:
    """Whether the optional ``cad`` extra (OCP) can be imported."""
    try:
        _require_ocp()
    except OptionalDependencyError:
        return False
    return True


def inspect_step(path: str | Path) -> StepSummary:
    """Open a STEP file with OCP and report topology counts and extents.

    Implements the FR-01 re-open verification: at least one solid or shell, a
    valid bounding box, millimetre units.

    Raises:
        OptionalDependencyError: If the ``cad`` extra is not installed.
        GeometryError: If the file is missing or cannot be read as STEP.
    """
    p = Path(path)
    if not p.is_file():
        raise GeometryError(f"STEP file not found: {p}")
    header = read_step_header(p)
    ocp = _require_ocp()  # pragma: no cover - exercised only with the cad extra

    reader = ocp["STEPControl_Reader"]()  # pragma: no cover
    status = reader.ReadFile(str(p))
    if status != ocp["IFSelect_ReturnStatus"].IFSelect_RetDone:
        raise GeometryError(f"OCP could not read STEP file {p} (status={status})")
    reader.TransferRoots()
    shape = reader.OneShape()

    counts: dict[str, int] = {}
    for label, kind in (
        ("solids", ocp["TopAbs_SOLID"]),
        ("shells", ocp["TopAbs_SHELL"]),
        ("faces", ocp["TopAbs_FACE"]),
    ):
        explorer = ocp["TopExp_Explorer"](shape, kind)
        count = 0
        while explorer.More():
            count += 1
            explorer.Next()
        counts[label] = count

    box = ocp["Bnd_Box"]()
    ocp["BRepBndLib"].Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

    warnings: list[str] = []
    if header.declared_unit not in ("mm", "unknown"):
        warnings.append(
            f"STEP declares unit {header.declared_unit!r}; crushsim works in mm "
            "- convert the export before meshing."
        )
    summary = StepSummary(
        path=p,
        solids=counts["solids"],
        shells=counts["shells"],
        faces=counts["faces"],
        bbox_min=(xmin, ymin, zmin),
        bbox_max=(xmax, ymax, zmax),
        header=header,
        warnings=warnings,
    )
    if not summary.is_valid:
        raise GeometryError(
            f"STEP file {p} failed the re-open check: "
            f"solids={summary.solids}, shells={summary.shells}, extents={summary.extents}"
        )
    return summary
