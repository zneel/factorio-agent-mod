"""Oil 1 (basic oil processing first, advanced later on the same refinery): 2 pumpjacks on the near wells, refinery
R1 facing north, chemical plants PL (plastic) and SU (sulfur) facing south across a 2-row pipe gap, a water pump
at the lake. Every fluid connection is routed by plan_nx.route_pipe; footprints are blocked and the port tiles of
other fluids count as foreign, so no two networks touch. Port tiles are reserved before anything else.
    .venv/Scripts/python blocks/oil1.py  -> runtime/blocks/new/oil1.json"""
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan
import plan_gen as pg
import plan_nx

R1 = (14.5, -36.5)
PL = (19.5, -44.5)
SU = (25.5, -44.5)
PJ1, PJ2 = (-77.5, -98.5), (-75.5, -101.5)
W2 = (57.5, -5.5)
machines = [
    {"item": "pumpjack", "x": PJ1[0], "y": PJ1[1], "direction": "east"},
    {"item": "pumpjack", "x": PJ2[0], "y": PJ2[1], "direction": "east"},
    {"item": "oil-refinery", "x": R1[0], "y": R1[1], "direction": "north", "recipe": "basic-oil-processing"},
    {"item": "chemical-plant", "x": PL[0], "y": PL[1], "direction": "south", "recipe": "plastic-bar"},
    {"item": "chemical-plant", "x": SU[0], "y": SU[1], "direction": "south", "recipe": "sulfur"},
    {"item": "offshore-pump", "x": W2[0], "y": W2[1], "direction": "east"},
]
def T(x, y):
    return (math.floor(x), math.floor(y))
ports = {  # fluid -> port tiles (where a pipe must sit); refinery north-facing, chem plants south-facing
    "crude": [T(PJ1[0] + 2, PJ1[1] - 1), T(PJ2[0] + 2, PJ2[1] - 1), T(R1[0] + 1, R1[1] + 3)],
    "water_r": [T(R1[0] - 1, R1[1] + 3)],
    "heavy": [T(R1[0] - 2, R1[1] - 3)], "light": [T(R1[0], R1[1] - 3)],
    "gas": [T(R1[0] + 2, R1[1] - 3), T(PL[0] - 1, PL[1] + 2), T(PL[0] + 1, PL[1] + 2), T(SU[0] - 1, SU[1] + 2)],
    "water": [T(W2[0] - 1, W2[1]), T(SU[0] + 1, SU[1] + 2)],
}
reserve = ["iron1", "copper1", "coal1", "grid1", "coalbelt1poles", "science1", "coalhub", "steel1", "pickups1", "iron2",
           "power1", "labs1", "blueblock"]
taken = set()
for n in reserve:
    for q in pg.load_plan(n):
        if not q.get("remove"):
            taken |= pg.footprint(q)
for m in machines:
    taken |= pg.footprint(m)
# solid I/O: plastic out (north side, onto a belt row -47.5), coal in (west side), sulfur out (north side)
solid = [{"item": "inserter", "x": PL[0], "y": PL[1] - 2, "direction": "north"},
         {"item": "inserter", "x": PL[0] - 2, "y": PL[1], "direction": "east"},
         {"item": "inserter", "x": SU[0], "y": SU[1] - 2, "direction": "north"}]
for q in solid:
    taken |= pg.footprint(q)
g = plan.Grid.load(plan._bridge(), -95, -115, 70, 5)
rows = [list(r) for r in g.rows]
import os
if os.path.exists(pg.OUT / "oil1.prev.json"):  # recomposing over a partly built version: its pieces are not obstacles
    for q in pg.load_plan("oil1.prev"):
        for (x, y) in pg.footprint(q):
            if g.x1 <= x < g.x2 and g.y1 <= y < g.y2:
                rows[y - g.y1][x - g.x1] = "."
for (x, y) in taken:
    if g.x1 <= x < g.x2 and g.y1 <= y < g.y2:
        rows[y - g.y1][x - g.x1] = "#"
g.rows = ["".join(r) for r in rows]
all_ports = {t: f for f, ts in ports.items() for t in ts}
nets = {}  # fluid -> tiles of its planned pipe network
places = []


def connect(fluid, a, b, own=()):
    foreign = {t for t, f in all_ports.items() if f != fluid} | {t for f, ts in nets.items() if f != fluid for t in ts}
    r = plan_nx.route_pipe(g, {a} | set(own), b, foreign=foreign, own=set(own))
    assert r, f"no {fluid} route {a} -> {b}"
    have = nets.setdefault(fluid, set())
    r = [p for p in r if T(p["x"], p["y"]) not in have or p["item"] != "pipe"]  # tiles already piped by this network
    have.update({T(p["x"], p["y"]) for p in r})
    have |= {a, b}
    for p in r:
        p.pop("source", None)
    places.extend({k: v for k, v in p.items() if k in ("item", "x", "y", "direction")} for p in r)
    print(fluid, len(r), "pieces", sum(p["item"] == "pipe-to-ground" for p in r), "p2g")


# short local networks first, the long crude and water lines last
connect("gas", ports["gas"][0], ports["gas"][1])
connect("gas", ports["gas"][0], ports["gas"][3], own=nets["gas"])
connect("crude", ports["crude"][1], ports["crude"][0])
connect("crude", ports["crude"][0], ports["crude"][2], own=nets["crude"])
connect("water", ports["water"][0], ports["water"][1])
place = machines + solid + places
place += pg.keep_prev_poles("oil1.prev", place)
place += pg.cover_power(place, reserve)
place += pg.connect_poles(place, reserve, exclude=["oil1.prev"])
place = pg.diff_removals("oil1.prev", place) + place
gate = {"power": True, "research": ["oil-gathering", "oil-processing", "plastics", "sulfur-processing"],
        "targets": [[m["x"], m["y"]] for m in machines if m["item"] in ("pumpjack", "oil-refinery", "offshore-pump")],
        "chests": [], "allow": []}
pg.save("oil1", place, gate)
