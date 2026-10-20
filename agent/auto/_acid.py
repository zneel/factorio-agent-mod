import json, math, sys, os
os.environ.setdefault("PYTHONHASHSEED", "0")
from auto import plan2, bus, geom, mapdata
dem = json.loads((mapdata.out_dir(1060) / "demand.json").read_text())
inner = plan2.bus_modules(dem)
order = bus.order_modules(list(inner), inner, plan2.RAW)
bus.ZONE, bus.SPACING = 18, 3
cx = bus.complex_layout(inner, order, plan2.RAW)
bus.route_fluids_bus(cx)
print(cx["fails"])
x0, y0 = int(sys.argv[1]), int(sys.argv[2])
G = {}
for q in cx["places"]:
    for t in geom.footprint(q):
        c = {"pipe": "+", "pipe-to-ground": "u", "transport-belt": "b", "underground-belt": "U", "inserter": "i",
             "small-electric-pole": "e", "chemical-plant": "C", "assembling-machine-2": "A", "splitter": "s",
             "long-handed-inserter": "l", "fast-inserter": "f", "rocket-silo": "S", "lab": "L", "stone-furnace": "F"}.get(q["item"], "?")
        G[t] = c
print("acid module", cx["bus"]["modules"].get("acid"))
for y in range(y0 - 8, y0 + 9):
    print(f"{y:4d} " + "".join("X" if (x, y) == (x0, y0) else G.get((x, y), ".") for x in range(x0 - 30, x0 + 12)))
