"""Steel 1: iron1's west plate belt (3.75 plates/s, plates on its right lane) is cut from the trunk and routed to a
12-furnace steel column at c=-20 (0.75 steel/s). Coal: a splitter on the hub's iron-coal belt (x=-30.5) taps a
belt that side-loads the steel input's free (east) lane. iron1's east belt alone stays the science trunk."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan_gen as pg

C, Y1 = -20, -58
remove = [{"remove": True, "item": "transport-belt", "x": x + 0.5, "y": -23.5} for x in range(-67, -57)]
remove.append({"remove": True, "item": "transport-belt", "x": -30.5, "y": -25.5})
s, sg = pg.smelt(C, Y1, 6, "iron-plate")
stub = [{"item": "transport-belt", "x": C + 0.5, "y": y, "direction": "south"} for y in (Y1 - 2.5, Y1 - 1.5, Y1 - 0.5)]
split = [{"item": "splitter", "x": -30.0, "y": -25.5, "direction": "north"}]
base = s + stub + split
pg.save("st-tmp", base, {})
ends = [(-66.5, -23.5), (C + 0.5, Y1 - 3.5), (-29.5, -26.5), (C + 1.5, Y1 - 1.5)]
pg.save("st-ends", [{"item": "pipe", "x": x, "y": y} for x, y in ends], {})
reserve = ["st-tmp", "st-ends", "iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1", "coalhub"]
plates = pg.route((-66.5, -23.5), (C + 0.5, Y1 - 3.5), "south", "south", reserve, margin=12)
assert plates, "no plate route"
pg.save("st-plates", plates, {})
coal = pg.route((-29.5, -26.5), (C + 1.5, Y1 - 1.5), "north", "west", reserve + ["st-plates"], margin=8)
assert coal, "no coal route"
print("plates", len(plates), "ug", sum("underground" in p["item"] for p in plates), "| coal", len(coal))
place = remove + base + plates + coal
place += pg.connect_poles([p for p in place if not p.get("remove")], ["iron1", "copper1", "coal1", "grid1", "science1", "coalhub"])
tg = [[p["x"], p["y"]] for p in s if p["item"] == "stone-furnace"]
pg.save("steel1", place, {"power": True, "targets": tg, "research": ["steel-processing"], "allow": ["coal", "iron-plate"],
                          "feeds": [{"x": -66.5, "y": -23.5, "lanes": {"west": "iron-plate"}, "tiles": 2},
                                    {"x": -29.5, "y": -26.5, "lanes": {"west": "coal", "east": "coal"}, "tiles": 2}]})
