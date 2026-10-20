"""Validate a plan with factorio-draftsman (MIT, Factorio 2.0 data): every entity goes into a Blueprint, which
reports overlaps and invalid entities/recipes as warnings.   python -m auto.dcheck <seed>"""
import json, sys, time, warnings
from draftsman.blueprintable import Blueprint
from draftsman.constants import Direction
from auto import mapdata

D = {"north": Direction.NORTH, "east": Direction.EAST, "south": Direction.SOUTH, "west": Direction.WEST}
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}


def check(places):
    bp = Blueprint()
    msgs = []
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        for p in places:
            if p.get("remove"):
                continue
            d = p.get("direction")
            if d and "inserter" in p["item"]:
                d = OPP[d]                  # plans say where items go; the game direction is the pickup side
            kw = {"name": p["item"], "position": (p["x"], p["y"])}
            if d:
                kw["direction"] = D[d]
            if p.get("belt_type"):
                kw["io_type"] = p["belt_type"]
            if p.get("recipe"):
                kw["recipe"] = p["recipe"]
            n = len(w)
            try:
                bp.entities.append(**kw)
            except Exception as e:
                msgs.append(f"{p['item']}@{p['x']},{p['y']}: {type(e).__name__} {str(e)[:120]}")
            for x in w[n:]:
                if "OffshorePump" in str(x.message) and "Overlapping" in x.category.__name__:
                    continue   # draftsman rotates the 2.0 offshore pump box wrongly; the game accepts these pipes
                msgs.append(f"{p['item']}@{p['x']},{p['y']}: {x.category.__name__} {str(x.message)[:120]}")
    return msgs


if __name__ == "__main__":
    plan = json.loads((mapdata.out_dir(int(sys.argv[1])) / "plan.json").read_text())
    t = time.time()
    msgs = check(plan["complex"] + plan["world"])
    kinds = {}
    for m in msgs:
        k = m.split(": ", 1)[1].split(" ", 1)[0]
        kinds[k] = kinds.get(k, 0) + 1
    print(f"{len(msgs)} warnings in {time.time() - t:.0f}s: {kinds}")
    for m in msgs[:15]:
        print("  " + m)
