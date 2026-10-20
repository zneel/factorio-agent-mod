"""Stage 5: connect the placed modules: belt trees per item, pipe trees per fluid, poles.

    .venv/Scripts/python -m auto.route --seed N   (reads layout.json, writes factory.json + factory.png)

Rules (cheat sheet + lessons): one item per transport belt between modules; a block's two-item input is made at a
merge stub (left item side-loads from the left, right from the right, a rear belt keeps a one-input stub straight);
coal reaches a drill column's far lane through a side-load stub at the column start; extra sources of an item
side-load onto the trunk; further consumers tap the trunk with a splitter; underground pairs never overlap on one
axis; nothing is built in front of a belt end; pipes of different fluids never touch (ports count).
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import plan_nx  # noqa: E402
from auto import geom, mapdata  # noqa: E402

DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
LEFT = {"north": "west", "south": "east", "east": "north", "west": "south"}
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}
FLUIDS = {"water", "crude-oil", "heavy-oil", "light-oil", "petroleum-gas", "lubricant", "sulfuric-acid", "steam"}


def T(x, y):
    return (math.floor(x), math.floor(y))


def step(t, d, k=1):
    return (t[0] + DV[d][0] * k, t[1] + DV[d][1] * k)


class World:
    """Global occupancy for routing: map terrain + everything planned so far."""

    def __init__(self, m, window):
        self.m = m
        self.x1, self.y1, self.x2, self.y2 = window
        self.blocked = set()       # tiles with an entity (or reserved)
        self.reserved = {}         # tile -> owner key (port tiles kept free for one route)
        self.pipes = {}            # tile -> fluid
        self.spans = {"v": set(), "h": set()}
        self.places = []

    # plan_nx Grid interface
    def at(self, x, y):
        if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2):
            return "#"
        if (x, y) in self.blocked:
            return "p" if (x, y) in self.pipes else "#"
        t = self.m.t(x, y)
        if t in "~C#":
            return "#"
        return "t" if t == "t" else ("o" if self.m.o(x, y) != "." else ".")

    def free(self, x, y, ore_ok=True):
        return self.at(x, y) in ".to"

    def add(self, places, fluid=None):
        for p in places:
            if p.get("remove"):
                continue
            for t in geom.footprint(p):
                self.blocked.add(t)
                self.reserved.pop(t, None)
                if fluid and p["item"] in ("pipe", "pipe-to-ground"):
                    self.pipes[t] = fluid
        self.places += [p for p in places if not p.get("remove")]

    def view(self, owner, box=None):
        """A grid view where tiles reserved for `owner` count as free, limited to `box` (x1, y1, x2, y2)."""
        w = self
        bx = box or (w.x1, w.y1, w.x2, w.y2)

        class V:
            x1, y1, x2, y2 = max(w.x1, bx[0]), max(w.y1, bx[1]), min(w.x2, bx[2]), min(w.y2, bx[3])

            def at(self, x, y):
                if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2):
                    return "#"
                r = w.reserved.get((x, y))
                if r is not None and r != owner:
                    return "#"
                return w.at(x, y)

            def free(self, x, y, ore_ok=True):
                return self.at(x, y) in ".to"
        return V()


def add_spans(world, places):
    for i, q in enumerate(places):
        if q.get("item") == "underground-belt" and q.get("belt_type") == "input":
            for r in places[i + 1:i + 3]:
                if r.get("item") == "underground-belt" and r.get("belt_type") == "output":
                    a, b = T(q["x"], q["y"]), T(r["x"], r["y"])
                    if a[0] == b[0]:
                        world.spans["v"].update((a[0], y) for y in range(min(a[1], b[1]), max(a[1], b[1]) + 1))
                    else:
                        world.spans["h"].update((x, a[1]) for x in range(min(a[0], b[0]), max(a[0], b[0]) + 1))
                    break


def box_of(pts, margin):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin + 1, max(ys) + margin + 1)


def route_belt(world, start, goal, from_dir, to_dir, owner):
    # tiles in front of existing belt ends are off limits (the end would drop its items onto the new belt),
    # except this route's own start and goal
    fronts = [t for t in belt_end_fronts(world.places) - {start, goal} if t not in world.reserved]
    for t in fronts:
        world.reserved[t] = ("front",)
    try:
        for margin in (8, 20, 45, 90):
            r = _route_belt(world, start, goal, from_dir, to_dir, owner, world.view(owner, box_of([start, goal], margin)))
            if r:
                return r
        return None
    finally:
        for t in fronts:
            world.reserved.pop(t, None)


def _route_belt(world, start, goal, from_dir, to_dir, owner, g):
    for _ in range(10):
        r = plan_nx.route_belt(g, start, goal, from_dir, to_dir, spans=world.spans)
        if not r:
            return None
        seen, dup = set(), None
        for q in r:
            t = T(q["x"], q["y"])
            if t in seen:
                dup = t
                break
            seen.add(t)
        if not dup:
            return r
        world.reserved[dup] = ("ban", owner)
    return None


def belt_end_fronts(places):
    """Tiles in front of belt ends (a belt end feeds whatever is built there)."""
    tiles = {T(p["x"], p["y"]) for p in places if "belt" in p["item"] or p["item"] == "splitter"}
    out = set()
    for p in places:
        if p["item"] == "transport-belt" or (p["item"] == "underground-belt" and p.get("belt_type") == "output"):
            f = step(T(p["x"], p["y"]), p["direction"])
            if f not in tiles:
                out.add(f)
    return out


def merge_spot(world, near, d, owner, rear=False):
    """Free tile M (belt flowing d) with its left, right, downstream (and upstream if rear) tiles free."""
    lx, ly = DV[LEFT[d]]
    for r in range(1, 30):
        for ox in range(-r, r + 1):
            for oy in range(-r, r + 1):
                if max(abs(ox), abs(oy)) != r:
                    continue
                mt = (near[0] + ox, near[1] + oy)
                ts = [mt, (mt[0] + lx, mt[1] + ly), (mt[0] - lx, mt[1] - ly), step(mt, d), step(mt, OPP[d])]
                if all(world.free(*t) and t not in world.reserved for t in ts):
                    for t in ts:
                        world.reserved[t] = owner
                    return mt
    raise RuntimeError(f"no merge spot near {near}")


def splitter_tap(world, tree, near, owner):
    """A straight tile of `tree` (a list of belt places) to replace with a splitter whose other half and output
    tile are free; nearest to `near`. Returns (removed belt, splitter place, branch start tile, branch dir)."""
    best = []
    belts = [p for p in tree if p["item"] == "transport-belt"]
    for i in range(1, len(belts) - 1):
        p, a, b = belts[i], belts[i - 1], belts[i + 1]
        d = p["direction"]
        if not (a["direction"] == d == b["direction"]) or T(*step(T(a["x"], a["y"]), d)) != T(p["x"], p["y"]):
            continue
        for side in (LEFT[d], OPP[LEFT[d]]):
            s = step(T(p["x"], p["y"]), side)
            o = step(s, d)
            if all(world.free(*t) and t not in world.reserved for t in (s, o)):
                dist = abs(s[0] - near[0]) + abs(s[1] - near[1])
                cx, cy = (p["x"] + s[0] + .5) / 2, (p["y"] + s[1] + .5) / 2
                best.append((dist, p, {"item": "splitter", "x": cx, "y": cy, "direction": d}, o, d))
    best.sort(key=lambda b: b[0])
    return [b[1:] for b in best]


def build(seed):
    t0 = time.time()
    out = mapdata.out_dir(seed)
    lay = json.loads((out / "layout.json").read_text())
    m = mapdata.Map.load(seed)
    x1, y1, x2, y2 = lay["window"]
    world = World(m, (x1 - 20, y1 - 20, x2 + 20, y2 + 20))
    mods = {}
    for n, it in lay["modules"].items():
        pl = geom.translate(it["places"], it["x"], it["y"])
        ports = [dict(p, x=p["x"] + it["x"], y=p["y"] + it["y"], module=n) for p in it["ports"]]
        mods[n] = dict(it, places=pl, ports=ports)
        world.add(pl)
        add_spans(world, pl)
        if it.get("networks"):
            for fl, ts in it["networks"].items():
                for tt in ts:
                    world.pipes[(tt[0] + it["x"], tt[1] + it["y"])] = fl
    for f in lay["fixed"]:
        world.add(f.get("places", []))
    # reserve every port tile and the tile beyond each out port, so no other route takes them
    for n, it in mods.items():
        for p in it["ports"]:
            t = T(p["x"], p["y"])
            world.reserved[t] = (n, "port")
            if p["kind"] == "out":
                world.reserved[step(t, p["dir"])] = (n, "port")
    for t in belt_end_fronts(world.places):
        world.reserved.setdefault(t, ("front",))
    report, fails = [], []
    places = []

    def commit(pl, fluid=None):
        world.add(pl, fluid)
        add_spans(world, pl)
        places.extend(pl)

    # ---------------- fluids
    fluid_src = {}
    for n, it in mods.items():
        for fl, ts in (it.get("networks") or {}).items():
            fluid_src.setdefault(fl, set()).update((tt[0] + it["x"], tt[1] + it["y"]) for tt in ts)
    pumps = [f for f in lay["fixed"] if f["name"] == "water"]
    for f in pumps:
        p = f["places"][0]
        d = OPP[p["direction"]]   # the pump outputs away from the water
        src = step(T(p["x"], p["y"]), d)
        fluid_src.setdefault("water", set()).add(src)
        commit([{"item": "pipe", "x": src[0] + .5, "y": src[1] + .5}], "water")
    pjs = [f for f in lay["fixed"] if f["name"].startswith("pj")]
    sinks = {}
    for n, it in mods.items():
        for p in it["ports"]:
            if p["kind"] == "fluid" and "input" in p["io"]:
                sinks.setdefault(p["fluid"], []).append(p)
            elif p["kind"] == "fluid" and p["io"] == "output":   # block-made fluids (acid, lubricant) feed others
                fluid_src.setdefault(p["fluid"], set())
    for n, it in mods.items():   # oil units: crude in
        for fl, ts in (it.get("networks") or {}).items():
            if fl == "crude-oil":
                sinks.setdefault("crude-oil", []).append({"x": ts[0][0] + it["x"] + .5, "y": ts[0][1] + it["y"] + .5,
                                                          "module": n, "kind": "fluid", "fluid": "crude-oil"})
    for f in pjs:
        p = f["places"][0]
        fluid_src.setdefault("crude-oil", set()).add(T(p["x"] + 1, p["y"] - 2))   # pumpjack facing north: output (1,-2)
    for fl in sorted(sinks, key=lambda f: f != "crude-oil"):
        srcs = fluid_src.get(fl, set())
        # block-made fluids: their output ports are the sources
        for n, it in mods.items():
            for p in it["ports"]:
                if p["kind"] == "fluid" and p["fluid"] == fl and p["io"] == "output":
                    srcs.add(T(p["x"], p["y"]))
        if not srcs:
            fails.append(f"no source for {fl}")
            continue
        net = set(srcs)
        done_machines = set()
        for p in sorted(sinks[fl], key=lambda p: min(abs(p["x"] - s[0]) + abs(p["y"] - s[1]) for s in net)):
            key = (p["module"], p.get("mx", round(p["x"])), fl)
            t = T(p["x"], p["y"])
            if t in net:
                continue
            foreign = {tt for tt, f2 in world.pipes.items() if f2 != fl}
            for n2, it2 in mods.items():
                for q in it2["ports"]:
                    if q["kind"] == "fluid" and q["fluid"] != fl:
                        foreign.add(T(q["x"], q["y"]))
            world.reserved.pop(t, None)
            near = min(net, key=lambda s: abs(s[0] - t[0]) + abs(s[1] - t[1]))
            r = None
            for margin in (8, 20, 45):
                g = world.view(("fluid", fl), box_of([near, t], margin))
                starts = {s for s in net if g.x1 <= s[0] < g.x2 and g.y1 <= s[1] < g.y2}
                r = plan_nx.route_pipe(g, starts, t, foreign=foreign,
                                       own=starts | {tt for tt, f2 in world.pipes.items() if f2 == fl}, tries=30)
                if r:
                    break
            if not r:
                fails.append(f"pipe {fl} -> {p['module']} {t}")
                continue
            r = [{k: v for k, v in q.items() if k in ("item", "x", "y", "direction")} for q in r
                 if not (T(q["x"], q["y"]) in world.pipes and q["item"] == "pipe")]
            commit(r, fl)
            net |= {T(q["x"], q["y"]) for q in r} | {t}
        report.append(f"{fl}: {len(sinks[fl])} ports")

    # ---------------- belts
    out_ports, bundles = {}, []
    for n, it in mods.items():
        for p in it["ports"]:
            if p["kind"] == "out":
                out_ports.setdefault(p["item"], []).append(p)
            elif p["kind"] == "bundle":
                bundles.append(p)
    cols = [f for f in lay["fixed"] if f.get("col")]
    sinks = {}    # item -> list of (goal tile, to_dir, owner)
    pre = []      # belts to place first (merge stubs, rear belts)
    for b in bundles:
        if b.get("fed") == "column" and not mods[b["module"]].get("feeds"):
            b.pop("fed")   # steel / brick smelters: fed through an ordinary merge stub
    for b in bundles:
        port = T(b["x"], b["y"])
        if b.get("fed") == "column":   # smelter unit: its drill column routes straight in (handled below)
            continue
        if not b.get("right") or not b.get("left"):
            item = b.get("left") or b.get("right")
            d = b["dir"]
            # one-item bundle: a stub with a rear belt; the item side-loads its lane (left lane if "left")
            mt = merge_spot(world, step(port, OPP[d], 3), d, ("m", port), rear=True)
            pre += [{"item": "transport-belt", "x": mt[0] + .5, "y": mt[1] + .5, "direction": d},
                    {"item": "transport-belt", "x": step(mt, OPP[d])[0] + .5, "y": step(mt, OPP[d])[1] + .5, "direction": d}]
            sd = LEFT[d] if b.get("left") else OPP[LEFT[d]]
            sinks.setdefault(item, []).append((step(mt, sd), OPP[sd], ("m", port)))
        else:
            d = b["dir"]
            mt = merge_spot(world, step(port, OPP[d], 3), d, ("m", port))
            pre.append({"item": "transport-belt", "x": mt[0] + .5, "y": mt[1] + .5, "direction": d})
            sinks.setdefault(b["left"], []).append((step(mt, LEFT[d]), OPP[LEFT[d]], ("m", port)))
            sinks.setdefault(b["right"], []).append((step(mt, OPP[LEFT[d]]), LEFT[d], ("m", port)))
        b["merge"] = mt
    commit(pre)
    # merged stubs -> their ports
    for b in bundles:
        if b.get("fed") == "column":
            continue
        mt, d = b["merge"], b["dir"]
        world.reserved.pop(T(b["x"], b["y"]), None)
        r = route_belt(world, step(mt, d), T(b["x"], b["y"]), d, b["dir"], ("m", T(b["x"], b["y"])))
        if r is None:
            fails.append(f"merge->port {b['module']}")
            continue
        commit(r)
    # drill columns -> smelter units; coal reaches each column start through a side-load stub
    for b in bundles:
        if b.get("fed") != "column":
            continue
        mod = mods[b["module"]]
        col = next(c for c in cols if c["name"] == mod["feeds"])["col"]
        end = step(T(*col["end"]), col["flow"])
        r = route_belt(world, end, T(b["x"], b["y"]), col["flow"], b["dir"], (b["module"], "port"))
        if r is None:
            fails.append(f"column -> {b['module']}")
            continue
        commit(r)
        st = T(*col["start"])
        d = col["flow"]
        stub = step(st, OPP[d])
        rear = step(stub, OPP[d])
        commit([{"item": "transport-belt", "x": stub[0] + .5, "y": stub[1] + .5, "direction": d},
                {"item": "transport-belt", "x": rear[0] + .5, "y": rear[1] + .5, "direction": d}])
        # drills sit on one side (near lane); coal must take the far lane: side-load from the far side
        drill = next(q for q in col["places"] if q["item"] == "electric-mining-drill")
        near_side = None
        for sd in (LEFT[d], OPP[LEFT[d]]):
            v = DV[sd]
            if (drill["x"] - (st[0] + .5)) * v[0] + (drill["y"] - (st[1] + .5)) * v[1] > 0:
                near_side = sd
        far = OPP[near_side]
        sinks.setdefault("coal", []).append((step(stub, far), OPP[far], ("col", st)))
    # item trees
    for item, sk in sorted(sinks.items(), key=lambda kv: -len(kv[1])):
        srcs = out_ports.get(item, [])
        if item == "coal":
            srcs = [{"x": step(T(*c["col"]["end"]), c["col"]["flow"])[0] + .5,
                     "y": step(T(*c["col"]["end"]), c["col"]["flow"])[1] + .5, "dir": c["col"]["flow"], "module": c["name"]}
                    for c in cols if c.get("ore") == "coal"]
        if item == "stone":
            srcs = [{"x": step(T(*c["col"]["end"]), c["col"]["flow"])[0] + .5,
                     "y": step(T(*c["col"]["end"]), c["col"]["flow"])[1] + .5, "dir": c["col"]["flow"], "module": c["name"]}
                    for c in cols if c.get("ore") == "stone"]
        if item in ("iron-ore", "copper-ore"):
            srcs = [{"x": step(T(*c["col"]["end"]), c["col"]["flow"])[0] + .5,
                     "y": step(T(*c["col"]["end"]), c["col"]["flow"])[1] + .5, "dir": c["col"]["flow"], "module": c["name"]}
                    for c in cols if c.get("ore") == item and c.get("raw")]
        if not srcs:
            fails.append(f"no source for {item}")
            continue
        src = srcs[0]
        tree = []
        for k, (goal, gd, owner) in enumerate(sorted(sk, key=lambda s: abs(s[0][0] - src["x"]) + abs(s[0][1] - src["y"]))):
            world.reserved.pop(goal, None)
            if not tree:
                st = T(src["x"], src["y"])
                world.reserved.pop(st, None)
                r = route_belt(world, st, goal, src["dir"], gd, owner)
            else:
                r = None
                for old, sp, o, d in splitter_tap(world, tree, goal, owner)[:8]:
                    r = route_belt(world, o, goal, d, gd, owner)
                    if r:
                        places.remove(old) if old in places else None
                        tree.remove(old)
                        commit([sp])
                        break
            if r is None:
                fails.append(f"belt {item} -> {owner}")
                continue
            commit(r)
            tree += r
        # extra sources join the trunk by side-loading onto a straight trunk tile
        for s2 in srcs[1:]:
            st = T(s2["x"], s2["y"])
            world.reserved.pop(st, None)
            best = None
            for p in [q for q in tree if q["item"] == "transport-belt"][1:-1]:
                d = p["direction"]
                for sd in (LEFT[d], OPP[LEFT[d]]):
                    tt = step(T(p["x"], p["y"]), sd)
                    if world.free(*tt) and tt not in world.reserved:
                        dist = abs(tt[0] - st[0]) + abs(tt[1] - st[1])
                        if best is None or dist < best[0]:
                            best = (dist, tt, OPP[sd])
            r = route_belt(world, st, best[1], s2["dir"], best[2], ("src", st)) if best else None
            if r is None:
                fails.append(f"source {item} {s2['module']} -> trunk")
                continue
            commit(r)
        report.append(f"{item}: {len(sk)} consumers, {len(srcs)} sources")
    res = {"seed": seed, "places": places, "report": report, "fails": fails}
    (out / "routes.json").write_text(json.dumps(res))
    print(f"routed in {time.time() - t0:.0f}s: {len(places)} pieces; {len(fails)} failures")
    for f in fails:
        print("  FAIL", f)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    a = ap.parse_args()
    build(a.seed)


if __name__ == "__main__":
    main()


def render(seed, marks=(), path=None):
    from PIL import Image, ImageDraw
    from auto.factory import render as base_render
    out = mapdata.out_dir(seed)
    lay = json.loads((out / "layout.json").read_text())
    m = mapdata.Map.load(seed)
    base_render(m, lay, out / "factory.png")
    img = Image.open(out / "factory.png")
    d = ImageDraw.Draw(img)
    x1, y1 = lay["window"][:2]
    sc = 3
    pal = {"transport-belt": (200, 180, 40), "underground-belt": (230, 120, 30), "pipe": (90, 150, 220),
           "pipe-to-ground": (60, 110, 200), "splitter": (255, 255, 255)}
    for p in json.loads((out / "routes.json").read_text())["places"]:
        for (tx, ty) in geom.footprint(p):
            d.rectangle([(tx - x1) * sc, (ty - y1) * sc, (tx - x1 + 1) * sc - 1, (ty - y1 + 1) * sc - 1],
                        fill=pal.get(p["item"], (255, 0, 255)))
    for (mx, my) in marks:
        d.ellipse([(mx - x1) * sc - 6, (my - y1) * sc - 6, (mx - x1) * sc + 6, (my - y1) * sc + 6], outline=(255, 0, 0), width=2)
    img.save(path or out / "factory.png")
