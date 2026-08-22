"""Exception hierarchy for crushsim.

Spec §13.5: failures are raised loudly; silent empty results are forbidden.
Every module raises a subclass of :class:`CrushSimError` so the CLI can report
a single, actionable sentence to the operator.
"""

from __future__ import annotations


class CrushSimError(Exception):
    """Base class for every error raised by crushsim."""


class ConfigError(CrushSimError):
    """A YAML case / material / solver configuration is missing or invalid."""


class HarvestError(CrushSimError):
    """The legacy material harvester could not parse its input (FR-10)."""


class GeometryError(CrushSimError):
    """Geometry generation or STEP pre-processing failed (FR-02)."""


class MeshingError(CrushSimError):
    """Mesh generation failed (FR-03)."""


class GateFailure(CrushSimError):
    """A quality gate was not met and the pipeline must stop (ADR-06)."""


class DeckError(CrushSimError):
    """Radioss deck generation failed (FR-04)."""


class SolverError(CrushSimError):
    """Solver execution failed or a required executable is missing (FR-05)."""


class PostProcessError(CrushSimError):
    """Post-processing / conversion failed (FR-06, FR-07)."""


class ReportError(CrushSimError):
    """Report generation failed (FR-08)."""


class OptionalDependencyError(CrushSimError, ImportError):
    """An optional extra is required but not installed.

    Derives from :class:`ImportError` so ``except ImportError`` at call sites
    still works, while carrying the crushsim-specific remediation message.
    """

    def __init__(self, package: str, extra: str, *, purpose: str = "") -> None:
        detail = f" ({purpose})" if purpose else ""
        super().__init__(
            f"Optional dependency {package!r} is not installed{detail}. "
            f"Install extras: pip install crushsim[{extra}]"
        )
        self.package = package
        self.extra = extra
