"""FR-03 - mesh a multi-part STEP assembly as one welded shell model.

A cell is a can, a cap welded into its mouth, and a vent foil welded into the
cap. In a shell model a weld is not geometry, it is *shared nodes*: the parts
are one body where they meet (``docs/HANDOVER.md`` §2). That forces the whole
assembly through a single mesh - meshing the parts separately produces three
node sets that touch nowhere, and the deck writer's ``host`` mechanism, which
merges parts by reusing a host's node block, then has nothing to merge.

So the parts are imported together, ``occ.fragment`` imprints their shared
boundaries so coincident edges become the *same* edge, and one mesh is
generated. Afterwards the elements are split back into parts by which surface
they came from - which is why :func:`crushsim.geometry.skin.extract_shell_skins`
is asked for one BREP per solid.

Every part in the result carries the *full* node array in the same order, as
the deck writer requires of a hosted part.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import MeshingError
from ..geometry.skin import SkinResult, extract_shell_skins
from ..units import MESH_TARGET_SIZE_DEFAULT_MM
from .mesh_data import ShellMesh
from .mesher import _GMSH_QUAD, _GMSH_TRIANGLE, gmsh_session


@dataclass(slots=True)
class AssemblyPart:
    """One part of a welded assembly mesh."""

    name: str
    mesh: ShellMesh
    """Elements of this part, over the assembly's shared node array."""
    thickness_mm: float
    skin: SkinResult

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "name": self.name,
            "thickness_mm": self.thickness_mm,
            "elements": self.mesh.n_elements,
            "quads": self.mesh.n_quads,
            "tris": self.mesh.n_tris,
            "skin": self.skin.summary(),
        }


@dataclass(slots=True)
class AssemblyMesh:
    """A welded assembly: shared nodes, one element group per part."""

    parts: list[AssemblyPart] = field(default_factory=list)
    node_count: int = 0
    shared_nodes: int = 0
    """Nodes referenced by more than one part - the welds."""

    def part(self, name: str) -> AssemblyPart:
        """Look one part up by name.

        Raises:
            MeshingError: If no part carries that name.
        """
        for item in self.parts:
            if item.name == name:
                return item
        raise MeshingError(
            f"No part named {name!r} in the assembly; have {[p.name for p in self.parts]}"
        )

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "nodes": self.node_count,
            "shared_nodes": self.shared_nodes,
            "parts": [p.summary() for p in self.parts],
        }


def mesh_step_assembly(
    step_path: str | Path,
    *,
    names: list[str],
    workdir: str | Path,
    target_size: float = MESH_TARGET_SIZE_DEFAULT_MM,
    local_sizes: dict[str, float] | None = None,
    coplanar: dict[str, str] | None = None,
    recombine: bool = True,
) -> AssemblyMesh:
    """Mesh every solid of ``step_path`` into one welded shell assembly.

    Args:
        step_path: Assembly STEP.
        names: Part name per solid, in file order. Naming is the caller's job
            because only the caller knows what the case means by them; a
            mismatched count raises rather than guessing.
        workdir: Directory for the intermediate BREP files.
        target_size: Element size away from any local refinement [mm].
        local_sizes: Per-part element size overrides, e.g. a finer vent.
        coplanar: ``{part: host}`` pairs to move onto the host's plane before
            meshing. A foil welded into an opening is *in* that opening, but
            each plate idealises to its own mid-plane and those planes sit at
            different heights - on the validation cell the cap lands at z=0 and
            the vent 2 mm below it, so nothing touches and the weld is lost.
            Shell models resolve this the way ``box_can`` builds it: the two
            live on one plane, the foil fills the hole, and their shared
            outline is the weld.
        recombine: Recombine into quads.

    Returns:
        An :class:`AssemblyMesh` whose parts share one node array.

    Raises:
        MeshingError: If the solid count and ``names`` disagree, or a part ends
            up with no elements - a part that vanished silently would be absent
            from the deck while the report still listed it (§13.5).
    """
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    skins = extract_shell_skins(step_path, work / "skin.brep", per_solid=True)
    if len(skins) != len(names):
        raise MeshingError(
            f"{step_path} has {len(skins)} solid(s) but {len(names)} name(s) were "
            f"given ({names}). Name every solid, in file order."
        )

    sizes = dict(local_sizes or {})
    with gmsh_session() as gmsh:
        owner: dict[int, str] = {}
        planes: dict[str, float] = {}
        for skin, name in zip(skins, names):
            if skin.brep_path is None:  # pragma: no cover - per_solid was requested
                raise MeshingError(f"Solid {skin.index} produced no BREP to import")
            added = gmsh.model.occ.importShapes(str(skin.brep_path))
            gmsh.model.occ.synchronize()
            mine = [tag for dim, tag in added if dim == 2]
            for tag in mine:
                owner[tag] = name
            planes[name] = _plane_height(gmsh, mine)

        for part, host in (coplanar or {}).items():
            if part not in planes or host not in planes:
                raise MeshingError(
                    f"coplanar maps {part!r} onto {host!r}, but the assembly has "
                    f"{sorted(planes)}"
                )
            shift = planes[host] - planes[part]
            if shift:
                movable = [(2, tag) for tag, name in owner.items() if name == part]
                gmsh.model.occ.translate(movable, 0.0, 0.0, shift)
                gmsh.model.occ.synchronize()

        # Imprint the parts on each other so a shared boundary becomes one
        # edge and the mesher puts one node there instead of two. Without this
        # the parts touch geometrically and stay separate bodies numerically -
        # a cap resting on a can rather than welded into it.
        surfaces = gmsh.model.getEntities(2)
        if len(surfaces) > 1:
            fragments, mapping = gmsh.model.occ.fragment(surfaces[:1], surfaces[1:])
            gmsh.model.occ.synchronize()
            owner = _remap_owner(surfaces, mapping, owner)

        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.0)
        gmsh.option.setNumber("Mesh.MeshSizeMax", float(target_size))
        for _, tag in gmsh.model.getEntities(2):
            local = sizes.get(owner.get(tag, ""))
            if local:
                boundary = gmsh.model.getBoundary([(2, tag)], oriented=False, recursive=True)
                gmsh.model.mesh.setSize(boundary, float(local))
        if recombine:
            gmsh.option.setNumber("Mesh.RecombineAll", 1)
            gmsh.option.setNumber("Mesh.RecombinationAlgorithm", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.model.mesh.generate(2)
        if recombine:
            gmsh.model.mesh.recombine()

        node_tags, node_coords, _ = gmsh.model.mesh.getNodes()
        node_ids = np.asarray(node_tags, dtype=np.int64)
        nodes = np.asarray(node_coords, dtype=float).reshape(-1, 3)
        if node_ids.size == 0:
            raise MeshingError(f"Meshing {step_path} produced no nodes")

        grouped: dict[str, list[tuple[int, np.ndarray]]] = {name: [] for name in names}
        for _, tag in gmsh.model.getEntities(2):
            name = owner.get(tag)
            if name is None:
                continue
            etypes, _, enodes = gmsh.model.mesh.getElements(2, tag)
            for etype, conn in zip(etypes, enodes):
                if etype in (_GMSH_QUAD, _GMSH_TRIANGLE):
                    grouped[name].append((int(etype), np.asarray(conn, dtype=np.int64)))

    return _assemble(skins, names, node_ids, nodes, grouped, str(step_path))


def _plane_height(gmsh: Any, tags: list[int]) -> float:
    """Z of a part's dominant face - the plane its shell sits on."""
    best_area, best_z = -1.0, 0.0
    for tag in tags:
        area = gmsh.model.occ.getMass(2, tag)
        if area > best_area:
            box = gmsh.model.getBoundingBox(2, tag)
            best_area, best_z = area, 0.5 * (box[2] + box[5])
    return best_z


def _remap_owner(
    before: list[tuple[int, int]],
    mapping: list[list[tuple[int, int]]],
    owner: dict[int, str],
) -> dict[str, Any]:
    """Carry part ownership across an ``occ.fragment``.

    Fragment returns, for each input entity, the output entities it became.
    Ownership follows that map; a fragment shared by two inputs keeps the
    first owner, which is correct for a weld seam - it belongs to both and the
    node is shared either way.
    """
    remapped: dict[int, str] = {}
    for (dim, tag), children in zip(before, mapping):
        if dim != 2:
            continue
        name = owner.get(tag)
        if name is None:
            continue
        for child_dim, child_tag in children:
            if child_dim == 2:
                remapped.setdefault(child_tag, name)
    return remapped


def _assemble(
    skins: list[SkinResult],
    names: list[str],
    node_ids: np.ndarray,
    nodes: np.ndarray,
    grouped: dict[str, list[tuple[int, np.ndarray]]],
    source: str,
) -> AssemblyMesh:
    """Build one :class:`ShellMesh` per part over the shared node array."""
    parts: list[AssemblyPart] = []
    used: dict[str, set[int]] = {}
    for skin, name in zip(skins, names):
        quads = np.zeros((0, 4), dtype=np.int64)
        tris = np.zeros((0, 3), dtype=np.int64)
        for etype, conn in grouped.get(name, []):
            block = conn.reshape(-1, 4 if etype == _GMSH_QUAD else 3)
            if etype == _GMSH_QUAD:
                quads = np.vstack([quads, block]) if quads.size else block
            else:
                tris = np.vstack([tris, block]) if tris.size else block
        if quads.size == 0 and tris.size == 0:
            raise MeshingError(
                f"Part {name!r} came out of the assembly mesh with no elements. "
                "Its surfaces were probably consumed by the fragment; check that "
                "the solids overlap rather than merely touch."
            )
        used[name] = set(np.unique(np.concatenate([quads.ravel(), tris.ravel()])).tolist())
        parts.append(
            AssemblyPart(
                name=name,
                # Every part carries the FULL node array in the same order:
                # that is what lets the deck writer host one part on another
                # and merge their shared nodes into a weld.
                mesh=ShellMesh(
                    node_ids=node_ids,
                    nodes=nodes,
                    quads=quads,
                    tris=tris,
                    name=name,
                    source=source,
                ),
                thickness_mm=skin.wall_thickness_mm,
                skin=skin,
            )
        )

    shared = 0
    if len(used) > 1:
        seen: dict[int, int] = {}
        for owned in used.values():
            for node in owned:
                seen[node] = seen.get(node, 0) + 1
        shared = sum(1 for count in seen.values() if count > 1)
    return AssemblyMesh(parts=parts, node_count=int(node_ids.size), shared_nodes=shared)
