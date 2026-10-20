"""Coal hub: splitter 1 on the coal column top sends half to the boilers (existing link) and half to splitter 2,
which feeds two belts: iron smelter (side-loads the ore belt's free west lane at -61.5,-60.5) and copper
(side-loads a stub under the copper drill belt's first tile, so coal lands on its free east lane)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan_gen as pg

# coal1's link belt at x=-29.5 flows north: rows -15.5 .. -17.5
place = [{"item": "splitter", "x": -30.0, "y": -15.5, "direction": "north"},
         {"item": "splitter", "x": -31.0, "y": -16.5, "direction": "north"},
         {"item": "transport-belt", "x": -78.5, "y": -27.5, "direction": "north"}]   # copper stub
pg.save("hub-tmp", place, {})
ends = [(-31.5, -17.5), (-30.5, -17.5), (-62.5, -60.5), (-77.5, -27.5)]
pg.save("hub-ends", [{"item": "pipe", "x": x, "y": y} for x, y in ends], {})
reserve = ["hub-tmp", "hub-ends", "iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1"]
copper = pg.route((-31.5, -17.5), (-77.5, -27.5), "north", "west", reserve, margin=10)
assert copper, "no copper coal route"
pg.save("hub-copper", copper, {})
iron = pg.route((-30.5, -17.5), (-62.5, -60.5), "north", "east", reserve + ["hub-copper"], margin=10)
assert iron, "no iron coal route"
print("iron", len(iron), "copper", len(copper), "ug", sum("underground" in p["item"] for p in iron + copper))
# the link tile under splitter 1 is replaced: coal1 had a belt at (-29.5,-15.5) and (-29.5,-16.5)
pg.save("coalhub", [{"remove": True, "item": "transport-belt", "x": -29.5, "y": -15.5}] + place + iron + copper,
        {"power": False, "research": ["logistics"], "allow": ["coal"],
         "feeds": [{"x": -29.5, "y": -14.5, "lanes": {"west": "coal", "east": "coal"}, "tiles": 1}],
         "targets": []})
