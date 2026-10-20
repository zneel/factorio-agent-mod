"""Repairs: (1) copper coal stub had no rear belt, so it curved and put coal on both lanes: add a rear belt.
(2) science1's red route ran past K3's east end (K3 side-loaded belts/inserters into it): remove the red route and
the clogged labs top belt, re-place the labs belt (clears its contents) and route red again with belt-end fronts
blocked."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan_gen as pg

cu = [{"item": "transport-belt", "x": -78.5, "y": -26.5, "direction": "north"}]
reserve = ["iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "coalhub", "steel1", "pickups1", "iron2", "iron2pick",
           "blue1", "oil1", "power1", "labs1"]
assert not pg.collisions(cu, reserve), pg.collisions(cu, reserve)
spec = json.load(open(Path(__file__).parent / "rg.json"))
red_old = pg.load_plan("sci-tmp-red")
sci = pg.load_plan("science1")
labs_top = [p for p in sci if p["item"] == "transport-belt" and p["y"] == 1.5 and -61 < p["x"] < -35]
remove = [dict(p, remove=True) for p in red_old + labs_top]
# science1 minus the old red route is the reservation for the new one
keep = [p for p in sci if p not in red_old]
pg.save("sci1-keep", keep, {})
K2 = next(bb for bb in spec["belts"] if "automation-science-pack" in set(bb["lanes"].values()))
start = (K2["x2"] + 1, K2["y"])
goal = (max(p["x"] for p in labs_top) + 1, 1.5)
red = pg.route(start, goal, K2["dir"], "west", reserve + ["sci1-keep"], margin=10)
assert red, "no red route"
print("red", len(red), "ug", sum("underground" in p["item"] for p in red))
place = remove + cu + labs_top + red
pg.save("sci1fix", place, {"power": False, "targets": [], "allow": ["automation-science-pack", "iron-gear-wheel", "coal"]})
