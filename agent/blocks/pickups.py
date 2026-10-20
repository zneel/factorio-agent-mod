"""Pickup chests: an inserter takes from a trunk belt into an iron chest, so build scripts can fetch plates
(supply.fetch reads every chest via `stock`). Each tap goes on the first trunk tile with two free tiles beside it."""
import json, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan, plan_gen as pg

PLANS = ["iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1", "coalhub", "steel1"]
taken = set()
for n in PLANS:
    for q in pg.load_plan(n):
        if not q.get("remove"):
            taken |= pg.footprint(q)
g = plan.Grid.load(plan._bridge(), -90, -70, 10, 15)

def tap(belts, n_chests=2):
    out = []
    for bt in belts:
        if len(out) >= 2 * n_chests:
            break
        if "underground" in bt["item"] or bt["item"] != "transport-belt":
            continue
        x, y = math.floor(bt["x"]), math.floor(bt["y"])
        horiz = bt["direction"] in ("east", "west")
        for sx, sy in ((0, -1), (0, 1)) if horiz else ((-1, 0), (1, 0)):
            a, c = (x + sx, y + sy), (x + 2 * sx, y + 2 * sy)
            if all(t not in taken and g.at(*t) in ".t" for t in (a, c)):
                d = {(0, -1): "north", (0, 1): "south", (-1, 0): "west", (1, 0): "east"}[(sx, sy)]
                out += [{"item": "inserter", "x": a[0] + .5, "y": a[1] + .5, "direction": d},
                        {"item": "iron-chest", "x": c[0] + .5, "y": c[1] + .5}]
                taken.update({a, c})
                break
    return out

sci = json.load(open(pg.OUT / "science1.json"))["place"]
iron_route = [p for p in pg.load_plan("sci-tmp-iron")]
cu_route = [p for p in pg.load_plan("sci-tmp-copper")]
st = [p for p in pg.load_plan("steel1") if p.get("item") == "transport-belt" and p.get("x") == -14.5]
place = tap(iron_route[3:], 2) + tap(cu_route[3:], 2) + tap(sorted(st, key=lambda p: -p["y"]), 1)
place += pg.cover_power(place, PLANS)
place += pg.connect_poles(place, PLANS)
pg.save("pickups1", place, {"power": True, "targets": [], "allow": []})
print(len(place), "entities")
