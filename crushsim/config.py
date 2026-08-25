"""Typed configuration objects loaded from YAML (spec §11, ADR-05).

Three kinds of configuration exist:

* **Material cards** - ``configs/materials/{harvested,verified}/*.yaml``. Data,
  not code (ADR-05); each card carries a ``verified`` flag.
* **Case files** - ``configs/cases/*.yaml``. One load case end to end.
* **Solver config** - ``configs/solver.yaml``. Pinned OpenRadioss version tag
  plus executable locations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from .errors import ConfigError
from .units import (
    CONTACT_FRICTION_DEFAULT,
    DRIVE_VELOCITY_DEFAULT_M_S,
    HARDENING_REFERENCE_STRAIN,
    MESH_TARGET_SIZE_DEFAULT_MM,
    UNIT_SYSTEM,
)

LoadCaseId = Literal["LC-1", "LC-2", "LC-3"]

LOAD_CASES: Final[tuple[str, ...]] = ("LC-1", "LC-2", "LC-3")
"""Supported load-case identifiers (spec §5.3)."""

VERIFICATION_STATES: Final[tuple[str, ...]] = ("unverified", "literature", "calibrated")
"""Material verification ladder (ADR-05)."""


def _read_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from ``path``.

    Raises:
        ConfigError: If the file is missing, unreadable or not a mapping.
    """
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - malformed YAML
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a YAML mapping at the top level of {path}")
    return data


def _require(data: dict[str, Any], key: str, source: Path | str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required key {key!r} in {source}")
    return data[key]


def _as_float(value: Any, key: str, source: Path | str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Key {key!r} in {source} must be a number, got {value!r}") from exc
    if not math.isfinite(out):
        raise ConfigError(f"Key {key!r} in {source} must be finite, got {value!r}")
    return out


# ---------------------------------------------------------------------------
# Material card
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Law2Params:
    """Johnson-Cook quasi-static terms used by OpenRadioss /MAT/LAW2.

    ``sigma = A + B * eps_p ** n``. Isomorphic to Ludwik-Hollomon
    ``sigma = sigma_y + K * eps_p ** n`` (spec §1.1).
    """

    A: float
    B: float
    n: float


@dataclass(frozen=True, slots=True)
class MaterialCard:
    """A material card in the working unit system (mm-s-tonne-N-MPa)."""

    key: str
    name: str
    E: float
    """Young's modulus [MPa]."""
    nu: float
    """Poisson ratio [-]."""
    rho: float
    """Density [tonne/mm^3]."""
    sigma_y: float
    """Initial yield stress [MPa]."""
    uts: float
    """Ultimate tensile strength [MPa]."""
    K: float
    """Ludwik-Hollomon hardening coefficient [MPa]."""
    n: float
    """Ludwik-Hollomon hardening exponent [-]."""
    t_default: float
    """Default shell thickness [mm]."""
    law2: Law2Params
    verified: bool = False
    verification: str = "unverified"
    source: str = "unknown"
    unit_system: str = UNIT_SYSTEM
    notes: str = ""

    def __post_init__(self) -> None:
        for name, value in (("E", self.E), ("rho", self.rho), ("sigma_y", self.sigma_y)):
            if value <= 0.0:
                raise ConfigError(f"Material {self.key!r}: {name} must be > 0, got {value!r}")
        if not 0.0 <= self.nu < 0.5:
            raise ConfigError(f"Material {self.key!r}: nu must be in [0, 0.5), got {self.nu!r}")
        if self.uts < self.sigma_y:
            raise ConfigError(
                f"Material {self.key!r}: uts ({self.uts}) must be >= sigma_y ({self.sigma_y})"
            )

    @property
    def is_verified(self) -> bool:
        """Whether the card may be used without an ``UNVERIFIED`` watermark."""
        return bool(self.verified) and self.verification != "unverified"

    def flow_stress(self, plastic_strain: float) -> float:
        """Evaluate the Ludwik-Hollomon flow stress [MPa] at a plastic strain."""
        eps = max(float(plastic_strain), 0.0)
        return self.sigma_y + self.K * (eps**self.n)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the card back to the YAML card schema."""
        return {
            "name": self.name,
            "E": self.E,
            "nu": self.nu,
            "rho": self.rho,
            "sigma_y": self.sigma_y,
            "uts": self.uts,
            "K": self.K,
            "n": self.n,
            "t_default": self.t_default,
            "law2": {"A": self.law2.A, "B": self.law2.B, "n": self.law2.n},
            "verified": self.verified,
            "verification": self.verification,
            "source": self.source,
            "unit_system": self.unit_system,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, key: str, source: Path | str = "<dict>") -> "MaterialCard":
        """Build a card from a parsed YAML mapping."""
        law2_raw = data.get("law2")
        sigma_y = _as_float(_require(data, "sigma_y", source), "sigma_y", source)
        n = _as_float(data.get("n", 0.0), "n", source)
        k = data.get("K")
        if k is None:
            uts = _as_float(_require(data, "uts", source), "uts", source)
            k_value = (uts - sigma_y) / (HARDENING_REFERENCE_STRAIN**n)
        else:
            k_value = _as_float(k, "K", source)
        if isinstance(law2_raw, dict):
            law2 = Law2Params(
                A=_as_float(law2_raw.get("A", sigma_y), "law2.A", source),
                B=_as_float(law2_raw.get("B", k_value), "law2.B", source),
                n=_as_float(law2_raw.get("n", n), "law2.n", source),
            )
        else:
            law2 = Law2Params(A=sigma_y, B=k_value, n=n)
        verification = str(data.get("verification", "unverified"))
        if verification not in VERIFICATION_STATES:
            raise ConfigError(
                f"Material {key!r}: verification must be one of {VERIFICATION_STATES}, "
                f"got {verification!r}"
            )
        return cls(
            key=key,
            name=str(data.get("name", key)),
            E=_as_float(_require(data, "E", source), "E", source),
            nu=_as_float(_require(data, "nu", source), "nu", source),
            rho=_as_float(_require(data, "rho", source), "rho", source),
            sigma_y=sigma_y,
            uts=_as_float(data.get("uts", sigma_y), "uts", source),
            K=k_value,
            n=n,
            t_default=_as_float(data.get("t_default", 1.0), "t_default", source),
            law2=law2,
            verified=bool(data.get("verified", False)),
            verification=verification,
            source=str(data.get("source", "unknown")),
            unit_system=str(data.get("unit_system", UNIT_SYSTEM)),
            notes=str(data.get("notes", "")),
        )


def load_material_card(path: str | Path) -> MaterialCard:
    """Load a material card YAML file.

    Raises:
        ConfigError: If the file is missing or the card is invalid.
    """
    p = Path(path)
    data = _read_yaml(p)
    return MaterialCard.from_dict(data, key=p.stem, source=p)


def find_material_card(key: str, search_roots: list[Path] | None = None) -> Path:
    """Locate a material card by key, preferring verified cards over harvested ones.

    Raises:
        ConfigError: If no card with that key exists under the search roots.
    """
    roots = search_roots or [
        Path("configs/materials/verified"),
        Path("configs/materials/literature"),
        Path("configs/materials/harvested"),
    ]
    for root in roots:
        candidate = Path(root) / f"{key}.yaml"
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(r) for r in roots)
    raise ConfigError(f"Material card {key!r} not found. Searched: {searched}")


# ---------------------------------------------------------------------------
# Case configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeometryConfig:
    """Can geometry: either code-generated (Phase 1) or a STEP file (Phase 2)."""

    kind: Literal["parametric_can", "step"] = "parametric_can"
    radius: float = 33.0
    """Outer radius [mm] (parametric only)."""
    height: float = 115.0
    """Height [mm] (parametric only)."""
    thickness: float = 0.1
    """Wall thickness [mm]; shell property thickness."""
    closed_bottom: bool = False
    closed_top: bool = False
    step_path: Path | None = None
    """Path to the STEP file when ``kind == 'step'``."""


@dataclass(frozen=True, slots=True)
class MeshConfig:
    """Meshing targets (FR-03)."""

    target_size: float = MESH_TARGET_SIZE_DEFAULT_MM
    min_size: float | None = None
    max_size: float | None = None
    recombine: bool = True
    """Recombine triangles into a quad-dominant mesh."""
    curvature_points: int = 12
    """Gmsh ``Mesh.MeshSizeFromCurvature`` - elements per 2*pi of curvature."""
    imperfection_mm: float = 0.0
    """Seeded radial imperfection amplitude for the parametric can wall [mm].

    A perfect cylinder's first buckling peak never mesh-converges (classical
    imperfection sensitivity): each refinement finds a higher buckling mode and
    the peak keeps falling. Seeding a small deterministic imperfection - the
    industry-standard remedy - makes every mesh buckle in the same physical
    mode so the peak converges. 0 disables it; the B-3 sweep uses ~0.5x the
    wall thickness."""


@dataclass(frozen=True, slots=True)
class ExtraToolConfig:
    """One additional driven rigid tool (multi-tool processes, e.g. beading).

    Extra tools share the can and contact settings with the main tool but
    carry their own shape, drive and *stage*: tools in stage 0 move first
    (together), stage 1 tools start once every stage-0 stroke has finished,
    and so on. A beading + crimping process is stage-0 radial rollers
    followed by a stage-1 axial die.
    """

    tool: Literal["platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller"] = (
        "platen"
    )
    direction: tuple[float, float, float] = (0.0, 0.0, -1.0)
    stroke: float = 10.0
    velocity_m_s: float = DRIVE_VELOCITY_DEFAULT_M_S
    ramp_fraction: float = 0.1
    tool_gap: float = 0.5
    tool_size: float | None = None
    indenter_radius: float = 10.0
    height_frac: float = 0.5
    """Axial position of a radial tool: fraction of the can height."""
    stage: int = 0
    """Motion stage; each stage starts when the previous one's strokes end."""
    motion: Literal["linear", "orbit"] = "linear"
    """``orbit``: the tool circles the can axis while feeding radially inward
    (rotary beading). ``stroke`` is the total radial feed, ``velocity`` the
    tangential speed of the tool centre [m/s]."""
    orbit_revs: float = 1.25
    """Total revolutions around the can axis (orbit motion)."""
    feed_revs: float = 1.0
    """Revolutions over which the radial feed completes; the remainder of the
    orbit irons the groove at constant depth."""


@dataclass(frozen=True, slots=True)
class LoadingConfig:
    """Reference-tool drive definition (spec §5.3)."""

    tool: Literal[
        "platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller", "step", "none"
    ] = "platen"
    """Main driven tool; ``none`` for tool-less cases (internal pressure only)."""
    direction: tuple[float, float, float] = (0.0, 0.0, -1.0)
    stroke: float = 40.0
    """Total imposed displacement magnitude [mm]."""
    velocity_m_s: float = DRIVE_VELOCITY_DEFAULT_M_S
    """Drive velocity [m/s]; converted to mm/s inside the deck writer."""
    ramp_fraction: float = 0.1
    """Fraction of the total time used to ramp the velocity up (smooth start)."""
    tool_gap: float = 0.5
    """Initial gap between tool and can surface [mm]."""
    tool_size: float | None = None
    """Characteristic tool size [mm]; defaults to the can diameter."""
    indenter_radius: float = 10.0
    """Hemispherical indenter radius [mm] (LC-3)."""
    step_path: Path | None = None
    """Real-shape jig STEP file (LC-2 with ``tool: step``)."""
    support: Literal["jig_plane", "v_block", "bead_arbor"] | None = None
    """Fixed rigid support opposite the drive direction (LC-2/LC-3).

    A radially driven jig with nothing behind the can simply bulldozes it
    across the floor; a fixed support (typically a V-block) cradles the can
    so the drive actually crushes it. ``None`` (LC-1) relies on the floor.
    """
    support_size: float | None = None
    """Characteristic support size [mm]; defaults like ``tool_size``."""
    clamp_can_base: bool = False
    """Fix the can's base node ring (all six DOF).

    A standing can pressed laterally at mid height has nothing holding it
    down in a gravity-free quasi-static model - it tips over the support and
    slides instead of crushing. Real fixtures grip the base; this models
    that grip."""
    height_frac: float = 0.5
    """Axial position of a radial main tool: fraction of the can height."""
    stage: int = 0
    """Motion stage of the main tool (see :class:`ExtraToolConfig`)."""
    motion: Literal["linear", "orbit"] = "linear"
    """``orbit``: the main tool circles the can axis (see ExtraToolConfig)."""
    orbit_revs: float = 1.25
    feed_revs: float = 1.0
    extra_tools: tuple[ExtraToolConfig, ...] = ()
    """Additional driven rigid tools (multi-tool processes)."""


@dataclass(frozen=True, slots=True)
class PressureConfig:
    """Uniform internal pressure on the can (vent burst / swelling track).

    ``peak == 0`` disables the load. The pressure ramps linearly from zero to
    ``peak`` over ``rise`` seconds and is then held for ``hold`` seconds; a
    tool-less case's engine end time is ``rise + hold``.
    """

    peak: float = 0.0
    """Peak internal pressure [MPa]."""
    rise: float = 1.0e-3
    """Linear ramp duration [s]."""
    hold: float = 0.5e-3
    """Hold duration at peak [s]."""


@dataclass(frozen=True, slots=True)
class ContactConfig:
    """Global /INTER/TYPE7 contact settings (FR-04)."""

    friction: float = CONTACT_FRICTION_DEFAULT
    gap_scale: float = 0.8
    stiffness_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class SolverRunConfig:
    """Per-case solver settings; executables come from ``configs/solver.yaml``."""

    config: Path = Path("configs/solver.yaml")
    threads: int = 4
    mpi_ranks: int = 1
    end_time: float | None = None
    """Engine end time [s]; derived from stroke/velocity when omitted."""
    animation_frames: int = 25
    time_history_points: int = 500
    stop_on_energy_error: bool = True
    stop_on_negative_volume: bool = True


@dataclass(frozen=True, slots=True)
class OutputConfig:
    """Output locations for one run."""

    dir: Path = Path("runs/default")
    render: bool = True
    report: bool = True


@dataclass(frozen=True, slots=True)
class CaseConfig:
    """A full load case, loaded from ``configs/cases/*.yaml``."""

    name: str
    load_case: str
    path: Path | None = None
    description: str = ""
    material_key: str = "aluminum_3003"
    material_path: Path | None = None
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    mesh: MeshConfig = field(default_factory=MeshConfig)
    loading: LoadingConfig = field(default_factory=LoadingConfig)
    contact: ContactConfig = field(default_factory=ContactConfig)
    pressure: PressureConfig = field(default_factory=PressureConfig)
    eps_p_max: float | None = None
    """Failure plastic strain (LAW2 EPS_p_max -> element deletion); None disables."""
    solver: SolverRunConfig = field(default_factory=SolverRunConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def material(self, search_roots: list[Path] | None = None) -> MaterialCard:
        """Resolve and load the material card referenced by this case."""
        if self.material_path is not None:
            return load_material_card(self.material_path)
        return load_material_card(find_material_card(self.material_key, search_roots))

    @property
    def run_dir(self) -> Path:
        """Directory holding every artefact of this run."""
        return Path(self.output.dir)


def _direction(value: Any, source: Path | str) -> tuple[float, float, float]:
    if value is None:
        return (0.0, 0.0, -1.0)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ConfigError(f"loading.direction in {source} must be a 3-element list, got {value!r}")
    vec = tuple(_as_float(v, "loading.direction", source) for v in value)
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0.0:
        raise ConfigError(f"loading.direction in {source} must be non-zero")
    return (vec[0] / norm, vec[1] / norm, vec[2] / norm)


_EXTRA_TOOL_KINDS = ("platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller")


def _extra_tool(raw: Any, index: int, source: Path | str) -> ExtraToolConfig:
    """Parse and validate one ``loading.extra_tools`` entry."""
    key = f"loading.extra_tools[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{key} in {source} must be a mapping, got {raw!r}")
    kind = str(raw.get("tool", "platen"))
    if kind not in _EXTRA_TOOL_KINDS:
        raise ConfigError(
            f"{key}.tool in {source} must be one of {', '.join(_EXTRA_TOOL_KINDS)}, got {kind!r}"
        )
    tool = ExtraToolConfig(
        tool=kind,  # type: ignore[arg-type]
        direction=_direction(raw.get("direction"), source),
        stroke=_as_float(raw.get("stroke", 10.0), f"{key}.stroke", source),
        velocity_m_s=_as_float(
            raw.get("velocity", DRIVE_VELOCITY_DEFAULT_M_S), f"{key}.velocity", source
        ),
        ramp_fraction=_as_float(raw.get("ramp_fraction", 0.1), f"{key}.ramp_fraction", source),
        tool_gap=_as_float(raw.get("tool_gap", 0.5), f"{key}.tool_gap", source),
        tool_size=None
        if raw.get("tool_size") is None
        else _as_float(raw["tool_size"], f"{key}.tool_size", source),
        indenter_radius=_as_float(
            raw.get("indenter_radius", 10.0), f"{key}.indenter_radius", source
        ),
        height_frac=_as_float(raw.get("height_frac", 0.5), f"{key}.height_frac", source),
        stage=int(raw.get("stage", 0)),
        motion=str(raw.get("motion", "linear")),  # type: ignore[arg-type]
        orbit_revs=_as_float(raw.get("orbit_revs", 1.25), f"{key}.orbit_revs", source),
        feed_revs=_as_float(raw.get("feed_revs", 1.0), f"{key}.feed_revs", source),
    )
    if tool.motion not in ("linear", "orbit"):
        raise ConfigError(f"{key}.motion in {source} must be 'linear' or 'orbit'")
    if tool.motion == "orbit" and not 0.0 < tool.feed_revs <= tool.orbit_revs:
        raise ConfigError(f"{key}.feed_revs in {source} must be in (0, orbit_revs]")
    if tool.stroke <= 0.0:
        raise ConfigError(f"{key}.stroke in {source} must be > 0")
    if tool.stage < 0:
        raise ConfigError(f"{key}.stage in {source} must be >= 0")
    if not 0.0 <= tool.height_frac <= 1.0:
        raise ConfigError(f"{key}.height_frac in {source} must be in [0, 1]")
    return tool


def load_case(path: str | Path) -> CaseConfig:
    """Load and validate a case YAML file.

    Raises:
        ConfigError: If the file is missing or any field is invalid.
    """
    p = Path(path)
    data = _read_yaml(p)
    base = p.parent

    load_case_id = str(_require(data, "load_case", p)).upper()
    if load_case_id not in LOAD_CASES:
        raise ConfigError(f"load_case in {p} must be one of {LOAD_CASES}, got {load_case_id!r}")

    geo_raw = dict(data.get("geometry") or {})
    kind = str(geo_raw.get("kind", "parametric_can"))
    if kind not in ("parametric_can", "step"):
        raise ConfigError(f"geometry.kind in {p} must be 'parametric_can' or 'step', got {kind!r}")
    step_path_raw = geo_raw.get("step_path") or geo_raw.get("path")
    if kind == "step" and not step_path_raw:
        raise ConfigError(f"geometry.step_path is required in {p} when geometry.kind == 'step'")
    geometry = GeometryConfig(
        kind=kind,  # type: ignore[arg-type]
        radius=_as_float(geo_raw.get("radius", 33.0), "geometry.radius", p),
        height=_as_float(geo_raw.get("height", 115.0), "geometry.height", p),
        thickness=_as_float(geo_raw.get("thickness", 0.1), "geometry.thickness", p),
        closed_bottom=bool(geo_raw.get("closed_bottom", False)),
        closed_top=bool(geo_raw.get("closed_top", False)),
        step_path=_resolve(step_path_raw, base),
    )
    if geometry.radius <= 0 or geometry.height <= 0 or geometry.thickness <= 0:
        raise ConfigError(f"geometry radius/height/thickness in {p} must all be > 0")

    mesh_raw = dict(data.get("mesh") or {})
    mesh = MeshConfig(
        target_size=_as_float(
            mesh_raw.get("target_size", MESH_TARGET_SIZE_DEFAULT_MM), "mesh.target_size", p
        ),
        min_size=None if mesh_raw.get("min_size") is None else _as_float(mesh_raw["min_size"], "mesh.min_size", p),
        max_size=None if mesh_raw.get("max_size") is None else _as_float(mesh_raw["max_size"], "mesh.max_size", p),
        recombine=bool(mesh_raw.get("recombine", True)),
        curvature_points=int(mesh_raw.get("curvature_points", 12)),
        imperfection_mm=_as_float(mesh_raw.get("imperfection_mm", 0.0), "mesh.imperfection_mm", p),
    )
    if mesh.target_size <= 0:
        raise ConfigError(f"mesh.target_size in {p} must be > 0")

    load_raw = dict(data.get("loading") or {})
    loading = LoadingConfig(
        tool=str(load_raw.get("tool", "platen")),  # type: ignore[arg-type]
        direction=_direction(load_raw.get("direction"), p),
        stroke=_as_float(load_raw.get("stroke", 40.0), "loading.stroke", p),
        velocity_m_s=_as_float(
            load_raw.get("velocity", DRIVE_VELOCITY_DEFAULT_M_S), "loading.velocity", p
        ),
        ramp_fraction=_as_float(load_raw.get("ramp_fraction", 0.1), "loading.ramp_fraction", p),
        tool_gap=_as_float(load_raw.get("tool_gap", 0.5), "loading.tool_gap", p),
        tool_size=None if load_raw.get("tool_size") is None else _as_float(load_raw["tool_size"], "loading.tool_size", p),
        indenter_radius=_as_float(load_raw.get("indenter_radius", 10.0), "loading.indenter_radius", p),
        step_path=_resolve(load_raw.get("step_path"), base),
        support=load_raw.get("support"),  # type: ignore[arg-type]
        support_size=None
        if load_raw.get("support_size") is None
        else _as_float(load_raw["support_size"], "loading.support_size", p),
        clamp_can_base=bool(load_raw.get("clamp_can_base", False)),
        height_frac=_as_float(load_raw.get("height_frac", 0.5), "loading.height_frac", p),
        stage=int(load_raw.get("stage", 0)),
        motion=str(load_raw.get("motion", "linear")),  # type: ignore[arg-type]
        orbit_revs=_as_float(load_raw.get("orbit_revs", 1.25), "loading.orbit_revs", p),
        feed_revs=_as_float(load_raw.get("feed_revs", 1.0), "loading.feed_revs", p),
        extra_tools=tuple(
            _extra_tool(raw, index, p) for index, raw in enumerate(load_raw.get("extra_tools") or [])
        ),
    )
    _tools = (
        "platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller", "step", "none"
    )
    if loading.tool not in _tools:
        raise ConfigError(
            f"loading.tool in {p} must be one of {', '.join(_tools)}, got {loading.tool!r}"
        )
    if loading.motion not in ("linear", "orbit"):
        raise ConfigError(f"loading.motion in {p} must be 'linear' or 'orbit'")
    if loading.tool == "none" and loading.extra_tools:
        raise ConfigError(f"loading.extra_tools in {p} need a main tool (loading.tool != 'none')")
    _supports = ("jig_plane", "v_block", "bead_arbor")
    if loading.support is not None and loading.support not in _supports:
        raise ConfigError(
            f"loading.support in {p} must be one of {', '.join(_supports)}, got {loading.support!r}"
        )
    if loading.stroke <= 0:
        raise ConfigError(f"loading.stroke in {p} must be > 0")
    if not 0.0 <= loading.ramp_fraction < 1.0:
        raise ConfigError(f"loading.ramp_fraction in {p} must be in [0, 1)")

    contact_raw = dict(data.get("contact") or {})
    contact = ContactConfig(
        friction=_as_float(contact_raw.get("friction", CONTACT_FRICTION_DEFAULT), "contact.friction", p),
        gap_scale=_as_float(contact_raw.get("gap_scale", 0.8), "contact.gap_scale", p),
        stiffness_scale=_as_float(contact_raw.get("stiffness_scale", 1.0), "contact.stiffness_scale", p),
    )

    pressure_raw = dict(data.get("pressure") or {})
    pressure = PressureConfig(
        peak=_as_float(pressure_raw.get("peak", 0.0), "pressure.peak", p),
        rise=_as_float(pressure_raw.get("rise", 1.0e-3), "pressure.rise", p),
        hold=_as_float(pressure_raw.get("hold", 0.5e-3), "pressure.hold", p),
    )
    if pressure.peak < 0.0:
        raise ConfigError(f"pressure.peak in {p} must be >= 0")
    if pressure.peak > 0.0 and pressure.rise <= 0.0:
        raise ConfigError(f"pressure.rise in {p} must be > 0 when pressure.peak is set")
    if loading.tool == "none" and pressure.peak <= 0.0:
        raise ConfigError(
            f"loading.tool 'none' in {p} needs an internal pressure load (pressure.peak > 0)"
        )

    solver_raw = dict(data.get("solver") or {})
    solver = SolverRunConfig(
        config=_resolve(solver_raw.get("config", "configs/solver.yaml"), Path(".")) or Path("configs/solver.yaml"),
        threads=int(solver_raw.get("threads", 4)),
        mpi_ranks=int(solver_raw.get("mpi_ranks", 1)),
        end_time=None if solver_raw.get("end_time") in (None, "auto") else _as_float(solver_raw["end_time"], "solver.end_time", p),
        animation_frames=int(solver_raw.get("animation_frames", 25)),
        time_history_points=int(solver_raw.get("time_history_points", 500)),
        stop_on_energy_error=bool(solver_raw.get("stop_on_energy_error", True)),
        stop_on_negative_volume=bool(solver_raw.get("stop_on_negative_volume", True)),
    )

    out_raw = dict(data.get("output") or {})
    name = str(data.get("name", p.stem))
    output = OutputConfig(
        dir=Path(str(out_raw.get("dir", f"runs/{name}"))),
        render=bool(out_raw.get("render", True)),
        report=bool(out_raw.get("report", True)),
    )

    mat_raw = data.get("material") or {}
    if isinstance(mat_raw, str):
        material_key, material_path = mat_raw, None
    else:
        material_key = str(mat_raw.get("key", "aluminum_3003"))
        material_path = _resolve(mat_raw.get("card"), Path("."))
    eps_p_max = None
    if isinstance(mat_raw, dict) and mat_raw.get("eps_p_max") is not None:
        eps_p_max = _as_float(mat_raw["eps_p_max"], "material.eps_p_max", p)
        if eps_p_max <= 0.0:
            raise ConfigError(f"material.eps_p_max in {p} must be > 0")

    return CaseConfig(
        name=name,
        load_case=load_case_id,
        path=p,
        description=str(data.get("description", "")),
        material_key=material_key,
        material_path=material_path,
        geometry=geometry,
        mesh=mesh,
        loading=loading,
        contact=contact,
        pressure=pressure,
        eps_p_max=eps_p_max,
        solver=solver,
        output=output,
        raw=data,
    )


def _resolve(value: Any, base: Path) -> Path | None:
    """Resolve a path-ish YAML value relative to ``base`` when it is relative.

    Paths that exist relative to the current working directory win, so a case
    file can reference ``examples/step/...`` from the repository root.
    """
    if value in (None, ""):
        return None
    p = Path(str(value))
    if p.is_absolute():
        return p
    if p.exists():
        return p
    candidate = base / p
    return candidate if candidate.exists() else p


# ---------------------------------------------------------------------------
# Solver configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolverPaths:
    """Locations of the pinned OpenRadioss toolchain (FR-05)."""

    version_tag: str
    starter: Path | None
    engine: Path | None
    th_to_csv: Path | None
    anim_to_vtk: Path | None
    install_root: Path | None
    env: dict[str, str] = field(default_factory=dict)
    source: Path | None = None

    def missing(self) -> list[str]:
        """Names of executables that are configured but not present on disk."""
        out: list[str] = []
        for label, exe in (
            ("starter", self.starter),
            ("engine", self.engine),
        ):
            if exe is None or not Path(exe).exists():
                out.append(label)
        return out


def load_solver_config(path: str | Path = "configs/solver.yaml") -> SolverPaths:
    """Load ``configs/solver.yaml``.

    Raises:
        ConfigError: If the file is missing or the pinned version tag is absent.
    """
    p = Path(path)
    data = _read_yaml(p)
    version_tag = str(_require(data, "version_tag", p))
    exe = dict(data.get("executables") or {})
    root_raw = data.get("install_root")

    def _exe(key: str) -> Path | None:
        value = exe.get(key)
        return None if value in (None, "") else Path(str(value)).expanduser()

    env_raw = dict(data.get("env") or {})
    return SolverPaths(
        version_tag=version_tag,
        starter=_exe("starter"),
        engine=_exe("engine"),
        th_to_csv=_exe("th_to_csv"),
        anim_to_vtk=_exe("anim_to_vtk"),
        install_root=None if root_raw in (None, "") else Path(str(root_raw)).expanduser(),
        env={str(k): str(v) for k, v in env_raw.items()},
        source=p,
    )
