"""UI_002 §3 WP1.4 - run-time estimates, and only from measurements.

This table holds **measured runs only**. Every entry names the run it was
timed on and the hardware it was timed on; anything not in the table returns
``available: false`` with a reason, and the execution bar then says
"이 형상의 실행 시간은 아직 추정할 수 없습니다" (UI_001 §8.1) instead of a
number nobody measured. Inventing an ETA is what makes a 14-minute preset get
advertised as 25 minutes and then take 39 (docs/LOG.md §12).
"""

from __future__ import annotations


from typing import Any

#: Multipliers applied to the measured total to get the displayed range.
RANGE_LOW: float = 0.85
RANGE_HIGH: float = 1.2

#: The measured samples. ``total_min`` is mesh-to-report wall time, the number
#: the user waits; ``engine_min`` is the solver stage inside it.
MEASURED: dict[str, dict[str, Any]] = {
    # analysis_005 §1.1 (2026-09-07): score 0.5 mm + solver.end_time 1.8e-3.
    "lc6_preset_30min": {
        "engine_min": 13.6,
        "total_min": 14.4,
        "hardware": "4-core container",
        "run": "lc6_pris_vent_burst_v5_preset",
        "measured_on": "2026-09-07",
        "vs_reference": "+5.7 % / +5.3 % vs 0.3 mm",
        "laptop_calibrated": False,
    },
    # The same model run to the end of the ramp (3.5 ms) - the advanced
    # "개방 이후 찢김까지 계산" option.
    "lc6_full_ramp": {
        "engine_min": 39.4,
        "total_min": 41.0,
        "hardware": "4-core container",
        "run": "lc6_pris_vent_burst_v5_medium",
        "measured_on": "2026-09-07",
        "laptop_calibrated": False,
    },
}

#: GOAL.md: one analysis in 30 minutes on a 4-core laptop. The budget is
#: per analysis, not per submitted batch - UI_002 §2.4's own example marks a
#: 2-case, 24-34 min submission as ``over_budget: false``.
TIME_BUDGET_MIN: float = 30.0


def estimate(sample_id: str | None, *, count: int = 1) -> dict[str, Any]:
    """The UI_002 §2.4 ``estimate`` block for ``count`` runs of one sample.

    Args:
        sample_id: Key into :data:`MEASURED`, or None when no measured sample
            applies to the submitted configuration.
        count: How many runs the submission contains.

    Returns:
        ``{per_run_min, total_min, basis, hardware, over_budget, available,
        reason}``. ``per_run_min``/``total_min`` are None when unavailable -
        never a guess.
    """
    row = MEASURED.get(sample_id or "")
    if row is None:
        return {
            "per_run_min": None,
            "total_min": None,
            "basis": None,
            "hardware": None,
            "over_budget": False,
            "available": False,
            "reason": (
                "이 구성에 대한 실측 자료가 없습니다"
                if sample_id
                else "실측한 프리셋과 일치하지 않는 구성입니다"
            ),
            "sample_id": sample_id,
        }
    total = float(row["total_min"])
    # Rounded to whole minutes at the per-run level, then multiplied: the bar
    # reads "1건 · 예상 12-17분" and "2건 · 24-34분" from the same two numbers
    # (UI_002 §2.4 example), which a per-total rounding would not reproduce.
    low = int(round(total * RANGE_LOW))
    high = int(round(total * RANGE_HIGH))
    return {
        "per_run_min": [low, high],
        "total_min": [low * max(count, 1), high * max(count, 1)],
        "basis": f"{sample_id} 실측 {total:g}분 ×{RANGE_LOW}~{RANGE_HIGH}",
        "hardware": row["hardware"],
        "over_budget": high > TIME_BUDGET_MIN,
        "available": True,
        "reason": None,
        "sample_id": sample_id,
        "measured": dict(row),
    }


def evidence(sample_id: str) -> dict[str, Any] | None:
    """The measured evidence block a capability/preset shows next to its badge."""
    row = MEASURED.get(sample_id)
    return dict(row) if row else None


__all__ = ["MEASURED", "RANGE_HIGH", "RANGE_LOW", "TIME_BUDGET_MIN", "estimate", "evidence"]
