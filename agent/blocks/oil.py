"""Oil upgrade: refinery 1 to advanced oil processing + lubricant, heavy and light cracking, solid fuel.
All pipe networks are routed by plan_pipe (multi-source A*, never touching another fluid network).
Writes runtime/blocks/oil-places.json; prints a short summary.   python blocks/oil.py [--build-scratch]"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
import plan  # noqa: E402
import plan_pipe  # noqa: E402

PLANTS = [("LUBE", "lubricant", -41.5), ("HC", "heavy-oil-cracking", -37.5),
          ("LC", "light-oil-cracking", -33.5), ("SF", "solid-fuel-from-light-oil", -29.5)]
Y = 52.5


def T(x, y):  # entity coordinate -> integer tile
    return (int(x // 1), int(y // 1))


def _add_span(spans, a, b):
    if a[0] == b[0]:
        for y in range(min(a[1], b[1]), max(a[1], b[1]) + 1):
            spans["v"].add((a[0], y))
    elif a[1] == b[1]:
        for x in range(min(a[0], b[0]), max(a[0], b[0]) + 1):
            spans["h"].add((x, a[1]))


def main():
    b = plan._bridge()
    g = plan.Grid.load(b, -60, 26, -10, 64)
    places = []
    blocked = set()
    for _, recipe, x in PLANTS:
        places.append({"item": "chemical-plant", "x": x, "y": Y, "direction": "north", "recipe": recipe})
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                blocked.add(T(x + dx, Y + dy))
    # solid fuel out: inserter + chest; poles between plants
    extra = [{"item": "inserter", "x": -29.5, "y": 54.5, "direction": "south"},
             {"item": "iron-chest", "x": -29.5, "y": 55.5}] + \
            [{"item": "small-electric-pole", "x": x, "y": Y} for x in (-39.5, -35.5, -31.5)]
    for p in extra:
        blocked.add(T(p["x"], p["y"]))
    places += extra
    # existing PLAIN pipes by fluid: branches may start only from these (an underground piece joins only along its axis)
    lua = ("/sc local s=game.surfaces[1] local o={} for _,e in pairs(s.find_entities_filtered{area={{-60,26},{-10,64}},"
           "type='pipe'}) do local f=e.fluidbox[1] o[#o+1]=e.position.x..','..e.position.y..','..(f and f.name or '-') end "
           "rcon.print(table.concat(o,';'))")
    existing = {}
    for item in b.raw(lua).strip().split(";"):
        if item:
            x, y, f = item.split(",")
            existing.setdefault(f, set()).add(T(float(x), float(y)))
    # existing pipe-to-ground pairs: their spans (ends included) per axis
    lua2 = ("/sc local s=game.surfaces[1] local o={} for _,e in pairs(s.find_entities_filtered{area={{-70,16},{0,74}},name='pipe-to-ground'}) do "
            "for _,grp in pairs(e.neighbours) do for _,n in pairs(grp) do if n.name=='pipe-to-ground' then o[#o+1]=e.position.x..','..e.position.y..','..n.position.x..','..n.position.y end end end end "
            "rcon.print(table.concat(o,';'))")
    spans = {"v": set(), "h": set()}
    # an underground pipe piece joins only along its axis (its pipe side is already a 'p' pipe tile), so side
    # contact is harmless: mark the pieces as plain obstacles instead of foreign fluid
    rows = [list(r) for r in g.rows]
    lua3 = ("/sc local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{area={{-60,26},{-10,64}},name='pipe-to-ground'}) do "
            "o[#o+1]=e.position.x..','..e.position.y end rcon.print(table.concat(o,';'))")
    for item in b.raw(lua3).strip().split(";"):
        if item:
            x, y = T(*(float(v) for v in item.split(",")))
            if g.y1 <= y < g.y2 and g.x1 <= x < g.x2:
                rows[y - g.y1][x - g.x1] = "#"
    g.rows = ["".join(r) for r in rows]
    for item in b.raw(lua2).strip().split(";"):
        if item:
            x1, y1, x2, y2 = (float(v) for v in item.split(","))
            _add_span(spans, T(x1, y1), T(x2, y2))
    nets = {"heavy": set(), "light": set(), "petro": set(existing.get("petroleum-gas", set())),
            "water": set(existing.get("water", set()))}

    # every port tile belongs to one network; the others must keep clear of it (and of the tiles beside it)
    ports = {"heavy": [(-50.5, 40.5), (-42.5, 50.5), (-40.5, 50.5), (-36.5, 50.5)],
             "light": [(-48.5, 40.5), (-32.5, 50.5), (-30.5, 50.5), (-28.5, 50.5), (-38.5, 54.5), (-36.5, 54.5)],
             "petro": [(-34.5, 54.5), (-32.5, 54.5)],
             "water": [(-49.5, 46.5), (-38.5, 50.5), (-34.5, 50.5)],
             "lube": [(-42.5, 54.5), (-40.5, 54.5)]}
    port_tiles = {k: {T(*p) for p in v} for k, v in ports.items()}

    def route(net, goal, start=None, into=None):
        foreign = set().union(*[v for k, v in nets.items() if k != net])             | set().union(*[v for k, v in port_tiles.items() if k != net])
        r = plan_pipe.route_pipe(g, T(*start) if start else None, T(*goal), extra_blocked=blocked, foreign=foreign, own=nets[net], spans=spans,
                                 goal_dir=into or ("south" if goal[1] < Y - 1 else "north"))
        if not r:
            raise SystemExit(f"no route for {net} to {goal}")
        new = [p for p in r if not (p.get("source") and T(p["x"], p["y"]) in nets[net])]
        ptg = [T(p["x"], p["y"]) for p in new if p["item"] == "pipe-to-ground"]
        for i in range(0, len(ptg), 2):
            _add_span(spans, ptg[i], ptg[i + 1])
        for p in new:
            p["net"] = net
            nets[net].add(T(p["x"], p["y"]))
        places.extend(new)
        return len(new)

    n = {}
    n["heavy"] = route("heavy", (-42.5, 50.5), start=(-50.5, 40.5))   # refinery heavy -> lubricant
    n["heavy"] += route("heavy", (-36.5, 50.5))                         # -> heavy cracking
    n["light"] = route("light", (-32.5, 50.5), start=(-48.5, 40.5))   # refinery light -> light cracking
    n["light"] += route("light", (-30.5, 50.5))                         # -> solid fuel
    n["light"] += route("light", (-38.5, 54.5))                         # heavy cracking output joins light
    n["petro"] = route("petro", (-34.5, 54.5))                          # light cracking output -> petroleum line
    n["water"] = route("water", (-49.5, 46.5))                          # refinery water input
    n["water"] += route("water", (-38.5, 50.5))                         # heavy cracking water
    n["water"] += route("water", (-34.5, 50.5))                         # light cracking water
    out = os.path.join(AGENT, "..", "runtime", "blocks", "oil-places.json")
    json.dump(places, open(out, "w"), indent=0)
    print("pipe tiles per network:", n, "| total placements", len(places))
    print("map check:", __import__("plan_block").check_on_map(b, [p for p in places]))


if __name__ == "__main__":
    main()
