"""Stage 4: where every module goes on the map.

Fixed first, from the map: drill columns on the patches (auto.mines), pumpjacks on wells, the steam plant and the
water pumps at the shore. Then CP-SAT places the movable modules (smelter units, steel, bricks, blocks, labs, oil,
silo) as rectangles with a routing margin: no overlap with each other, with fixed modules, or with obstacles
(water, cliffs, unused ore); minimise sum over flows of rate x Manhattan distance between module centres.
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from auto import geom  # noqa: E402

MARGIN = 4   # free tiles kept around every module for belts, pipes and poles
CELL = 4     # obstacle raster resolution


def obstacle_rects(m, window, blocked):
    """Blocked CELLxCELL cells (water, cliff, ore, any fixed module tile) merged into row runs: disjoint boxes."""
    x1, y1, x2, y2 = window
    rects = []
    for cy in range(y1, y2, CELL):
        run = None
        for cx in range(x1, x2 + CELL, CELL):
            bad = False
            if cx < x2:
                bad = any(m.t(x, y) in "~C#" or m.o(x, y) != "." or (x, y) in blocked
                          for y in range(cy, cy + CELL) for x in range(cx, cx + CELL))
            if bad and run is None:
                run = cx
            elif not bad and run is not None:
                rects.append((run, cy, cx, cy + CELL))
                run = None
    return rects


def solve(movable, fixed, flows, m, window, seconds=60):
    """movable: [{"name", "w", "h"}]; fixed: [{"name", "x1", "y1", "x2", "y2"}] (no margin added);
    flows: [(a, b, weight)] by module name. Returns {name: (x, y)} top-left positions."""
    from ortools.sat.python import cp_model
    md = cp_model.CpModel()
    x1, y1, x2, y2 = window
    pos, xi, yi = {}, [], []
    for mo in movable:
        w, h = mo["w"] + 2 * MARGIN, mo["h"] + 2 * MARGIN
        x = md.NewIntVar(x1, x2 - w, f"x_{mo['name']}")
        y = md.NewIntVar(y1, y2 - h, f"y_{mo['name']}")
        pos[mo["name"]] = (x, y, w, h)
        xi.append(md.NewFixedSizeIntervalVar(x, w, ""))
        yi.append(md.NewFixedSizeIntervalVar(y, h, ""))
    centres = {}
    for mo in movable:
        x, y, w, h = pos[mo["name"]]
        centres[mo["name"]] = (2 * x + w, 2 * y + h)   # doubled to stay integer
    blocked = set()
    for f in fixed:
        centres[f["name"]] = (f["x1"] + f["x2"], f["y1"] + f["y2"])
        for x in range(f["x1"] - 1, f["x2"] + 1):
            for y in range(f["y1"] - 1, f["y2"] + 1):
                blocked.add((x, y))
    for r in obstacle_rects(m, window, blocked):
        xi.append(md.NewFixedSizeIntervalVar(r[0], r[2] - r[0], ""))
        yi.append(md.NewFixedSizeIntervalVar(r[1], r[3] - r[1], ""))
    md.AddNoOverlap2D(xi, yi)
    terms = []
    span = 4 * (x2 - x1 + y2 - y1)
    for a, b, wgt in flows:
        if a not in centres or b not in centres or a == b:
            continue
        (ax, ay), (bx, by) = centres[a], centres[b]
        dx = md.NewIntVar(0, span, "")
        dy = md.NewIntVar(0, span, "")
        md.AddAbsEquality(dx, ax - bx)
        md.AddAbsEquality(dy, ay - by)
        terms.append(int(round(wgt * 10)) * (dx + dy))
    md.Minimize(sum(terms))
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = seconds
    sv.parameters.num_workers = 8
    st = sv.Solve(md)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("placement: no solution (window too small?)")
    return {n: (sv.Value(v[0]) + MARGIN, sv.Value(v[1]) + MARGIN) for n, v in pos.items()}, sv.StatusName(st)
