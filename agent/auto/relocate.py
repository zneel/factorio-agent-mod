"""Dry drills moved to ore that is left, beside the belts they already feed (one OR-Tools CP-SAT selection).

    places, info = relocate.plan(q, m, plan, live, drills, belts)

drills  dry drills to pick up ({"item","x","y",...}, plan pieces or our own)
belts   built belts of the same mine (live pieces {"item","x","y","direction"}): the new drills drop onto these

Candidates: every free 3x3 spot beside one of the belts, the drill facing the belt (an electric drill drops 2 tiles
ahead of its centre, mines the 5x5 around it). Live data: the ore left under each 5x5 (resource entities), no other
resource in it (a drill mines everything under it and would put it on the belt).
Model: a boolean per candidate; at most one drill per tile (AddAtMostOne); at most as many drills as were picked up;
maximise the ore under the chosen drills. Returns removals (the dry drills, reused) + the new drills.
"""
import math

from ortools.sat.python import cp_model

from auto import geom

DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}

RES_LUA = r"""
local S=game.surfaces[1] local o={}
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},type='resource'}) do
  o[#o+1]=e.name..'|'..math.floor(e.position.x)..'|'..math.floor(e.position.y)..'|'..e.amount end
rcon.print(table.concat(o,';'))
"""


def resources(q, box):
    """(x, y) -> (name, amount) for every resource tile in box (live)."""
    out = {}
    x1, y1, x2, y2 = (int(v) for v in box)
    for x in range(x1, x2, 128):
        for y in range(y1, y2, 128):
            r = q("/silent-command " + (RES_LUA % (x, y, min(x + 128, x2), min(y + 128, y2))).replace("\n", " ")) or ""
            for t in r.strip().split(";"):
                if t:
                    n, a, b, amt = t.split("|")
                    out[(int(a), int(b))] = (n, int(float(amt)))
    return out


def plan(q, m, plan_, live, drills, belts, ore, occupied, min_ore=1500, seconds=10.0, both_sides=False):
    """occupied: tiles that may not be used (live entities, future plan pieces); the dry drills' own tiles are
    freed here. Returns (places, info) or (None, reason)."""
    if not drills or not belts:
        return None, "nothing to move"
    xs = [b["x"] for b in belts]
    ys = [b["y"] for b in belts]
    box = (min(xs) - 6, min(ys) - 6, max(xs) + 7, max(ys) + 7)
    res = resources(q, box)
    freed = set()
    for d in drills:
        freed |= geom.footprint(d)
    occ = set(occupied) - freed
    # the side the drills were on: an ore column's other lane carries the smelter's fuel (a coal column has none)
    sides = {OPP[d.get("direction", "north")] for d in drills}
    cands = []
    for b in belts:
        bt = (math.floor(b["x"]), math.floor(b["y"]))
        d = b.get("direction", "north")
        for side, (sx, sy) in DV.items():
            if side in (d, OPP[d]):
                continue                       # beside the belt, not in line with it
            if not both_sides and side not in sides:
                continue
            cx, cy = bt[0] + 2 * sx, bt[1] + 2 * sy
            p = {"item": "electric-mining-drill", "x": cx + .5, "y": cy + .5, "direction": OPP[side]}
            fp = geom.footprint(p)
            if fp & occ or any(m.t(*t) in "~C#" for t in fp):
                continue
            area = [(cx + i, cy + j) for i in range(-2, 3) for j in range(-2, 3)]
            names = {res[t][0] for t in area if t in res}
            if names != {ore}:
                continue                       # nothing left, or another resource would ride along
            amt = sum(res[t][1] for t in area if t in res)
            if amt >= min_ore:
                cands.append((p, fp, amt))
    if not cands:
        return None, f"no spot beside the belts with {ore} left"
    md = cp_model.CpModel()
    x = [md.NewBoolVar(f"c{k}") for k in range(len(cands))]
    by_tile = {}
    for k, (_, fp, _) in enumerate(cands):
        for t in fp:
            by_tile.setdefault(t, []).append(x[k])
    for lits in by_tile.values():
        if len(lits) > 1:
            md.AddAtMostOne(lits)
    md.Add(sum(x) <= len(drills))
    md.Maximize(sum(int(a // 100) * x[k] for k, (_, _, a) in enumerate(cands)))
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = seconds
    sv.parameters.num_workers = 8
    st = sv.Solve(md)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, "no selection"
    chosen = [cands[k] for k in range(len(cands)) if sv.Value(x[k])]
    places = [dict({k_: v for k_, v in d.items() if k_ in ("item", "x", "y", "direction")}, remove=True)
              for d in drills]
    places += [p for p, _, _ in chosen]
    return places, (f"{len(chosen)} of {len(drills)} drills re-placed on {sum(a for *_, a in chosen) // 1000}k "
                    f"{ore} ({len(cands)} spots, {sv.StatusName(st)})")
