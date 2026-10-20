"""Blue complex: blue block (CP-SAT, blocks/blue.json) + its feeds and export.
  IC bundle  <- iron2 trunk (north side of the stub) + copper from a splitter on the copper trunk (south side)
  SP bundle  <- steel1 trunk (north) + plastic belt from oil1's plastic plant (south)
  SU         <- sulfur belt from oil1's sulfur plant, side-loaded onto the north lane of a 3-tile pre-stub
  export     -> blue packs side-load the empty (right) lane of science1's green belt, so labs read green+blue
  coal       -> plastic plant, from a splitter on the boiler coal belt (row -18.5)
    .venv/Scripts/python blocks/blue1.py -> runtime/blocks/new/blue1.json"""
import json
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan
import plan_block
import plan_gen as pg

b = plan._bridge()
spec = json.load(open(Path(__file__).parent / "blue.json"))
res = plan_block.compile_block(spec, plan_block.recipes_from_game(b, plan.RECIPE_LUA))
assert not res["errors"], res["errors"]
_bp = [{k: v for k, v in p.items() if k in ("item", "x", "y", "direction", "recipe", "belt_type")} for p in res["places"]]
_ys = sorted({bb["y"] for bb in spec["belts"]})
_x1 = min(min(bb["x1"], bb["x2"]) for bb in spec["belts"])
_x2 = max(max(bb["x1"], bb["x2"]) for bb in spec["belts"])
# the columns just outside the belt ends: loops and feeds start there, so other plans must keep them free
_bp += [{"item": "pipe", "x": x, "y": y} for x in (_x1 - 2, _x1 - 1, _x2 + 1, _x2 + 2) for y in _ys]
pg.save("blueblock", _bp, {})
if "--block-only" in sys.argv:
    sys.exit(0)
RATE_FIX = {(float(w.split()[1].split(",")[0]), float(w.split()[1].split(",")[1])) for w in res.get("rates", []) if "fast-inserter" in w}
block = []
for p in res["places"]:
    q = {k: v for k, v in p.items() if k in ("item", "x", "y", "direction", "recipe", "belt_type")}
    if q["item"] == "inserter" and (q["x"], q["y"]) in RATE_FIX:
        q["item"] = "fast-inserter"
    block.append(q)


def lanes(bb):
    return set(v for v in bb["lanes"].values() if v)


def after(bb):
    return (bb["x2"] + 1 if bb["dir"] == "east" else bb["x2"] - 1, bb["y"])


belts = spec["belts"]
def entry(items):
    cands = [bb for bb in belts if items <= lanes(bb) and bb["dir"] == "east"]
    return min(cands, key=lambda bb: bb["x1"])
IC = entry({"iron-plate", "copper-plate"})
SP = entry({"steel-plate", "plastic-bar"})
SU = next(bb for bb in belts if "sulfur" in lanes(bb))
EX = next(bb for bb in belts if "chemical-science-pack" in lanes(bb))
assert SU["dir"] == "east" and SU["lanes"]["north"] == "sulfur", SU
x0 = IC["x1"]
def start(bb):
    return (bb["x1"] - 1 if bb["dir"] == "east" else bb["x1"] + 1, bb["y"])
def ret_of(e):
    return next(bb for bb in belts if bb is not e and lanes(bb) == lanes(e))
stubs = []
oil = pg.load_plan("oil1")
PLx, PLy = 19.5, -44.5
SUx, SUy = 25.5, -44.5
# coal for the plastic plant: splitter on the boiler coal belt (flows east along y=-18.5)
remove = [{"remove": True, "item": "transport-belt", "x": 10.5, "y": -18.5}]
split = [{"item": "splitter", "x": 10.5, "y": -18.0, "direction": "east"}]

# copper tap: a splitter on a straight run of the science copper route with a free tile beside it
taken = set()
reserve = ["iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1", "coalhub", "steel1", "pickups1", "iron2",
           "power1", "labs1", "oil1"]
for n in reserve:
    for q in pg.load_plan(n):
        if not q.get("remove"):
            taken |= pg.footprint(q)
g = plan.Grid.load(b, -80, -60, 40, 15)
DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
cu_route = pg.load_plan("sci-tmp-copper")
tap = None
for i in range(2, len(cu_route) - 2):
    p, nxt, prv = cu_route[i], cu_route[i + 1], cu_route[i - 1]
    if not (p["item"] == nxt["item"] == prv["item"] == "transport-belt" and p["direction"] == nxt["direction"] == prv["direction"]):
        continue
    dx, dy = DV[p["direction"]]
    for sx, sy in ((dy, -dx), (-dy, dx)):  # the two perpendicular sides
        side = (math.floor(p["x"]) + sx, math.floor(p["y"]) + sy)
        out = (side[0] + dx, side[1] + dy)
        if all(t not in taken and g.at(*t) in ".t" for t in (side, out)):
            tap = (p, (p["x"] + sx / 2, p["y"] + sy / 2), (out[0] + .5, out[1] + .5))
            break
    if tap:
        break
assert tap, "no copper tap spot"
cp, cpos, cout = tap
remove.append({"remove": True, "item": "transport-belt", "x": cp["x"], "y": cp["y"]})
split.append({"item": "splitter", "x": cpos[0], "y": cpos[1], "direction": cp["direction"]})
print("copper tap", cp["x"], cp["y"], cp["direction"], "->", cout)

base = block + stubs + split
bad = pg.collisions(base, reserve, removed={(r["x"], r["y"]) for r in remove})
assert not bad, bad[:5]
pg.save("bl-tmp", base, {})
steel_trunk = max((p for p in pg.load_plan("steel1") if p.get("item") == "transport-belt" and p.get("x") == -14.5),
                  key=lambda p: p["y"])
iron_trunk = max((p for p in pg.load_plan("iron2") if p.get("item") == "transport-belt" and p.get("x") == -37.5),
                 key=lambda p: p["y"])
green = pg.load_plan("sci-tmp-green")
# blue joins the green belt's right lane: from the right side of a straight green tile
RIGHT = {"east": (0, 1), "west": (0, -1), "north": (1, 0), "south": (-1, 0)}
gj = None
for i in range(3, len(green) - 2):
    p = green[i]
    if p["item"] != "transport-belt" or green[i - 1].get("direction") != p["direction"] or green[i + 1].get("direction") != p["direction"]:
        continue
    rx, ry = RIGHT[p["direction"]]
    t = (math.floor(p["x"]) + rx, math.floor(p["y"]) + ry)
    if t not in taken and g.at(*t) in ".t":
        gj = (p, (t[0] + .5, t[1] + .5), {(0, 1): "north", (0, -1): "south", (1, 0): "west", (-1, 0): "east"}[(rx, ry)])
        break
assert gj, "no green join"
LEFT = {"east": (0, -1), "west": (0, 1), "north": (-1, 0), "south": (1, 0)}
INTO = {(0, 1): "north", (0, -1): "south", (1, 0): "west", (-1, 0): "east"}  # side offset -> direction pointing into M
used = set()
def merge_point(near, D, need_right=True):
    """A free tile M (belt flowing D) whose left, right and downstream neighbours are free too, near `near`."""
    dx, dy = DV[D]
    lx, ly = LEFT[D]
    best = None
    for r in range(2, 25):
        for ox in range(-r, r + 1):
            for oy in range(-r, r + 1):
                if max(abs(ox), abs(oy)) != r:
                    continue
                m = (math.floor(near[0]) + ox, math.floor(near[1]) + oy)
                tiles = [m, (m[0] + lx, m[1] + ly), (m[0] + dx, m[1] + dy), (m[0] - dx, m[1] - dy),
                         (m[0] - lx, m[1] - ly)]
                if all(t not in taken and t not in used and g.at(*t) in ".t" for t in tiles):
                    used.update(tiles)
                    return m
    raise SystemExit(f"no merge point near {near}")
def side(m, off):
    return (m[0] + off[0] + .5, m[1] + off[1] + .5)
mi = merge_point((x0 - 14, IC["y"] + 6), "east")
ms = merge_point((-16.5, -22.5), "east")
mu = merge_point((x0 - 6, SU["y"] - 3), "east")
merges = [{"item": "transport-belt", "x": m[0] + .5, "y": m[1] + .5, "direction": "east"} for m in (mi, ms, mu)]
merges.append({"item": "transport-belt", "x": mu[0] - .5, "y": mu[1] + .5, "direction": "east"})  # rear belt: SU merge stays straight
pg.save("bl-merge", merges, {})
L, R = LEFT["east"], (-LEFT["east"][0], -LEFT["east"][1])
ends = [(steel_trunk["x"], steel_trunk["y"] + 1), side(ms, L), (PLx, PLy - 3), side(ms, R),
        (SUx, SUy - 3), side(mu, L), (iron_trunk["x"], iron_trunk["y"] + 1), side(mi, L),
        cout, side(mi, R), after(EX), gj[1], (11.5, -17.5), (PLx - 3, PLy),
        (mi[0] + 1.5, mi[1] + .5), (ms[0] + 1.5, ms[1] + .5), (mu[0] + 1.5, mu[1] + .5),
        start(IC), start(SP), start(SU), after(IC), after(SP), start(ret_of(IC)), start(ret_of(SP))]
pg.save("bl-ends", [{"item": "pipe", "x": x, "y": y} for x, y in ends], {})
res_list = reserve + ["bl-tmp", "bl-ends", "bl-merge"]
routes = []
def rt(name, a, z, fd, td, margin=10):
    global res_list
    r = pg.route(a, z, fd, td, res_list, margin=margin, own=["blue1.prev"] if (pg.OUT / "blue1.prev.json").exists() else [])
    assert r, f"no route {name}"
    routes.extend(r)
    pg.save("bl-" + name, r, {})
    res_list = res_list + ["bl-" + name]
    print(name, len(r), "ug", sum("underground" in p["item"] for p in r))
D_IN = lambda off: INTO[off]  # a route ending beside M points into it
rt("loop-ic", after(IC), start(ret_of(IC)), "east", ret_of(IC)["dir"], 6)
rt("loop-sp", after(SP), start(ret_of(SP)), "east", ret_of(SP)["dir"], 6)
rt("ic", ends[14], start(IC), "east", "east", 12)
rt("sp", ends[15], start(SP), "east", "east", 12)
rt("su", ends[16], start(SU), "east", "east", 12)
rt("iron", ends[6], ends[7], "south", D_IN(L), 14)
rt("copper", cout, ends[9], cp["direction"], D_IN(R), 14)
rt("export", after(EX), gj[1], EX["dir"], gj[2], 14)
rt("steel", ends[0], ends[1], "south", D_IN(L))
rt("plastic", ends[2], ends[3], None, D_IN(R), 18)
rt("sulfur", ends[4], ends[5], None, D_IN(L), 18)
rt("coal", (11.5, -17.5), ends[13], "east", "east", 14)
routes = merges + routes
place = remove + base + routes
place = pg.diff_removals("blue1.prev", place) + place
place += pg.keep_prev_poles("blue1.prev", [p for p in place if not p.get("remove")])
keep = [p for p in place if not p.get("remove")]
place += pg.cover_power(keep, reserve)
place += pg.connect_poles([p for p in place if not p.get("remove")], reserve, exclude=["blue1.prev"])
tg = [[p["x"], p["y"]] for p in block if p["item"].startswith("assembling")]
pg.save("blue1", place, {
    "power": True, "targets": tg, "research": ["chemical-science-pack", "advanced-circuit", "engine", "fast-inserter",
                                               "automation-2", "plastics", "sulfur-processing"],
    "allow": ["iron-plate", "copper-plate", "steel-plate", "chemical-science-pack", "coal"],
    "feeds": [{"x": ends[6][0], "y": ends[6][1], "lanes": {"west": "iron-plate", "east": "iron-plate"}, "tiles": 6},
              {"x": cout[0], "y": cout[1], "lanes": {"west": "copper-plate", "east": "copper-plate", "north": "copper-plate", "south": "copper-plate"}, "tiles": 6},
              {"x": ends[0][0], "y": ends[0][1], "lanes": {"west": "steel-plate", "east": "steel-plate"}, "tiles": 6},
              {"x": ends[2][0], "y": ends[2][1], "lanes": {"north": "plastic-bar", "south": "plastic-bar", "west": "plastic-bar", "east": "plastic-bar"}, "tiles": 6},
              {"x": ends[4][0], "y": ends[4][1], "lanes": {"north": "sulfur", "south": "sulfur", "west": "sulfur", "east": "sulfur"}, "tiles": 4}]})
