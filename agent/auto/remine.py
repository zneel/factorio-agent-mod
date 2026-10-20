"""A mine column that has run dry, replaced from the ore that is still there (closed loop: grow toward the targets).
The nearest live patch may be far (starting patches are small): the new ore belt runs back to the old column.

    places, info = remine.plan(q, m, plan, live, group)

1. The ore map of the patch area from the running game (resource entities with ore left), not the seed's map.
2. The column's drills that live-report no minable resources are picked up (they are reused); the working ones
   stay. Its belt stays: it carries the fuel lane to the smelter.
3. New drill columns (auto.mines on the live ore map) on free ground, flowing toward the old column, as many drills
   as are dry or gone.
4. The new column's belt is routed (networkx A*, plan_nx.route_belt) to a tile beside the old belt on the drills'
   side and side-loads onto it there: that is the ore lane, the coal lane stays as it is.
Every route keeps its undergrounds off the axis spans of existing pairs (plan pieces, our own pieces, and the
pairs the game made live): the game pairs an underground with the nearest one on its axis, so a new pair laid
along an old one steals its partner (measured: the iron smelter's feed went to 0/min that way).
Poles come from the controller's pole pass like any increment.
"""
import math

from auto import geom, mines

DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}
AXIS = {"north": "v", "south": "v", "east": "h", "west": "h"}
CODE = {"iron-ore": "i", "copper-ore": "c", "coal": "k", "stone": "s"}
UG_REACH = 5        # a yellow underground's exit is at most 5 tiles ahead of its entrance
# coal burned at full load (energy / 4 MJ per coal): what a fuel bridge must lift into a dry network
FUEL = {"stone-furnace": 0.0225, "steel-furnace": 0.0225, "boiler": 0.45, "burner-mining-drill": 0.0375}
# items/s chest to chest, and how far ahead the inserter drops (long-handed: picks 2 behind, drops 2 ahead);
# the burner inserter needs no power and fuels itself from the coal it lifts
INS = {"burner-inserter": (0.6, 1), "inserter": (0.83, 1), "long-handed-inserter": (1.2, 2), "fast-inserter": (2.31, 1)}


def _pairs(pieces):
    """Underground pairs among placements, matched as the game does: an entrance with the first piece ahead of it
    facing the same way, when that is an exit. Segments (axis, a, b); an unmatched piece is one of its own tile."""
    ug = {}
    for p in pieces:
        if p.get("item") == "underground-belt" and not p.get("remove"):
            ug[(math.floor(p["x"]), math.floor(p["y"]))] = (p.get("direction", "north"), p.get("belt_type"))
    segs, paired = [], set()
    for t, (d, bt) in ug.items():
        if bt != "input":
            continue
        for k in range(1, UG_REACH + 1):
            u = (t[0] + DV[d][0] * k, t[1] + DV[d][1] * k)
            if u in ug and ug[u][0] == d:
                if ug[u][1] == "output":
                    segs.append((AXIS[d], t, u))
                    paired |= {t, u}
                break
    return segs + [(AXIS[d], t, t) for t, (d, _) in ug.items() if t not in paired]


def _segments(q, plan_, live, box):
    """Underground segments in box: plan pieces still to come or standing, our own pieces, and the live pairs as
    the game made them (entity.neighbours; it may differ from both)."""
    from auto import live as lv
    x1, y1, x2, y2 = box

    def inb(p):
        return p["item"] == "underground-belt" and x1 <= p["x"] <= x2 and y1 <= p["y"] <= y2
    pl = [p for i, p in enumerate(plan_.p) if inb(p) and (p.get("grp") not in live.retired or i in live.built)]
    segs = _pairs(pl) + _pairs([p for p in live.own if inb(p)])
    for x, y, d, _bt, pr in lv.undergrounds(q, box):
        a = (math.floor(x), math.floor(y))
        segs.append((AXIS.get(d, "v"), a, (math.floor(pr[0]), math.floor(pr[1])) if pr else a))
    return segs


def _spans(segs, skip=()):
    """plan_nx spans: the tiles each segment covers along its axis, ends included (segments touching skip left out:
    pieces this increment picks up)."""
    sp = {"v": set(), "h": set()}
    for ax, a, b in segs:
        if a in skip or b in skip:
            continue
        if ax == "v":
            sp["v"].update((a[0], y) for y in range(min(a[1], b[1]), max(a[1], b[1]) + 1))
        else:
            sp["h"].update((x, a[1]) for x in range(min(a[0], b[0]), max(a[0], b[0]) + 1))
    return sp


def _box(pts, m):
    return (min(p[0] for p in pts) - m, min(p[1] for p in pts) - m, max(p[0] for p in pts) + m + 1,
            max(p[1] for p in pts) + m + 1)


def _n_ug(r):
    return sum(1 for p in r if p["item"] == "underground-belt")


def _ug_note(ug_left, why):
    """Why a route may be missing for want of undergrounds ('' when stock does not limit it)."""
    if ug_left is None or (ug_left >= 2 and not why):
        return ""
    return f" ({ug_left} undergrounds in stock" + (f", a route needs {min(why)})" if why else ": surface only)")


def _route(grid, start, goal, fd, td, spans, ug_left, why=None):
    """A* belt route (plan_nx) that keeps off the spans. ug_left: undergrounds in stock still free (None: no limit);
    below a pair the route stays on the surface, and one that needs more than is left is re-run on the surface
    (None when the surface has no way: a route is never planned from undergrounds nobody can fetch; why gets the
    count). A route that lays two pieces on one tile bans that tile and runs again."""
    import plan_nx
    gap = 4 if ug_left is None or ug_left >= 2 else 0
    for _ in range(6):
        r = plan_nx.route_belt(grid, start, goal, fd, td, spans=spans, ug_gap=gap)
        if r and ug_left is not None and _n_ug(r) > ug_left:
            if why is not None:
                why.append(_n_ug(r))
            r = plan_nx.route_belt(grid, start, goal, fd, td, spans=spans, ug_gap=0)
        if not r:
            return None
        tiles = [(math.floor(p["x"]), math.floor(p["y"])) for p in r]
        dup = next((t for t in tiles if tiles.count(t) > 1), None)
        if dup is None:
            return r
        if dup in (start, goal):
            return None
        grid.blocked = grid.blocked | {dup}
    return None


ORE_LUA = r"""
local S=game.surfaces[1] local o={}
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},type='resource',name='%s'}) do
  if e.amount>=%d then o[#o+1]=math.floor(e.position.x)..','..math.floor(e.position.y) end end
rcon.print(table.concat(o,';'))
"""


class TerrainGrid:
    """bus.Grid plus the map: water and cliffs block."""

    def __init__(self, box, blocked, m):
        self.x1, self.y1, self.x2, self.y2 = box
        self.blocked, self.m = blocked, m

    def at(self, x, y):
        if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2) or (x, y) in self.blocked or                 self.m.t(x, y) in "~C#":
            return "#"
        return "."

    def free(self, x, y, ore_ok=True):
        return self.at(x, y) == "."


class LiveOre:
    """Map stand-in for auto.mines: terrain from the seed's map, this ore from the live game, other ores as mapped."""

    def __init__(self, m, code, tiles, box):
        self.m, self.code, self.live = m, code, tiles
        self.x0, self.y0 = box[0], box[1]
        self.w, self.h = box[2] - box[0], box[3] - box[1]
        self.ore = ["".join(self.o(self.x0 + c, self.y0 + r) for c in range(self.w)) for r in range(self.h)]

    def t(self, x, y):
        return self.m.t(x, y)

    def o(self, x, y):
        if (x, y) in self.live:           # live ore wins (also beyond the seed's map: scouted ground)
            return self.code
        mo = self.m.o(x, y)
        return "." if mo == self.code else mo


def live_tiles(q, name, box, min_amount=500):
    out = set()
    for x in range(box[0], box[2], 128):
        for y in range(box[1], box[3], 128):
            r = q("/silent-command " + (ORE_LUA % (x, y, min(x + 128, box[2]), min(y + 128, box[3]), name,
                                                   min_amount)).replace("\n", " ")) or ""
            for t in r.strip().split(";"):
                if t:
                    a, b = t.split(",")
                    out.add((int(a), int(b)))
    return out


def plan(q, m, plan_, live, g, radius=360, stock=None, outpost=None, n_new=None):
    """Placements (removals first) to replace the dry drills of group g ("mine:<ore>:k"), or (None, reason).
    Only plan drills that live-report no minable resources are picked up (a group is never dry as a whole:
    measured, coal:0 had 5 of 7 drills on 86k coal when it was retired), also those the controller wrote off
    (live.dead: they stand, mining nothing); plan drills gone and not dead count as dry; our own (relocated) drills
    beside its belts that are dry count but stay standing (the controller restores every own piece it lists).
    stock: {item: count} the builders can fetch; undergrounds are used only while it holds a pair.
    outpost: a box (x1, y1, x2, y2) on a scouted patch (auto.scout): n_new drills there, routed to g's belt, nothing
    picked up (g keeps mining; the new ore side-loads onto its ore lane)."""
    from auto import live as lv
    ore = g.split(":")[1]
    name = "iron-ore" if ore == "raw-iron-ore" else ore
    code = CODE.get(name)
    idx = plan_.groups[g]
    di = [i for i in idx if plan_.p[i]["item"] == "electric-mining-drill"]
    drills = [plan_.p[i] for i in di]
    belts = [i for i in idx if plan_.p[i]["item"] == "transport-belt"]
    if not drills or not belts or not code:
        return None, "not a drill column"
    at = {(round(x, 2), round(y, 2)): s for n, x, y, s in lv.status(q, plan_.box(idx, 6))
          if n == "electric-mining-drill"}
    key = lambda p: (round(p["x"], 2), round(p["y"], 2))      # noqa: E731
    dry = [d for d in drills if at.get(key(d)) == "no_minable_resources"]
    gone = [plan_.p[i] for i in di if key(plan_.p[i]) not in at and i not in live.dead]
    moved = [plan_.p[i] for i in di if key(plan_.p[i]) not in at and i in live.dead]   # relocated: own drills now
    working = sum(1 for d in drills if key(d) in at) - len(dry)
    bt_ = {(math.floor(plan_.p[i]["x"]), math.floor(plan_.p[i]["y"])) for i in belts}
    own_dry = [p for p in live.own if p["item"] == "electric-mining-drill" and at.get(key(p)) == "no_minable_resources"
               and (math.floor(p["x"]) + 2 * DV[p.get("direction", "north")][0],
                    math.floor(p["y"]) + 2 * DV[p.get("direction", "north")][1]) in bt_]  # our relocated drills here
    if outpost:
        dry, gone, moved, own_dry = [], [], [], []
        box = tuple(int(v) for v in outpost)
    else:
        n_new = len(dry) + len(gone) + len(own_dry)
        if not n_new:
            return None, f"no drill of {g} is dry ({working} standing on ore)"
        cx = sum(d["x"] for d in drills) / len(drills)
        cy = sum(d["y"] for d in drills) / len(drills)
        box = (int(cx) - radius, int(cy) - radius, int(cx) + radius, int(cy) + radius)
    tiles = live_tiles(q, name, box)
    if not tiles:
        return None, f"no {name} left within {radius} tiles"
    lm = LiveOre(m, code, tiles, box)
    # taken: every plan piece except the drills picked up (or gone), every live entity but those, our own pieces
    # (restore puts back what did not place), trees are fine (cleared on build)
    old_drill_tiles = set()
    for d in dry + gone + moved:        # moved drills never come back: their ground is free
        old_drill_tiles |= geom.footprint(d)
    rm = {key(d) for d in dry}
    taken = set(plan_.tiles) - old_drill_tiles
    for i in live.built:
        if key(plan_.p[i]) not in rm:
            taken |= geom.footprint(plan_.p[i])
    for (x, y), n in live.extra.items():
        if (round(x, 2), round(y, 2)) not in rm:
            taken |= geom.footprint({"item": n, "x": x, "y": y})
    for p in live.own:
        if key(p) not in rm:
            taken |= geom.footprint(p)
    taken |= set(live.__dict__.get("extra_tiles", ()))
    # the drill-side neighbours of the old belt: where the new ore side-loads onto the ore lane
    face = drills[0].get("direction", "north")
    fx, fy = DV[face]
    targets = []
    for i in belts:
        b = plan_.p[i]
        bt = (math.floor(b["x"]), math.floor(b["y"]))
        if b.get("direction") in (face, {"north": "south", "south": "north", "east": "west", "west": "east"}[face]):
            continue            # belt running along the face direction: no side to load from
        t = (bt[0] - fx, bt[1] - fy)
        if t not in taken:
            targets.append((t, face, bt))
    if outpost and name == "coal":
        # a coal belt carries coal on both lanes: an outpost may join from either side, or straight into its head
        f_ = plan_.flow
        for i in belts:
            b = plan_.p[i]
            d = b.get("direction", "north")
            bt = (math.floor(b["x"]), math.floor(b["y"]))
            for sd, (sx, sy) in DV.items():
                t = (bt[0] + sx, bt[1] + sy)
                if t in taken or sd == d:
                    continue
                if sd == OPP[d]:
                    if any(plan_.p[j]["item"] == "transport-belt" for j in f_.fed_by[i]):
                        continue          # behind a belt that has one: only the head takes a belt from behind
                    targets.append((t, d, bt))
                else:
                    targets.append((t, OPP[sd], bt))
    if not targets:
        return None, "no free tile beside the old belt"
    end = targets[len(targets) // 2][0]
    cols = mines.plan_columns(lm, code, n_new, end, taken, max_cols=2)
    if not cols:
        return None, f"no room for a {name} column on the ore that is left"
    places = [dict(d, remove=True) for d in dry]
    routes = []
    blocked = set(taken)
    for c in cols:
        blocked |= mines.footprint(c["places"])
    starts = []
    for c in cols:
        e = (math.floor(c["end"][0]), math.floor(c["end"][1]))
        starts.append((e[0] + DV[c["flow"]][0], e[1] + DV[c["flow"]][1]))
    segs = _segments(q, plan_, live, _box(starts + [t for t, _, _ in targets], 14))
    ug_left = None if stock is None else stock.get("underground-belt", 0)
    why = []
    for c, start in zip(cols, starts):
        r = None
        # targets spread along the old belt (its ends are often boxed in by the smelter and the fuel stub)
        order = sorted(targets, key=lambda tt: abs(tt[0][0] - start[0]) + abs(tt[0][1] - start[1]))
        step = max(1, len(order) // 6)
        for t, td, _bt in order[::step][:8]:
            x1, y1 = min(start[0], t[0]) - 12, min(start[1], t[1]) - 12
            x2, y2 = max(start[0], t[0]) + 13, max(start[1], t[1]) + 13
            grid = TerrainGrid((x1, y1, x2, y2), blocked - {start, t}, m)
            r = _route(grid, start, t, c["flow"], td, _spans(segs), ug_left, why)
            if r:
                blocked |= geom.tiles(r)
                segs += _pairs(r)
                if ug_left is not None:
                    ug_left = max(0, ug_left - _n_ug(r))
                break
        if not r:
            continue
        places += c["places"] + r
        routes.append(len(r))
    if not routes:
        return None, "the new column cannot reach the old belt" + _ug_note(ug_left, why)
    for p in places:     # the column planner names small poles: the pole pass decides the kind
        p.setdefault("grp", ("out:" if outpost else "re:") + g)
    places = [p for p in places if "electric-pole" not in p["item"]]
    return places, (f"{sum(c['drills'] for c in cols)} drills in {len(cols)} column(s), routes {routes} "
                    f"(replaces {len(dry)} dry + {len(own_dry)} dry own (left standing) + {len(gone)} gone; "
                    f"{working} working stay; {_n_ug(places)} undergrounds)")


def fuel_bridge(q, m, plan_, live, dry, sources, ins=None, n_ins=None, stock=None, researched=None):
    """A dry source's network (the belts it fed, now empty) gets fuel from a live column of the same resource that
    backs up: inserters lift from the live column's belt onto a short collector, a route (A*) takes it into the
    dry column's head from behind, and the old path carries it on. Returns (places, info) or (None, reason).
    The inserters come from an enumeration over the types: each type at the count whose rate lifts the fuel demand
    of the dry network's built consumers (furnaces, boilers, burner drills downstream of its belts), kept when it is
    researched (researched, else live.researched) and in stock (stock: {item: count}, None: no limit); the cheapest
    in raw plates that has a free site and a route wins. ins / n_ins force a type / a count. info names the type."""
    from auto import gamedata, stage
    f = plan_.flow
    dbelts = [i for i in plan_.groups[dry] if plan_.p[i]["item"] == "transport-belt"]
    head = next((i for i in dbelts if not any(plan_.p[j]["item"] == "transport-belt" for j in f.fed_by[i])), None)
    if head is None:
        return None, "dry column has no head belt"
    occupied = {t for p_ in plan_.p if p_.get("grp") not in live.retired for t in geom.footprint(p_)}
    for i in live.built:
        occupied |= geom.footprint(plan_.p[i])
    for (x, y), n in live.extra.items():
        occupied |= geom.footprint({"item": n, "x": x, "y": y})
    for p_ in live.own:
        occupied |= geom.footprint(p_)
    # entries into the dry network: straight into its head from behind, or a side-load on any of its belts
    goals = []
    hp = plan_.p[head]
    hd = hp.get("direction", "north")
    ht = (math.floor(hp["x"]), math.floor(hp["y"]))
    goals.append(((ht[0] - DV[hd][0], ht[1] - DV[hd][1]), hd))
    for i in dbelts:
        b = plan_.p[i]
        d = b.get("direction", "north")
        bt = (math.floor(b["x"]), math.floor(b["y"]))
        for sd, (sx, sy) in DV.items():
            if sd in (d, OPP[d]):
                continue
            goals.append(((bt[0] + sx, bt[1] + sy), OPP[sd]))
    goals = [g_ for g_ in goals if g_[0] not in occupied and m.t(*g_[0]) == "."]
    if not goals:
        return None, "no free entry into the dry column's network"
    # fuel demand: the built consumers the dry network's belts reach over built transport (belts, inserters)
    need, seen, todo = 0.0, set(dbelts), list(dbelts)
    while todo:
        for j in f.feeds[todo.pop()]:
            if j in seen or j not in live.built:
                continue
            seen.add(j)
            it_ = plan_.p[j]["item"]
            if it_ in FUEL:
                need += FUEL[it_]
            elif f.transport(j) and "pole" not in it_ and "pipe" not in it_:
                todo.append(j)
    gd = gamedata.load()
    rec = gd["recipes"]

    def plates(it_):        # raw plates in one item (a plate or a raw resource counts 1)
        r_ = rec.get(it_)
        if not r_ or it_.endswith("-plate"):
            return 1.0
        k_ = next((p_[1] for p_ in r_["prod"] if p_[0] == it_), 1)
        return sum(i_[1] * plates(i_[0]) for i_ in r_["ing"]) / k_
    if researched is None:
        researched = getattr(live, "researched", None) or None
    opts, why = [], []
    for t_, (rate, reach) in INS.items():
        n_ = n_ins or max(1, math.ceil(need / rate - 1e-9))     # a forced count is kept even below the demand
        if ins and t_ != ins:
            continue
        if researched is not None and stage.techs_for({t_}, gd) - set(researched):
            why.append(f"{t_} not researched")
            continue
        if stock is not None and stock.get(t_, 0) < n_:
            why.append(f"{t_} {stock.get(t_, 0)}/{n_} in stock")
            continue
        opts.append((n_ * plates(t_), t_, n_, rate, reach))
    if not opts:
        return None, f"no inserter type lifts {need:.2f} coal/s: {', '.join(why) or 'none fits'}"
    ug_left = None if stock is None else stock.get("underground-belt", 0)
    tried = []
    for _cost, ins_, n_, rate, reach in sorted(opts):
        best = None
        for s in sources:
            sp = [plan_.p[i] for i in plan_.groups[s]]
            drills = [p for p in sp if p["item"] == "electric-mining-drill"]
            belts = [p for p in sp if p["item"] == "transport-belt"]
            if not drills or len(belts) < n_ + 2:
                continue
            away = DV[drills[0].get("direction", "north")]    # drills face the belt: the far side is "face" onward
            for k in range(len(belts) - n_ - 1, len(belts) // 2 - 1, -1):   # downstream end: where the coal is
                run = belts[k:k + n_]
                if any(b.get("direction") != run[0].get("direction") for b in run):
                    continue
                bt = [(math.floor(b["x"]), math.floor(b["y"])) for b in run]
                it = [(x + reach * away[0], y + reach * away[1]) for x, y in bt]            # inserters
                ct = [(x + 2 * reach * away[0], y + 2 * reach * away[1]) for x, y in bt]    # collector under drops
                if any(t in occupied or m.t(*t) in "~C#" for t in it + ct):
                    continue
                d = min(math.dist(ct[-1], g_[0]) for g_ in goals)
                if best is None or d < best[0]:
                    best = (d, s, run, it, ct)
        if not best:
            tried.append(f"{n_} {ins_}: no free side on a live column")
            continue
        _, s, run, it, ct = best
        flow = run[0].get("direction", "north")
        away = ((it[0][0] - math.floor(run[0]["x"])) // reach, (it[0][1] - math.floor(run[0]["y"])) // reach)
        dname = {v: k for k, v in DV.items()}[away]     # the plan direction is the drop side
        places = [{"item": ins_, "x": x + .5, "y": y + .5, "direction": dname} for x, y in it]
        places += [{"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": flow} for x, y in ct[:-1]]
        start = ct[-1]
        blocked = occupied | set(it) | set(ct[:-1])
        near = sorted(goals, key=lambda g_: math.dist(start, g_[0]))[:8]
        sp_ = _spans(_segments(q, plan_, live, _box([start] + [g_[0] for g_ in near], 14)))
        r, why_ug = None, []
        for goal, gd_ in near:
            x1, y1 = min(start[0], goal[0]) - 12, min(start[1], goal[1]) - 12
            x2, y2 = max(start[0], goal[0]) + 13, max(start[1], goal[1]) + 13
            r = _route(TerrainGrid((x1, y1, x2, y2), blocked - {start, goal}, m), start, goal, flow, gd_, sp_, ug_left,
                       why_ug)
            if r:
                break
        if not r:
            tried.append(f"{n_} {ins_}: no route from {s}" + _ug_note(ug_left, why_ug))
            continue
        return places + r, (f"{n_} {ins_} on {s}, route {len(r)} into {dry}'s network "
                            f"(lifts {n_ * rate:.2f} of {need:.2f} coal/s; {_n_ug(r)} undergrounds)")
    return None, f"no fuel bridge into {dry}'s network: {'; '.join(tried)}"


def reconnect(q, m, plan_, live, groups, box_margin=8, stock=None):
    """Remined groups whose new ore does not reach their old belt (a route piece failed or was picked up): follow
    the ore over what stands (a flow graph of the live entities), take each dead end once, and route (A*) from there
    to a free tile beside one of the old belts (each such tile used once). Returns (places, info) or (None, reason).
    stock: {item: count} the builders can fetch; undergrounds are used only while it holds a pair."""
    from auto import flow as flowmod, live as lv
    own = list(live.own)
    if not own:
        return None, "no pieces of our own"
    olds = {}
    for g in groups:
        bl = [i for i in plan_.groups[g] if plan_.p[i]["item"] == "transport-belt"]
        dr = [plan_.p[i] for i in plan_.groups[g] if plan_.p[i]["item"] == "electric-mining-drill"]
        if bl and dr:
            olds[g] = (bl, dr[0].get("direction", "north"))
    if not olds:
        return None, "no drill columns"
    xs = [p["x"] for p in own] + [plan_.p[i]["x"] for bl, _ in olds.values() for i in bl]
    ys = [p["y"] for p in own] + [plan_.p[i]["y"] for bl, _ in olds.values() for i in bl]
    box = (min(xs) - box_margin, min(ys) - box_margin, max(xs) + box_margin, max(ys) + box_margin)
    pl = []
    for n, x, y, d, bt in lv.scan(q, box):
        p = {"item": n, "x": x, "y": y, "direction": d or "north"}
        if bt:
            p["belt_type"] = bt
        pl.append(p)
    F = flowmod.Flow(pl, None)
    # undergrounds carry to the partner the game paired them with (Flow pairs by geometry): an entrance whose exit
    # was taken by a nearer entrance is a dead end
    at_ = {(round(p["x"], 2), round(p["y"], 2)): i for i, p in enumerate(pl) if p["item"] == "underground-belt"}
    for x, y, _d, bt, pr in lv.undergrounds(q, box):
        i = at_.get((round(x, 2), round(y, 2)))
        if i is None or bt != "input":
            continue
        for j in [j for j in F.feeds[i] if pl[j]["item"] == "underground-belt"]:
            F.feeds[i].discard(j)
            F.fed_by[j].discard(i)
        F._link(i, at_.get((round(pr[0], 2), round(pr[1], 2))) if pr else None)
    keys = {(round(p["x"], 2), round(p["y"], 2)) for p in plan_.p if p["item"] == "electric-mining-drill"}
    plan_keys = {(round(p["x"], 2), round(p["y"], 2)) for p in plan_.p}
    newd = [i for i, p in enumerate(pl) if p["item"] == "electric-mining-drill" and
            (round(p["x"], 2), round(p["y"], 2)) not in keys]
    BELTISH = ("transport-belt", "underground-belt", "splitter")
    dead = set()
    for s0 in newd:          # the ore's own path: belts only (never through a machine or an inserter)
        seen, fr = {s0}, [s0]
        while fr:
            i = fr.pop()
            nxt = [j for j in F.feeds[i] if j not in seen and pl[j]["item"] in BELTISH]
            if not F.feeds[i] and pl[i]["item"] in ("transport-belt", "underground-belt") and i != s0                     and (round(pl[i]["x"], 2), round(pl[i]["y"], 2)) not in plan_keys:
                dead.add(i)      # only our own route pieces: a plan belt's end (a smelter's ore line) is by design
            for j in nxt:
                seen.add(j)
                fr.append(j)
    # occupied: what stands, plus plan pieces still to come (retired groups' pieces are not coming back)
    occupied = {t for p_ in plan_.p if p_.get("grp") not in live.retired for t in geom.footprint(p_)}
    for p in pl:
        occupied |= geom.footprint(p)
    targets = []
    for g, (bl, face) in olds.items():
        fx, fy = DV[face]
        opp = {"north": "south", "south": "north", "east": "west", "west": "east"}[face]
        for i in bl:
            b = plan_.p[i]
            if b.get("direction") in (face, opp):
                continue
            t = (math.floor(b["x"]) - fx, math.floor(b["y"]) - fy)
            if t not in occupied:
                targets.append((t, face))
    segs = _segments(q, plan_, live, (box[0] - 12, box[1] - 12, box[2] + 12, box[3] + 12))
    ug_left = None if stock is None else stock.get("underground-belt", 0)
    out, removes, used, why = [], [], set(), []
    for e in sorted(dead):
        p = pl[e]
        d = p["direction"]
        if p["item"] == "underground-belt" and p.get("belt_type") == "input":
            start, rm = (math.floor(p["x"]), math.floor(p["y"])), dict(p, remove=True)   # orphan entrance: re-lay
        else:
            start, rm = (math.floor(p["x"]) + DV[d][0], math.floor(p["y"]) + DV[d][1]), None
            if start in occupied:
                continue
        cands = sorted((t for t in targets if t[0] not in used),
                       key=lambda tt: abs(tt[0][0] - start[0]) + abs(tt[0][1] - start[1]))
        if not cands or abs(cands[0][0][0] - start[0]) + abs(cands[0][0][1] - start[1]) > 60:
            continue
        sp_ = _spans(segs, skip={start} if rm else ())    # a re-laid orphan entrance leaves its span
        for t, td in cands[:6]:
            x1, y1 = min(start[0], t[0]) - 10, min(start[1], t[1]) - 10
            x2, y2 = max(start[0], t[0]) + 11, max(start[1], t[1]) + 11
            r = _route(TerrainGrid((x1, y1, x2, y2), (occupied - {start, t}), m), start, t, d, td, sp_, ug_left, why)
            if r:
                out += r
                occupied |= geom.tiles(r)
                segs += _pairs(r)
                if ug_left is not None:
                    ug_left = max(0, ug_left - _n_ug(r))
                used.add(t)
                if rm:
                    removes.append(rm)
                break
    if not out:
        return None, f"{len(dead)} dead end(s), none reconnectable" + _ug_note(ug_left, why)
    return removes + out, f"{len(out)} pieces from {len(removes) + len(out) and len(dead)} dead end(s)"
