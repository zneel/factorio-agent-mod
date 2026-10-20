"""Iron 2: replaces the burner iron pairs with electric drills (column A at x=-66.5 one-sided, column B at
x=-73.5 two-sided side-loading A's ore lane), routes the ore to a 24-furnace smelter at c=-43, y1=-52, and taps
the hub's iron-coal belt (row -59.5, flowing west) with a splitter for its fuel."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan_gen as pg

C, Y1 = -43, -52
remove = [dict(p, remove=True) for p in pg.load_plan("boot-iron")]
remove.append({"remove": True, "item": "transport-belt", "x": -39.5, "y": -59.5})
dA, _ = pg.drills("iron-ore", -66.5, -88, -66, "south", sides=("w",))
dB, _ = pg.drills("iron-ore", -73.5, -88, -66, "south", sides=("w", "e"))
bA = [p for p in dA if p["item"] == "transport-belt"]
bB = [p for p in dB if p["item"] == "transport-belt"]
endA, endB = max(p["y"] for p in bA), max(p["y"] for p in bB)
s, sg = pg.smelt(C, Y1, 11, "iron-ore")
stub = [{"item": "transport-belt", "x": C + 0.5, "y": y, "direction": "south"} for y in (Y1 - 2.5, Y1 - 1.5, Y1 - 0.5)]
split = [{"item": "splitter", "x": -39.5, "y": -59.0, "direction": "west"}]
base = dA + dB + s + stub + split
removed = {(r["x"], r["y"]) for r in remove}
bad = pg.collisions(base, ["iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1", "coalhub",
                           "steel1", "pickups1"], removed)
assert not bad, bad[:5]
pg.save("i2-tmp", base, {})
ends = [(-66.5, endA + 1), (-73.5, endB + 1), (C + 0.5, Y1 - 3.5), (-40.5, -58.5), (C + 1.5, Y1 - 1.5)]
pg.save("i2-ends", [{"item": "pipe", "x": x, "y": y} for x, y in ends], {})
reserve = ["i2-tmp", "i2-ends", "iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1", "coalhub", "steel1", "pickups1"]
# B joins A's west lane from the west, on A's straight run below its last drill
ore = pg.route((-66.5, endA + 1), (C + 0.5, Y1 - 3.5), "south", "south", reserve, margin=8)
assert ore, "no ore route"
pg.save("i2-ore", ore, {})
# B side-loads onto the first ore-route tile that runs south (ore lane = west lane = right lane facing south)
j = next(p for p in ore[2:] if p["item"] == "transport-belt" and p["direction"] == "south")
jb = pg.route((-73.5, endB + 1), (j["x"] - 1, j["y"]), "south", "east", reserve + ["i2-ore"], margin=6)
assert jb, "no B route"
pg.save("i2-b", jb, {})
coal = pg.route((-40.5, -58.5), (C + 1.5, Y1 - 1.5), "west", "west", reserve + ["i2-ore", "i2-b"], margin=6)
assert coal, "no coal route"
print("ore", len(ore), "ug", sum("underground" in p["item"] for p in ore), "| B", len(jb), "| coal", len(coal),
      "| drills", sum(p["item"] == "electric-mining-drill" for p in dA + dB))
place = remove + base + ore + jb + coal
keep = [p for p in place if not p.get("remove")]
place += pg.cover_power(keep, reserve)
place += pg.connect_poles([p for p in place if not p.get("remove")], reserve)
tg = [[p["x"], p["y"]] for p in s if p["item"] == "stone-furnace"]
pg.save("iron2", place, {"power": True, "targets": tg, "allow": ["coal", "iron-ore"],
                         "feeds": [{"x": -38.5, "y": -59.5, "lanes": {"north": "coal", "south": "coal"}, "tiles": 1}],
                         "sinks": sg["sinks"]})
