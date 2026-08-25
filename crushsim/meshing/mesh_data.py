"""In-memory shell mesh container shared by the mesher, the deck writer and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import MeshingError


@dataclass(slots=True)
class ShellMesh:
    """A quad-dominant shell mesh in the mm-s-tonne-N-MPa system.

    Attributes:
        node_ids: ``(N,)`` 1-based node identifiers as written to the deck.
        nodes: ``(N, 3)`` node coordinates [mm].
        quads: ``(Q, 4)`` quad connectivity, entries are node *ids*.
        tris: ``(T, 3)`` triangle connectivity, entries are node *ids*.
        name: Part name used in deck comments and file names.
        source: Provenance string (parametric definition or STEP path).
    """

    node_ids: np.ndarray
    nodes: np.ndarray
    quads: np.ndarray
    tris: np.ndarray
    name: str = "part"
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.node_ids = np.asarray(self.node_ids, dtype=np.int64).reshape(-1)
        self.nodes = np.asarray(self.nodes, dtype=float).reshape(-1, 3)
        self.quads = np.asarray(self.quads, dtype=np.int64).reshape(-1, 4)
        self.tris = np.asarray(self.tris, dtype=np.int64).reshape(-1, 3)
        if self.node_ids.shape[0] != self.nodes.shape[0]:
            raise MeshingError(
                f"Mesh {self.name!r}: {self.node_ids.shape[0]} node ids for "
                f"{self.nodes.shape[0]} coordinates"
            )
        if self.node_ids.size == 0:
            raise MeshingError(f"Mesh {self.name!r} has no nodes")
        if self.n_elements == 0:
            raise MeshingError(f"Mesh {self.name!r} has no shell elements")

    @property
    def n_nodes(self) -> int:
        """Number of nodes."""
        return int(self.nodes.shape[0])

    @property
    def n_quads(self) -> int:
        """Number of quadrilateral shells."""
        return int(self.quads.shape[0])

    @property
    def n_tris(self) -> int:
        """Number of triangular shells."""
        return int(self.tris.shape[0])

    @property
    def n_elements(self) -> int:
        """Total number of shell elements."""
        return self.n_quads + self.n_tris

    @property
    def triangle_fraction(self) -> float:
        """Triangles as a fraction of all shell elements."""
        return self.n_tris / self.n_elements if self.n_elements else 0.0

    def bounding_box(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Axis-aligned bounding box ``(min, max)`` [mm]."""
        lo = self.nodes.min(axis=0)
        hi = self.nodes.max(axis=0)
        return (tuple(lo.tolist()), tuple(hi.tolist()))  # type: ignore[return-value]

    def node_index(self) -> dict[int, int]:
        """Map node id -> row index in :attr:`nodes`."""
        return {int(nid): i for i, nid in enumerate(self.node_ids)}

    def edges(self) -> dict[tuple[int, int], int]:
        """Count how many shells reference each undirected edge."""
        counts: dict[tuple[int, int], int] = {}
        for block, size in ((self.quads, 4), (self.tris, 3)):
            for element in block:
                for i in range(size):
                    a = int(element[i])
                    b = int(element[(i + 1) % size])
                    key = (a, b) if a < b else (b, a)
                    counts[key] = counts.get(key, 0) + 1
        return counts

    def free_edge_count(self) -> int:
        """Edges referenced by exactly one shell (open rims are legitimate)."""
        return sum(1 for c in self.edges().values() if c == 1)

    def non_manifold_edge_count(self) -> int:
        """Edges referenced by more than two shells (always a defect)."""
        return sum(1 for c in self.edges().values() if c > 2)

    def translate(self, offset: tuple[float, float, float]) -> None:
        """Translate all nodes in place by ``offset`` [mm]."""
        self.nodes = self.nodes + np.asarray(offset, dtype=float).reshape(1, 3)

    def seat_on_floor(self) -> tuple[float, float, float]:
        """Move the mesh so it rests centred on the floor plane (in place).

        Imported CAD geometry arrives in the coordinates of its source file -
        anywhere in space, in any orientation. The pipeline's convention
        (shared with the parametric can) is: bounding box centred on the Z
        axis, lowest point at Z = 0, with the floor plate just below. This
        translates the mesh into that convention without rotating it - the
        modelled orientation (standing, lying, tilted) is respected.

        Returns:
            The applied ``(dx, dy, dz)`` offset [mm]; also recorded in
            ``metadata["seated_offset_mm"]``.
        """
        lo, hi = self.bounding_box()
        offset = (-(lo[0] + hi[0]) / 2.0, -(lo[1] + hi[1]) / 2.0, -lo[2])
        self.translate(offset)
        self.metadata["seated_offset_mm"] = [round(c, 6) for c in offset]
        return offset

    def orient_outward(self, centre: tuple[float, float, float] | None = None) -> int:
        """Flip shells so every element normal points away from ``centre``.

        Internal-pressure loads (/PLOAD) act along the element normal, so a
        closed can must present a consistent outward orientation - Gmsh's OCC
        surfaces carry arbitrary per-face orientations. Valid for convex
        closed shells (cylindrical or box cans); returns the flip count.
        ``centre`` defaults to the mesh centroid.
        """
        c = np.asarray(centre if centre is not None else self.nodes.mean(axis=0), dtype=float)
        index = self.node_index()
        flipped = 0
        for block in (self.quads, self.tris):
            for element in block:
                pts = self.nodes[[index[int(n)] for n in element]]
                normal = np.cross(pts[1] - pts[0], pts[2] - pts[0])
                if float(np.dot(normal, pts.mean(axis=0) - c)) < 0.0:
                    element[:] = element[::-1]
                    flipped += 1
        return flipped

    def renumber(self, node_offset: int) -> "ShellMesh":
        """Return a copy whose node ids start at ``node_offset + 1``.

        Used to merge the can mesh and the tool mesh into one deck.
        """
        mapping = {int(nid): node_offset + i + 1 for i, nid in enumerate(self.node_ids)}
        new_ids = np.array([mapping[int(n)] for n in self.node_ids], dtype=np.int64)
        remap = np.vectorize(lambda v: mapping[int(v)], otypes=[np.int64])
        return ShellMesh(
            node_ids=new_ids,
            nodes=self.nodes.copy(),
            quads=remap(self.quads) if self.n_quads else self.quads.copy(),
            tris=remap(self.tris) if self.n_tris else self.tris.copy(),
            name=self.name,
            source=self.source,
            metadata=dict(self.metadata),
        )

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for reports and run summaries."""
        lo, hi = self.bounding_box()
        return {
            "name": self.name,
            "source": self.source,
            "nodes": self.n_nodes,
            "elements": self.n_elements,
            "quads": self.n_quads,
            "tris": self.n_tris,
            "triangle_fraction": self.triangle_fraction,
            "free_edges": self.free_edge_count(),
            "non_manifold_edges": self.non_manifold_edge_count(),
            "bbox_min_mm": list(lo),
            "bbox_max_mm": list(hi),
        }


def write_mesh_npz(mesh: ShellMesh, path: str | Path) -> Path:
    """Persist a :class:`ShellMesh` next to the ``.msh`` for downstream stages."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        p,
        node_ids=mesh.node_ids,
        nodes=mesh.nodes,
        quads=mesh.quads,
        tris=mesh.tris,
        name=np.array(mesh.name),
        source=np.array(mesh.source),
    )
    return p


def read_mesh_npz(path: str | Path) -> ShellMesh:
    """Load a :class:`ShellMesh` written by :func:`write_mesh_npz`.

    Raises:
        MeshingError: If the file is missing.
    """
    p = Path(path)
    if not p.is_file():
        raise MeshingError(f"Mesh archive not found: {p}")
    data = np.load(p, allow_pickle=False)
    return ShellMesh(
        node_ids=data["node_ids"],
        nodes=data["nodes"],
        quads=data["quads"],
        tris=data["tris"],
        name=str(data["name"]),
        source=str(data["source"]),
    )
