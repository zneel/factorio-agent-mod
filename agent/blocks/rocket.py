"""Rocket chain, phase 1: smelter (blocks/rocketS.json) + block X (blocks/rocketX.json) + every feed, routed.
Belts: plan_nx A* (networkx); water: plan_nx pipe router. Writes runtime/blocks/rocket-places.json (groups)
and prints a short summary.      .venv/Scripts/python blocks/rocket.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
import plan  # noqa: E402
import plan_block  # noqa: E402
import plan_nx  # noqa: E402

RT = os.path.join(AGENT, "..", "runtime", "blocks")
SIZE = {"assembling-machine-2": 3, "electric-furnace": 3, "chemical-plant": 3}


def T(x, y):
    return (int(x // 1), int(y // 1))


def tiles(p):
    n = SIZE.get(p["item"], 1)
    if p["item"] == "splitter":  # two tiles across its direction
        dx = 0 if p["direction"] in ("east", "west") else 0.5
        dy = 0.5 if p["direction"] in ("east", "west") else 0
        return {T(p["x"] - dx, p["y"] - dy), T(p["x"] + dx, p["y"] + dy)}
    return {T(p["x"] - n / 2 + 0.5 + i, p["y"] - n / 2 + 0.5 + j) for i in range(n) for j in range(n)}


def main():
    b = plan._bridge()
    rec = plan_block.recipes_from_game(b, plan.RECIPE_LUA)
    out = {}
    for name in ("rocketS", "rocketX"):
        spec = json.load(open(os.path.join(HERE, name + ".json"), encoding="utf-8"))
        r = plan_block.compile_block(spec, rec)
        if r["errors"]:
            raise SystemExit(f"{name}: " + "; ".join(r["errors"]))
        # inserters the rate check flags (not long-handed) become fast inserters
        slow = {w.split()[1] for w in r["rates"] if w.startswith("inserter ")}
        for p in r["places"]:
            if p["item"] == "inserter" and f"{p['x']:g},{p['y']:g}" in slow:
                p["item"] = "fast-inserter"
        out[name] = r["places"]
    g = plan.Grid.load(b, -32, 14, 52, 70)
    blocked = set()

    def block(ps):
        for p in ps:
            blocked.update(tiles(p))
        rows = [list(r) for r in g.rows]
        for (x, y) in blocked:
            if g.x1 <= x < g.x2 and g.y1 <= y < g.y2:
                rows[y - g.y1][x - g.x1] = "#"
        g.rows = ["".join(r) for r in rows]
    block(out["rocketS"] + out["rocketX"])
    splitter = {"item": "splitter", "x": 10.5, "y": 44.0, "direction": "east"}
    block([splitter])
    routes = {}
    for key, s, gl, fd, td in [("loop2", (-22.5, 49.5), (-22.5, 62.5), "west", "east"),
                               ("loop1", (-22.5, 48.5), (-22.5, 63.5), "west", "east"),
                               ("plastic", (11.5, 44.5), (21.5, 48.5), "east", "west"),
                               ("ironore", (14.5, 17.5), (23.5, 55.5), "south", "east")]:
        r = plan.route(g, T(*s), T(*gl), fd, td)
        if not r:
            raise SystemExit(f"no belt route {key}")
        routes[key] = r
        block(r)
    # water: from the fed water column (plain pipes) to ACID's west input port
    lua = ("/sc local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{area={{-30,40},{-18,56}},type='pipe'}) do "
           "local f=e.fluidbox[1] if f and f.name=='water' then o[#o+1]=e.position.x..','..e.position.y end end rcon.print(table.concat(o,';'))")
    water = {T(*map(float, s.split(","))) for s in b.raw(lua).strip().split(";") if s}
    w = None
    for port in ((-21.5, 51.5), (-21.5, 53.5)):  # ACID's two west input ports
        w = w or plan_nx.route_pipe(g, water, T(*port), blocked=blocked, own=water, goal_dir="east", tries=12)
    if not w:
        raise SystemExit("no water route")
    w = [p for p in w if not (p.get("source") and T(p["x"], p["y"]) in water)]
    routes["water"] = w
    allp = []
    for k, ps in list(out.items()) + list(routes.items()):
        for p in ps:
            allp.append(dict(p, group=f"{k}:{p.get('group', k)}"))
    allp.append(dict(splitter, group="plastic:splitter"))
    json.dump(allp, open(os.path.join(RT, "rocket-places.json"), "w"), indent=0)
    print({k: len(v) for k, v in routes.items()}, "total", len(allp))
    chk = [p for p in allp if p["item"] != "splitter"]
    print("map check:", plan_block.check_on_map(b, chk))



def pole_chain(b, sources, targets, existing, box, reach=7.5):
    """Fewest new small poles linking sources to targets: networkx shortest path over free tiles
    (scipy KD-tree for the wire-reach pairs). existing: poles already standing (free to pass through)."""
    import networkx as nx
    from scipy.spatial import cKDTree
    g = plan.Grid.load(b, *box)
    pts = [(x + 0.5, y + 0.5) for y in range(g.y1, g.y2) for x in range(g.x1, g.x2) if g.free(x, y)]
    pts += list(sources) + list(targets) + list(existing)
    pts = list(dict.fromkeys(pts))
    G = nx.Graph()
    G.add_edges_from(cKDTree(pts).query_pairs(reach))
    old = set(sources) | set(targets) | set(existing)
    for i, p in enumerate(pts):  # stepping on a new tile costs a pole
        G.nodes[i]["new"] = p not in old
    for u, v in G.edges:
        G.edges[u, v]["w"] = 1 + G.nodes[v].get("new", True) + G.nodes[u].get("new", True)
    idx = {p: i for i, p in enumerate(pts)}
    G.add_edges_from((("S", idx[s]) for s in sources), w=0)
    G.add_edges_from(((idx[t], "T") for t in targets), w=0)
    path = nx.shortest_path(G, "S", "T", weight="w")
    return [pts[i] for i in path[1:-1] if pts[i] not in old]


def power_main():
    b = plan._bridge()
    import mapq
    s = mapq.load()
    poles = {(e["x"], e["y"]): e.get("net") for e in s["entities"] if e["t"] == "electric-pole"}
    allp = json.load(open(os.path.join(RT, "rocket-places.json")))
    smelter = [(p["x"], p["y"]) for p in allp if p["group"].startswith("rocketS") and p["item"] == "small-electric-pole"]
    net55 = [p for p, n in poles.items() if n == 55]
    net54 = [p for p, n in poles.items() if n == 54]
    a = pole_chain(b, smelter, net55, [], (30, 40, 70, 115))
    c = pole_chain(b, net55, net54, [], (55, 110, 95, 222))
    new = a + c
    allp = [p for p in allp if not p["group"].startswith("power")]
    allp += [{"item": "small-electric-pole", "x": x, "y": y, "group": "power:poles"} for x, y in new]
    json.dump(allp, open(os.path.join(RT, "rocket-places.json"), "w"), indent=0)
    print(f"power line: {len(a)} + {len(c)} new small poles; map check:",
          plan_block.check_on_map(b, [{"item": "small-electric-pole", "x": x, "y": y} for x, y in new]))


if __name__ == "__main__":
    main()
    power_main()
