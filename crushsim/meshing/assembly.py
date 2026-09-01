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

from ..errors import GateFailure, MeshingError
from ..geometry.skin import (
    SkinResult,
    extract_shell_skins,
    gauge_elements,
    shell_mass_error,
)
from ..units import MESH_TARGET_SIZE_DEFAULT_MM
from .gates import GateResult, evaluate_idealisation_gate
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
    gate: GateResult | None = None
    """Idealisation verdict for this part."""

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "name": self.name,
            "thickness_mm": self.thickness_mm,
            "elements": self.mesh.n_elements,
            "quads": self.mesh.n_quads,
            "tris": self.mesh.n_tris,
            "skin": self.skin.summary(),
            "gate": self.gate.to_dict() if self.gate else None,
        }


@dataclass(slots=True)
class AssemblyMesh:
    """A welded assembly: shared nodes, one element group per part."""

    parts: list[AssemblyPart] = field(default_factory=list)
    node_count: int = 0
    shared_nodes: int = 0
    """Nodes referenced by more than one part - the welds."""
    weld_counts: dict[str, int] = field(default_factory=dict)
    """Shared-node count per declared weld pair."""
    weld_seam_fractions: dict[str, float] = field(default_factory=dict)
    """Shared boundary fraction per welded part - the value the gate judges."""

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
            "weld_counts": dict(self.weld_counts),
            "weld_seam_fractions": dict(self.weld_seam_fractions),
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
    welds: list[tuple[str, str]] | None = None,
    per_element_thickness: bool = True,
    enforce_gate: bool = True,
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
        welds: Part pairs the case says are welded. Each is checked for shared
            nodes and a pair that shares none is an error, not a warning: an
            unwelded foil is an island that flies off under pressure while the
            run still reports a burst. Measured on the validation cell the vent
            welded to nothing until ``coplanar`` was applied, and only the
            shared-node count showed it.
        per_element_thickness: Carry each face's gauged thickness onto its
            elements instead of one value per part. A single number is a
            fiction for a real part - pockets cut for weight, pads left on the
            load path, and a vent score are all *deliberately* not the nominal
            thickness, and the part median erases them (measured: the vent's
            0.100 mm score residual disappears into a 0.300 mm median).
        enforce_gate: Stop on a part that did not idealise to a valid shell
            (ADR-06). Turn it off only to inspect a failing import.
        recombine: Recombine into quads.

    Returns:
        An :class:`AssemblyMesh` whose parts share one node array.

    Raises:
        MeshingError: If the solid count and ``names`` disagree, or a part ends
            up with no elements - a part that vanished silently would be absent
            from the deck while the report still listed it (§13.5).
        GateFailure: If a part's shell does not carry its solid's mass, or a
            declared weld shares too little boundary.
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
        thickness_of: dict[int, float] = {}
        for skin, name in zip(skins, names):
            if skin.brep_path is None:  # pragma: no cover - per_solid was requested
                raise MeshingError(f"Solid {skin.index} produced no BREP to import")
            added = gmsh.model.occ.importShapes(str(skin.brep_path))
            gmsh.model.occ.synchronize()
            mine = [tag for dim, tag in added if dim == 2]
            order = sorted(mine, key=lambda tg: -gmsh.model.occ.getMass(2, tg))
            for position, tag in enumerate(order):
                owner[tag] = name
                # Faces come back in import order; skin.face_gauges is keyed by
                # the same kept-face order, so the two line up by area rank.
                thickness_of[tag] = skin.face_gauges.get(position, skin.wall_thickness_mm)
            planes[name] = _plane_height(gmsh, mine)

        shifts: dict[str, float] = {}
        for part, host in (coplanar or {}).items():
            if part not in planes or host not in planes:
                raise MeshingError(
                    f"coplanar maps {part!r} onto {host!r}, but the assembly has "
                    f"{sorted(planes)}"
                )
            shift = planes[host] - planes[part]
            shifts[part] = shift
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
            owner, thickness_of = _remap_owner(surfaces, mapping, owner, thickness_of)

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

        grouped: dict[str, list[tuple[int, np.ndarray, float]]] = {name: [] for name in names}
        for _, tag in gmsh.model.getEntities(2):
            name = owner.get(tag)
            if name is None:
                continue
            gauge = thickness_of.get(tag, 0.0)
            etypes, _, enodes = gmsh.model.mesh.getElements(2, tag)
            for etype, conn in zip(etypes, enodes):
                if etype in (_GMSH_QUAD, _GMSH_TRIANGLE):
                    grouped[name].append((int(etype), np.asarray(conn, dtype=np.int64), gauge))

    assembly = _assemble(
        skins,
        names,
        node_ids,
        nodes,
        grouped,
        str(step_path),
        per_element_thickness,
        shifts,
    )
    _check_welds(assembly, welds or [])
    _check_connectivity(assembly)
    _judge(assembly, enforce_gate)
    return assembly


def _check_connectivity(assembly: AssemblyMesh) -> None:
    """Every part must be one connected body (spec §13.5).

    A part in several pieces is not a coarser model, it is a different
    structure: the loose pieces are free bodies that fly under load while
    every global number still prints. Measured on the validation can before
    the skin was sewn: an 883-element ring left the model at 47 m/s and the
    kinetic energy dwarfed the strain energy it should have stored. The
    islands come from cavity-lining faces the ray test misreads at tangent
    angles; the skin extraction drops them by keeping the largest sewn shell,
    and this check is what keeps that class of defect from ever reaching a
    deck silently again.
    """
    for part in assembly.parts:
        sizes = _component_sizes(part.mesh)
        if len(sizes) > 1:
            raise MeshingError(
                f"Part {part.name!r} meshed into {len(sizes)} disconnected pieces "
                f"(element counts {sizes[:5]}). Loose pieces fly under load; "
                "check the skin extraction for misclassified cavity faces."
            )


def _component_sizes(mesh: ShellMesh) -> list[int]:
    """Element counts of the mesh's connected components, largest first."""
    parent: dict[int, int] = {}

    def find(item: int) -> int:
        root = item
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(item, item) != item:
            parent[item], item = root, parent[item]
        return root

    owner: dict[int, int] = {}
    count = 0
    for block in (mesh.quads, mesh.tris):
        for element in block:
            for raw in element:
                node = int(raw)
                if node in owner:
                    left, right = find(count), find(owner[node])
                    if left != right:
                        parent[left] = right
                else:
                    owner[node] = count
            count += 1
    sizes: dict[int, int] = {}
    for index in range(count):
        root = find(index)
        sizes[root] = sizes.get(root, 0) + 1
    return sorted(sizes.values(), reverse=True)


def _judge(assembly: AssemblyMesh, enforce: bool) -> None:
    """Run the idealisation gate on every part (ADR-06)."""
    failures: list[str] = []
    for part in assembly.parts:
        area = float(_meshed_area(part.mesh))
        thickness = part.mesh.element_thickness
        volume = part.skin.volume_mm3
        # Per-element thickness makes the mass exact rather than nominal: sum
        # area x thickness element by element instead of assuming one gauge.
        if thickness is not None and thickness.size:
            shell_volume = float(_meshed_area(part.mesh, thickness))
            error = (shell_volume - volume) / volume if volume > 0 else float("inf")
        else:
            error = shell_mass_error(
                meshed_area_mm2=area,
                thickness_mm=part.thickness_mm,
                solid_volume_mm3=volume,
            )
        seam = assembly.weld_seam_fractions.get(part.name)
        part.gate = evaluate_idealisation_gate(
            part=part.name,
            mass_error=error,
            weld_seam_fraction=seam,
            info={
                "meshed_area_mm2": area,
                "solid_volume_mm3": volume,
                "thickness_uniformity": part.skin.uniformity,
                "kind": part.skin.kind,
            },
        )
        if not part.gate.passed:
            failures.append(part.gate.describe())
    if failures and enforce:
        raise GateFailure(
            "Shell idealisation failed - these solids did not become valid "
            "shells:\n" + "\n".join(failures)
        )


def _meshed_area(mesh: ShellMesh, weights: np.ndarray | None = None) -> float:
    """Total meshed area, or area x per-element weight when given."""
    index = {int(t): i for i, t in enumerate(mesh.node_ids)}
    total = 0.0
    offset = 0
    for block, corners in ((mesh.quads, 4), (mesh.tris, 3)):
        if block.size == 0:
            continue
        rows = np.vectorize(index.__getitem__)(block)
        pts = mesh.nodes[rows]
        n = np.zeros((pts.shape[0], 3), dtype=float)
        for k in range(corners):
            a, b = pts[:, k], pts[:, (k + 1) % corners]
            n[:, 0] += (a[:, 1] - b[:, 1]) * (a[:, 2] + b[:, 2])
            n[:, 1] += (a[:, 2] - b[:, 2]) * (a[:, 0] + b[:, 0])
            n[:, 2] += (a[:, 0] - b[:, 0]) * (a[:, 1] + b[:, 1])
        area = np.linalg.norm(n, axis=1) / 2.0
        if weights is not None:
            area = area * weights[offset : offset + area.size]
        total += float(area.sum())
        offset += area.size
    return total


def _check_welds(assembly: AssemblyMesh, welds: list[tuple[str, str]]) -> None:
    """Measure each declared weld, and refuse one that shares nothing.

    Counting shared nodes is not enough on its own: two parts grazing at a
    corner share one node and would pass, while the foil is still free to lift
    off. What matters is how much of the *boundary* is shared, so the seam is
    measured as a length and judged by the idealisation gate. Zero shared
    nodes is still refused here and immediately - past that point the part is
    simply not attached, and every later number would describe a different
    model than the one the case asked for (§13.5).
    """
    for left, right in welds:
        host, guest = assembly.part(left), assembly.part(right)
        a, b = _node_set(host), _node_set(guest)
        shared = len(a & b)
        assembly.weld_counts[f"{left}-{right}"] = shared
        if shared == 0:
            raise MeshingError(
                f"{left} and {right} are declared welded but share no nodes, so "
                f"{right} is a free body in the model. Their shells are not "
                "touching after idealisation - check the gap between them, and "
                f"whether the case should map {right} onto {left}'s plane."
            )
        assembly.weld_seam_fractions[right] = _seam_fraction(guest, a & b)


def _seam_fraction(part: AssemblyPart, shared: set[int]) -> float:
    """Shared length of this part's free boundary, as a fraction of it.

    The free boundary is the part's own rim - the edges its elements use once.
    A foil welded all round has essentially all of it shared; one touching at
    a corner has almost none.
    """
    counts: dict[tuple[int, int], int] = {}
    for block, corners in ((part.mesh.quads, 4), (part.mesh.tris, 3)):
        for element in block:
            for k in range(corners):
                lo, hi = int(element[k]), int(element[(k + 1) % corners])
                key = (lo, hi) if lo < hi else (hi, lo)
                counts[key] = counts.get(key, 0) + 1
    index = {int(t): i for i, t in enumerate(part.mesh.node_ids)}
    rim = welded = 0.0
    for (lo, hi), seen in counts.items():
        if seen != 1:
            continue
        length = float(
            np.linalg.norm(part.mesh.nodes[index[lo]] - part.mesh.nodes[index[hi]])
        )
        rim += length
        if lo in shared and hi in shared:
            welded += length
    return welded / rim if rim > 0 else 0.0


def _node_set(part: AssemblyPart) -> set[int]:
    """Node ids this part's elements reference."""
    blocks = [part.mesh.quads.ravel(), part.mesh.tris.ravel()]
    return set(np.unique(np.concatenate(blocks)).tolist())


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
    thickness_of: dict[int, float],
) -> tuple[dict[int, str], dict[int, float]]:
    """Carry part ownership across an ``occ.fragment``.

    Fragment returns, for each input entity, the output entities it became.
    Ownership follows that map; a fragment shared by two inputs keeps the
    first owner, which is correct for a weld seam - it belongs to both and the
    node is shared either way.
    """
    remapped: dict[int, str] = {}
    gauges: dict[int, float] = {}
    for (dim, tag), children in zip(before, mapping):
        if dim != 2:
            continue
        name = owner.get(tag)
        if name is None:
            continue
        for child_dim, child_tag in children:
            if child_dim == 2:
                remapped.setdefault(child_tag, name)
                gauges.setdefault(child_tag, thickness_of.get(tag, 0.0))
    return remapped, gauges


def _gauge_part(
    source: str,
    skin: SkinResult,
    node_ids: np.ndarray,
    nodes: np.ndarray,
    quads: np.ndarray,
    tris: np.ndarray,
    shift: float,
) -> np.ndarray:
    """Per-element thickness for one part, gauged element by element.

    A part moved onto another's plane by ``coplanar`` no longer sits where its
    solid is, so the rays are cast at the *original* location and the result
    carried back. Without this the vent - the one part whose thickness varies
    in the way that matters - gauges against empty space and comes back
    uniformly nominal, score and all.
    """
    index = {int(t): i for i, t in enumerate(node_ids)}
    centroids: list[np.ndarray] = []
    normals: list[np.ndarray] = []
    for block, corners in ((quads, 4), (tris, 3)):
        if block.size == 0:
            continue
        rows = np.vectorize(index.__getitem__)(block)
        pts = nodes[rows]
        middle = pts.mean(axis=1)
        if shift:
            middle = middle - np.array([0.0, 0.0, shift])
        centroids.append(middle)
        n = np.zeros((pts.shape[0], 3), dtype=float)
        for k in range(corners):
            a, b = pts[:, k], pts[:, (k + 1) % corners]
            n[:, 0] += (a[:, 1] - b[:, 1]) * (a[:, 2] + b[:, 2])
            n[:, 1] += (a[:, 2] - b[:, 2]) * (a[:, 0] + b[:, 0])
            n[:, 2] += (a[:, 0] - b[:, 0]) * (a[:, 1] + b[:, 1])
        normals.append(n)
    if not centroids:
        return np.zeros(0, dtype=float)
    return gauge_elements(
        source,
        skin.index,
        np.vstack(centroids),
        np.vstack(normals),
        fallback_mm=skin.wall_thickness_mm,
    )


def _assemble(
    skins: list[SkinResult],
    names: list[str],
    node_ids: np.ndarray,
    nodes: np.ndarray,
    grouped: dict[str, list[tuple[int, np.ndarray, float]]],
    source: str,
    per_element_thickness: bool,
    shifts: dict[str, float],
) -> AssemblyMesh:
    """Build one :class:`ShellMesh` per part over the shared node array."""
    parts: list[AssemblyPart] = []
    used: dict[str, set[int]] = {}
    for skin, name in zip(skins, names):
        quads = np.zeros((0, 4), dtype=np.int64)
        tris = np.zeros((0, 3), dtype=np.int64)
        quad_t: list[float] = []
        tri_t: list[float] = []
        for etype, conn, gauge in grouped.get(name, []):
            block = conn.reshape(-1, 4 if etype == _GMSH_QUAD else 3)
            width = gauge if gauge > 0.0 else skin.wall_thickness_mm
            if etype == _GMSH_QUAD:
                quads = np.vstack([quads, block]) if quads.size else block
                quad_t += [width] * block.shape[0]
            else:
                tris = np.vstack([tris, block]) if tris.size else block
                tri_t += [width] * block.shape[0]
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
                    # Quads first then tris - the order the deck writer emits.
                    element_thickness=(
                        _gauge_part(
                            source, skin, node_ids, nodes, quads, tris, shifts.get(name, 0.0)
                        )
                        if per_element_thickness
                        else None
                    ),
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
