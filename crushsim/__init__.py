"""crushsim - cylindrical can crush/strength simulation pipeline.

Pipeline (spec §3): geometry -> Gmsh shell mesh + quality gate -> OpenRadioss
deck -> solver -> official converters -> curves/animation -> HTML report.

Architecture decisions worth remembering while editing this package:

* ADR-01 - physics is never implemented here; OpenRadioss is the only solver.
* ADR-02 - computation and visualisation are separate; rendering reads files.
* ADR-04 - the unit system and every numeric limit live in :mod:`crushsim.units`.
* ADR-05 - materials are YAML data cards, not code.
* ADR-06 - artefacts that fail a gate never advance to the next stage.
"""

from __future__ import annotations

from .errors import (
    ConfigError,
    CrushSimError,
    DeckError,
    GateFailure,
    GeometryError,
    HarvestError,
    MeshingError,
    OptionalDependencyError,
    PostProcessError,
    ReportError,
    SolverError,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ConfigError",
    "CrushSimError",
    "DeckError",
    "GateFailure",
    "GeometryError",
    "HarvestError",
    "MeshingError",
    "OptionalDependencyError",
    "PostProcessError",
    "ReportError",
    "SolverError",
]
