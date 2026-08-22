"""Shared pytest fixtures.

Meshes used by the deck tests are hand-built and tiny so golden files stay
readable and byte-stable; the meshing tests exercise the real Gmsh path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from crushsim.config import CaseConfig, Law2Params, MaterialCard, load_case
from crushsim.meshing.mesh_data import ShellMesh

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_STEP = REPO_ROOT / "examples" / "step" / "Honda_Can.stp"

LEGACY_MATERIAL_CANDIDATES = (
    # Fetched by scripts/fetch_legacy.* (git-ignored, so absent in CI).
    REPO_ROOT / "legacy_ref" / "src" / "engine" / "MaterialModel.ts",
    # Development checkout of the legacy repository next to this one.
    REPO_ROOT.parent / "doroper98" / "can_crush_sim" / "src" / "engine" / "MaterialModel.ts",
    Path.home() / "doroper98" / "can_crush_sim" / "src" / "engine" / "MaterialModel.ts",
)
"""Where the read-only legacy material library may live, in preference order."""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--update-golden`` for the deck snapshot tests."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite the golden deck files instead of comparing against them.",
    )


@pytest.fixture(scope="session")
def update_golden(pytestconfig: pytest.Config) -> bool:
    """Whether golden files should be rewritten."""
    return bool(pytestconfig.getoption("--update-golden"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def legacy_material_file() -> Path:
    """The real legacy ``MaterialModel.ts``; skips when it is not checked out."""
    for candidate in LEGACY_MATERIAL_CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.skip(
        "Legacy reference not available. Run scripts/fetch_legacy.sh to clone it "
        "into legacy_ref/ (it is git-ignored, so CI skips these tests)."
    )


@pytest.fixture(scope="session")
def example_step() -> Path:
    """A CATIA-exported STEP example; skips when the examples are absent."""
    if not EXAMPLE_STEP.is_file():
        pytest.skip(f"Example STEP not available at {EXAMPLE_STEP}")
    return EXAMPLE_STEP


@pytest.fixture
def material_card() -> MaterialCard:
    """A deterministic aluminium card in the working unit system."""
    return MaterialCard(
        key="test_al",
        name="Test Aluminium",
        E=69000.0,
        nu=0.33,
        rho=2.73e-9,
        sigma_y=145.0,
        uts=150.0,
        K=6.756,
        n=0.25,
        t_default=0.1,
        law2=Law2Params(A=145.0, B=6.756, n=0.25),
        verified=False,
        source="test-fixture",
    )


def _plate_mesh(
    name: str,
    origin: tuple[float, float, float],
    du: tuple[float, float, float],
    dv: tuple[float, float, float],
    nu: int = 2,
    nv: int = 2,
) -> ShellMesh:
    """Build a structured quad plate mesh - deterministic, tiny, no Gmsh."""
    o = np.asarray(origin, dtype=float)
    u = np.asarray(du, dtype=float)
    v = np.asarray(dv, dtype=float)
    nodes = []
    for j in range(nv + 1):
        for i in range(nu + 1):
            nodes.append(o + u * (i / nu) + v * (j / nv))
    quads = []
    stride = nu + 1
    for j in range(nv):
        for i in range(nu):
            n0 = j * stride + i + 1
            quads.append([n0, n0 + 1, n0 + stride + 1, n0 + stride])
    return ShellMesh(
        node_ids=np.arange(1, len(nodes) + 1, dtype=np.int64),
        nodes=np.array(nodes, dtype=float),
        quads=np.array(quads, dtype=np.int64),
        tris=np.zeros((0, 3), dtype=np.int64),
        name=name,
        source=f"fixture:{name}",
    )


@pytest.fixture
def can_mesh_fixture() -> ShellMesh:
    """A tiny four-quad 'can wall' patch standing on Z = 0."""
    return _plate_mesh("CAN", (33.0, -10.0, 0.0), (0.0, 20.0, 0.0), (0.0, 0.0, 40.0))


@pytest.fixture
def floor_mesh_fixture() -> ShellMesh:
    """A tiny floor plate in the Z = 0 plane."""
    return _plate_mesh("FLOOR", (-50.0, -50.0, 0.0), (100.0, 0.0, 0.0), (0.0, 100.0, 0.0))


@pytest.fixture
def tool_mesh_fixture() -> ShellMesh:
    """A tiny tool plate above the can (LC-1 platen position)."""
    return _plate_mesh("REF_TOOL", (-50.0, -50.0, 40.5), (100.0, 0.0, 0.0), (0.0, 100.0, 0.0))


@pytest.fixture
def radial_tool_mesh_fixture() -> ShellMesh:
    """A tiny tool plate beside the can (LC-2 jig position)."""
    return _plate_mesh("REF_TOOL", (-33.5, -50.0, 0.0), (0.0, 100.0, 0.0), (0.0, 0.0, 40.0))


def write_case(tmp_path: Path, name: str, payload: dict) -> Path:
    """Write a case YAML into ``tmp_path`` and return its path."""
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def make_case(tmp_path: Path):
    """Factory writing a case YAML and returning the loaded :class:`CaseConfig`."""

    def _make(name: str = "case", **overrides) -> CaseConfig:
        payload: dict = {
            "name": name,
            "load_case": "LC-1",
            "description": "test case",
            "geometry": {
                "kind": "parametric_can",
                "radius": 33.0,
                "height": 115.0,
                "thickness": 0.1,
            },
            "material": {"key": "test_al"},
            "mesh": {"target_size": 6.0},
            "loading": {
                "tool": "platen",
                "direction": [0.0, 0.0, -1.0],
                "stroke": 40.0,
                "velocity": 2.0,
                "ramp_fraction": 0.1,
            },
            "contact": {"friction": 0.15},
            "solver": {"threads": 2, "animation_frames": 25, "time_history_points": 500},
            "output": {"dir": str(tmp_path / "runs" / name)},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                payload[key] = {**payload[key], **value}
            else:
                payload[key] = value
        return load_case(write_case(tmp_path, name, payload))

    return _make
