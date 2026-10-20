"""Mining on any patch: one-sided electric drill columns, chosen from the map raster.

A column = a straight belt (vertical or horizontal) with drills on one side facing it. Drills put ore on the belt's
near lane, so the far lane stays free for coal (fed straight into the column's first tile). Every drill's 5x5 mining
area holds only this ore (an electric drill mines all resources under it), all 3x3 tiles are buildable.
Columns are picked greedily (most drills first) without overlapping, until the needed drill count is reached.
"""
import math

DIRS = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def patches(m, code):
    """Connected ore patches of one kind: list of tile sets (scipy.ndimage.label on the ore raster)."""
    import numpy as np
    from scipy import ndimage
    a = np.array([[ch == code for ch in row] for row in m.ore])
    lab, n = ndimage.label(a, structure=np.ones((3, 3)))
    out = []
    for k in range(1, n + 1):
        ys, xs = np.nonzero(lab == k)
        out.append({(int(x) + m.x0, int(y) + m.y0) for x, y in zip(xs, ys)})
    out.sort(key=len, reverse=True)
    return out


def _drill_ok(m, code, cx, cy, taken):
    """Drill centred at (cx+.5, cy+.5) over tiles cx-1..cx+1: buildable, enough ore, pure 5x5 area."""
    n = 0
    for dx in range(-1, 2):
        for dy in range(-1, 2):
            t = (cx + dx, cy + dy)
            if t in taken or m.t(*t) in "~C#":
                return False
            n += m.o(*t) == code
    if n < 5:
        return False
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            o = m.o(cx + dx, cy + dy)
            if o not in (".", code):
                return False
    return True


def column(m, code, axis, line, lo, hi, side, flow, taken):
    """One column. axis 'v': belt at x=line (tiles), drills at x=line-2 (side 'w') or line+2 ('e'), rows lo..hi.
    axis 'h': belt at y=line, drills at y=line-2 ('n') or line+2 ('s'). Drills step 3 with a pole every 2 drills.
    Returns (places, drill_count) or None."""
    places, drills, poles = [], [], []
    p = lo
    k = 0
    while p + 1 <= hi:
        if axis == "v":
            cx, cy = line + (-2 if side == "w" else 2), p + 1
        else:
            cx, cy = p + 1, line + (-2 if side == "n" else 2)
        if _drill_ok(m, code, cx, cy, taken):
            face = {"w": "east", "e": "west", "n": "south", "s": "north"}[side]
            drills.append({"item": "electric-mining-drill", "x": cx + .5, "y": cy + .5, "direction": face})
        if k % 2 == 0:
            # pole row after the first drill of a pair: in the drill lane, between this drill and the next
            px, py = (cx, cy + 2) if axis == "v" else (cx + 2, cy)
            poles.append({"item": "small-electric-pole", "x": px + .5, "y": py + .5})
            p += 4
        else:
            p += 3
        k += 1
    if len(drills) < 2:
        return None
    # keep only poles next to a drill; trim the belt to the drills' span
    poles = [q for q in poles if any(abs(q["x"] - d["x"]) + abs(q["y"] - d["y"]) <= 2.01 for d in drills)]
    if axis == "v":
        span = [int(d["y"] - .5) for d in drills]
        a, b = min(span) - 1, max(span) + 1
        tiles = [(line, y) for y in range(a, b + 1)]
    else:
        span = [int(d["x"] - .5) for d in drills]
        a, b = min(span) - 1, max(span) + 1
        tiles = [(x, line) for x in range(a, b + 1)]
    if flow in ("north", "west"):
        tiles.reverse()
    if any(t in taken or m.t(*t) in "~C#" for t in tiles):
        return None
    belt = [{"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": flow} for x, y in tiles]
    return drills + poles + belt, len(drills)


def footprint(places):
    out = set()
    for q in places:
        w = 3 if q["item"] == "electric-mining-drill" else 1
        x0, y0 = math.floor(q["x"] - w / 2 + .01), math.floor(q["y"] - w / 2 + .01)
        out |= {(x0 + i, y0 + j) for i in range(w) for j in range(w)}
    return out


def plan_columns(m, code, need, toward, taken=frozenset(), max_cols=12, only=None):
    """Columns on the biggest patches of `code` until `need` drills; belts flow toward the point `toward`
    (the base). only: boxes (x1, y1, x2, y2) of the patches the mine plan chose (auto.budget.select): no other patch
    (all of them when none lies in a box). Returns list of {"places", "drills", "start", "end", "flow"}."""
    taken = set(taken)
    cols, have = [], 0
    ps = [p for p in patches(m, code) if len(p) >= 60]    # big enough to hold a column of drills
    if only:
        inb = [p for p in ps if any(b[0] <= x < b[2] and b[1] <= y < b[3] for b in only for x, y in p)]
        ps = inb or ps
    ps.sort(key=lambda p: math.dist(toward, (sum(x for x, _ in p) / len(p), sum(y for _, y in p) / len(p))))
    for patch in ps:
        xs = [x for x, _ in patch]
        ys = [y for _, y in patch]
        cands = []
        for axis in ("v", "h"):
            lines = range(min(xs) - 2, max(xs) + 3) if axis == "v" else range(min(ys) - 2, max(ys) + 3)
            lo, hi = (min(ys) - 1, max(ys) + 1) if axis == "v" else (min(xs) - 1, max(xs) + 1)
            for line in lines:
                for side in (("w", "e") if axis == "v" else ("n", "s")):
                    if axis == "v":
                        flow = "south" if toward[1] > (lo + hi) / 2 else "north"
                    else:
                        flow = "east" if toward[0] > (lo + hi) / 2 else "west"
                    r = column(m, code, axis, line, lo, hi, side, flow, taken)
                    if r:
                        cands.append((r[1], axis, line, side, flow, r[0]))
        cands.sort(key=lambda c: -c[0])
        for n, axis, line, side, flow, places in cands:
            fp = footprint(places)
            if fp & taken:
                continue
            belt = [q for q in places if q["item"] == "transport-belt"]
            cols.append({"places": places, "drills": n, "flow": flow,
                         "start": (belt[0]["x"], belt[0]["y"]), "end": (belt[-1]["x"], belt[-1]["y"])})
            taken |= fp
            # keep the tile beyond the belt end and before its start free (feed in, ore out)
            for q, s in ((belt[-1], 1), (belt[0], -1)):
                dx, dy = DIRS[flow]
                taken.add((math.floor(q["x"]) + s * dx, math.floor(q["y"]) + s * dy))
            have += n
            if have >= need or len(cols) >= max_cols:
                return cols
    return cols
