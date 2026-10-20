"""Stage 5c: everything outside the bus complex (see site.py for the complex placement)."""
import math

import plan_nx
from auto import fluidseg, geom, interiors, rules
from auto.route import DV, LEFT, OPP, T, World, add_spans, route_belt, splitter_tap, step
from auto.site import free_rect, rot_dir, rot_tile, transform


def connect(m, cx, k, dx, dy, columns, boilers, wells_needed):
    """columns: {"iron-ore": [...], "copper-ore": [...], "stone": [...], "coal": [...], "raw-iron-ore": [...]}.
    Returns (places, fails, notes, world)."""
    from auto.factory import power_plant
    world = World(m, (m.x0, m.y0, m.x0 + m.w, m.y0 + m.h))
    cplaces = transform(cx["places"], k, dx, dy)
    world.add(cplaces)
    add_spans(world, cplaces)
    out, fails, notes = [], [], []
    cur = {"grp": "world"}   # plan group of what commit() is placing now

    def commit(pl, fluid=None):
        geom.tag(pl, cur["grp"])
        world.add(pl, fluid)
        add_spans(world, pl)
        out.extend(pl)

    def W(t):   # local complex tile -> world tile
        x, y = rot_tile(t, k)
        return (x + dx, y + dy)
    for t, f in cx.get("pipe_fluids", {}).items():   # the complex's pipes, by fluid: same-fluid routes may join them
        world.pipes[W(t)] = f
    head = {it: W(t) for it, t in cx["head"].items()}
    head_dir = rot_dir("south", k)
    for t in head.values():
        world.reserved[t] = ("head",)
    for key, cols in columns.items():
        for ci, c in enumerate(cols):
            cur["grp"] = f"mine:{key}:{ci}"
            commit(c["places"])
    # ---- steam plant at the shore site nearest the bus head
    hc = (sum(t[0] for t in head.values()) / len(head), sum(t[1] for t in head.values()) / len(head))
    plant = None
    for s in sorted(m.pumps, key=lambda s: (s[0] - hc[0]) ** 2 + (s[1] - hc[1]) ** 2):
        pl = power_plant(s, boilers)
        ts = geom.tiles(pl)
        ring = {(x + d[0], y + d[1]) for x, y in ts for d in DV.values()} - ts
        # no pipe of the complex beside it: one at an engine's end would join the steam to its segment
        if all(world.free(*t) for t in ts) and not any(t in world.pipes for t in ring):
            plant = pl
            notes.append(f"steam plant: {boilers} boilers at {s}")
            break
    if not plant:
        fails.append("no shore site for the steam plant")
    else:
        cur["grp"] = "power"
        commit(plant)
        for q in plant:   # steam joints (engine ends, boiler outputs) and the plant's own water pipes: world pipes
            if q["item"] in ("pipe", "pipe-to-ground"):   # keep off them (a pipe beside one joins its segment)
                world.pipes[T(q["x"], q["y"])] = "plant-water"
            elif q["item"] in ("steam-engine", "boiler"):
                d = DV[q["direction"]]
                ends = (2, -2) if q["item"] == "steam-engine" else (.5,)
                for s in ends:
                    world.pipes[T(q["x"] + s * d[0], q["y"] + s * d[1])] = "steam"
    # ---- reserve every column's start stub and end tile before anything is routed
    for cs in columns.values():
        for c in cs:
            world.reserved[step(T(*c["end"]), c["flow"])] = ("col-end", c["end"])
            st = step(T(*c["start"]), OPP[c["flow"]])
            for t in (st, step(st, OPP[c["flow"]])):
                world.reserved[t] = ("col-start", c["start"])
    # ---- water pump for the complex (piped at the end)
    inl = {f: W(t) for f, t in cx.get("inlets", {}).items()}
    for t in inl.values():
        world.reserved[t] = ("inlet",)
    water_out = None
    for s in sorted(m.pumps, key=lambda s: (s[0] - inl["water"][0]) ** 2 + (s[1] - inl["water"][1]) ** 2):
        p = {"item": "offshore-pump", "x": s[0], "y": s[1], "direction": s[2]}
        o = step(T(s[0], s[1]), OPP[s[2]])
        if all(world.free(*t) for t in geom.tiles([p])) and world.free(*o) and o not in world.reserved:
            cur["grp"] = "pipe:water"
            commit([p])
            commit([{"item": "pipe", "x": o[0] + .5, "y": o[1] + .5}], "water")
            water_out = o
            break
    # ---- pumpjacks on the wells nearest the crude inlet (piped at the end)
    jacks = []
    for w in sorted(m.wells, key=lambda w: (w[0] - inl["crude-oil"][0]) ** 2 + (w[1] - inl["crude-oil"][1]) ** 2):
        if len(jacks) >= wells_needed:
            break
        pj = {"item": "pumpjack", "x": w[0], "y": w[1], "direction": "north"}
        j = T(w[0] + 1, w[1] - 2)    # a north-facing pumpjack outputs at (+1, -2)
        if not all(world.free(*t) for t in geom.tiles([pj])) or not world.free(*j):
            continue
        cur["grp"] = "oil-wells"
        commit([pj])
        commit([{"item": "pipe", "x": j[0] + .5, "y": j[1] + .5}], "crude-oil")
        jacks.append(j)
    # ---- ore units: a smelting column near each iron/copper column end
    trunks = {}
    for ore, plate in (("iron-ore", "iron-plate"), ("copper-ore", "copper-plate")):
        for c in columns.get(ore, []):
            fur = math.ceil(c["drills"] * rules.DRILL["electric-mining-drill"] * rules.SMELT_TIME[plate])
            sm = interiors.smelter_interior(math.ceil(fur / 2), ore)
            end = step(T(*c["end"]), c["flow"])
            pos = free_rect(world, sm["w"], sm["h"], (end[0] + 3, end[1] + 3))
            if not pos:
                fails.append(f"no room for the {ore} smelter")
                continue
            cur["grp"] = f"smelt:{ore}:{columns.get(ore, []).index(c)}"
            commit(geom.translate(sm["places"], *pos))
            top = next(p for p in sm["ports"] if p["kind"] == "bundle")
            port = T(top["x"] + pos[0], top["y"] + pos[1])
            r = route_belt(world, end, port, c["flow"], "south", ("ore", port))
            if r is None:
                fails.append(f"{ore} column -> smelter")
            else:
                commit(r)
            o = next(p for p in sm["ports"] if p["kind"] == "out")
            ot = T(o["x"] + pos[0], o["y"] + pos[1])
            for t_ in (ot, step(ot, o["dir"]), step(ot, o["dir"], 2)):   # keep the plates' way out free
                world.reserved[t_] = ("src", ot)
            trunks.setdefault(plate, []).append((ot, o["dir"]))
            c["coal_goal"] = coal_stub(world, c, commit)
    for item, cols_ in (("stone", columns.get("stone", [])), ("iron-ore", columns.get("raw-iron-ore", []))):
        for c in cols_:
            trunks.setdefault(item, []).append((step(T(*c["end"]), c["flow"]), c["flow"]))
    # ---- coal: columns -> ore column stubs, boilers, bus head
    coal_sinks = [(c["coal_goal"][0], c["coal_goal"][1], ("col", c["coal_goal"][0])) for cs in columns.values()
                  for c in cs if c.get("coal_goal")]
    if plant:
        b0 = next(p for p in plant if p["item"] == "transport-belt")   # the boiler belt's first tile
        bt = T(b0["x"], b0["y"])
        coal_sinks.append((step(bt, OPP[b0["direction"]]), b0["direction"], ("boilers",)))
    sinks = {it: [(t, head_dir, ("head", it))] for it, t in head.items()}   # the trunk's last belt is the head tile
    sinks["coal"] = coal_sinks + sinks.get("coal", [])
    trunks["coal"] = [(step(T(*c["end"]), c["flow"]), c["flow"]) for c in columns.get("coal", [])]
    cbelts = {T(q["x"], q["y"]): q for q in cplaces if q["item"] == "transport-belt"}
    for item, sk in sinks.items():
        srcs = trunks.get(item, [])
        if not srcs:
            fails.append(f"no trunk for {item}")
            continue
        extra = []
        if item in head:   # the bus column's first belts below the head also take side-loads
            for kk in range(1, 14):
                q = cbelts.get(step(head[item], head_dir, kk))
                if q and q["direction"] == head_dir:
                    extra.append(q)
        cur["grp"] = "coal" if item == "coal" else f"trunk:{item}"
        tree_route(world, srcs, sk, commit, fails, item, extra)
    # ---- pipes last: they hop belts with pipe-to-ground
    cur["grp"] = "pipe:water"
    if water_out:
        r = pipe(world, {water_out}, inl["water"], "water")
        if r is None:
            fails.append("water pipe")
        else:
            r = pumped(world, r, "goal")
            if r is None:
                fails.append("water pipe: no straight stretch for a pump")
            else:
                commit(r, "water")
    net = set()
    cur["grp"] = "pipe:crude"
    for j in jacks:
        r = pipe(world, {j}, inl["crude-oil"], "crude-oil") if not net else pipe(world, net, j, "crude-oil")
        if r is None:
            fails.append(f"crude pipe from {j}")
            continue
        r = [q for q in r if T(q["x"], q["y"]) not in (j,) or q["item"] != "pipe"]
        r = pumped(world, r, "goal" if not net else "start")   # the crude flows toward the inlet / the net
        if r is None:
            fails.append(f"crude pipe from {j}: no straight stretch for a pump")
            continue
        commit(r, "crude-oil")
        net |= {T(q["x"], q["y"]) for q in r if q["item"] != "pump"} | {j, inl["crude-oil"]}
    return out, fails, notes, world


def pumped(world, r, attach_end):
    """Route r (start -> goal) with pumps where the segment it joins at attach_end (the fluid flows that way) would
    pass the game's pipeline extent (fluidseg). None: no straight stretch takes a pump."""
    if not r:
        return r
    end = T(r[-1]["x"], r[-1]["y"]) if attach_end == "goal" else T(r[0]["x"], r[0]["y"])
    pieces = fluidseg.pieces_of(world.places)
    near = {end} | {(end[0] + dx, end[1] + dy) for dx, dy in DV.values()}
    attach = set()
    for s in fluidseg.segments(None, pieces):
        if s & near:
            attach |= s
    return fluidseg.split_route(r, attach, attach_end=attach_end)


def coal_stub(world, c, commit):
    """Side-load stub before a drill column's first belt: coal comes from the far side (drills fill the near lane)."""
    st = T(*c["start"])
    d = c["flow"]
    stub = step(st, OPP[d])
    rear = step(stub, OPP[d])
    commit([{"item": "transport-belt", "x": stub[0] + .5, "y": stub[1] + .5, "direction": d},
            {"item": "transport-belt", "x": rear[0] + .5, "y": rear[1] + .5, "direction": d}])
    drill = next(q for q in c["places"] if q["item"] == "electric-mining-drill")
    near = None
    for sd in (LEFT[d], OPP[LEFT[d]]):
        v = DV[sd]
        if (drill["x"] - (st[0] + .5)) * v[0] + (drill["y"] - (st[1] + .5)) * v[1] > 0:
            near = sd
    far = OPP[near]
    goal = step(stub, far)
    world.reserved[goal] = ("col", goal)
    return (goal, OPP[far])


def tree_route(world, srcs, sinks, commit, fails, item, extra=()):
    src, sdir = srcs[0]
    tree = []
    if len(srcs) > 1:
        # a straight collector from the first source: the other sources side-load onto it, the tree starts after it
        L = 3 * len(srcs) + 2
        seg = [step(src, sdir, i) for i in range(L)]
        if all((world.free(*t) or t == src) and (t not in world.reserved or world.reserved[t][0] == "src") for t in seg):
            col = [{"item": "transport-belt", "x": t[0] + .5, "y": t[1] + .5, "direction": sdir} for t in seg]
            for t in seg:
                world.reserved.pop(t, None)
            commit(col)
            extra = list(extra) + col[1:-1]
            tree += col
            src = step(seg[-1], sdir)
    for goal, gd, owner in sorted(sinks, key=lambda s: abs(s[0][0] - src[0]) + abs(s[0][1] - src[1])):
        world.reserved.pop(goal, None)
        r = None
        if not tree or (len(srcs) > 1 and len(tree) == 3 * len(srcs) + 2):
            world.reserved.pop(src, None)
            r = route_belt(world, src, goal, sdir, gd, owner)
        else:
            for old, sp, o, d in splitter_tap(world, tree, goal, owner)[:8]:
                r = route_belt(world, o, goal, d, gd, owner)
                if r:
                    tree.remove(old)
                    commit([dict(old, remove=True), sp])
                    break
        if r is None:
            fails.append(f"{item} -> {owner}")
            continue
        commit(r)
        tree += r
    lane = {"left": 1, "right": 1}   # sources per trunk lane (the first source fills both): side-loads alternate
    for s2, d2 in srcs[1:]:
        best = None
        front = step(s2, d2)
        tb = {T(q["x"], q["y"]): q for q in tree if q["item"] == "transport-belt"}
        if front in tb and tb[front]["direction"] not in (d2, OPP[d2]):   # trunk right in front: one belt side-loads
            world.reserved.pop(s2, None)
            commit([{"item": "transport-belt", "x": s2[0] + .5, "y": s2[1] + .5, "direction": d2}])
            continue
        for p in [q for q in tree if q["item"] == "transport-belt"][1:-1] + list(extra):
            d = p["direction"]
            for sd in (LEFT[d], OPP[LEFT[d]]):
                tt = step(T(p["x"], p["y"]), sd)
                if world.free(*tt) and tt not in world.reserved:
                    side = "left" if sd == LEFT[d] else "right"
                    dist = abs(tt[0] - s2[0]) + abs(tt[1] - s2[1])
                    key = (lane[side], dist)        # the emptier lane first, then the nearest tile
                    if best is None or key < best[0]:
                        best = (key, tt, OPP[sd], side)
        r = None
        if best:
            r = route_belt(world, s2, best[1], d2, best[2], ("src", s2))
        if r is None and best:   # the emptier lane's tile is unreachable: any reachable tile, nearest first
            cands = []
            for p in [q for q in tree if q["item"] == "transport-belt"][1:-1] + list(extra):
                d = p["direction"]
                for sd in (LEFT[d], OPP[LEFT[d]]):
                    tt = step(T(p["x"], p["y"]), sd)
                    if world.free(*tt) and tt not in world.reserved:
                        cands.append((abs(tt[0] - s2[0]) + abs(tt[1] - s2[1]), tt, OPP[sd],
                                      "left" if sd == LEFT[d] else "right"))
            for c_ in sorted(cands)[:6]:
                r = route_belt(world, s2, c_[1], d2, c_[2], ("src", s2))
                if r:
                    best = c_
                    break
        if r is None:
            fails.append(f"{item} extra source {s2}")
            continue
        lane[best[3]] += 1
        commit(r)
    return tree


def pipe(world, starts, goal, fluid):
    r = _pipe(world, starts, goal, fluid)
    if r is None:
        print(f"pipe {fluid} {sorted(starts)[:3]} -> {goal} failed; around goal:", flush=True)
        for y in range(goal[1] - 4, goal[1] + 5):
            print("   " + "".join("G" if (x, y) == goal else ("p" if (x, y) in world.pipes else
                                  ("r" if (x, y) in world.reserved else world.at(x, y)))
                                  for x in range(goal[0] - 10, goal[0] + 11)), flush=True)
    return r


def _pipe(world, starts, goal, fluid):
    if world.pipes.get(goal) == fluid and goal in world.blocked:
        r = _pipe_to(world, starts, goal, fluid)   # end on the same-fluid pipe itself (its own piece stays)
        if r is not None:
            return r
        # the goal already holds a pipe of this fluid: end on a free tile beside it instead
        for d in ("north", "south", "east", "west"):
            n = step(goal, d)
            if world.free(*n) and n not in world.reserved and not any(
                    world.pipes.get(step(n, e), fluid) != fluid for e in ("north", "south", "east", "west")):
                r = _pipe_to(world, starts, n, fluid)
                if r is not None:
                    return r
        return None
    return _pipe_to(world, starts, goal, fluid)


def _pipe_to(world, starts, goal, fluid):
    foreign = {t for t, f in world.pipes.items() if f != fluid}
    near = min(starts, key=lambda s: abs(s[0] - goal[0]) + abs(s[1] - goal[1]))
    for mg in (8, 20, 50, 120, 260):
        g = world.view(("fluid", fluid), (min(near[0], goal[0]) - mg, min(near[1], goal[1]) - mg,
                                          max(near[0], goal[0]) + mg + 1, max(near[1], goal[1]) + mg + 1))
        st = {s for s in starts if g.x1 <= s[0] < g.x2 and g.y1 <= s[1] < g.y2}
        world.reserved.pop(goal, None)
        if not hasattr(world, "pspans"):
            world.pspans = geom.pipe_spans([q for q in world.places if q["item"] == "pipe-to-ground"])
        r = plan_nx.route_pipe(g, st, goal, foreign=foreign, spans=world.pspans, goal_dir="none",
                               own=st | {t for t, f in world.pipes.items() if f == fluid}, tries=30)
        if r:
            # starts and same-fluid pipes already there (the route may run along them) keep their own pieces
            r = [{k_: v for k_, v in q.items() if k_ in ("item", "x", "y", "direction")} for q in r
                 if not (q["item"] == "pipe" and (T(q["x"], q["y"]) in st or
                                                  (world.pipes.get(T(q["x"], q["y"])) == fluid and
                                                   T(q["x"], q["y"]) in world.blocked)))]
            geom.pipe_spans(r, world.pspans)
            return r
    return None
