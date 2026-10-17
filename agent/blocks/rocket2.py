"""Rocket chain, phase 2: block Y as laid out by CP-SAT (plan.py layout blocks/rocketY.layout.json --out
blocks/rocketY.json): engines, electric engines, batteries, frames, utility packs; and every feed:
B5 -> K3, B3 -> K1 (looped to K4), B4 -> K6, utility K5 -> lab 58.5,-15.5, lubricant -> EEU, acid -> BAT, poles. Belts: plan_nx A*; pipes: plan_nx pipe router; poles: rocket.pole_chain.
Writes runtime/blocks/rocket2-places.json.        .venv/Scripts/python blocks/rocket2.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
sys.path.insert(0, HERE)
import plan  # noqa: E402
import plan_block  # noqa: E402
import plan_nx  # noqa: E402
from rocket import T, tiles, pole_chain, RT  # noqa: E402


def fluid_pipes(b, fluid, box):
    x1, y1, x2, y2 = box
    lua = ("/sc local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{area={{%d,%d},{%d,%d}},type='pipe'}) do "
           "local f=e.fluidbox[1] if f and f.name=='%s' then o[#o+1]=e.position.x..','..e.position.y end end "
           "rcon.print(table.concat(o,';'))") % (x1, y1, x2, y2, fluid)
    return {T(*map(float, s.split(","))) for s in b.raw(lua).strip().split(";") if s}


def main():
    b = plan._bridge()
    rec = plan_block.recipes_from_game(b, plan.RECIPE_LUA)
    spec = json.load(open(os.path.join(HERE, "rocketY.json"), encoding="utf-8"))
    r = plan_block.compile_block(spec, rec)
    if r["errors"]:
        raise SystemExit("; ".join(r["errors"]))
    slow = {w.split()[1] for w in r["rates"] if w.startswith("inserter ")}
    for p in r["places"]:
        if p["item"] == "inserter" and f"{p['x']:g},{p['y']:g}" in slow:
            p["item"] = "fast-inserter"
    places = {"Y": r["places"]}
    g = plan.Grid.load(b, -60, -24, 66, 96)
    blocked = set()
    phase1 = json.load(open(os.path.join(RT, "rocket-places.json")))

    def block(ps):
        for p in ps:
            blocked.update(tiles(p))
        rows = [list(row) for row in g.rows]
        for (x, y) in blocked:
            if g.x1 <= x < g.x2 and g.y1 <= y < g.y2:
                rows[y - g.y1][x - g.x1] = "#"
        g.rows = ["".join(row) for row in rows]
    block(phase1 + r["places"])
    block([{"item": "x", "x": 3.5, "y": 63.5}])  # B6 end stays a dead end
    routes = {}
    for key, s, gl, fd, td in [("yellow", (-2.5, 80.5), (58.5, -18.5), "east", "south"),   # K5 -> lab (long: first)
                               ("loopB3", (-20.5, 66.5), (-20.5, 79.5), "west", "east"),   # K1 -> K4
                               ("feedB5", (3.5, 62.5), (-20.5, 73.5), "east", "east"),     # B5 -> K3
                               ("feedB4", (21.5, 56.5), (-20.5, 86.5), "east", "east"),    # B4 -> K6
                               ("feedB3", (21.5, 55.5), (-2.5, 66.5), "east", "west")]:    # B3 -> K1
        rt = plan.route(g, T(*s), T(*gl), fd, td)
        if not rt:
            raise SystemExit(f"no belt route {key}")
        routes[key] = rt
        block(rt)
        print(key, len(rt), " | ".join(plan.segments(rt))[:160])
    lab_ins = {"item": "inserter", "x": 58.5, "y": -17.5, "direction": "south"}  # belt -> lab 58.5,-15.5
    routes["yellow"].append(lab_ins)
    block([lab_ins])
    # lubricant: from the lubricant plant's output ports to EEU's west port
    lube = None
    for src in ((-42.5, 54.5), (-40.5, 54.5)):
        lube = lube or plan_nx.route_pipe(g, {T(*src)}, T(-19.5, 69.5), blocked=blocked, goal_dir="east", tries=12)
    if not lube:
        raise SystemExit("no lubricant route")
    routes["lube"] = lube
    block(lube)
    acid = fluid_pipes(b, "sulfuric-acid", (-30, 45, 0, 60)) or {T(-17.5, 53.5), T(-16.5, 53.5), T(-15.5, 53.5)}
    a = None
    for port in ((-3.5, 75.5), (-3.5, 77.5)):  # BAT faces east: inputs on its east side
        a = a or plan_nx.route_pipe(g, acid, T(*port), blocked=blocked, own=acid, goal_dir="west", tries=12)
    if not a:
        raise SystemExit("no acid route")
    routes["acid"] = [p for p in a if not (p.get("source") and T(p["x"], p["y"]) in acid)]
    allp = []
    for k, ps in list(places.items()) + list(routes.items()):
        for p in ps:
            allp.append(dict(p, group=f"{k}:{p.get('group', k)}"))
    # poles: link every new pole group to the phase-1 network
    base = [(p["x"], p["y"]) for p in phase1 if p["item"] == "small-electric-pole"]
    extra = []
    for comp in r["pole_components"]:
        comp = [tuple(c) for c in comp]
        if not any(((c[0] - q[0]) ** 2 + (c[1] - q[1]) ** 2) ** 0.5 <= 7.5 for c in comp for q in base + extra):
            extra += pole_chain(b, comp, base + extra, [], (-26, 50, 24, 84))
        base += comp
    allp += [{"item": "small-electric-pole", "x": x, "y": y, "group": "power:poles"} for x, y in extra]
    json.dump(allp, open(os.path.join(RT, "rocket2-places.json"), "w"), indent=0)
    print({k: len(v) for k, v in routes.items()}, "poles+", len(extra), "total", len(allp))
    print("map check:", plan_block.check_on_map(b, allp))


if __name__ == "__main__":
    main()
