"""Poles for a whole plan, on the plan's own occupancy (not the live map).

cover():   greedy set cover with small poles on free tiles until every electric consumer overlaps a supply area
connect(): union the poles by wire reach; join each group to the group holding the steam engines' poles with the
           fewest-hop chain over free tiles (networkx shortest path in a bounded box)
"""
import math

import networkx as nx

from auto import geom

ELECTRIC = ("inserter", "assembling-machine", "electric-mining-drill", "lab", "electric-furnace", "chemical-plant",
            "oil-refinery", "pumpjack", "radar", "beacon", "rocket-silo")
REACH = 7.5 - 0.01
SUPPLY = 2.5
ITEM = "small-electric-pole"


REACH_OF = {"small-electric-pole": 7.5 - 0.01, "medium-electric-pole": 9 - 0.01}
SUPPLY_OF = {"small-electric-pole": 2.5, "medium-electric-pole": 3.5}


def use(item):
    """Plan with this pole type from now on (medium poles once researched: supply 3.5, wire reach 9)."""
    global REACH, SUPPLY, ITEM
    ITEM = item
    REACH, SUPPLY = {"small-electric-pole": (7.5 - 0.01, 2.5), "medium-electric-pole": (9 - 0.01, 3.5)}[item]


def _covers(px, py, c, sup=None):
    w, h = geom.size(c)
    sup = SUPPLY if sup is None else sup
    return abs(c["x"] - px) < sup + w / 2 - 0.3 and abs(c["y"] - py) < sup + h / 2 - 0.3


def cover(places, free, extra=()):
    """free(t) -> bool for a tile; extra: more places to touch with a supply area (steam engines: a pole next to
    them joins them to the network). Returns new pole places."""
    poles = [(p["x"], p["y"], SUPPLY_OF.get(p["item"], SUPPLY)) for p in places if "electric-pole" in p["item"]]
    cons = [p for p in places if (any(k in p["item"] for k in ELECTRIC) or p["item"] == "pump")
            and "burner" not in p["item"] and not p.get("remove")] + list(extra)
    buck = {}
    for x, y, sup in poles:
        buck.setdefault((int(x // 8), int(y // 8)), []).append((x, y, sup))

    def covered(c):
        bx, by = int(c["x"] // 8), int(c["y"] // 8)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py, sup in buck.get((bx + dx, by + dy), []):
                    if _covers(px, py, c, sup):
                        return True
        return False
    left = [c for c in cons if not covered(c)]
    taken = set()
    added = []
    while left:
        c = left[0]
        best, bn = None, 0
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                t = (math.floor(c["x"]) + dx, math.floor(c["y"]) + dy)
                if t in taken or not free(t):
                    continue
                n = sum(1 for o in left[:60] if _covers(t[0] + .5, t[1] + .5, o))
                if n > bn:
                    best, bn = t, n
        if not best:
            left.pop(0)
            continue
        p = (best[0] + .5, best[1] + .5)
        added.append({"item": ITEM, "x": p[0], "y": p[1]})
        taken.add(best)
        buck.setdefault((int(p[0] // 8), int(p[1] // 8)), []).append((p[0], p[1], SUPPLY))
        left = [o for o in left if not _covers(p[0], p[1], o)]
    return added, taken


def connect(places, free, root):
    """root: a pole position on the generator network. Returns new pole places (chains)."""
    poles = [(p["x"], p["y"]) for p in places if "electric-pole" in p["item"]]
    reach = {(p["x"], p["y"]): REACH_OF.get(p["item"], REACH) for p in places if "electric-pole" in p["item"]}
    added, used, skip = [], set(), set()
    for _ in range(400):
        G = nx.Graph()
        G.add_nodes_from(poles)
        buck = {}
        for q in poles:
            buck.setdefault((int(q[0] // 8), int(q[1] // 8)), []).append(q)
        for q in poles:
            bx, by = int(q[0] // 8), int(q[1] // 8)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for r in buck.get((bx + dx, by + dy), []):
                        if r != q and math.dist(q, r) <= min(reach.get(q, REACH), reach.get(r, REACH)):
                            G.add_edge(q, r)
        main = nx.node_connected_component(G, root)
        lonely = [c for c in nx.connected_components(G) if root not in c and not (c & skip)]
        if not lonely:
            return added
        comp = min(lonely, key=lambda c: min(math.dist(a, root) for a in c))
        path = None
        # nearest pair (comp pole, main pole) gives the box for the hop search
        a, b = min(((a, b) for a in comp for b in main), key=lambda ab: math.dist(*ab))
        for m in (10, 25, 50):
            path = _chain(comp, main, a, b, m, free, used, reach)
            if path is not None:
                break
        if path is None:
            skip |= comp
            continue
        for c in path:
            added.append({"item": ITEM, "x": c[0], "y": c[1]})
            used.add((math.floor(c[0]), math.floor(c[1])))
            poles.append(c)
            reach[c] = REACH
    raise RuntimeError("poles: too many groups")


def _chain(comp, main, a, b, m, free, used, reach=None):
    reach = reach or {}
    if True:
        x1, x2 = int(min(a[0], b[0])) - m, int(max(a[0], b[0])) + m
        y1, y2 = int(min(a[1], b[1])) - m, int(max(a[1], b[1])) + m
        cand = [(x + .5, y + .5) for x in range(x1, x2) for y in range(y1, y2) if (x, y) not in used and free((x, y))]
        H = nx.Graph()
        S, T = "S", "T"
        cb = {}
        for c in cand:
            cb.setdefault((int(c[0] // 8), int(c[1] // 8)), []).append(c)
        for c in cand:
            if any(math.dist(c, p) <= min(REACH, reach.get(p, REACH)) for p in comp):
                H.add_edge(S, c, weight=1)
            if any(math.dist(c, p) <= min(REACH, reach.get(p, REACH)) for p in main):
                H.add_edge(c, T, weight=1)
            bx, by = int(c[0] // 8), int(c[1] // 8)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for d in cb.get((bx + dx, by + dy), []):
                        if d > c and math.dist(c, d) <= REACH:
                            H.add_edge(c, d, weight=1 + 0.01 * (REACH - math.dist(c, d)))
        try:
            return nx.shortest_path(H, S, T, weight="weight")[1:-1]
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
