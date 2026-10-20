"""Science complex 1: red+green block (CP-SAT, blocks/rg.json) + labs + the routes that feed it.
    .venv/Scripts/python blocks/science1.py   -> runtime/blocks/new/science1.json (then gate.py)"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan, plan_block, plan_gen as pg

b = plan._bridge()
spec = json.load(open(Path(__file__).parent / "rg.json"))
res = plan_block.compile_block(spec, plan_block.recipes_from_game(b, plan.RECIPE_LUA))
assert not res["errors"], res["errors"]
block = [{k: v for k, v in p.items() if k in ("item", "x", "y", "direction", "recipe", "belt_type")} for p in res["places"]]
def lanes(bb):
    return set(v for v in bb["lanes"].values() if v)
def start(bb):  # tile just upstream of the belt's first tile
    return (bb["x1"] - 1 if bb["dir"] == "east" else bb["x1"] + 1, bb["y"])
def after(bb):  # tile just downstream of its last tile
    return (bb["x2"] + 1 if bb["dir"] == "east" else bb["x2"] - 1, bb["y"])
bus = [bb for bb in spec["belts"] if lanes(bb) == {"iron-plate", "copper-plate"}]
entry = min(bus, key=lambda bb: start(bb)[0])          # the bundle belt starting at the west side
ret = [bb for bb in bus if bb is not entry]
redb = next(bb for bb in spec["belts"] if "automation-science-pack" in lanes(bb))
grnb = next(bb for bb in spec["belts"] if "logistic-science-pack" in lanes(bb))
print("entry", entry["id"], entry["dir"], "return", [r["id"] for r in ret], "red", redb["id"], redb["dir"], "green", grnb["id"], grnb["dir"])
labs = pg.labs(-58.5, 1.5, 6, top_dir="west", bottom_dir="east")
# stub west of the entry belt: the bundle starts here; iron side-loads from the north, copper from the south
assert entry["dir"] == "east"
stub = [{"item": "transport-belt", "x": entry["x1"] - k, "y": entry["y"], "direction": "east"} for k in (2, 1)]
sx = stub[0]["x"]
base = block + labs + stub
pg.save("sci-tmp", base, {})
# every route's end tiles are claimed up front, so an earlier route can't run over a later route's start
ends = [after(entry)] + [start(r_) for r_ in ret] + [after(redb), after(grnb), (-56.5, -21.5), (-67.5, -31.5)]
pg.save("sci-ends", [{"item": "pipe", "x": x, "y": y} for x, y in ends], {})
reserve = ["sci-tmp", "iron1", "copper1", "coal1", "grid1", "sci-ends"]
routes = []
def rt(name, a, z, fd, td):
    global reserve
    r = pg.route(a, z, fd, td, reserve, margin=10)
    assert r, f"no route {name}"
    routes.append((name, r))
    pg.save("sci-tmp-" + name, r, {})
    reserve = reserve + ["sci-tmp-" + name]
    print(name, len(r), "pieces", sum("underground" in p["item"] for p in r), "underground")
# long ones first
lab_top = [p for p in labs if p["item"] == "transport-belt" and p["y"] == 1.5]
lab_bot = [p for p in labs if p["item"] == "transport-belt" and p["y"] == 7.5]
rt("red", after(redb), (max(p["x"] for p in lab_top) + 1, 1.5), redb["dir"], "west")
rt("green", after(grnb), (min(p["x"] for p in lab_bot) - 1, 7.5), grnb["dir"], "east")
rt("iron", (-56.5, -21.5), (sx, entry["y"] - 1), "south", "south")
rt("copper", (-67.5, -31.5), (sx, entry["y"] + 1), "south", "north")
for r_ in ret:
    rt("loop", after(entry), start(r_), entry["dir"], r_["dir"])
place = base + [p for _, r in routes for p in r]
place += pg.connect_poles(place, ["iron1", "copper1", "coal1", "grid1"])
print("poles added", sum(1 for p in place if p["item"] == "small-electric-pole"))
allp = [p for _, r in routes for p in r]
poles = pg.poles_along([p for p in labs if p["item"] == "transport-belt"][:1] + [{"x": -56.5, "y": -20.5}], reserve, spacing=7)
tg = [[p["x"], p["y"]] for p in block if p["item"].startswith("assembling")]
gate = {"power": True, "targets": tg,
        "feeds": [{"x": -56.5, "y": -21.5, "lanes": {"west": "iron-plate", "east": "iron-plate"}, "tiles": 6},
                  {"x": -67.5, "y": -31.5, "lanes": {"west": "copper-plate", "east": "copper-plate"}, "tiles": 6}],
        "research": ["logistic-science-pack"], "allow": ["iron-plate", "copper-plate"]}
pg.save("science1", place, gate)
