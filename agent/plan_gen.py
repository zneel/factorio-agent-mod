"""Generators for blocks the CP-SAT layout doesn't cover (power, mining, smelting): positions come from rules,
never from hand-placement. Each writes runtime/blocks/new/<name>.json = {"place": [...], "gate": {...}}.
Directions follow the build tool: direction = where items go (inserters drop that way, belts move that way).

    python plan_gen.py power   --pump 55.5,0.5 --boiler-x 44 --boilers 4
    python plan_gen.py drills  --name coal1 --ore coal --belt-x -16.5 --y1 -17 --y2 -2 --flow south
    python plan_gen.py smelt   --name iron1 --x 0.5 --y1 -40 --n 12 --flow south ...
"""
import argparse
import math
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "runtime" / "blocks" / "new"


def ore_map():
    d = json.loads((ROOT / "runtime" / "map" / "ore.json").read_text())
    return {(x, y): (n, a) for x, y, n, a in d}


def save(name, place, gate):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps({"place": place, "gate": gate}, indent=0))
    print(f"wrote runtime/blocks/new/{name}.json: {len(place)} entities")


def power(pump, bx, n, pipe_from=None):
    """Offshore pump (facing east, water east of it) -> pipe west along the pump row -> a column of boilers
    (facing west, stacked north with pitch 4 and a pipe in each gap) -> two steam engines west of each boiler.
    Gap rows hold small poles over the engines; burner inserters east of the boilers take coal from a belt
    running south at x = bx + 2.5."""
    px, py = pump
    place = [{"item": "offshore-pump", "x": px, "y": py, "direction": "east"}]
    x = px - 1
    while x >= bx + 0.5:
        place.append({"item": "pipe", "x": x, "y": py})
        x -= 1
    yc0 = py - 2  # boiler k=0's south water connection is the last pipe (bx+0.5, py)
    for k in range(n):
        yc = yc0 - 4 * k
        place.append({"item": "boiler", "x": bx, "y": yc, "direction": "west"})
        place.append({"item": "steam-engine", "x": bx - 3.5, "y": yc, "direction": "east"})
        place.append({"item": "steam-engine", "x": bx - 8.5, "y": yc, "direction": "east"})
        place.append({"item": "burner-inserter", "x": bx + 1.5, "y": yc, "direction": "west"})
        if k < n - 1:
            gy = yc - 2
            place.append({"item": "pipe", "x": bx + 0.5, "y": gy})
            place.append({"item": "small-electric-pole", "x": bx - 3.5, "y": gy})
            place.append({"item": "small-electric-pole", "x": bx - 8.5, "y": gy})
    top = yc0 - 4 * (n - 1) - 1.5
    belt = [{"item": "transport-belt", "x": bx + 2.5, "y": y + 0.5, "direction": "south"}
            for y in range(int(top - 1), int(yc0 + 1))]
    place += belt
    gate = {"feeds": [{"x": belt[0]["x"], "y": belt[0]["y"], "lanes": {"west": "coal", "east": "coal"}}],
            "fuel": "coal",
            "load": {"x": bx - 6, "y": yc0 - 2 * n, "watts": 1e6 * n}}
    return place, gate


def drills(ore, belt_x, y1, y2, flow, min_ore=5, sides=("w", "e")):
    """Electric drills on both sides of a vertical belt at x = belt_x (tile centre), facing it, between rows
    y1..y2. Pattern down the column: drill, pole row, drill (pitch 7 per pair), so each small pole
    sits between two drills of its side and powers both. Drills need >= min_ore ore tiles under them."""
    m = ore_map()
    place = []
    y = y1 + 1.5  # first drill centre
    pair = 0
    while y + 1.5 <= y2 + 1:
        for side in sides:
            cx = belt_x - 2 if side == "w" else belt_x + 2
            n = sum(1 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                    if m.get((int(cx - 0.5) + dx, int(y - 0.5) + dy), ("",))[0] == ore)
            area = [m.get((int(cx - 0.5) + dx, int(y - 0.5) + dy), ("",))[0] for dx in range(-2, 3) for dy in range(-2, 3)]
            pure = all(r in ("", ore) for r in area)  # an electric drill mines its whole 5x5 area: no other resource
            if n >= min_ore and pure:
                place.append({"item": "electric-mining-drill", "x": cx, "y": y,
                              "direction": "east" if side == "w" else "west"})
        if pair % 2 == 0:  # pole row after the first drill of each pair
            for side in sides:
                place.append({"item": "small-electric-pole", "x": belt_x - 2 if side == "w" else belt_x + 2, "y": y + 2})
            y += 4
        else:
            y += 3
        pair += 1
    ys = [p["y"] for p in place if p["item"] == "electric-mining-drill"]
    # drop poles that power nothing (no drill directly above or below them)
    place = [p for p in place if p["item"] != "small-electric-pole"
             or any(abs(q["x"] - p["x"]) < 0.1 and abs(q["y"] - p["y"]) <= 2.01 for q in place
                    if q["item"] == "electric-mining-drill")]
    lo, hi = min(ys) - 1, max(ys) + 1
    rng = range(int(lo - 0.5), int(hi + 0.5) + 1)
    if flow == "north":
        rng = reversed(list(rng))
    belt = [{"item": "transport-belt", "x": belt_x, "y": yy + 0.5, "direction": flow} for yy in rng]
    place += belt
    end = belt[-1]
    gate = {"power": True, "sinks": [{"x": end["x"], "y": end["y"]}]}
    return place, gate


def smelt(c, y1, n, ore="iron-ore", fuel="coal", furnace="stone-furnace"):
    """Vertical smelting column, flowing south. Tile columns (c = the ore belt's tile x):
       c-5 plate belt | c-4 out inserter | c-3,c-2 furnace | c-1 in inserter | c ore+fuel belt | mirror east.
    Furnaces come in groups of two (4 rows) plus one gap row holding a small pole on each side, so every
    inserter is powered. The west plate belt turns east under the column and side-loads the east one, which
    continues south as the trunk: west furnaces' plates on its west lane, east furnaces' on its east lane.
    n = furnaces per side. The ore belt's first tile is (c, y1); feed ore on one lane and fuel on the other."""
    place = [{"item": "small-electric-pole", "x": c - 1.5, "y": y1 - 0.5},
             {"item": "small-electric-pole", "x": c + 2.5, "y": y1 - 0.5}]  # powers the first furnace pair
    y = y1
    for k in range(n):
        if k and k % 2 == 0:
            place.append({"item": "small-electric-pole", "x": c - 1.5, "y": y + 0.5})
            place.append({"item": "small-electric-pole", "x": c + 2.5, "y": y + 0.5})
            y += 1
        iy = y + (0 if k % 2 == 0 else 1)  # inserter row: top row of the first furnace, bottom of the second
        place += [{"item": furnace, "x": c - 2, "y": y + 1}, {"item": furnace, "x": c + 3, "y": y + 1},
                  {"item": "inserter", "x": c - 0.5, "y": iy + 0.5, "direction": "west"},
                  {"item": "inserter", "x": c + 1.5, "y": iy + 0.5, "direction": "east"},
                  {"item": "inserter", "x": c - 3.5, "y": iy + 0.5, "direction": "west"},
                  {"item": "inserter", "x": c + 4.5, "y": iy + 0.5, "direction": "east"}]
        y += 2
    if n % 2 == 0 or True:  # a last pole pair below the final group
        place.append({"item": "small-electric-pole", "x": c - 1.5, "y": y + 0.5})
        place.append({"item": "small-electric-pole", "x": c + 2.5, "y": y + 0.5})
    bottom = y + 1  # row under the column: west belt crosses east here
    for yy in range(y1, y):
        place.append({"item": "transport-belt", "x": c + 0.5, "y": yy + 0.5, "direction": "south"})
    for yy in range(y1, bottom):
        place.append({"item": "transport-belt", "x": c - 4.5, "y": yy + 0.5, "direction": "south"})
        place.append({"item": "transport-belt", "x": c + 5.5, "y": yy + 0.5, "direction": "south"})
    for xx in range(c - 5, c + 5):
        place.append({"item": "transport-belt", "x": xx + 0.5, "y": bottom + 0.5, "direction": "east"})
    place.append({"item": "transport-belt", "x": c + 5.5, "y": bottom + 0.5, "direction": "south"})
    place.append({"item": "transport-belt", "x": c + 5.5, "y": bottom + 1.5, "direction": "south"})
    plate = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick", "iron-plate": "steel-plate"}[ore]
    gate = {"feeds": [{"x": c + 0.5, "y": y1 + 0.5, "lanes": {"west": ore, "east": fuel}, "tiles": 10}],
            "sinks": [{"x": c + 5.5, "y": bottom + 1.5}], "power": True}
    # far-lane rule check data: plate belts carry only plates
    for p in place:
        if p["item"] == "transport-belt" and p["x"] != c + 0.5:
            p["lanes"] = {"a": plate}
    return place, gate


def labs(x0, y_top, n, top_dir="west", bottom_dir="east"):
    """A row of n labs between two belts: belt y_top | inserters | labs (3 rows) | inserters | belt y_top+6.
    Labs sit 4 apart with a small pole in each gap (powers both neighbours and their inserters).
    x0 = centre of the first lab; labs go east."""
    place = []
    xs = [x0 + 4 * i for i in range(n)]
    for x in xs:
        place += [{"item": "lab", "x": x, "y": y_top + 3},
                  {"item": "inserter", "x": x, "y": y_top + 1, "direction": "south"},
                  {"item": "inserter", "x": x, "y": y_top + 5, "direction": "north"}]
    for x in xs[:-1]:
        place.append({"item": "small-electric-pole", "x": x + 2, "y": y_top + 3})
    place.append({"item": "small-electric-pole", "x": xs[-1] + 2, "y": y_top + 3})
    span = range(int(xs[0] - 1.5), int(xs[-1] + 1.5) + 1)
    top = [{"item": "transport-belt", "x": xx + 0.5, "y": y_top, "direction": top_dir} for xx in span]
    bot = [{"item": "transport-belt", "x": xx + 0.5, "y": y_top + 6, "direction": bottom_dir} for xx in span]
    if top_dir == "west":
        top.reverse()
    if bottom_dir == "west":
        bot.reverse()
    return place + top + bot


SIZE = {"electric-mining-drill": (3, 3), "burner-mining-drill": (2, 2), "stone-furnace": (2, 2), "steel-furnace": (2, 2),
        "boiler": (3, 2), "steam-engine": (3, 5), "offshore-pump": (1, 1), "assembling-machine-1": (3, 3),
        "assembling-machine-2": (3, 3), "assembling-machine-3": (3, 3), "chemical-plant": (3, 3), "oil-refinery": (5, 5),
        "lab": (3, 3), "electric-furnace": (3, 3), "pumpjack": (3, 3), "storage-tank": (3, 3), "big-electric-pole": (2, 2),
        "substation": (2, 2), "rocket-silo": (9, 9), "radar": (3, 3), "beacon": (3, 3), "splitter": (2, 1)}


def footprint(p):
    w, h = SIZE.get(p["item"], (1, 1))
    if p.get("direction") in ("east", "west"):
        w, h = h, w
    x0, y0 = math.floor(p["x"] - w / 2 + 0.01), math.floor(p["y"] - h / 2 + 0.01)
    return {(x0 + dx, y0 + dy) for dx in range(w) for dy in range(h)}


def poles_along(belts, reserve=(), spacing=7, item="small-electric-pole"):
    """Poles beside a belt route, at most `spacing` belt tiles apart (small pole wire reach 7.5), on a free
    side tile (not in any reserved footprint or the route itself)."""
    import plan
    taken = set()
    for name in reserve:
        for q in load_plan(name):
            taken |= footprint(q)
    tiles = [(math.floor(b["x"]), math.floor(b["y"])) for b in belts]
    taken |= set(tiles)
    xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
    g = plan.Grid.load(plan._bridge(), min(xs) - 2, min(ys) - 2, max(xs) + 3, max(ys) + 3)
    out, last = [], None
    for i, (x, y) in enumerate(tiles):
        if last is not None and i - last < spacing and i != len(tiles) - 1:
            continue
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            t = (x + dx, y + dy)
            if t not in taken and g.at(*t) in ".t":
                out.append({"item": item, "x": t[0] + 0.5, "y": t[1] + 0.5})
                taken.add(t)
                last = i
                break
    return out


def load_plan(name):
    p = Path(name)
    if not p.exists():
        p = OUT / (name if name.endswith(".json") else name + ".json")
    s = json.loads(p.read_text())
    return s["place"] if isinstance(s, dict) else s


def keep_prev_poles(prev_name, place):
    """Poles of a previous version that still fit beside the new plan: keep them (they are already built and the
    new plan's power cover counts on them)."""
    try:
        prev = [q for q in load_plan(prev_name) if not q.get("remove") and "electric-pole" in q["item"]]
    except OSError:
        return []
    taken = set()
    for q in place:
        if not q.get("remove"):
            taken |= footprint(q)
    return [q for q in prev if not (footprint(q) & taken)]


def diff_removals(prev_name, place):
    """Entries of a previously built plan that the new plan no longer has: return them as removals."""
    key = lambda q: (q["item"], q["x"], q["y"], q.get("direction"), q.get("belt_type"))
    now = {key(q) for q in place if not q.get("remove")}
    try:
        prev = [q for q in load_plan(prev_name) if not q.get("remove")]
    except OSError:
        return []
    return [dict(q, remove=True) for q in prev if key(q) not in now]


def collisions(place, reserve, removed=()):
    """Tiles of `place` that overlap a reserved plan's footprint (fixed-geometry generators must not).
    removed: (x, y) of reserved entities this plan takes out first."""
    taken = {}
    removed = set(removed)
    for name in reserve:
        for q in load_plan(name):
            if not q.get("remove") and (q["x"], q["y"]) not in removed:
                for t in footprint(q):
                    taken[t] = f"{name}:{q['item']}"
    out = []
    for p in place:
        if p.get("remove"):
            continue
        for t in footprint(p):
            if t in taken:
                out.append(f"{p['item']}@{p['x']},{p['y']} vs {taken[t]}")
    return out


def ug_spans(reserve, area, skip=()):
    """Tiles covered by underground belt pairs (ends included), per axis: from the reserved plans (an input followed
    by its output in the same plan) and from the live map (paired neighbours)."""
    import plan
    spans = {"v": set(), "h": set()}

    def add(a, b):
        ax, ay, bx, by = math.floor(a[0]), math.floor(a[1]), math.floor(b[0]), math.floor(b[1])
        if ax == bx:
            spans["v"].update((ax, y) for y in range(min(ay, by), max(ay, by) + 1))
        elif ay == by:
            spans["h"].update((x, ay) for x in range(min(ax, bx), max(ax, bx) + 1))
    for name in reserve:
        pl = [q for q in load_plan(name) if not q.get("remove")]
        for i, q in enumerate(pl):
            if q.get("item") == "underground-belt" and q.get("belt_type") == "input":
                for r in pl[i + 1:i + 3]:
                    if r.get("item") == "underground-belt" and r.get("belt_type") == "output" and r["direction"] == q["direction"]:
                        add((q["x"], q["y"]), (r["x"], r["y"]))
                        break
    x1, y1, x2, y2 = area
    out = plan._lua(plan._bridge(), "local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{area={{%d,%d},{%d,%d}},"
                    "type='underground-belt'}) do local n=e.neighbours if n and e.belt_to_ground_type=='input' then "
                    "o[#o+1]=e.position.x..','..e.position.y..','..n.position.x..','..n.position.y end end rcon.print(table.concat(o,';'))"
                    % (x1, y1, x2, y2))
    for t in out.strip().split(";"):
        if t:
            v = [float(c) for c in t.split(",")]
            if (math.floor(v[0]), math.floor(v[1])) not in skip:
                add((v[0], v[1]), (v[2], v[3]))
    return spans


def route(start, goal, from_dir=None, to_dir=None, reserve=(), margin=12, pipe=False, own=()):
    """A* belt (or pipe) route on the live map, with every reserved plan's footprint blocked."""
    import plan
    import plan_nx
    sx, sy, gx, gy = int(start[0] // 1), int(start[1] // 1), int(goal[0] // 1), int(goal[1] // 1)
    g = plan.Grid.load(plan._bridge(), min(sx, gx) - margin, min(sy, gy) - margin, max(sx, gx) + margin + 1,
                       max(sy, gy) + margin + 1)
    rows = [list(r) for r in g.rows]
    own_tiles = set()
    for name in own:  # a previous version of the plan being recomposed: its built pieces are not obstacles
        for q in load_plan(name):
            if not q.get("remove"):
                own_tiles |= footprint(q)
    for (x, y) in own_tiles:
        if g.x1 <= x < g.x2 and g.y1 <= y < g.y2:
            rows[y - g.y1][x - g.x1] = "."
    DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
    for name in reserve:
        pl = [q for q in load_plan(name) if not q.get("remove")]
        belt_tiles = {(math.floor(q["x"]), math.floor(q["y"])) for q in pl if "belt" in q["item"] or q["item"] == "splitter"}
        for p in pl:
            for (x, y) in footprint(p):
                if g.x1 <= x < g.x2 and g.y1 <= y < g.y2 and (x, y) not in ((sx, sy), (gx, gy)):
                    rows[y - g.y1][x - g.x1] = "#"
            # a planned belt end feeds whatever is built in front of it: keep that tile free
            if p["item"] == "transport-belt" or (p["item"] == "underground-belt" and p.get("belt_type") == "output"):
                dx, dy = DV.get(p.get("direction", "north"), (0, 0))
                f = (math.floor(p["x"]) + dx, math.floor(p["y"]) + dy)
                if f not in belt_tiles and g.x1 <= f[0] < g.x2 and g.y1 <= f[1] < g.y2 and f not in ((sx, sy), (gx, gy)):
                    if rows[f[1] - g.y1][f[0] - g.x1] in ".to":
                        rows[f[1] - g.y1][f[0] - g.x1] = "#"
    g.rows = ["".join(r) for r in rows]
    if pipe:
        return plan_nx.route_pipe(g, {(sx, sy)}, (gx, gy), goal_dir=to_dir)
    spans = ug_spans(reserve, (g.x1, g.y1, g.x2, g.y2), skip=own_tiles)
    for _ in range(8):  # the state graph can cross its own path; ban a repeated tile and search again
        r = plan_nx.route_belt(g, (sx, sy), (gx, gy), from_dir, to_dir, spans=spans)
        if not r:
            return r
        seen, dup = set(), None
        for q in r:
            t = (math.floor(q["x"]), math.floor(q["y"]))
            if t in seen:
                dup = t
                break
            seen.add(t)
        if not dup:
            return r
        rows = [list(x) for x in g.rows]
        rows[dup[1] - g.y1][dup[0] - g.x1] = "#"
        g.rows = ["".join(x) for x in rows]
    return None


POLE_REACH = {"small-electric-pole": 7.5, "medium-electric-pole": 9, "big-electric-pole": 30, "substation": 18}


def live_poles(x1, y1, x2, y2):
    import plan
    # only poles on the powered grid: the network of the pole nearest the steam engines
    out = plan._lua(plan._bridge(), "local S=game.surfaces[1] local r=S.find_entities_filtered{position={38,-7},radius=12,type='electric-pole',limit=1}[1] "
                    "local id=r and r.electric_network_id local o={} for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},"
                    "type='electric-pole'}) do if e.electric_network_id==id then o[#o+1]=e.position.x..','..e.position.y end end rcon.print(table.concat(o,';'))"
                    % (x1, y1, x2, y2))
    return [tuple(float(v) for v in t.split(",")) for t in out.strip().split(";") if t]


ELECTRIC = ("inserter", "assembling-machine", "electric-mining-drill", "lab", "electric-furnace", "chemical-plant",
            "oil-refinery", "pumpjack", "pump", "radar", "beacon", "rocket-silo", "centrifuge", "roboport")
SUPPLY = {"small-electric-pole": 2.5, "medium-electric-pole": 3.5, "big-electric-pole": 1, "substation": 9}


def cover_power(place, reserve=(), item="small-electric-pole"):
    """Greedy set cover: add poles on free tiles until every electric consumer in `place` overlaps a pole's
    supply area (planned or live)."""
    import plan
    r = SUPPLY[item]
    cons = [p for p in place if any(k in p["item"] for k in ELECTRIC) and "burner" not in p["item"]]
    if not cons:
        return []
    xs = [p["x"] for p in place]
    ys = [p["y"] for p in place]
    x1, y1, x2, y2 = int(min(xs)) - 6, int(min(ys)) - 6, int(max(xs)) + 6, int(max(ys)) + 6
    poles = [(p["x"], p["y"], SUPPLY.get(p["item"], 2.5)) for p in place if "electric-pole" in p["item"]]
    poles += [(x, y, 2.5) for x, y in live_poles(x1, y1, x2, y2)]
    taken = set()
    for name in reserve:
        for q in load_plan(name):
            if not q.get("remove"):
                taken |= footprint(q)
    for q in place:
        taken |= footprint(q)
    g = plan.Grid.load(plan._bridge(), x1, y1, x2, y2)

    def covers(px, py, rr, c):
        w, h = SIZE.get(c["item"], (1, 1))
        if c.get("direction") in ("east", "west"):
            w, h = h, w
        return abs(c["x"] - px) < rr + w / 2 - 0.3 and abs(c["y"] - py) < rr + h / 2 - 0.3
    left = [c for c in cons if not any(covers(px, py, rr, c) for px, py, rr in poles)]
    added = []
    while left:
        best, bn = None, 0
        for c in left[:1]:
            for dx in range(-4, 5):
                for dy in range(-4, 5):
                    t = (math.floor(c["x"]) + dx, math.floor(c["y"]) + dy)
                    if t in taken or g.at(*t) not in ".t":
                        continue
                    n = sum(1 for o in left if covers(t[0] + .5, t[1] + .5, r, o))
                    if n > bn:
                        best, bn = t, n
        if not best:
            raise SystemExit(f"no pole spot for {left[0]['item']} {left[0]['x']},{left[0]['y']}")
        added.append({"item": item, "x": best[0] + .5, "y": best[1] + .5})
        taken.add(best)
        left = [o for o in left if not covers(best[0] + .5, best[1] + .5, r, o)]
    return added


def connect_poles(place, reserve=(), item="small-electric-pole", margin=12, exclude=()):
    """Add poles so every pole group of `place` joins the live grid: union the plan's poles by wire reach, then
    for each group not touching a live pole, the fewest-hop chain of new poles over free tiles (networkx)."""
    import networkx as nx
    import plan
    R = POLE_REACH[item] - 0.01
    poles = [(p["x"], p["y"]) for p in place if "electric-pole" in p["item"] or p["item"] == "substation"]
    xs = [p["x"] for p in place]
    ys = [p["y"] for p in place]
    x1, y1, x2, y2 = int(min(xs)) - margin, int(min(ys)) - margin, int(max(xs)) + margin, int(max(ys)) + margin
    excl = set()
    for name in exclude:  # e.g. the previous version of this plan: its built poles may be removed, so no anchors
        try:
            excl |= {(q["x"], q["y"]) for q in load_plan(name) if "electric-pole" in q.get("item", "")}
        except OSError:
            pass
    keep_xy = {(p["x"], p["y"]) for p in place if "electric-pole" in p["item"]}
    live = [l for l in live_poles(x1, y1, x2, y2) if l not in excl]  # kept old poles are plan poles, not anchors
    taken = set()
    for name in reserve:
        for q in load_plan(name):
            taken |= footprint(q)
    for q in place:
        taken |= footprint(q)
    g = plan.Grid.load(plan._bridge(), x1, y1, x2, y2)
    for name in exclude:
        try:
            for q in load_plan(name):
                if not q.get("remove") and (q["x"], q["y"]) not in keep_xy:
                    for t in footprint(q):
                        if t not in taken and g.x1 <= t[0] < g.x2 and g.y1 <= t[1] < g.y2:
                            g.rows[t[1] - g.y1] = g.rows[t[1] - g.y1][:t[0] - g.x1] + "." + g.rows[t[1] - g.y1][t[0] - g.x1 + 1:]
        except OSError:
            pass
    free = [(x + 0.5, y + 0.5) for y in range(y1, y2) for x in range(x1, x2)
            if (x, y) not in taken and g.at(x, y) in ".t"]
    added = []

    def d(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    for _ in range(20):
        nodes = poles + [a["x"] and (a["x"], a["y"]) for a in added]
        G = nx.Graph()
        G.add_nodes_from(nodes + live)
        allp = nodes + live
        for i, a in enumerate(allp):
            for b in allp[i + 1:]:
                if d(a, b) <= R:
                    G.add_edge(a, b)
        comps = list(nx.connected_components(G))
        grid_comp = set().union(*[c for c in comps if any(l in c for l in live)]) if live else set()
        lonely = [c for c in comps if not (c & grid_comp)]
        if not lonely:
            return added
        if not grid_comp:  # no live grid nearby: join the plan's own groups to the largest one
            grid_comp = max(comps, key=len)
            lonely = [c for c in comps if c is not grid_comp]
            if not lonely:
                return added
        comp = lonely[0]
        H = nx.Graph()
        S, T = ("S",), ("T",)
        cand = free
        for f in cand:
            if any(d(f, p) <= R for p in comp):
                H.add_edge(S, f, weight=1)
            if any(d(f, p) <= R for p in grid_comp):
                H.add_edge(f, T, weight=1)
        if any(d(a, b) <= R for a in comp for b in grid_comp):
            continue
        # hop graph over free tiles: link tiles within reach (bucketed for speed)
        buck = {}
        for f in cand:
            buck.setdefault((int(f[0] // 8), int(f[1] // 8)), []).append(f)
        for f in cand:
            bx, by = int(f[0] // 8), int(f[1] // 8)
            for ddx in (-1, 0, 1):
                for ddy in (-1, 0, 1):
                    for h in buck.get((bx + ddx, by + ddy), []):
                        if h > f and d(f, h) <= R:
                            H.add_edge(f, h, weight=1 + 0.001 * (7.5 - d(f, h)))
        try:
            path = nx.shortest_path(H, S, T, weight="weight")[1:-1]
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            raise SystemExit(f"no pole chain for group near {sorted(comp)[0]}")
        for f in path:
            added.append({"item": item, "x": f[0], "y": f[1]})
            taken.add((int(f[0] // 1), int(f[1] // 1)))
        free = [f for f in free if f not in path]
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind")
    ap.add_argument("--name")
    ap.add_argument("--pump")
    ap.add_argument("--boiler-x", type=float)
    ap.add_argument("--boilers", type=int, default=4)
    ap.add_argument("--ore")
    ap.add_argument("--belt-x", type=float)
    ap.add_argument("--y1", type=float)
    ap.add_argument("--y2", type=float)
    ap.add_argument("--flow", default="south")
    ap.add_argument("--sides", default="we")
    ap.add_argument("--start")
    ap.add_argument("--goal")
    ap.add_argument("--from-dir")
    ap.add_argument("--to-dir")
    ap.add_argument("--reserve", default="")
    ap.add_argument("--margin", type=int, default=12)
    ap.add_argument("--pipe", action="store_true")
    a = ap.parse_args()
    if a.kind == "power":
        px, py = (float(v) for v in a.pump.split(","))
        place, gate = power((px, py), a.boiler_x, a.boilers)
        save(a.name or "power1", place, gate)
    elif a.kind == "drills":
        place, gate = drills(a.ore, a.belt_x, a.y1, a.y2, a.flow, sides=tuple(a.sides))
        save(a.name, place, gate)
    elif a.kind == "smelt":
        place, gate = smelt(int(a.belt_x), int(a.y1), a.boilers, a.ore or "iron-ore")
        save(a.name, place, gate)
    elif a.kind == "route":
        st = tuple(float(v) for v in a.start.split(","))
        go = tuple(float(v) for v in a.goal.split(","))
        r = route(st, go, a.from_dir, a.to_dir, [n for n in a.reserve.split(",") if n], a.margin, a.pipe)
        if not r:
            raise SystemExit("no route")
        n_ug = sum(1 for p in r if "underground" in p["item"] or "to-ground" in p["item"])
        save(a.name, r, {})
        print(f"route {len(r)} pieces, {n_ug} underground")


if __name__ == "__main__":
    main()
