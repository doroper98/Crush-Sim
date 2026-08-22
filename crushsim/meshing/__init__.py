"""FR-03 - Gmsh shell meshing and the §7 quality gates."""

from __future__ import annotations

from .gates import GateMetric, GateResult, evaluate_mesh_gate, evaluate_solution_gate
from .mesh_data import ShellMesh, read_mesh_npz, write_mesh_npz
from .mesher import (
    MeshQuality,
    MeshResult,
    gmsh_session,
    mesh_parametric_can,
    mesh_step_surfaces,
    mesh_tool,
)

__all__ = [
    "GateMetric",
    "GateResult",
    "MeshQuality",
    "MeshResult",
    "ShellMesh",
    "evaluate_mesh_gate",
    "evaluate_solution_gate",
    "gmsh_session",
    "mesh_parametric_can",
    "mesh_step_surfaces",
    "mesh_tool",
    "read_mesh_npz",
    "write_mesh_npz",
]
