"""Plan geometry: footprints, bounding boxes, rotation and translation of placement lists."""
import math

SIZE = {"electric-mining-drill": (3, 3), "burner-mining-drill": (2, 2), "stone-furnace": (2, 2), "steel-furnace": (2, 2),
        "boiler": (3, 2), "steam-engine": (3, 5), "offshore-pump": (1, 1), "assembling-machine-1": (3, 3),
        "assembling-machine-2": (3, 3), "assembling-machine-3": (3, 3), "chemical-plant": (3, 3), "oil-refinery": (5, 5),
        "lab": (3, 3), "electric-furnace": (3, 3), "pumpjack": (3, 3), "storage-tank": (3, 3), "big-electric-pole": (2, 2),
        "substation": (2, 2), "rocket-silo": (9, 9), "radar": (3, 3), "beacon": (3, 3), "splitter": (2, 1),
        "cargo-landing-pad": (8, 8), "accumulator": (2, 2), "solar-panel": (3, 3), "pump": (1, 2)}
ROT = {"north": "east", "east": "south", "south": "west", "west": "north"}


def size(p):
    w, h = SIZE.get(p["item"], (1, 1))
    if p.get("direction") in ("east", "west"):
        w, h = h, w
    return w, h


def footprint(p):
    w, h = size(p)
    x0, y0 = math.floor(p["x"] - w / 2 + .01), math.floor(p["y"] - h / 2 + .01)
    return {(x0 + i, y0 + j) for i in range(w) for j in range(h)}


def tiles(places):
    out = set()
    for p in places:
        if not p.get("remove"):
            out |= footprint(p)
    return out


def bbox(places):
    ts = tiles(places)
    xs = [x for x, _ in ts]
    ys = [y for _, y in ts]
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1   # x1, y1, x2, y2 (exclusive)


def rotate(places, k):
    """Rotate k quarter turns clockwise about (0, 0). (x, y) -> (-y, x) per turn (y grows south)."""
    out = []
    for p in places:
        q = dict(p)
        for _ in range(k % 4):
            q["x"], q["y"] = -q["y"], q["x"]
            if "direction" in q:
                q["direction"] = ROT[q["direction"]]
        out.append(q)
    return out


def translate(places, dx, dy):
    return [dict(p, x=p["x"] + dx, y=p["y"] + dy) for p in places]


def normalize(places):
    """Shift so the bounding box starts at (0, 0); returns (places, w, h)."""
    x1, y1, x2, y2 = bbox(places)
    return translate(places, -x1, -y1), x2 - x1, y2 - y1


def pipe_spans(places, spans=None):
    """Add the tiles covered by pipe-to-ground pairs (consecutive entrance/exit pieces in a route) to spans."""
    spans = spans if spans is not None else {"v": set(), "h": set()}
    pl = [q for q in places if q["item"] == "pipe-to-ground"]
    for a, b in zip(pl[0::2], pl[1::2]):
        ax, ay, bx, by = math.floor(a["x"]), math.floor(a["y"]), math.floor(b["x"]), math.floor(b["y"])
        if ax == bx:
            spans["v"].update((ax, y) for y in range(min(ay, by), max(ay, by) + 1))
        elif ay == by:
            spans["h"].update((x, ay) for x in range(min(ax, bx), max(ax, bx) + 1))
    return spans


DV4 = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def pipe_components(places):
    """Union-find over pipe pieces with the game's joining rules: a plain pipe joins all 4 sides, a pipe-to-ground
    only on its direction side plus its underground partner (consecutive entrance/exit in the list). Returns
    (find, pieces): find(tile) -> component root, pieces: tile -> place."""
    pieces = {}
    for q in places:
        if q["item"] in ("pipe", "pipe-to-ground") and not q.get("remove"):
            pieces[(math.floor(q["x"]), math.floor(q["y"]))] = q
    parent = {t: t for t in pieces}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    def sides(q):
        return set(DV4.values()) if q["item"] == "pipe" else {DV4[q.get("direction", "north")]}
    for t, q in pieces.items():
        for d in sides(q):
            n = (t[0] + d[0], t[1] + d[1])
            if n in pieces and (-d[0], -d[1]) in sides(pieces[n]):
                parent[find(t)] = find(n)
    ptg = [q for q in places if q["item"] == "pipe-to-ground" and not q.get("remove")]
    for a, b in zip(ptg[0::2], ptg[1::2]):
        parent[find((math.floor(a["x"]), math.floor(a["y"])))] = find((math.floor(b["x"]), math.floor(b["y"])))
    return find, pieces, sides


def port_joined(pieces, sides, t, machine_tiles):
    """True when the piece on port tile t opens toward the machine."""
    q = pieces.get(t)
    if not q:
        return False
    return any((t[0] + d[0], t[1] + d[1]) in machine_tiles for d in sides(q))


def belt_fronts(places):
    """Tiles in front of belt ends: a transport belt or underground exit whose front tile holds no belt piece.
    A belt end drops its items onto whatever belt is laid there, so routes must not pass over these tiles."""
    belts = {}
    for q in places:
        if q.get("remove"):
            continue
        if q["item"] in ("transport-belt", "underground-belt", "splitter"):
            for t in footprint(q):
                belts[t] = q
    out = set()
    for t, q in belts.items():
        if q["item"] == "splitter" or (q["item"] == "underground-belt" and q.get("belt_type") != "output"):
            continue
        d = DV4[q.get("direction", "north")]
        f = (t[0] + d[0], t[1] + d[1])
        if f not in belts:
            out.add(f)
    return out


def tag(places, grp):
    """Mark placements with the plan group they belong to (kept if already set). Returns places."""
    for q in places:
        q.setdefault("grp", grp)
    return places
