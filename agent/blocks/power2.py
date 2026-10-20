"""Power 2: pump at (64.5,26.5), 4 boilers + 8 engines (7.2 MW), coal from a splitter on the boiler coal belt."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan_gen as pg

place, gate = pg.power((64.5, 26.5), 50, 4)
reserve = ["iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "coalhub", "steel1", "pickups1", "iron2", "iron2pick",
           "blue1", "oil1", "power1", "labs1", "science1", "sci1fix"]
bad = pg.collisions(place, reserve)
assert not bad, bad[:5]
top = min((p for p in place if p["item"] == "transport-belt"), key=lambda p: p["y"])
remove = [{"remove": True, "item": "transport-belt", "x": 30.5, "y": -18.5}]
split = [{"item": "splitter", "x": 30.5, "y": -18.0, "direction": "east"}]
pg.save("p2-tmp", place + split, {})
coal = pg.route((31.5, -17.5), (top["x"], top["y"] - 1), "east", "south", reserve + ["p2-tmp"], margin=12)
assert coal, "no coal route"
print("coal route", len(coal), "ug", sum("underground" in p["item"] for p in coal))
allp = place + split + coal
allp += pg.connect_poles(allp, reserve)
gate["allow"] = ["coal"]
gate["feeds"] = [{"x": 31.5, "y": -17.5, "lanes": {"both": "coal"}, "tiles": 3}]
pg.save("power2", remove + allp, gate)
