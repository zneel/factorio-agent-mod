"""Module interiors in local coordinates, with typed ports for routing.

interior(module, ...) -> {"places": [...], "w", "h", "ports": [...]}   (places normalised to start at 0,0)
Port kinds
  bundle  {"left", "right", "x", "y", "dir"}  a belt carrying left/right items must arrive at (x, y) moving dir
  out     {"item", "x", "y", "dir"}           the module's product leaves from (x, y) moving dir (the tile is free)
  fluid   {"fluid", "io", "x", "y"}           a pipe of this fluid must sit on (x, y)
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import plan_block  # noqa: E402
from auto import cache  # noqa: E402
import plan_gen  # noqa: E402
import plan_nx  # noqa: E402
from auto import blocks, geom  # noqa: E402
from auto.ports import ports as fluid_ports  # noqa: E402

LAYOUT_SRC = ("auto/interiors.py", "auto/blocks.py", "plan_layout.py", "plan_block.py", "plan_nx.py", "auto/geom.py")

FLUIDS = blocks.FLUIDS
DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


class LocalGrid:
    """Minimal plan.Grid stand-in for routing inside a module's own box."""

    def __init__(self, x1, y1, x2, y2, blocked):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.blocked = blocked

    def at(self, x, y):
        if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2) or (x, y) in self.blocked:
            return "#"
        return "."

    def free(self, x, y, ore_ok=True):
        return self.at(x, y) == "."


@cache.disk("block", LAYOUT_SRC)
def block_interior(block, demand, skip=0):
    """Lay out a block; when a candidate's loops or ports can't be routed, try the next pairing / grid. skip: pass
    over that many buildable candidates (the plan's repair loop asks for another one when the bus can't connect)."""
    last = None
    for pr in blocks.pairings(block["inputs"])[:12]:
        for mirror in (False, True):
            pp = [((b, a) if mirror and b else (a, b)) for a, b in pr]
            spec = blocks.spec_for(block, demand, pp)
            try:
                layout, sp, info = blocks.solve(spec, demand, 20)
                out = _block_interior(block, demand, layout, sp, pp, info)
            except RuntimeError as e:
                last = e
                continue
            if skip == 0:
                return out
            skip -= 1
    raise RuntimeError(f"{block['name']}: no buildable layout ({last})")


def _block_interior(block, demand, layout, spec, pairing, info):
    rec = {r: (v["ing"], v["prod"], v["energy"]) for r, v in demand["recipes"].items()}
    res = plan_block.compile_block(layout, rec)
    if res["errors"]:
        raise RuntimeError(f"{block['name']}: compile errors {res['errors'][:3]}")
    fast = {(float(w.split()[1].split(",")[0]), float(w.split()[1].split(",")[1])) for w in res.get("rates", [])
            if "fast-inserter" in w}
    places = []
    for p in res["places"]:
        q = {k: v for k, v in p.items() if k in ("item", "x", "y", "direction", "recipe", "mall")}
        if q["item"] == "inserter" and (q["x"], q["y"]) in fast:
            q["item"] = "fast-inserter"
        places.append(q)
    belts = layout["belts"]

    def lanes(bb):
        return {v for v in bb["lanes"].values() if v}

    def start(bb):
        return (bb["x1"] - 1 if bb["dir"] == "east" else bb["x1"] + 1, bb["y"])

    def after(bb):
        return (bb["x2"] + 1 if bb["dir"] == "east" else bb["x2"] - 1, bb["y"])
    ports = []
    taken = geom.tiles(places)
    xs1, ys1, xs2, ys2 = geom.bbox(places)
    # each input bundle: its entry belt (the first of its belts) gets the port; a second belt is its loop
    loops = []
    by_id = {bb["id"]: bb for bb in belts}
    for bi, b in enumerate(spec["inputs"]):
        bs = [by_id[i] for i in layout["bundle_belts"][bi]]
        bs.sort(key=lambda bb: bb["y"])
        entry = bs[0]
        sx, sy = start(entry)
        ports.append({"kind": "bundle", "left": b["left"], "right": b["right"], "x": sx, "y": sy, "dir": entry["dir"]})
        for other in bs[1:]:
            loops.append((after(entry), entry["dir"], start(other), other["dir"]))
    # loops: route inside the module's box grown by 3 tiles, around every port (exports and fluid ports too) and an
    # export's next tile
    g = LocalGrid(xs1 - 4, ys1 - 4, xs2 + 4, ys2 + 4, taken)
    port_tiles = {(math.floor(p["x"]), math.floor(p["y"])) for p in ports}
    for m in layout["machines"]:
        if any(i in FLUIDS for i, _ in rec[m["recipe"]][0] + rec[m["recipe"]][1]):
            port_tiles |= {(math.floor(m["x"] + dx), math.floor(m["y"] + dy)) for _, _, dx, dy in
                           fluid_ports(m.get("entity", "assembling-machine-2"), m["recipe"], m.get("direction", "north"))}
    for bb in belts:
        if (lanes(bb) & set(spec["exports"])) and not (lanes(bb) - set(spec["exports"])):
            x, y = after(bb)
            port_tiles |= {(math.floor(x), math.floor(y)), (math.floor(x) + (1 if bb["dir"] == "east" else -1), math.floor(y))}
    ends_ = {(math.floor(a[0]), math.floor(a[1])) for a, _, _, _ in loops} |             {(math.floor(z[0]), math.floor(z[1])) for _, _, z, _ in loops}
    places = [q for q in places if not ("electric-pole" in q["item"] and (math.floor(q["x"]), math.floor(q["y"])) in ends_)]
    taken = geom.tiles(places)
    for a, ad, z, zd in loops:
        g.blocked = taken | port_tiles
        r = plan_nx.route_belt(g, (math.floor(a[0]), math.floor(a[1])), (math.floor(z[0]), math.floor(z[1])), ad, zd)
        if not r:
            raise RuntimeError(f"{block['name']}: no loop route")
        places += r
        taken |= geom.tiles(r)
    chest = set(block.get("chest", []))
    for bb in belts:
        ex = lanes(bb) & set(spec["exports"])
        if ex and not (lanes(bb) - set(spec["exports"])):
            x, y = after(bb)
            if ex <= chest:   # a mall item nobody else reads: the belt ends in an inserter and a chest (the mall)
                dx = 1 if bb["dir"] == "east" else -1
                tt = {(math.floor(x), math.floor(y)), (math.floor(x + dx), math.floor(y))}
                places = [q for q in places if not ("electric-pole" in q["item"] and
                                                    (math.floor(q["x"]), math.floor(q["y"])) in tt)]
                cx_, cy_ = math.floor(x) + .5, math.floor(y) + .5
                places += [{"item": "inserter", "x": cx_, "y": cy_, "direction": bb["dir"]},
                           {"item": "iron-chest", "x": cx_ + dx, "y": cy_, "mall": sorted(ex)}]
                taken |= tt
                continue
            for it in ex:
                ports.append({"kind": "out", "item": it, "x": x, "y": y, "dir": bb["dir"]})
    keep_free = set()
    for p in ports:
        if p["kind"] == "out":
            t = (math.floor(p["x"]), math.floor(p["y"]))
            keep_free |= {t, (t[0] + (1 if p["dir"] == "east" else -1), t[1])}
    for m in layout["machines"]:
        if any(i in FLUIDS for i, _ in rec[m["recipe"]][0] + rec[m["recipe"]][1]):
            ent = m.get("entity", "assembling-machine-2")
            for fl, io, dx, dy in fluid_ports(ent, m["recipe"], m.get("direction", "north")):
                ports.append({"kind": "fluid", "fluid": fl, "io": io, "x": m["x"] + dx, "y": m["y"] + dy})
                keep_free.add((math.floor(m["x"] + dx), math.floor(m["y"] + dy)))
    places = [q for q in places if not ("electric-pole" in q["item"] and
                                        (math.floor(q["x"]), math.floor(q["y"])) in keep_free)]
    on = geom.tiles(places)   # a piece on a port tile itself (a loop belt over a fluid port) boxes it in too
    bad = [p for p in ports if (math.floor(p["x"]), math.floor(p["y"])) in on] + unreachable_ports(places, ports)
    if bad:
        raise RuntimeError(f"{block['name']}: boxed-in ports {[(p['kind'], p.get('fluid') or p.get('item') or p.get('left')) for p in bad]}")
    return finish(places, ports, info=info, pairing=pairing)


def unreachable_ports(places, ports, fluid_side="west"):
    """Ports a connector could not reach: BFS over free tiles from each port to outside the module's box, hopping
    obstacles underground (belts 4 tiles, pipes 9). Fluid ports must get out on the fluid side, belt ports anywhere.
    A module failing this is re-solved (the next candidate) instead of failing later on the bus."""
    taken = geom.tiles(places)
    x1, y1, x2, y2 = geom.bbox(places + [{"item": "x", "x": p["x"], "y": p["y"]} for p in ports])
    bad = []
    for p in ports:
        if p["kind"] not in ("fluid", "bundle", "out"):
            continue
        s = (math.floor(p["x"]), math.floor(p["y"]))
        hop = 9 if p["kind"] == "fluid" else 4
        seen, todo, ok = {s}, [s], False
        while todo and not ok:
            t = todo.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                for k in range(1, hop + 2):
                    n = (t[0] + dx * k, t[1] + dy * k)
                    if n in taken:
                        continue           # underground: keep looking further along this line
                    if k > 1 and any((t[0] + dx * j, t[1] + dy * j) not in taken for j in range(1, k)):
                        break              # a free tile before it: that one is the step, not a hop
                    if not (x1 <= n[0] < x2 and y1 <= n[1] < y2):
                        if p["kind"] != "fluid" or (fluid_side == "west" and n[0] < x1) or                                 (fluid_side == "east" and n[0] >= x2):
                            ok = True
                            break
                        if not (x1 - 2 <= n[0] < x2 + 2 and y1 - 2 <= n[1] < y2 + 2):
                            break          # a fluid pipe may go round the box, not far off
                    if n not in seen:
                        seen.add(n)
                        todo.append(n)
                    break
        if not ok:
            bad.append(p)
    return bad


def finish(places, ports, **extra):
    x1, y1, x2, y2 = geom.bbox(places + [{"item": "x", "x": p["x"], "y": p["y"]} for p in ports])
    pl = geom.translate(places, -x1, -y1)
    po = [dict(p, x=p["x"] - x1, y=p["y"] - y1) for p in ports]
    return dict({"places": pl, "w": x2 - x1, "h": y2 - y1, "ports": po}, **extra)


def smelter_interior(n_per_side, ore):
    """Smelting column (plan_gen.smelt) fed at its top by an ore+coal belt; plates leave at the bottom."""
    s, gate = plan_gen.smelt(0, 2, n_per_side, ore)
    s = [{k: v for k, v in p.items() if k != "lanes"} for p in s]
    top = {"kind": "bundle", "left": "coal", "right": ore, "x": 0.5, "y": 1.5, "dir": "south", "fed": "column"}
    out = max((p for p in s if p["item"] == "transport-belt" and p["x"] == 5.5), key=lambda p: p["y"])
    plate = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick", "iron-plate": "steel-plate"}[ore]
    ports = [top, {"kind": "out", "item": plate, "x": 5.5, "y": out["y"] + 1, "dir": "south"}]
    return finish(s, ports)


def labs_interior(n, packs):
    """n labs between two belts, plus long-handed inserters from a third belt above: 6 lanes for up to 6 packs."""
    pl = plan_gen.labs(1.5, 3.5, n, top_dir="west", bottom_dir="east")
    xs = [p["x"] for p in pl if p["item"] == "lab"]
    span = range(int(min(xs) - 1.5), int(max(xs) + 1.5) + 1)
    top3 = [{"item": "transport-belt", "x": x + .5, "y": 2.5, "direction": "west"} for x in reversed(span)]
    longs = [{"item": "long-handed-inserter", "x": x + 1, "y": 4.5, "direction": "south"} for x in xs]
    # the long inserter sits beside the normal top inserter: picks from the third belt (y 2.5), drops in the lab
    pl += top3 + longs
    packs = list(packs) + [None] * (6 - len(packs))
    ports = []
    rows = [(2.5, "west"), (3.5, "west"), (9.5, "east")]
    for k, (y, d) in enumerate(rows):
        l, r = packs[2 * k], packs[2 * k + 1]
        if not (l or r):
            continue
        x = max(span) + 1.5 if d == "west" else min(span) - .5
        ports.append({"kind": "bundle", "left": l, "right": r, "x": x, "y": y, "dir": d})
    return finish(pl, ports)




@cache.disk("oil", LAYOUT_SRC)
def oil_interior(recipes, skip=0):
    """Search cracker arrangements until every fluid network routes and every port is reachable; skip: pass over
    that many good ones (repair loop)."""
    return _oil_search(recipes, skip)


def _oil_search(recipes, skip=0):
    import itertools
    last = None
    orders = list(itertools.permutations(("light-oil", "heavy-oil", "petroleum-gas", "water")))
    orders = [o + (("lubricant",) if recipes.get("lubricant") else ()) for o in orders]
    # placement of the cracker row (relative to the refineries) x chemical plant pitch
    arrangements = [f"{a}:{pitch}" for pitch in (5, 6, 7, 8) for a in ("above", "beside", "below", "above_wide")]
    for arr in arrangements:
        for order in orders:
            try:
                out = _oil_interior(recipes, arr, order + ("crude-oil",))
            except RuntimeError as e:
                last = e
                continue
            if skip == 0:
                return out
            skip -= 1
            break          # another variant: move to the next arrangement, not the next pipe order
    raise last


def _oil_interior(recipes, arrangement, order=("water", "heavy-oil", "light-oil", "petroleum-gas", "crude-oil")):
    """Refineries (facing north) in a row; heavy and light crackers (chemical plants facing south) in a row above,
    3 tiles of pipe space between. Pipes inside are routed by plan_nx.route_pipe with every other fluid's ports and
    pipes foreign. Returns the interior plus "networks": fluid -> tiles (consumers branch from any of them)."""
    arrangement, _, wide = arrangement.partition(":")
    step = int(wide or 5)    # chemical plant pitch: wider leaves pipe room between plants
    nref = recipes.get("advanced-oil-processing", 1)
    nh, nl = recipes.get("heavy-oil-cracking", 0), recipes.get("light-oil-cracking", 0)
    nlub = recipes.get("lubricant", 0)
    places, machines = [], []
    for k in range(nref):
        machines.append({"item": "oil-refinery", "x": 2.5 + 6 * k, "y": 13.5, "direction": "north",
                         "recipe": "advanced-oil-processing"})
    if arrangement in ("above", "above_wide"):
        cx, cy, d = 1.5, (2.5 if arrangement == "above" else -1.5), "south"
    elif arrangement == "beside":
        cx, cy, d = 6 * nref + 3.5, 13.5, "north"
    else:
        cx, cy, d = 1.5, 23.5, "north"
    for r, n in (("heavy-oil-cracking", nh), ("light-oil-cracking", nl), ("lubricant", nlub)):
        for _ in range(n):
            machines.append({"item": "chemical-plant", "x": cx, "y": cy, "direction": d, "recipe": r})
            cx += step
    places += machines
    taken = geom.tiles(places)
    ports = {}   # fluid -> list of (tile, io)
    groups = {}  # (machine index, fluid) -> alternative port tiles: one of them is enough
    for k, mc in enumerate(machines):
        for fl, io, dx, dy in fluid_ports(mc["item"], mc["recipe"], mc["direction"]):
            t = (math.floor(mc["x"] + dx), math.floor(mc["y"] + dy))
            ports.setdefault(fl, []).append((t, io))
            groups.setdefault((k, fl), []).append(t)
    allp = {t: f for f, ts in ports.items() for t, _ in ts}
    face = {}    # port tile -> direction from it into its machine (a pipe-to-ground ending there must face it)
    for (k, fl), ts in groups.items():
        mt = geom.footprint(machines[k])
        for t in ts:
            face[t] = next((d for d, (ddx, ddy) in geom.DV4.items() if (t[0] + ddx, t[1] + ddy) in mt), "none")
    x1, y1, x2, y2 = geom.bbox(places)
    g = LocalGrid(x1 - 6, y1 - 6, x2 + 6, y2 + 6, set(taken))
    nets = {}
    pspans = {"v": set(), "h": set()}

    def join(fl, alts):
        """Connect one port of every machine using fluid fl into one network (alts: list of alternative tiles)."""
        if not alts:
            return
        net = nets.setdefault(fl, set())
        if not net:
            net.add(alts[0][0])
        for opts in alts:
            if any(t in net for t in opts):
                continue
            foreign = {p for p, f in allp.items() if f != fl} | {p for f, ts in nets.items() if f != fl for p in ts}
            foreign -= set(opts)
            r = None
            for t in opts:
                r = plan_nx.route_pipe(g, set(net), t, foreign=foreign | (set(opts) - {t}), own=set(net),
                                       spans=pspans, tries=60, goal_dir=face[t])   # t is a machine port
                if r:
                    break
            if not r:
                raise RuntimeError(f"oil[{arrangement}]: no {fl} pipe to {opts}")
            geom.pipe_spans(r, pspans)
            for p in r:
                p.pop("source", None)
                tt = (math.floor(p["x"]), math.floor(p["y"]))
                if not (tt in net and p["item"] == "pipe"):
                    places.append({k: v for k, v in p.items() if k in ("item", "x", "y", "direction")})
                net.add(tt)
                g.blocked.add(tt)
            net.add(t)
    for fl in order:
        join(fl, [ts for (k, f), ts in groups.items() if f == fl])
    for t in [t for f, ts in nets.items() if len(ts) > 1 for t in ts]:   # a one-tile network is a port: left free
        if not any(p["item"] in ("pipe", "pipe-to-ground") and (math.floor(p["x"]), math.floor(p["y"])) == t for p in places):
            places.append({"item": "pipe", "x": t[0] + .5, "y": t[1] + .5})
    # every machine must reach its fluid's network through a piece that opens toward it, with no fluid split
    find, pieces, sides = geom.pipe_components(places)
    for fl in [f for f, ts in nets.items() if len(ts) > 1]:   # one-tile networks are bare ports, joined later
        roots = set()
        for (k, f), ts in groups.items():
            if f != fl:
                continue
            mt = geom.footprint(machines[k])
            ok = [find(t) for t in ts if geom.port_joined(pieces, sides, t, mt)]
            if not ok:
                raise RuntimeError(f"oil[{arrangement}]: {fl} port of {machines[k]['recipe']} not joined")
            roots |= set(ok[:1])
        if len(roots) > 1:
            raise RuntimeError(f"oil[{arrangement}]: {fl} network split in {len(roots)}")
    ext = [{"kind": "fluid", "fluid": f, "x": ts_[0][0] + .5, "y": ts_[0][1] + .5}
           for f, ts_ in nets.items() for ts_ in [sorted(ts_)]]
    if unreachable_ports(places, ext):
        raise RuntimeError(f"oil[{arrangement}]: a network can't reach the fluid side")
    out = finish(places, [])
    dx, dy = geom.bbox(places)[:2]
    out["networks"] = {f: [(x - dx, y - dy) for x, y in ts] for f, ts in nets.items()}
    return out


def acid_interior(skip=0):
    """One chemical plant making sulfuric acid, its iron+sulfur bundle belt and inserter on one side. Variants
    (plant facing x belt side) are generated and the first whose fluid ports reach the fluid side and whose bundle
    port is free is used; skip passes over good ones (repair loop)."""
    for d in ("north", "south", "east", "west"):
        for side in (-1, 1):
            cx_, cy_ = 2.5, 3.5
            ix = cx_ + side * 2          # inserter just outside the plant, belt one further
            bx = cx_ + side * 3
            pl = [{"item": "chemical-plant", "x": cx_, "y": cy_, "direction": d, "recipe": "sulfuric-acid"},
                  {"item": "inserter", "x": ix, "y": cy_, "direction": "east" if side < 0 else "west"},
                  {"item": "small-electric-pole", "x": ix, "y": cy_ - 1}]
            pl += [{"item": "transport-belt", "x": bx, "y": y + .5, "direction": "south"} for y in (2, 3, 4)]
            ports = [{"kind": "bundle", "left": "iron-plate", "right": "sulfur", "x": bx, "y": 1.5, "dir": "south"}]
            for fl, io, dx, dy in fluid_ports("chemical-plant", "sulfuric-acid", d):
                ports.append({"kind": "fluid", "fluid": fl, "io": io, "x": cx_ + dx, "y": cy_ + dy})
            taken = geom.tiles(pl)
            if any((math.floor(p["x"]), math.floor(p["y"])) in taken for p in ports) or unreachable_ports(pl, ports):
                continue
            if skip == 0:
                return finish(pl, ports)
            skip -= 1
    raise RuntimeError("acid: no variant left")
