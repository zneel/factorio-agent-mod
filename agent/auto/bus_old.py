"""Main bus (cheat sheet: group belts in a main bus) in local coordinates; the bus flows south from row 0.

Column of item i: belt at x = 2i, gap column at x = 2i + 1. A column exists from the row its item joins (bus head
for raw items, the producer's output band for products) to its last consumer's tap.
Modules sit west (x < -ZONE) or east (x >= BW + ZONE) of the bus, in dependency order down the bus. Each module
has an input band and an output band: a few rows where every live bus belt runs underground (entrance on the row
above the band, exit on the row below; the gap allows at most 4 rows), so belts can cross the bus on the surface:
  input band   a splitter on the tapped belt two rows above the band sends half into its gap column, which drops
               to the branch's band row and turns toward the module (rows ordered so branches never cross)
  output band  the product's belt comes from the module on its band row, turns down its own column and becomes
               that item's bus belt below the band
Between the bus and a module lies the connector zone; local A* joins the band rows to the module's ports there
(merge stubs for two-item inputs).
"""
import math

from auto import geom

ZONE = 18        # connector zone width between the bus and a module
BAND = 4         # rows per band (underground belt gap: at most 4 tiles between entrance and exit)
SPACING = 3      # free rows between modules on one side


def order_modules(mods, inner, raw):
    """Dependency order: a module after every module producing one of its inputs."""
    prod = {}
    for n, it in inner.items():
        for p in it["ports"]:
            if p["kind"] == "out":
                prod.setdefault(p["item"], set()).add(n)
    need = {n: {i for p in it["ports"] if p["kind"] == "bundle" for i in (p.get("left"), p.get("right")) if i}
            for n, it in inner.items()}
    done, out = set(raw), []
    left = [n for n in mods]
    while left:
        ready = [n for n in left if all(i in done or not (prod.get(i, set()) - {n}) for i in need[n])]
        if not ready:
            raise RuntimeError(f"bus: cycle among {left}")
        n = ready[0]
        out.append(n)
        left.remove(n)
        done |= {p["item"] for p in inner[n]["ports"] if p["kind"] == "out"}
    return out


def plan(inner, order, raw_items, fluid_side="west"):
    """Lay out the bus and its modules. Returns {"places", "modules": {name: (x, y, side)}, "columns",
    "bands": [...], "BW", "length", "head": {item: (x, y)}}. Connections to module ports are made by connect()."""
    items = list(raw_items)
    for n in order:
        for p in inner[n]["ports"]:
            if p["kind"] == "out" and p["item"] not in items:
                items.append(p["item"])
    col = {it: 2 * i for i, it in enumerate(items)}
    BW = 2 * len(items)
    reads = {n: [i for p in inner[n]["ports"] if p["kind"] == "bundle" for i in (p.get("left"), p.get("right")) if i]
             for n in order}
    outs = {n: list(dict.fromkeys(p["item"] for p in inner[n]["ports"] if p["kind"] == "out")) for n in order}
    # vertical slots: modules alternate sides unless they have fluid ports (those go to the fluid side)
    y_side = {"west": 6, "east": 6}
    pos, bands = {}, []
    band_rows = set()
    last_band = 2
    for n in order:
        it = inner[n]
        has_fluid = any(p["kind"] == "fluid" for p in it["ports"])
        side = fluid_side if has_fluid else min(("west", "east"), key=lambda s: y_side[s])
        y0 = max(y_side[side], last_band + 1)
        # input band then output band, both inside the module's y span when it is tall enough
        need_in = math.ceil(len(set(reads[n])) / BAND)
        need_out = math.ceil(len(outs[n]) / BAND)
        rows = []
        yb = max(y0, last_band + 3)
        for _ in range(need_in + need_out):
            rows.append(yb)
            yb += BAND + 4          # splitter row + entrance row above, exit row below, one spare
        last_band = rows[-1] + BAND + 1 if rows else last_band
        h = max(it["h"], (rows[-1] + BAND - y0) if rows else 0)
        x = -ZONE - it["w"] if side == "west" else BW + ZONE
        pos[n] = (x, y0, side)
        y_side[side] = y0 + h + SPACING
        k = 0
        r_in = reads[n]
        for b in range(need_in):
            bands.append({"module": n, "kind": "in", "y": rows[k], "items": list(dict.fromkeys(r_in))[b * BAND:(b + 1) * BAND],
                          "side": side})
            k += 1
        for b in range(need_out):
            bands.append({"module": n, "kind": "out", "y": rows[k], "items": outs[n][b * BAND:(b + 1) * BAND], "side": side})
            k += 1
    length = max(max(y_side.values()), last_band + 2)
    # column spans: raw items from the head, products from their output band; to their last input band
    start = {it: 0 for it in raw_items}
    end = {}
    for b in bands:
        if b["kind"] == "out":
            for it in b["items"]:
                start[it] = b["y"]
        else:
            for it in b["items"]:
                end[it] = max(end.get(it, 0), b["y"])
    places = []
    for it in items:
        if it not in end:
            continue   # nobody reads it from the bus (e.g. a final product handled elsewhere)
        x = col[it]
        y = start.get(it, 0)
        if start.get(it, 0) and it not in raw_items:
            y = start[it] + BAND   # a product's belt runs from below its output band
        ylast = end[it] - 2        # its last splitter row
        _column(places, x, y, ylast, bands, it, start.get(it))
    return {"places": places, "modules": pos, "columns": col, "bands": bands, "BW": BW, "length": length,
            "items": items, "start": start, "end": end, "raw": list(raw_items)}


def _column(places, x, y0, y1, bands, item, joined):
    """Bus belt of one item from row y0 to y1 (south), diving underground through every band it passes."""
    band_tops = sorted(b["y"] for b in bands if y0 < b["y"] and b["y"] - 2 <= y1 + 2)
    y = y0
    while y <= y1 + 1:
        dive = next((t for t in band_tops if t - 1 == y), None)
        if dive is not None:
            tapped = any(b["y"] == dive and b["kind"] == "in" and item in b["items"] for b in bands)
            places.append({"item": "underground-belt", "x": x + .5, "y": y + .5, "direction": "south", "belt_type": "input"})
            places.append({"item": "underground-belt", "x": x + .5, "y": dive + BAND + .5, "direction": "south",
                           "belt_type": "output"})
            y = dive + BAND + 1
            continue
        tap = next((b for b in bands if b["kind"] == "in" and item in b["items"] and b["y"] - 2 == y), None)
        if tap:
            places.append({"item": "splitter", "x": x + 1.0, "y": y + .5, "direction": "south"})
        else:
            places.append({"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": "south"})
        y += 1


def bands_places(bus):
    """Surface belts inside the bands. Returns (places, edges): edges[(module, item, kind)] = (tile, dir) where the
    connector-zone route starts (input: leaving the bus edge toward the module) or ends (output: entering it)."""
    col, BW = bus["columns"], bus["BW"]
    places, edges = [], {}
    for b in bus["bands"]:
        y, side = b["y"], b["side"]
        west = side == "west"
        edge_x = -1 if west else BW
        if b["kind"] == "in":
            # branches farther from the module turn later (lower rows) so no vertical crosses a horizontal
            its = sorted(b["items"], key=lambda i: col[i], reverse=not west)
            for j, it in enumerate(its):
                gx = col[it] + 1
                for yy in range(y - 1, y + j):          # gap column: down from the splitter to its band row
                    places.append({"item": "transport-belt", "x": gx + .5, "y": yy + .5, "direction": "south"})
                d = "west" if west else "east"
                xs = range(gx, edge_x - 1, -1) if west else range(gx, edge_x + 1)
                for xx in xs:
                    places.append({"item": "transport-belt", "x": xx + .5, "y": y + j + .5, "direction": d})
                edges[(b["module"], it, "in")] = ((edge_x - 1 if west else edge_x + 1, y + j), d)
        else:
            # outputs: the column farthest from the module gets the top row, so horizontals never cross verticals
            its = sorted(b["items"], key=lambda i: col[i], reverse=west)
            for j, it in enumerate(its):
                cx = col[it]
                d = "east" if west else "west"
                xs = range(edge_x, cx) if west else range(edge_x, cx, -1)
                for xx in xs:
                    places.append({"item": "transport-belt", "x": xx + .5, "y": y + j + .5, "direction": d})
                for yy in range(y + j, y + BAND):
                    places.append({"item": "transport-belt", "x": cx + .5, "y": yy + .5, "direction": "south"})
                edges[(b["module"], it, "out")] = ((edge_x - 1 if west else edge_x + 1, y + j), d)
    return places, edges


class Grid:
    """Occupancy for local routing (plan_nx Grid interface)."""

    def __init__(self, box, blocked, pipes=None):
        self.x1, self.y1, self.x2, self.y2 = box
        self.blocked = blocked
        self.pipes = pipes or {}

    def at(self, x, y):
        if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2):
            return "#"
        if (x, y) in self.pipes:
            return "p"
        return "#" if (x, y) in self.blocked else "."

    def free(self, x, y, ore_ok=True):
        return self.at(x, y) == "."


DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
LEFTOF = {"north": "west", "south": "east", "east": "north", "west": "south"}
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}


MIRROR = {"east": "west", "west": "east", "north": "north", "south": "south"}


def mirror(it):
    """Mirror a module interior left-right (x -> w - x). Directions east<->west; a bundle's left and right lanes swap
    (the same physical lanes, seen from the other direction)."""
    w = it["w"]
    pl = [dict(p, x=w - p["x"], direction=MIRROR[p["direction"]]) if "direction" in p else dict(p, x=w - p["x"])
          for p in it["places"]]
    ports = []
    for p in it["ports"]:
        q = dict(p, x=w - p["x"])
        if "dir" in q:
            q["dir"] = MIRROR[q["dir"]]
        if q["kind"] == "bundle":
            q["left"], q["right"] = p.get("right"), p.get("left")
        ports.append(q)
    return dict(it, places=pl, ports=ports)


def facing(it, side):
    """How many belt ports point toward the bus (east-side modules: west-flowing in / out ports on the west)."""
    want = "west" if side == "east" else "east"
    n = 0
    for p in it["ports"]:
        if p["kind"] == "bundle":
            n += p["dir"] == ("east" if side == "east" else "west")   # entry flows into the block from the bus side
        elif p["kind"] == "out":
            n += p["dir"] == want
    return n


def complex_layout(inner, order, raw_items, fluid_side="west"):
    """Bus + bands + modules + connector routes, in local coordinates. Returns dict with places, fluid ports
    (world-local), head entries, failures."""
    import plan_nx
    bus = plan(inner, order, raw_items, fluid_side)
    places = list(bus["places"])
    bp, edges = bands_places(bus)
    places += bp
    mods = {}
    for n, (x, y, side) in bus["modules"].items():
        it = inner[n]
        if not any(p["kind"] == "fluid" for p in it["ports"]) and facing(mirror(it), side) > facing(it, side):
            it = mirror(it)
        pl = geom.translate(it["places"], x, y)
        ports = [dict(p, x=p["x"] + x, y=p["y"] + y) for p in it["ports"]]
        mods[n] = {"places": pl, "ports": ports, "side": side, "x": x, "y": y, "w": it["w"], "h": it["h"],
                   "networks": {f: [(a + x, b + y) for a, b in ts] for f, ts in (it.get("networks") or {}).items()}}
        places += pl
    blocked = geom.tiles(places)
    spans = {"v": set(), "h": set()}
    for i, q in enumerate(places):
        if q["item"] == "underground-belt" and q.get("belt_type") == "input":
            for r in places[i + 1:i + 2]:
                if r["item"] == "underground-belt":
                    a, b = (math.floor(q["x"]), math.floor(q["y"])), (math.floor(r["x"]), math.floor(r["y"]))
                    if a[0] == b[0]:
                        spans["v"].update((a[0], yy) for yy in range(min(a[1], b[1]), max(a[1], b[1]) + 1))
    # reserve: every module port tile and the tile beyond each out port
    reserved = set()
    for n, mo in mods.items():
        for p in mo["ports"]:
            t = (math.floor(p["x"]), math.floor(p["y"]))
            reserved.add(t)
            if p["kind"] == "out":
                reserved.add((t[0] + DV[p["dir"]][0], t[1] + DV[p["dir"]][1]))
    for (n, it, kind), (t, d) in edges.items():
        reserved.add(t)
    fails = []
    xs = [math.floor(p["x"]) for p in places]
    ys = [math.floor(p["y"]) for p in places]
    box = (min(xs) - 6, min(ys) - 6, max(xs) + 7, max(ys) + 7)

    def route(start, goal, fd, td, mine):
        for m in (6, 16, 40):
            wb = (max(box[0], min(start[0], goal[0]) - m), max(box[1], min(start[1], goal[1]) - m),
                  min(box[2], max(start[0], goal[0]) + m + 1), min(box[3], max(start[1], goal[1]) + m + 1))
            r = _route(Grid(wb, blocked | (reserved - set(mine))), start, goal, fd, td)
            if r:
                return r
        return None

    def _route(g, start, goal, fd, td):
        for _ in range(6):
            r = plan_nx.route_belt(g, start, goal, fd, td, spans=spans)
            if not r:
                return None
            tiles = [(math.floor(q["x"]), math.floor(q["y"])) for q in r]
            if len(set(tiles)) == len(tiles):
                return r
            dup = next(t for t in tiles if tiles.count(t) > 1)
            g.blocked = g.blocked | {dup}
        return None

    def add_spans(pl):
        for i_, q in enumerate(pl):
            if q["item"] == "underground-belt" and q.get("belt_type") == "input":
                for r_ in pl[i_ + 1:i_ + 3]:
                    if r_["item"] == "underground-belt" and r_.get("belt_type") == "output":
                        a, b = (math.floor(q["x"]), math.floor(q["y"])), (math.floor(r_["x"]), math.floor(r_["y"]))
                        if a[0] == b[0]:
                            spans["v"].update((a[0], yy) for yy in range(min(a[1], b[1]), max(a[1], b[1]) + 1))
                        else:
                            spans["h"].update((xx, a[1]) for xx in range(min(a[0], b[0]), max(a[0], b[0]) + 1))
                        break

    def commit(pl, tag=""):
        add_spans(pl)
        for q in pl:
            q["_t"] = tag
        clash = geom.tiles(pl) & blocked
        if clash and tag:
            fails.append(f"clash {tag} {sorted(clash)[:2]}")
        places.extend(pl)
        blocked.update(geom.tiles(pl))

    def free(t):
        return box[0] <= t[0] < box[2] and box[1] <= t[1] < box[3] and t not in blocked and t not in reserved

    for n, mo in mods.items():
        west = mo["side"] == "west"
        first_out = {}
        for p in mo["ports"]:
            if p["kind"] == "out":
                t0 = (math.floor(p["x"]), math.floor(p["y"]))
                e = edges.get((n, p["item"], "out"))
                if e and p["item"] not in first_out:
                    r = route(t0, e[0], p["dir"], e[1], {t0, e[0]})
                    if r is None:
                        fails.append(f"{n} out {p['item']}")
                        continue
                    commit(r, 'out278')
                    first_out[p["item"]] = r
                    continue
                trunk = first_out.get(p["item"])
                if not trunk:
                    continue
                # a second export belt of the same item side-loads onto the first one's route
                done_ = False
                for q in sorted([q for q in trunk[1:-1] if q["item"] == "transport-belt"],
                                key=lambda q: abs(q["x"] - t0[0]) + abs(q["y"] - t0[1])):
                    d0 = q["direction"]
                    for sd in (LEFTOF[d0], OPP[LEFTOF[d0]]):
                        tt = (math.floor(q["x"]) + DV[sd][0], math.floor(q["y"]) + DV[sd][1])
                        if not free(tt) and tt != t0:
                            continue
                        r = route(t0, tt, p["dir"], OPP[sd], {t0, tt}) if tt != t0 else                             ([{"item": "transport-belt", "x": t0[0] + .5, "y": t0[1] + .5, "direction": OPP[sd]}]
                             if p["dir"] == OPP[sd] else None)
                        if r:
                            commit(r, 'out2')
                            done_ = True
                            break
                    if done_:
                        break
                if not done_:
                    fails.append(f"{n} second export {p['item']}")
        for p in mo["ports"]:
            if p["kind"] != "bundle":
                continue
            port = (math.floor(p["x"]), math.floor(p["y"]))
            d = p["dir"]
            items = [i for i in (p.get("left"), p.get("right")) if i]
            # merge stub M near the port: try spots until the stub->port and both feeds route; roll back otherwise
            lx, ly = DV[LEFTOF[d]]
            cands = []
            for rr in range(2, 14):
                for cand in [(port[0] + dx, port[1] + dy) for dx in range(-rr, rr + 1) for dy in range(-rr, rr + 1)
                             if max(abs(dx), abs(dy)) == rr]:
                    ts = [cand, (cand[0] + lx, cand[1] + ly), (cand[0] - lx, cand[1] - ly),
                          (cand[0] + DV[d][0], cand[1] + DV[d][1]), (cand[0] - DV[d][0], cand[1] - DV[d][1])]
                    if all(free(t) for t in ts):
                        cands.append(cand)
            ok = False
            for mt in cands[:12]:
                snap = (len(places), set(blocked), set(reserved))
                stub = [{"item": "transport-belt", "x": mt[0] + .5, "y": mt[1] + .5, "direction": d}]
                rear = (mt[0] - DV[d][0], mt[1] - DV[d][1])
                if len(items) == 1:
                    stub.append({"item": "transport-belt", "x": rear[0] + .5, "y": rear[1] + .5, "direction": d})
                commit(stub)
                reserved.update({(mt[0] + lx, mt[1] + ly), (mt[0] - lx, mt[1] - ly)})
                good = True
                r = route((mt[0] + DV[d][0], mt[1] + DV[d][1]), port, d, d, {port})
                if r is None:
                    good = False
                else:
                    commit(r, 'route310')
                for it_, sd in ((p.get("left"), LEFTOF[d]), (p.get("right"), OPP[LEFTOF[d]])):
                    if not it_ or not good:
                        continue
                    e = edges.get((n, it_, "in"))
                    goal = (mt[0] + DV[sd][0], mt[1] + DV[sd][1])
                    r = route(e[0], goal, e[1], OPP[sd], {e[0], goal}) if e else None
                    if r is None:
                        good = False
                    else:
                        commit(r, 'route320')
                if good:
                    ok = True
                    break
                del places[snap[0]:]          # roll back this attempt
                blocked.clear(); blocked.update(snap[1])
                reserved.clear(); reserved.update(snap[2])
            if not ok:
                fails.append(f"{n} bundle {items}")
    head = {it: (bus["columns"][it], -1) for it in bus["raw"]}
    fluid_ports = [dict(p, module=n) for n, mo in mods.items() for p in mo["ports"] if p["kind"] == "fluid"]
    networks = {n: mo["networks"] for n, mo in mods.items() if mo["networks"]}
    return {"places": places, "fails": fails, "head": head, "bus": bus, "fluid_ports": fluid_ports,
            "networks": networks, "box": box, "spans": spans}


def route_fluids(cx, fluid_side="west"):
    """Pipe trees inside the complex. Water and crude come in at inlet tiles on the fluid side's outer edge."""
    import plan_nx
    places = cx["places"]
    blocked = geom.tiles(places)
    pipes = {}
    for n, nets in cx["networks"].items():
        for f, ts in nets.items():
            for t in ts:
                pipes[tuple(t)] = f
    ports = cx["fluid_ports"]
    for p in ports:
        pipes.setdefault((math.floor(p["x"]), math.floor(p["y"])), p["fluid"])  # port tiles: foreign to others
    box = cx["box"]
    xs = [p["x"] for p in ports] + [t[0] for nets in cx["networks"].values() for ts in nets.values() for t in ts]
    edge_x = (min(xs) - 4) if fluid_side == "west" else (max(xs) + 4)
    oil_y = min(t[1] for nets in cx["networks"].values() for ts in nets.values() for t in ts) if cx["networks"] else 0
    inlets = {"water": (int(edge_x), int(oil_y) + 2), "crude-oil": (int(edge_x), int(oil_y) + 6)}
    fails, out = [], []
    src = {}
    for n, nets in cx["networks"].items():
        for f, ts in nets.items():
            if f in ("petroleum-gas", "light-oil", "heavy-oil"):
                src.setdefault(f, set()).update(tuple(t) for t in ts)
    for p in ports:
        if p["io"] == "output":
            src.setdefault(p["fluid"], set()).add((math.floor(p["x"]), math.floor(p["y"])))
    for f, t in inlets.items():
        src.setdefault(f, set()).add(t)
        pipes[t] = f
        out.append({"item": "pipe", "x": t[0] + .5, "y": t[1] + .5})
    sinks = [p for p in ports if "input" in p["io"]]
    for n, nets in cx["networks"].items():   # oil units take water and crude
        for f in ("water", "crude-oil"):
            if nets.get(f):
                sinks.append({"fluid": f, "x": nets[f][0][0] + .5, "y": nets[f][0][1] + .5, "module": n, "io": "input"})
    by_machine = set()
    for p in sorted(sinks, key=lambda p: (p["fluid"] != "crude-oil", p["fluid"] != "water")):
        f = p["fluid"]
        t = (math.floor(p["x"]), math.floor(p["y"]))
        key = (p["module"], round(p["x"] / 3), round(p["y"] / 3), f)   # one port per machine is enough
        net = src.get(f, set())
        if not net:
            fails.append(f"no source for {f}")
            continue
        if t in net or key in by_machine:
            continue
        if pipes.get(t) == f and p.get("module", "").startswith("oil"):
            # an oil unit's own network tile: aim for a free tile beside that network instead
            mnet = [tuple(q) for q in cx["networks"][p["module"]].get(f, [])]
            cands = [(a + ddx, b + ddy) for a, b in mnet for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            cands = [c for c in cands if c not in blocked and c not in pipes and
                     not any(pipes.get((c[0] + ex, c[1] + ey), f) != f for ex, ey in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
            if not cands:
                fails.append(f"no free tile beside {p['module']} {f} network")
                continue
            t = min(cands, key=lambda c: min(abs(c[0] - s_[0]) + abs(c[1] - s_[1]) for s_ in net))
        foreign = {q for q, g in pipes.items() if g != f}
        own = {q for q, g in pipes.items() if g == f} | net
        r = None
        near = min(net, key=lambda s: abs(s[0] - t[0]) + abs(s[1] - t[1]))
        for m in (8, 20, 45):
            wb = (min(near[0], t[0]) - m, min(near[1], t[1]) - m, max(near[0], t[0]) + m + 1, max(near[1], t[1]) + m + 1)
            g = Grid(wb, blocked - own - {t}, {q: g_ for q, g_ in pipes.items() if g_ != f})
            starts = {s for s in net if wb[0] <= s[0] < wb[2] and wb[1] <= s[1] < wb[3]}
            r = plan_nx.route_pipe(g, starts, t, foreign=foreign - {t}, own=own, tries=30)
            if r:
                break
        if not r:
            fails.append(f"pipe {f} -> {p['module']}")
            continue
        r = [{k: v for k, v in q.items() if k in ("item", "x", "y", "direction")} for q in r
             if not ((math.floor(q["x"]), math.floor(q["y"])) in own and q["item"] == "pipe")]
        out += r
        for q in r:
            tt = (math.floor(q["x"]), math.floor(q["y"]))
            blocked.add(tt)
            pipes[tt] = f
            net.add(tt)
        net.add(t)
        by_machine.add(key)
    cx["places"] += out
    cx["inlets"] = inlets
    cx["pipe_fluids"] = {t: f for t, f in pipes.items()}
    cx["fails"] += fails
    return out
