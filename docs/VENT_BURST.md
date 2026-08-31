# Prismatic vent burst — modelling guide (issue #24)

How the scored vent of a prismatic (Honda-class) cell is modelled, what the
numbers mean, and which knob to turn. Every figure below was measured on
LC-6; the runs that produced it are named so they can be re-run.

## Construction

A production cap is not a scored plate. It is thick aluminium with a
stadium opening, and a hand-pressably thin soft-aluminium foil **welded**
into that opening carries the burst score. The model mirrors this:

| Part | How it is modelled |
|---|---|
| Can + cap | one shell (`geometry.kind: box_can`, `closed_top`); a weld is exact as merged nodes |
| Vent foil | a **separate deformable part** over the stadium, its own material and thickness (`vent.membrane_thickness`, `vent.material`, `vent.eps_p_max`) |
| Weld | the foil shares the can's node block along the stadium outline — merged nodes again |
| Score | per-element thinning to `vent.score_thickness` along the pattern |

`vent.pattern` picks the score layout:

- `perimeter` — the stadium outline. The flap detaches and **ejects** when
  it tears all round (measured on v3). Production caps avoid this.
- `petal_x` — four arcs crossing at the centre, **imprinted into the mesh**
  as real geometry (OCC fragment), so element edges align with the grooves.
  The burst starts at the crossing, tears run outward along the arms, and
  the petals fold back on the unscored welded perimeter: the vent opens
  without throwing a fragment.

`loading.brace_walls` locks the face-normal translation of a box can's two
large faces — the jelly roll and the module end plates. Without it an empty
can balloons and **tears its own walls before the vent opens** (v1: wall
rupture at 1.22 MPa vs the score opening only from 2.36 MPa).

## Reading the result

`crushsim/post/vent_metrics.py` reports two milestones, cached into
`pipeline_summary.json` and shown by the viewer and the UI's result node:

- **initiation** — the first membrane element deletes: a through-crack, a
  pinhole. Gas starts leaking; the flow area is negligible.
- **opening (activation)** — the open flow area first reaches
  `OPENING_AREA_FRACTION` (25 %) of the vent. The petals are free and gas
  can actually leave. **This is the number a vent datasheet's activation
  pressure corresponds to.**

"Every score element has torn" was tried as the opening rule and rejected:
it is not mesh-robust (one lightly-loaded element surviving at an arc tip
suppressed the milestone entirely, 179 of 180), and the count moves with
the mesh while the area does not.

## Mesh: use `mesh.vent_size` 0.5 mm

Element-deletion fracture is mesh-sensitive, so the **score's own**
resolution — not the can's — is what moves the burst pressure.
`mesh.vent_size` refines the flap alone; the can wall, the weld band and
the narrow cap remainder keep their gated sizes (that remainder is at its
best coarse — refining it fails the §7 gate at every size tried).

| `vent_size` | score elements | per mm of arc | initiation | opening | run time |
|---|---|---|---|---|---|
| – (1.2 mm uniform) | 25 | 0.6 | 0.514 MPa | 0.703 MPa | ~12 min |
| **0.5 mm** | 180 | 4.5 | **0.303** | **0.385** | ~25 min |
| 0.3 mm | 553 | 13.8 | 0.287 | 0.366 | ~7.3 h |

The uniform mesh is simply **under-resolved** — a one-element-wide score
cannot localise the tear, and it overpredicts the burst by ~70 %. From
0.5 mm on the answer settles: refining a further 40 % moves it only **5 %**,
the same tolerance B-3 uses. 0.3 mm costs 17× the run time for that 5 %, so
**0.5 mm is the working mesh**; 0.2 mm is refused by the gate (min SICN
0.219, aspect ratio 5.45).

Runs: `lc6_pris_vent_burst_v5{,_medium,_fine}`.

## Caveat: the energy balance

Deleting elements removes their internal energy from the balance, so the
solution gate's energy error grows with the number of deletions: 4.9 % (25
scored) → 9.1 % (180) → 13.0 % (553), against a 5 % limit. These cases
therefore set `solver.stop_on_energy_error: false`, and the UI marks such a
run amber with the number rather than red. It is bookkeeping, not a wrong
answer — but quote it alongside any burst pressure.

## Changing the answer

In order of increasing verification cost:

1. **Case YAML** — vent size, score residual, pattern, pressure ramp, mesh.
   Design questions end here. Wire several mesh/material/loading nodes into
   one solver node in the UI graph to sweep combinations automatically.
2. **Material card** (`configs/materials/`) — `sigma_y`, `uts`, `n` set the
   load level; `eps_p_max` sets when it tears. Both cards in play here are
   `verified: false` (literature), so every report carries a trend-only
   watermark. **A measured tensile curve replaces the card and nothing
   else.**
3. **Gate limits** (`crushsim/units.py`, `OPENING_AREA_FRACTION`) — changes
   what counts as acceptable. Re-run B-1…B-3.
4. **Physics** (`crushsim/deck/writer.py`) — new material laws, boundary
   conditions, loads. Validate against the pinned starter, check a theory
   case, and keep the golden decks byte-identical.

The one open item is at level 4: a mesh-size-dependent failure strain would
let any mesh reproduce the same burst. It is **not** urgent now that 0.5 mm
is shown converged, and it needs measured data to calibrate — a single
measured burst pressure is better spent back-fitting `eps_p_max` at 0.5 mm
(level 2).
