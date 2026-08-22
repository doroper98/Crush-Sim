"""FR-04 - OpenRadioss starter/engine deck generation (pure text).

.. warning::

   **DECK SYNTAX MUST BE VALIDATED AGAINST THE PINNED OPENRADIOSS STARTER
   BEFORE ANY PRODUCTION USE.**

   Everything in this module is text generation: it is fully unit-testable
   without a solver, and the tests here only prove that the writer is
   *deterministic and self-consistent*. They cannot prove that the field
   order, the column layout or the option flags match the block format of the
   OpenRadioss release pinned in ``configs/solver.yaml``. The keyword set below
   follows the OpenRadioss block-format reference as understood at authoring
   time, but the reference guide of the pinned tag is the only authority.

   Phase-0 acceptance therefore requires running the starter on a generated
   deck and reaching a clean ``*.out`` with zero errors. Until that has been
   done for a given solver tag, every result produced through this writer is
   ``UNVERIFIED``. Do not "fix" a starter error by guessing: look the keyword
   up in the reference guide for the pinned tag and correct it here, then
   refresh the golden files under ``tests/golden/``.

Deck layout produced (spec §4 FR-04, §5.2, §5.3):

* ``/MAT/LAW2`` from the material card - Johnson-Cook quasi-static terms
  ``(A, B, n)`` mapped from Ludwik-Hollomon ``(sigma_y, K, n)``.
* ``/PROP/SHELL`` with ``Ishell=24`` (QEPH) and 5 through-thickness points.
* ``/INTER/TYPE7`` global contact, friction 0.15 by default.
* ``FLOOR`` - rigid body at Z = 0, all six DOF fixed, common to every case.
* ``REF_TOOL`` - rigid body driven by ``/IMPVEL`` along a velocity ramp.
* ``/TH/RBODY`` time-history output for the reaction forces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

from ..config import CaseConfig, MaterialCard
from ..errors import DeckError
from ..meshing.mesh_data import ShellMesh
from ..units import (
    CONTACT_FRICTION_DEFAULT,
    SHELL_INTEGRATION_POINTS,
    SHELL_ISHELL_FORMULATION,
    SHELL_SHEAR_FACTOR,
    UNIT_SYSTEM,
    m_per_s_to_mm_per_s,
)
from .format import RULER, f20, i10, reals, s10, title

PartRole = Literal["deformable", "floor", "tool"]

DECK_VALIDATION_WARNING: str = (
    "Deck syntax is generated from the OpenRadioss block-format reference and "
    "MUST be validated against the pinned starter before production use."
)
"""Single-sentence warning echoed into every generated deck and report."""

RIGID_MATERIAL_E: float = 210000.0
"""Young's modulus [MPa] of the rigid tool/floor material (nominal steel)."""

RIGID_MATERIAL_NU: float = 0.3
"""Poisson ratio of the rigid tool/floor material."""

RIGID_MATERIAL_RHO: float = 7.85e-9
"""Density [tonne/mm^3] of the rigid tool/floor material (nominal steel)."""

RIGID_SHELL_THICKNESS: float = 0.5
"""Shell thickness [mm] of rigid parts; contact thickness only.

Kept thin so the TYPE7 contact gap ``(t_can + t_rigid) / 2`` stays below the
initial geometric clearances - a 2 mm tool shell put the gap above the 0.5 mm
tool stand-off and the starter rejected the deck with initial penetrations.
"""

INITIAL_CONTACT_CLEARANCE: float = 0.35
"""Initial stand-off [mm] between the can surface and static rigid parts.

Must exceed the scaled TYPE7 contact gap ``gap_scale * (t_can + t_rigid) / 2``
or the starter reports ERROR 612 (initial penetration)."""

_AXIS_NAMES: tuple[str, str, str] = ("X", "Y", "Z")


@dataclass(slots=True)
class DeckPart:
    """One meshed part of the deck with its ids assigned by the writer."""

    name: str
    mesh: ShellMesh
    thickness: float
    role: PartRole
    material: MaterialCard | None = None
    part_id: int = 0
    prop_id: int = 0
    mat_id: int = 0
    node_offset: int = 0
    element_offset: int = 0
    rbody_master_node: int = 0

    @property
    def rigid(self) -> bool:
        """Whether the part is a rigid body (/RBODY)."""
        return self.role in ("floor", "tool")

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "name": self.name,
            "role": self.role,
            "part_id": self.part_id,
            "prop_id": self.prop_id,
            "mat_id": self.mat_id,
            "thickness_mm": self.thickness,
            "material": self.material.key if self.material else None,
            "nodes": self.mesh.n_nodes,
            "elements": self.mesh.n_elements,
            "rigid": self.rigid,
        }


@dataclass(slots=True)
class DriveDefinition:
    """The imposed-displacement drive applied to REF_TOOL."""

    direction: tuple[float, float, float]
    stroke: float
    """Total imposed displacement [mm]."""
    velocity_mm_s: float
    ramp_fraction: float = 0.1
    points: int = 201
    """Number of points in the displacement-vs-time function.

    The engine interpolates the table linearly, so the drive velocity is
    piecewise constant between points. A coarse table (e.g. 21 points) makes
    the velocity a staircase whose jumps hammer the contact hard enough to
    break through the shell; 201 points keeps each jump ~1 % of the drive
    velocity.
    """

    @property
    def ramp_time(self) -> float:
        """Duration of the velocity ease-in [s]."""
        return max(0.0, min(0.99, self.ramp_fraction)) * self.end_time

    @property
    def end_time(self) -> float:
        """Engine end time [s] that delivers exactly ``stroke``.

        The cosine ease-in loses ``v * t_ramp / 2`` of travel relative to a
        step start, so the end time is stretched to compensate; the function
        table then ends exactly on the requested stroke with no jump.
        """
        if self.velocity_mm_s <= 0.0:
            raise DeckError(f"Drive velocity must be > 0 mm/s, got {self.velocity_mm_s!r}")
        fraction = max(0.0, min(0.99, self.ramp_fraction))
        return (self.stroke / self.velocity_mm_s) / (1.0 - 0.5 * fraction)

    def table(self) -> list[tuple[float, float]]:
        """Displacement-vs-time points: cosine ease-in, then constant velocity.

        The smooth start keeps the initial acceleration finite so the kinetic /
        internal energy ratio stays inside the quasi-static gate.
        """
        t_end = self.end_time
        n = max(3, int(self.points))
        ramp = self.ramp_time
        pts: list[tuple[float, float]] = []
        for i in range(n):
            t = t_end * i / (n - 1)
            if ramp > 0.0 and t < ramp:
                # Integral of a cosine velocity ease-in over [0, ramp].
                s = self.velocity_mm_s * (t - (ramp / math.pi) * math.sin(math.pi * t / ramp)) / 2.0
            else:
                s = self.velocity_mm_s * (t - ramp / 2.0) if ramp > 0.0 else self.velocity_mm_s * t
            pts.append((t, min(s, self.stroke)))
        pts[-1] = (t_end, self.stroke)
        return pts

    def velocity_table(self) -> list[tuple[float, float]]:
        """Velocity-vs-time points: cosine ease-in, then constant velocity.

        Used with ``/IMPVEL`` (velocity control): the engine's energy
        bookkeeping for an imposed velocity on a rigid-body master is
        well-behaved, whereas the same drive expressed through ``/IMPDISP``
        accumulated a runaway external-work term on the pinned engine build.
        ``end_time`` already compensates the ramp so the integral of this
        profile is exactly ``stroke``.
        """
        t_end = self.end_time
        n = max(3, int(self.points))
        ramp = self.ramp_time
        pts: list[tuple[float, float]] = []
        for i in range(n):
            t = t_end * i / (n - 1)
            if ramp > 0.0 and t < ramp:
                v = self.velocity_mm_s * (1.0 - math.cos(math.pi * t / ramp)) / 2.0
            else:
                v = self.velocity_mm_s
            pts.append((t, v))
        return pts


@dataclass(slots=True)
class DeckResult:
    """Paths and identifiers of a generated deck."""

    run_name: str
    starter_path: Path
    engine_path: Path
    parts: list[DeckPart]
    drive: DriveDefinition
    end_time: float
    node_count: int
    element_count: int
    material: MaterialCard
    warning: str = DECK_VALIDATION_WARNING

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "run_name": self.run_name,
            "starter": str(self.starter_path),
            "engine": str(self.engine_path),
            "parts": [p.summary() for p in self.parts],
            "end_time_s": self.end_time,
            "stroke_mm": self.drive.stroke,
            "velocity_mm_s": self.drive.velocity_mm_s,
            "direction": list(self.drive.direction),
            "nodes": self.node_count,
            "elements": self.element_count,
            "material": self.material.key,
            "material_verified": self.material.is_verified,
            "unit_system": UNIT_SYSTEM,
            "warning": self.warning,
        }


class RadiossDeckWriter:
    """Assemble and serialise an OpenRadioss starter/engine deck pair.

    The writer owns all id assignment: node ids and element ids are made unique
    across parts, and every keyword block gets a deterministic id so golden-file
    tests can pin the output byte for byte.
    """

    def __init__(
        self,
        *,
        run_name: str,
        parts: list[DeckPart],
        drive: DriveDefinition,
        material: MaterialCard,
        friction: float = CONTACT_FRICTION_DEFAULT,
        gap_scale: float = 0.8,
        stiffness_scale: float = 1.0,
        animation_frames: int = 25,
        time_history_points: int = 500,
        end_time: float | None = None,
        load_case: str = "LC-1",
        description: str = "",
        solver_version_tag: str = "unpinned",
    ) -> None:
        if not parts:
            raise DeckError("A deck needs at least one part")
        roles = [p.role for p in parts]
        if "deformable" not in roles:
            raise DeckError("A deck needs at least one deformable part (the can)")
        if roles.count("tool") != 1:
            raise DeckError("A deck needs exactly one REF_TOOL part")
        self.run_name = run_name
        self.parts = parts
        self.drive = drive
        self.material = material
        self.friction = float(friction)
        self.gap_scale = float(gap_scale)
        self.stiffness_scale = float(stiffness_scale)
        self.animation_frames = max(1, int(animation_frames))
        self.time_history_points = max(1, int(time_history_points))
        self.end_time = float(end_time) if end_time else drive.end_time
        self.load_case = load_case
        self.description = description
        self.solver_version_tag = solver_version_tag
        self._assign_ids()

    # -- id assignment ------------------------------------------------------

    def _assign_ids(self) -> None:
        """Renumber nodes/elements across parts and assign block ids."""
        node_cursor = 0
        element_cursor = 0
        for index, part in enumerate(self.parts, start=1):
            part.part_id = index
            part.prop_id = index
            part.mat_id = index
            part.node_offset = node_cursor
            part.element_offset = element_cursor
            part.mesh = part.mesh.renumber(node_cursor)
            node_cursor += part.mesh.n_nodes
            element_cursor += part.mesh.n_elements
        # Rigid-body master nodes live after every meshed node.
        for part in self.parts:
            if part.rigid:
                node_cursor += 1
                part.rbody_master_node = node_cursor
        self.node_count = node_cursor
        self.element_count = element_cursor

    @property
    def floor(self) -> DeckPart | None:
        """The FLOOR part, if the case defines one."""
        return next((p for p in self.parts if p.role == "floor"), None)

    @property
    def tool(self) -> DeckPart:
        """The REF_TOOL part."""
        return next(p for p in self.parts if p.role == "tool")

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _centroid(mesh: ShellMesh) -> tuple[float, float, float]:
        centre = np.asarray(mesh.nodes, dtype=float).mean(axis=0)
        return (float(centre[0]), float(centre[1]), float(centre[2]))

    def _drive_axis(self) -> tuple[str, int, float]:
        """Resolve the drive direction to ``(axis_name, skew_id, sign)``.

        An axis-aligned direction uses the global frame; anything else gets a
        ``/SKEW/FIX`` whose local X axis is the drive direction.
        """
        d = np.asarray(self.drive.direction, dtype=float)
        norm = float(np.linalg.norm(d))
        if norm <= 0.0:
            raise DeckError("Drive direction must be a non-zero vector")
        d = d / norm
        for axis, name in enumerate(_AXIS_NAMES):
            if abs(abs(d[axis]) - 1.0) < 1.0e-9:
                return name, 0, math.copysign(1.0, d[axis])
        return "X", 1, 1.0

    # -- keyword blocks -----------------------------------------------------

    def _block_header(self) -> list[str]:
        return [
            "#RADIOSS STARTER",
            RULER,
            f"# Generated by crushsim - load case {self.load_case}",
            f"# {DECK_VALIDATION_WARNING}",
            f"# Pinned solver tag: {self.solver_version_tag}",
            f"# Unit system: {UNIT_SYSTEM} (mass Mg, length mm, time s)",
            f"# {self.description}" if self.description else "#",
            RULER,
            "/BEGIN",
            title(self.run_name),
            i10(2022),
            # Unit codes are 20-character fields (begin.cfg: %20s%20s%20s).
            "Mg".rjust(20) + "mm".rjust(20) + "s".rjust(20),
            "Mg".rjust(20) + "mm".rjust(20) + "s".rjust(20),
        ]

    def _block_nodes(self) -> list[str]:
        lines = [RULER, "/NODE", "#  node_ID                   X                   Y                   Z"]
        for part in self.parts:
            lines.append(f"# part: {part.name}")
            for nid, (x, y, z) in zip(part.mesh.node_ids, part.mesh.nodes):
                lines.append(i10(int(nid)) + reals((x, y, z)))
        for part in self.parts:
            if part.rigid:
                cx, cy, cz = self._centroid(part.mesh)
                lines.append(f"# {part.name} rigid-body master node")
                lines.append(i10(part.rbody_master_node) + reals((cx, cy, cz)))
        return lines

    def _block_elements(self, part: DeckPart) -> list[str]:
        lines: list[str] = [RULER]
        eid = part.element_offset
        # Element blocks (/SHELL, /SH3N) take NO title card - validated
        # against the real starter (a title line raises ERROR 100101).
        if part.mesh.n_quads:
            lines.append(f"/SHELL/{part.part_id}")
            lines.append("# shell_ID  node_ID1  node_ID2  node_ID3  node_ID4")
            for quad in part.mesh.quads:
                eid += 1
                lines.append(i10(eid) + "".join(i10(int(n)) for n in quad))
        if part.mesh.n_tris:
            lines.append(f"/SH3N/{part.part_id}")
            lines.append("#  sh3n_ID  node_ID1  node_ID2  node_ID3")
            for tri in part.mesh.tris:
                eid += 1
                lines.append(i10(eid) + "".join(i10(int(n)) for n in tri))
        return lines

    def _block_part(self, part: DeckPart) -> list[str]:
        return [
            RULER,
            f"/PART/{part.part_id}",
            title(part.name),
            "#  prop_ID    mat_ID subset_ID",
            i10(part.prop_id) + i10(part.mat_id) + i10(0),
        ]

    def _block_property(self, part: DeckPart) -> list[str]:
        """/PROP/SHELL - Ishell=24 (QEPH) with 5 integration points for the can."""
        n_points = SHELL_INTEGRATION_POINTS if part.role == "deformable" else 1
        return [
            RULER,
            f"/PROP/SHELL/{part.prop_id}",
            title(f"{part.name}_SHELL"),
            "#   Ishell    Ismstr     ISH3N     Idril"
            "                                                   P_thickfail",
            i10(SHELL_ISHELL_FORMULATION) + i10(0) + i10(2) + i10(0),
            "#                 hm                  hf                  hr"
            "                  dm                  dn",
            reals((0.0, 0.0, 0.0, 0.0, 0.0)),
            # Layout per prop_p1_shell.cfg: N, Istrain, Thick, Ashear, then a
            # 10-column spacer before Ithick and Iplas.
            "#        N   Istrain               Thick              Ashear              Ithick     Iplas",
            i10(n_points)
            + i10(1)
            + reals((part.thickness, SHELL_SHEAR_FACTOR))
            + " " * 10
            + i10(1)
            + i10(1),
        ]

    def _block_material(self, part: DeckPart) -> list[str]:
        """/MAT/LAW2 for the deformable can, /MAT/LAW1 for rigid parts."""
        if part.role == "deformable":
            card = part.material or self.material
            return [
                RULER,
                f"/MAT/LAW2/{part.mat_id}",
                title(f"{card.name} ({card.key})"),
                f"# harvested={card.source} verified={str(card.is_verified).lower()}",
                "#              RHO_I               RHO_O",
                reals((card.rho,)),
                "#                  E                  NU",
                reals((card.E, card.nu)),
                "#                  a                   b                   n"
                "           EPS_p_max           SIG_max0",
                reals((card.law2.A, card.law2.B, card.law2.n, 0.0, 0.0)),
                "#                  c           EPS_DOT_0       ICC   Fsmooth"
                "               F_cut               Chard",
                reals((0.0, 0.0)) + i10(1) + i10(0) + reals((0.0, 0.0)),
                # Thermal softening card is mandatory in the LAW2 layout
                # (matl2_plas_johns.cfg); zeros disable it.
                "#                  m              T_melt              rhoC_p                 T_r",
                reals((0.0, 0.0, 0.0, 0.0)),
            ]
        return [
            RULER,
            f"/MAT/LAW1/{part.mat_id}",
            title(f"{part.name}_RIGID_ELASTIC"),
            "#              RHO_I               RHO_O",
            reals((RIGID_MATERIAL_RHO,)),
            "#                  E                  NU",
            reals((RIGID_MATERIAL_E, RIGID_MATERIAL_NU)),
        ]

    def _block_groups(self) -> list[str]:
        """Node groups and the global contact surface."""
        lines: list[str] = [RULER]
        for part in self.parts:
            lines.append(f"/GRNOD/PART/{part.part_id}")
            lines.append(title(f"{part.name}_NODES"))
            lines.append(i10(part.part_id))
        lines.append(RULER)
        # Separate main surfaces per contact partner so /TH/INTER can report
        # the tool-side reaction force on its own (the crush-force curve).
        lines.append("/SURF/PART/1")
        lines.append(title("TOOL_SURFACE"))
        lines.append("".join(i10(p.part_id) for p in self.parts if p.role == "tool"))
        lines.append("/SURF/PART/2")
        lines.append(title("FIXED_SURFACES"))
        lines.append("".join(i10(p.part_id) for p in self.parts if p.role == "floor"))
        lines.append("/SURF/PART/3")
        lines.append(title("CAN_SURFACE"))
        lines.append("".join(i10(p.part_id) for p in self.parts if not p.rigid))
        lines.append(RULER)
        lines.append("/GRNOD/PART/90")
        lines.append(title("CONTACT_SECONDARY_NODES"))
        # Deformable nodes only: rigid parts stay main-side. Rigid-vs-rigid
        # pairs (e.g. a support edge near the floor plane) otherwise trip the
        # initial-penetration check, and their nodes cannot be relocated.
        lines.append("".join(i10(p.part_id) for p in self.parts if not p.rigid))
        return lines

    def _block_rbody(self, part: DeckPart, rbody_id: int) -> list[str]:
        return [
            RULER,
            f"/RBODY/{rbody_id}",
            title(f"{part.name}_RBODY"),
            "#  node_ID   sens_ID   skew_ID    Ispher                Mass   grnd_ID"
            "     Ikrem      ICoG   surf_ID",
            i10(part.rbody_master_node)
            + i10(0)
            + i10(0)
            + i10(0)
            + f20(0.0)
            + i10(part.part_id)
            + i10(0)
            + i10(1)
            + i10(0),
            "#                Jxx                 Jyy                 Jzz",
            reals((0.0, 0.0, 0.0)),
            "#                Jxy                 Jyz                 Jxz",
            reals((0.0, 0.0, 0.0)),
            # Mandatory trailing card per rbody.cfg (radioss2021).
            "#  Ioptoff   Iexpams     Ifail",
            i10(0) + i10(0) + i10(0),
        ]

    def _block_fixed_bcs(self, part: DeckPart, bcs_id: int) -> list[str]:
        """Fixed rigid part (FLOOR, SUPPORT): all six master DOF locked."""
        return [
            RULER,
            f"/GRNOD/NODE/{80 + bcs_id}",
            title(f"{part.name}_MASTER"),
            i10(part.rbody_master_node),
            f"/BCS/{bcs_id}",
            title(f"{part.name}_FIXED"),
            "#  Tra rot   skew_ID  grnod_ID",
            s10("111 111") + i10(0) + i10(80 + bcs_id),
        ]

    def _block_tool_drive(self) -> list[str]:
        """REF_TOOL: constrain every DOF but the drive axis, then impose displacement."""
        tool = self.tool
        axis, skew_id, sign = self._drive_axis()
        lines: list[str] = [RULER]

        if skew_id:
            d = np.asarray(self.drive.direction, dtype=float)
            d = d / float(np.linalg.norm(d))
            helper = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
            y_axis = np.cross(helper, d)
            y_axis /= float(np.linalg.norm(y_axis))
            origin = self._centroid(tool.mesh)
            lines += [
                f"/SKEW/FIX/{skew_id}",
                title("REF_TOOL_DRIVE_SKEW"),
                "#                 Ox                  Oy                  Oz",
                reals(origin),
                "#                 X1                  Y1                  Z1",
                reals(tuple(float(v) for v in d)),
                "#                 X2                  Y2                  Z2",
                reals(tuple(float(v) for v in y_axis)),
                RULER,
            ]

        # Free DOF: translation along the drive axis only; all rotations fixed.
        trans_code = "".join("0" if _AXIS_NAMES[i] == axis else "1" for i in range(3))
        lines += [
            "/GRNOD/NODE/95",
            title("REF_TOOL_MASTER"),
            i10(tool.rbody_master_node),
            "/BCS/2",
            title("REF_TOOL_GUIDE"),
            "#  Tra rot   skew_ID  grnod_ID",
            s10(f"{trans_code} 111") + i10(skew_id) + i10(95),
            RULER,
            "/FUNCT/1",
            title("REF_TOOL_VELOCITY_RAMP"),
            "#                  X                   Y",
        ]
        for t, v in self.drive.velocity_table():
            lines.append(reals((t, v)))
        lines += [
            RULER,
            "/IMPVEL/1",
            title("REF_TOOL_IMPOSED_VELOCITY"),
            "#funct_IDT       Dir   skew_ID sensor_ID  grnod_ID  frame_ID",
            i10(1) + s10(axis) + i10(skew_id) + i10(0) + i10(95) + i10(0),
            "#            Scale_x             Scale_y              Tstart               Tstop",
            reals((0.0, sign, 0.0, self.end_time)),
        ]
        return lines

    def _block_contact(self) -> list[str]:
        """/INTER/TYPE7 x3 - tool, fixed rigids, and can self-contact.

        Split per partner so the tool interface's /TH/INTER output is the
        crush-force curve directly.
        """
        lines: list[str] = []
        for inter_id, name, surf_id in (
            (1, "TOOL_CONTACT", 1),
            (2, "FIXED_CONTACT", 2),
            (3, "SELF_CONTACT", 3),
        ):
            lines += self._contact_cards(inter_id, name, surf_id)
        return lines

    def _contact_cards(self, inter_id: int, name: str, surf_id: int) -> list[str]:
        """One /INTER/TYPE7 block (card layouts per inter_type7.cfg)."""
        return [
            RULER,
            f"/INTER/TYPE7/{inter_id}",
            title(name),
            # Card layouts per inter_type7.cfg (radioss2018).
            "# grnod_id   surf_id      Istf      Ithe      Igap                Ibag"
            "      Idel     Icurv      Iadm",
            i10(90) + i10(surf_id) + i10(4) + i10(0) + i10(2) + " " * 10 + i10(0) + i10(0) + i10(0) + i10(0),
            "#          Fscalegap             Gap_max             Fpenmax",
            reals((self.gap_scale, 0.0, 0.0)),
            "#              Stmin               Stmax   Percent_mesh_size               dtmin"
            "  Irem_gap   Irem_i2",
            reals((0.0, 0.0, 0.0, 0.0)) + i10(0) + i10(0),
            "#              Stfac                Fric              GAPmin              Tstart"
            "               Tstop",
            reals((self.stiffness_scale, self.friction, 0.0, 0.0, 0.0)),
            # %7s + three 1-digit BC flags + %20s spacer + Inacti + 3 reals.
            # Inacti=6: shrink the contact gap for initially penetrating nodes
            # - without it the handful of unavoidable mesh-roundoff
            # penetrations inject a large contact-energy shock at t=0.
            "#      IBC                        Inacti               VIS_S"
            "               VIS_F              Bumult",
            " " * 7 + "000" + " " * 20 + i10(6) + reals((0.0, 0.0, 0.0)),
            # Mandatory friction-model card (Ifric=0: static Coulomb).
            "#    Ifric    Ifiltr               Xfreq     Iform   sens_ID   fct_IDF"
            "             AscaleF   fric_ID",
            i10(0) + i10(0) + f20(0.0) + i10(0) + i10(0) + i10(0) + f20(0.0) + i10(0),
        ]

    def _block_time_history(self) -> list[str]:
        """/TH/RBODY - reaction forces of the rigid bodies (FR-04)."""
        rigid_parts = [p for p in self.parts if p.rigid]
        # The TH object list is integer ids only, 10 per line (th_rbody.cfg);
        # names are not part of the block format.
        lines = [
            RULER,
            "/TH/RBODY/1",
            title("RBODY_REACTIONS"),
            # DX/DY/DZ are not valid RBODY variables (starter ERROR 260);
            # master-node displacements come from the /TH/NODE group below.
            "#      var       var       var       var",
            "DEF       FX        FY        FZ",
            "#      Obj       Obj",
            "".join(i10(index) for index in range(1, len(rigid_parts) + 1)),
        ]
        lines += [
            RULER,
            "/TH/NODE/3",
            title("RBODY_MASTER_DISPLACEMENTS"),
            "#      var       var       var       var",
            "DEF       DX        DY        DZ",
            # One node per line: node_ID, skew_ID, name (th_node.cfg).
            "#  node_ID   skew_ID node_name",
        ] + [
            i10(p.rbody_master_node) + i10(0) + f"{p.name}_MASTER"
            for p in rigid_parts
        ]
        lines += [
            RULER,
            "/TH/INTER/4",
            title("TOOL_CONTACT_FORCE"),
            # Valid interface variables are FN*/FT* (hm_read_thgrou.F VARIN);
            # FX/FY/FZ are rigid-body variables and are rejected here.
            "#      var       var       var       var       var       var",
            "FNX       FNY       FNZ       FTX       FTY       FTZ",
            "#      Obj",
            i10(1),
        ]
        lines += [
            RULER,
            "/TH/PART/2",
            title("PART_ENERGIES"),
            "#      var       var       var",
            "DEF       IE        KE",
            "#      Obj       Obj       Obj",
            "".join(i10(part.part_id) for part in self.parts),
        ]
        return lines

    # -- serialisation ------------------------------------------------------

    def starter_text(self) -> str:
        """Render the starter deck (``*_0000.rad``) as text."""
        lines: list[str] = []
        lines += self._block_header()
        lines += self._block_nodes()
        for part in self.parts:
            lines += self._block_elements(part)
        for part in self.parts:
            lines += self._block_part(part)
            lines += self._block_property(part)
            lines += self._block_material(part)
        lines += self._block_groups()

        rbody_id = 0
        for part in self.parts:
            if part.rigid:
                rbody_id += 1
                lines += self._block_rbody(part, rbody_id)
        # Fixed rigid parts (floor, support): BCS ids 1, 3, 4, ... - id 2 is
        # reserved for the REF_TOOL guide in _block_tool_drive.
        bcs_id = 0
        for part in self.parts:
            if part.role == "floor":
                bcs_id = 1 if bcs_id == 0 else bcs_id + 1
                if bcs_id == 2:
                    bcs_id = 3
                lines += self._block_fixed_bcs(part, bcs_id)
        lines += self._block_tool_drive()
        lines += self._block_contact()
        lines += self._block_time_history()
        lines += [RULER, "/END"]
        return "\n".join(lines) + "\n"

    def engine_text(self) -> str:
        """Render the engine deck (``*_0001.rad``) as text."""
        dt_anim = self.end_time / self.animation_frames
        dt_th = self.end_time / self.time_history_points
        return (
            "\n".join(
                [
                    "#RADIOSS ENGINE",
                    f"# Generated by crushsim - load case {self.load_case}",
                    f"# {DECK_VALIDATION_WARNING}",
                    "/VERS/2022",
                    f"/RUN/{self.run_name}/1",
                    f20(self.end_time),
                    "# Constant nodal timestep: added mass is gated at "
                    "units.ADDED_MASS_MAX",
                    "/DT/NODA/CST",
                    reals((0.9, 0.0)),
                    "# Animation output interval [s]",
                    "/ANIM/DT",
                    reals((0.0, dt_anim)),
                    "/ANIM/VECT/DISP",
                    "/ANIM/VECT/VEL",
                    "/ANIM/ELEM/VONM",
                    "/ANIM/ELEM/EPSP",
                    # /ANIM/SHELL/TENS/STRESS/ALL crashes the pinned engine
                    # (segfault while parsing); THIC + VONM + EPSP cover FR-06.
                    "/ANIM/SHELL/THIC",
                    "# Time-history output interval [s]",
                    "/TFILE/0",
                    f20(dt_th),
                    "/PRINT/-100",
                    "/END",
                ]
            )
            + "\n"
        )

    def write(self, outdir: str | Path) -> DeckResult:
        """Write the starter and engine decks into ``outdir``.

        Returns:
            A :class:`DeckResult` with both paths and the assembled ids.
        """
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        starter = out / f"{self.run_name}_0000.rad"
        engine = out / f"{self.run_name}_0001.rad"
        starter.write_text(self.starter_text(), encoding="utf-8")
        engine.write_text(self.engine_text(), encoding="utf-8")
        return DeckResult(
            run_name=self.run_name,
            starter_path=starter,
            engine_path=engine,
            parts=self.parts,
            drive=self.drive,
            end_time=self.end_time,
            node_count=self.node_count,
            element_count=self.element_count,
            material=self.material,
        )


def build_deck(
    case: CaseConfig,
    material: MaterialCard,
    *,
    can_mesh: ShellMesh,
    tool_mesh: ShellMesh,
    floor_mesh: ShellMesh | None = None,
    support_mesh: ShellMesh | None = None,
    outdir: str | Path,
    run_name: str | None = None,
    solver_version_tag: str = "unpinned",
) -> DeckResult:
    """Assemble a deck for one case from its meshed parts.

    Args:
        case: The load case.
        material: Material card for the can.
        can_mesh: Deformable can shell mesh.
        tool_mesh: Rigid REF_TOOL shell mesh.
        floor_mesh: Rigid FLOOR shell mesh; omitted only for tests.
        outdir: Directory receiving ``*_0000.rad`` and ``*_0001.rad``.
        run_name: Deck run name; defaults to the case name.
        solver_version_tag: Pinned solver tag, echoed into the deck header.

    Raises:
        DeckError: If the case is inconsistent (zero velocity, no tool, ...).
    """
    parts: list[DeckPart] = [
        DeckPart(
            name="CAN",
            mesh=can_mesh,
            thickness=case.geometry.thickness,
            role="deformable",
            material=material,
        )
    ]
    if floor_mesh is not None:
        parts.append(
            DeckPart(
                name="FLOOR",
                mesh=floor_mesh,
                thickness=RIGID_SHELL_THICKNESS,
                role="floor",
            )
        )
    if support_mesh is not None:
        parts.append(
            DeckPart(
                name="SUPPORT",
                mesh=support_mesh,
                thickness=RIGID_SHELL_THICKNESS,
                role="floor",
            )
        )
    parts.append(
        DeckPart(
            name="REF_TOOL",
            mesh=tool_mesh,
            thickness=RIGID_SHELL_THICKNESS,
            role="tool",
        )
    )

    drive = DriveDefinition(
        direction=case.loading.direction,
        stroke=case.loading.stroke,
        velocity_mm_s=m_per_s_to_mm_per_s(case.loading.velocity_m_s),
        ramp_fraction=case.loading.ramp_fraction,
    )
    writer = RadiossDeckWriter(
        run_name=run_name or case.name,
        parts=parts,
        drive=drive,
        material=material,
        friction=case.contact.friction,
        gap_scale=case.contact.gap_scale,
        stiffness_scale=case.contact.stiffness_scale,
        animation_frames=case.solver.animation_frames,
        time_history_points=case.solver.time_history_points,
        end_time=case.solver.end_time,
        load_case=case.load_case,
        description=case.description,
        solver_version_tag=solver_version_tag,
    )
    return writer.write(outdir)
