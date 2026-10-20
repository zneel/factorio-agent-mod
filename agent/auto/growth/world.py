"""Placing pieces on the live map: poles for an increment or an outpost's route, what is in the way, burner
pairs and the bootstrap lab's spot."""
import json
import math

from auto.growth.core import BOOT_ENTS


def overlap(a, b):
    """Two boxes (x1, y1, x2, y2) share area."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def route_poles(plan, L, pieces, q, m, box=None):
    """Poles that join an outpost's pole group to the powered network along its belt route: networkx fewest hops
    over free tiles beside the route (4-neighbours of its belts), from the outpost's poles to any powered live pole
    within reach. [] when already joined or no chain exists."""
    import networkx as nx
    from auto import geom, live as lv, poles
    R = poles.REACH
    net = lv.network(q)
    on = {(round(x, 2), round(y, 2)) for x, y, o_, *_ in net.get("poles") or [] if o_}
    own = [p for p in pieces if "electric-pole" in p["item"] and (box is None or
           box[0] - 8 <= p["x"] <= box[2] + 8 and box[1] - 8 <= p["y"] <= box[3] + 8)]   # chain from the outpost's own
    own = [p for p in own if (round(p["x"], 2), round(p["y"], 2)) not in on]   # the poles still off the grid
    if not own:
        return []
    belts = [p for p in pieces if p["item"] in ("transport-belt", "underground-belt", "splitter")]
    occ = set(geom.tiles(pieces)) | set(plan.tiles)
    for (x, y), n in L.extra.items():
        occ |= geom.footprint({"item": n, "x": x, "y": y})
    cand = {(math.floor(b["x"]) + a, math.floor(b["y"]) + c) for b in belts for a, c in ((1, 0), (-1, 0), (0, 1), (0, -1))}
    cand = {t for t in cand if t not in occ and m.t(*t) in ".t"}
    pts = {("c", t): (t[0] + .5, t[1] + .5) for t in cand}
    pts.update({("s", k): (p["x"], p["y"]) for k, p in enumerate(own)})
    xs = [v[0] for v in pts.values()]
    ys = [v[1] for v in pts.values()]
    x1, x2, y1, y2 = min(xs) - R, max(xs) + R, min(ys) - R, max(ys) + R
    pts.update({("t", k): (x, y) for k, (x, y) in enumerate(sorted(on)) if x1 <= x <= x2 and y1 <= y <= y2})
    buck = {}
    for k, (x, y) in pts.items():
        buck.setdefault((int(x // 8), int(y // 8)), []).append(k)
    G = nx.Graph()
    for k, (x, y) in pts.items():
        bx, by = int(x // 8), int(y // 8)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k2 in buck.get((bx + dx, by + dy), ()):
                    if k2 != k and math.dist((x, y), pts[k2]) <= R and not (k[0] != "c" and k2[0] != "c"):
                        G.add_edge(k, k2)
    G.add_edges_from(("S", k) for k in pts if k[0] == "s")
    G.add_edges_from((k, "T") for k in pts if k[0] == "t")
    try:
        path = nx.shortest_path(G, "S", "T")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []
    return [{"item": poles.ITEM, "x": pts[k][0], "y": pts[k][1]} for k in path[1:-1] if k[0] == "c"]


def outpost_pieces(state, o):
    """Our own pieces of outpost o: what stands in its patch box and its route's corridor to its target group."""
    bx = o["box"]
    pts = state.get("extra_places", [])
    inb = [p for p in pts if bx[0] - 8 <= p["x"] <= bx[2] + 8 and bx[1] - 8 <= p["y"] <= bx[3] + 8]
    # the route: own belts chained to the box's belts through the own pieces (4-neighbour flood over belt tiles)
    tiles = {(math.floor(p["x"]), math.floor(p["y"])): p for p in pts}
    seen = {(math.floor(p["x"]), math.floor(p["y"])) for p in inb}
    fr = list(seen)
    while fr:
        nx_ = []
        for t in fr:
            for a, c in ((1, 0), (-1, 0), (0, 1), (0, -1), (5, 0), (-5, 0), (0, 5), (0, -5), (2, 0), (-2, 0), (0, 2), (0, -2)):
                u = (t[0] + a, t[1] + c)
                if u in tiles and u not in seen and (abs(a) + abs(c) == 1 or tiles[u]["item"] in
                                                     ("underground-belt", "small-electric-pole", "medium-electric-pole")):
                    seen.add(u)
                    nx_.append(u)
        fr = nx_
    return [tiles[t] for t in seen]


def plan_poles(plan, live, places, m, corridor=None):
    """Poles for an increment on the live map: cover its electric consumers (and new steam engines), then chain
    every unpowered pole group to the engines' network. Free tiles: no live entity, no plan piece, no water or
    cliff, no tree. corridor: a tile set poles must stay in (an outpost's chain runs along its belt route, not
    through a search box hundreds of tiles wide)."""
    from auto import geom, live as lv, poles
    if not places:
        return []
    box = plan.box(range(len(plan.p)), 12) if False else None
    xs = [p["x"] for p in places]
    ys = [p["y"] for p in places]
    box = (min(xs) - 30, min(ys) - 30, max(xs) + 30, max(ys) + 30)
    net = lv.network(live.q)
    live_poles = [{"item": pp[3] if len(pp) > 3 else poles.ITEM, "x": pp[0], "y": pp[1]} for pp in net.get("poles") or []]
    occupied = set()
    for (x, y), n in live.extra.items():
        occupied.add((math.floor(x), math.floor(y)))
    for i in live.built:
        occupied |= geom.footprint(plan.p[i])
    obst = lv.obstacles(live.q, box)

    own = geom.tiles([p for p in places if not p.get("remove")])   # pieces not in the plan (remine) included

    def free(t):
        if t in plan.tiles or t in occupied or t in obst or t in own:
            return False
        if corridor is not None and t not in corridor:
            return False
        return m.t(*t) == "."
    engines = [p for p in places if p["item"] == "steam-engine"]
    cov, _ = poles.cover(live_poles + places, free, extra=engines)
    roots = [(pp[0], pp[1]) for pp in net.get("poles") or [] if pp[2]]
    if not roots:
        eng = engines or [plan.p[i] for i in live.built if plan.p[i]["item"] == "steam-engine"]
        cands = [c for c in cov if eng and any(abs(c["x"] - e["x"]) < poles.SUPPLY + 1.5 and abs(c["y"] - e["y"]) < poles.SUPPLY + 2.5 for e in eng)]
        if not cands:
            return cov
        roots = [(cands[0]["x"], cands[0]["y"])]
    occupied |= {(math.floor(c["x"]), math.floor(c["y"])) for c in cov}
    try:
        chains = poles.connect(live_poles + places + cov, free, roots[0])
    except RuntimeError:
        chains = []
    return cov + chains




def removals(plan, live, places):
    """Burner-phase entities (and the hut lab) standing where the increment builds: picked up first."""
    from auto import geom
    want = set()
    for p in places:
        want |= geom.footprint(p)
    real = set()     # tiles of planned splitters placed for real now: their stand-in belts go first
    for p in places:
        if p["item"] == "splitter":
            real |= geom.footprint(p)
    out = []
    for (x, y), n in live.extra.items():
        if n in BOOT_ENTS and geom.footprint({"item": n, "x": x, "y": y}) & want:
            out.append({"item": n, "x": x, "y": y, "remove": True})
        elif n == "transport-belt" and (math.floor(x), math.floor(y)) in real:
            out.append({"item": n, "x": x, "y": y, "remove": True})
    return out


def validate(plan, live, places):
    """Static checks before building: draftsman (overlaps, bad entities), nothing live in the way but trees."""
    from auto import dcheck, geom
    errs = [w for w in dcheck.check(places)][:5]
    mine = {}
    for i in live.built:
        for t in geom.footprint(plan.p[i]):
            mine[t] = plan.p[i]["item"]
    for (x, y), n in live.extra.items():
        if "electric-pole" not in n and n not in BOOT_ENTS and n != "transport-belt":
            mine[(math.floor(x), math.floor(y))] = n
    for p in places:
        if p.get("upgrade"):
            continue
        for t in geom.footprint(p):
            if t in mine and "electric-pole" not in p["item"]:
                errs.append(f"{p['item']} {p['x']},{p['y']} on live {mine[t]}")
                break
    return errs[:8]


BURNER_CAP = {"iron-plate": 24, "copper-plate": 12, "stone": 6}   # extra burner pairs per item at most
BURNER_ORE = {"iron-plate": ("i", "stone-furnace"), "copper-plate": ("c", "stone-furnace"), "stone": ("s", "iron-chest")}


def burner_pairs(plan, L, m, item, boot_dir, k, n=4):
    """n more burner drill + furnace (chest) pairs on the nearest patch, off every plan tile and live entity;
    written to boot/boot-extra-<k>.json for the tender."""
    from auto import boot as bootmod, geom
    blocked = set(plan.tiles)
    for i in L.built:
        blocked |= geom.footprint(plan.p[i])
    for (x, y), nm in L.extra.items():
        blocked |= geom.footprint({"item": nm, "x": x, "y": y})
    for f in boot_dir.glob("boot-*.json"):
        blocked |= geom.tiles(json.loads(f.read_text())["place"])
    code, out_item = BURNER_ORE[item]
    pl = bootmod.pairs(m, code, n, out_item, blocked)
    (boot_dir / f"boot-extra-{k}.json").write_text(json.dumps({"place": pl, "gate": {"fuel": "coal"}}))
    return pl


def hut_spot(plan, boot, m):
    """A free 3x3 spot (with a free ring) for the bootstrap lab, as near the first boiler unit as possible."""
    from auto import geom
    blocked = set(plan.tiles) | geom.tiles(boot)
    eng = [plan.p[i] for i in plan.groups.get("power:0", []) if plan.p[i]["item"] == "steam-engine"] or           [p for p in plan.p if p["item"] == "steam-engine"]
    if not eng:
        return None
    cx, cy = math.floor(eng[0]["x"]), math.floor(eng[0]["y"])
    for r in range(4, 30):
        for dx in range(-r, r + 1):
            for dy in (-r, r) if abs(dx) != r else range(-r, r + 1):
                x, y = cx + dx, cy + dy
                lab = {"item": "lab", "x": x + .5, "y": y + .5}
                ring = {(a + i, b + j) for a, b in geom.footprint(lab) for i in (-1, 0, 1) for j in (-1, 0, 1)}
                if not ring & blocked and all(m.t(*t) == "." for t in ring):
                    return lab
    return None


def hut_missing(L, hut):
    return not any(n == "lab" and abs(x - hut["x"]) < .1 and abs(y - hut["y"]) < .1 for (x, y), n in L.extra.items())
