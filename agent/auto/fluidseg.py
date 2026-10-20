"""Fluid segments of a plan (Factorio 2.0). Pipes and pipe-to-ground pairs that join by the game's rules form one
segment; machines and pumps end it. A segment holds still when its pipes span more than EXTENT tiles: the larger side
of their bounding box (utility-constants default_pipeline_extent; on the scratch server, 2026-10-18: a straight pipe of
320 tiles flows, 321 doesn't, a 200 x 200 L flows; the offshore pump is not part of the segment).

    segments(places) -> [set of tiles]        extent(tiles) -> larger bounding-box side in tiles
    long_segments(places) -> [(extent, bbox, pieces)] over EXTENT
    split_route(route, attach) -> the route with pumps, so the segment it joins and its own stay within EXTENT
    pump_headers(...) -> pumps that cut the bus's fluid headers (auto.bus.route_fluids_bus) into short segments
"""
import math

from auto import geom

EXTENT = 320
DV = geom.DV4
UNDER = 10   # pipe-to-ground max_underground_distance


def T(x, y):
    return (math.floor(x), math.floor(y))


def pieces_of(places):
    return {T(q["x"], q["y"]): q for q in places if q["item"] in ("pipe", "pipe-to-ground") and not q.get("remove")}


def sides(q):
    return set(DV.values()) if q["item"] == "pipe" else {DV[q.get("direction", "north")]}


def segments(places, pieces=None):
    """Connected pipe pieces: a pipe joins all 4 sides, a pipe-to-ground its direction side and the nearest
    pipe-to-ground facing back on its other side within UNDER tiles (by position, not list order)."""
    pieces = pieces if pieces is not None else pieces_of(places)
    parent = {t: t for t in pieces}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t
    for t, q in pieces.items():
        for d in sides(q):
            n = (t[0] + d[0], t[1] + d[1])
            if n in pieces and (-d[0], -d[1]) in sides(pieces[n]):
                parent[find(t)] = find(n)
        if q["item"] == "pipe-to-ground":
            d = DV[q.get("direction", "north")]
            for k in range(1, UNDER + 1):
                n = (t[0] - d[0] * k, t[1] - d[1] * k)
                o = pieces.get(n)
                if o and o["item"] == "pipe-to-ground" and DV[o.get("direction", "north")] == (-d[0], -d[1]):
                    parent[find(t)] = find(n)
                    break
    comp = {}
    for t in pieces:
        comp.setdefault(find(t), set()).add(t)
    return list(comp.values())


def bbox(ts):
    xs = [x for x, _ in ts]
    ys = [y for _, y in ts]
    return min(xs), min(ys), max(xs), max(ys)


def size(b):
    return 0 if b is None else max(b[2] - b[0] + 1, b[3] - b[1] + 1)


def union(b, c):
    if b is None:
        return c
    if c is None:
        return b
    return min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3])


def extent(ts):
    return size(bbox(ts)) if ts else 0


def long_segments(places, limit=EXTENT):
    out = []
    for s in segments(places):
        if extent(s) > limit:
            out.append((extent(s), bbox(s), len(s)))
    return sorted(out, reverse=True)


def pump(a, b, flow):
    """Pump on tiles a, b (adjacent), pumping in direction flow."""
    return {"item": "pump", "x": (a[0] + b[0]) / 2 + .5, "y": (a[1] + b[1]) / 2 + .5, "direction": flow}


def _dir(a, b):
    return next(d for d, v in DV.items() if v == (b[0] - a[0], b[1] - a[1]))


def split_route(route, attach, limit=EXTENT, attach_end="goal"):
    """route: pipe pieces in order (start -> goal); the fluid flows toward attach_end, where the route joins pieces
    whose tiles are attach (that segment). Walking out from there, a pump replaces two pipes of a straight stretch
    each time the segment would pass limit. Returns the new piece list, or None when no straight stretch fits."""
    walk = list(range(len(route)))
    if attach_end == "goal":
        walk.reverse()
    tiles = [T(route[i]["x"], route[i]["y"]) for i in walk]
    box = bbox(attach) if attach else None
    seg0 = 0   # first walk index of the current segment
    cuts = []
    for i, t in enumerate(tiles):
        nb = union(box, (t[0], t[1], t[0], t[1]))
        if size(nb) <= limit:
            box = nb
            continue
        best = None   # the pump nearest i: walk[k], walk[k + 1] plain pipes in a straight line with k - 1 and k + 2
        for k in range(i - 2, seg0, -1):
            run = tiles[k - 1:k + 3]
            if len(run) < 4 or any(route[walk[j]]["item"] != "pipe" for j in range(k - 1, min(k + 3, len(tiles)))):
                continue
            d = (run[1][0] - run[0][0], run[1][1] - run[0][1])
            if abs(d[0]) + abs(d[1]) == 1 and all((run[j + 1][0] - run[j][0], run[j + 1][1] - run[j][1]) == d
                                                  for j in range(3)):
                best = k
                break
        if best is None:
            return None
        cuts.append(best)
        seg0 = best + 2
        box = bbox(tiles[seg0:i + 1])
    if not cuts:
        return route
    drop, pumps = set(), {}
    for k in cuts:
        drop |= {walk[k], walk[k + 1]}
        # walking away from the attach end; the fluid comes back: from tile k + 1 to tile k
        pumps[min(walk[k], walk[k + 1])] = pump(tiles[k], tiles[k + 1], _dir(tiles[k + 1], tiles[k]))
    out = []
    for i, q in enumerate(route):
        if i in pumps:
            out.append(pumps[i])
        if i not in drop:
            out.append(q)
    return out


PIPE_EXTENT = 300   # header segments stay under the game's 320 (EXTENT): room for the world link at an inlet
OIL_OUT = ("petroleum-gas", "light-oil", "heavy-oil", "lubricant")   # oil units make these, take water and crude


def pump_headers(places, fluid_of, hx, top, bottom, ports, nets, inlets, limit=PIPE_EXTENT):
    """Pumps in every header whose segment (the header and all pieces piped onto it) would span more than limit
    tiles. A pump replaces two header pipes on rows no piece joins, as far down as the segment above still fits; it
    pumps from the side with the fluid's sources toward the side whose sinks have none (inlets and oil units making
    the fluid are sources, ports by their flow, oil units taking it are sinks). Returns (pumps, tiles, fails)."""
    pieces = pieces_of(places)
    role = {}   # tile -> "src" / "snk"
    for p in ports:
        role[(math.floor(p["x"]), math.floor(p["y"]), p["fluid"])] = "src" if p["io"] == "output" else "snk"
    for nw in nets.values():
        for f, ts in nw.items():
            for t in ts:
                role[(t[0], t[1], f)] = "src" if f in OIL_OUT else "snk"
    pumps, taken, fails = [], set(), []
    for f, col in hx.items():
        H = {(col, y) for y in range(top[f], bottom[f] + 1)}
        fp = {t: q for t, q in pieces.items() if fluid_of.get(t) == f and t not in H}
        comps = []   # (first, last header row it joins, bbox, makes f, takes f)
        for c in segments(None, fp):
            R = {(t[1] + d[1]) for t in c for d in sides(fp[t]) if (t[0] + d[0], t[1] + d[1]) in H}
            if R:
                rs = {role.get((t[0], t[1], f)) for t in c}
                comps.append((min(R), max(R), bbox(c), "src" in rs, "snk" in rs))
        box = (col, top[f], col, bottom[f])
        for c in comps:
            box = union(box, c[2])
        if size(box) <= limit:
            continue
        plain = {t for t in H if pieces.get(t, {}).get("item") == "pipe"}
        a, cuts = top[f], []
        while True:
            rest = [c for c in comps if c[0] >= a]
            box = (col, a, col, bottom[f])
            for c in rest:
                box = union(box, c[2])
            if size(box) <= limit:
                break
            best, box = None, None
            for y in range(a + 1, bottom[f] - 1):   # pump on rows y, y + 1; header pipes stay on y - 1 and y + 2
                box = union(box, (col, y - 1, col, y - 1))
                for c in rest:
                    if c[1] == y - 1:
                        box = union(box, c[2])
                if size(box) > limit:
                    break
                if any(c[0] <= y + 1 and c[1] >= y for c in rest):   # a piece joins there or on both sides
                    continue
                if all((col, yy) in plain for yy in range(y - 1, y + 3)):
                    best = y
            if best is None:
                fails.append(f"header {f}: no pump row below {a}")
                break
            cuts.append(best)
            a = best + 2
        for y in cuts:
            up = [c for c in comps if c[1] < y]
            down = [c for c in comps if c[0] > y + 1]
            src_up = f in inlets or any(c[3] for c in up)
            src_down = any(c[3] for c in down)
            south = any(c[4] for c in down) and not src_down
            north = any(c[4] for c in up) and not src_up
            if (south and north) or (south and not src_up) or (north and not src_down):
                fails.append(f"header {f}: no pump direction at row {y}")
            pumps.append(pump((col, y), (col, y + 1), "north" if north else "south"))
            taken |= {(col, y), (col, y + 1)}
    return pumps, taken, fails
