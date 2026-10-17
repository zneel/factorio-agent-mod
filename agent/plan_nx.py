"""Belt and pipe routing on networkx (A* over an explicit state graph).

Belt state (x, y, din, kind): a belt on tile (x, y) whose items arrive moving in direction `din`;
kind 'b' = plain belt, 'u' = underground exit (it must continue straight). Edges:
  plain  (x,y,din) -> (x+dx, y+dy, nd)           the belt on (x,y) points nd (never back against din)
  under  (x,y,din) -> (x+k*dx, y+k*dy, din, 'u')  entrance on (x,y) only when fed straight (nd == din),
                                                  exit 2..5 tiles ahead (yellow: up to 4 between)
Pipe state (x, y, din, kind) works the same way with pipe-to-ground hops up to 10; a pipe tile must not
touch other networks (static, filtered out of the graph). A pipe must not touch its own earlier tiles
either: that is path-dependent, so the route is checked after the search and the offending tiles are
banned for a re-run.
"""
import networkx as nx

DIRS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
REV = {"north": "south", "south": "north", "east": "west", "west": "east"}


def _belt_graph(g, start, goal, from_dir, to_dir, ug_gap=4):
    G = nx.DiGraph()
    sx, sy = start
    starts = [from_dir] if from_dir else list(DIRS)
    seen, todo = set(), []
    for d in starts:  # the start tile is chosen on purpose (often the tile a belt end points into): no free check
        G.add_edge("S", (sx, sy, d, "b"), weight=0, kind="start")
        todo.append((sx, sy, d, "b"))
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        x, y, din, kind = node
        if (x, y) == goal:
            for nd in DIRS:
                if (to_dir is None or nd == to_dir) and nd != REV[din] and not (kind == "u" and nd != din):
                    G.add_edge(node, "G", weight=0, kind="end", out=nd)
        for nd, (dx, dy) in DIRS.items():
            if nd == REV[din] or (kind == "u" and nd != din):
                continue
            nx_, ny = x + dx, y + dy
            if g.free(nx_, ny):
                c = g.at(nx_, ny)
                w = 1 + (2 if c == "t" else 0) + (0.2 if c == "o" else 0) + (0.5 if nd != din else 0)
                nxt = (nx_, ny, nd, "b")
                G.add_edge(node, nxt, weight=w, kind="belt", out=nd)
                todo.append(nxt)
            if nd == din and kind == "b":           # entrance only when fed straight
                for k in range(2, ug_gap + 2):
                    ex, ey = x + dx * k, y + dy * k
                    if g.free(ex, ey):
                        nxt = (ex, ey, nd, "u")
                        G.add_edge(node, nxt, weight=3 + k, kind="ug", out=nd)
                        todo.append(nxt)
    return G


def route_belt(g, start, goal, from_dir=None, to_dir=None):
    G = _belt_graph(g, start, goal, from_dir, to_dir)
    if "G" not in G:
        return None
    gx, gy = goal

    def h(u, v):
        if u in ("S", "G"):
            return 0
        return abs(u[0] - gx) + abs(u[1] - gy)
    try:
        path = nx.astar_path(G, "S", "G", heuristic=h, weight="weight")
    except nx.NetworkXNoPath:
        return None
    out = []
    for u, v in zip(path, path[1:]):
        e = G.edges[u, v]
        if e["kind"] == "start":
            continue
        x, y, din, kind = u
        if e["kind"] == "ug":
            out.append({"item": "underground-belt", "x": x + .5, "y": y + .5, "direction": e["out"], "belt_type": "input"})
        elif kind == "u":
            out.append({"item": "underground-belt", "x": x + .5, "y": y + .5, "direction": e["out"], "belt_type": "output"})
        else:
            out.append({"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": e["out"]})
    return out


def route_pipe(g, starts, goal, blocked=(), foreign=(), own=(), spans=None, goal_dir=None, ptg_gap=10, tries=6):
    """starts: tiles to branch from (or one tile). Returns placements or None."""
    spans = spans or {"v": set(), "h": set()}
    banned = set()
    starts = set(starts)
    own = set(own) | starts
    ends = own | {goal}
    blocked, foreign = set(blocked) - own, set(foreign) - own

    def ok(t):
        return t in ends or (t not in blocked and t not in foreign and t not in banned and g.free(*t))

    def touches_foreign(t, came):
        for dx, dy in DIRS.values():
            n = (t[0] + dx, t[1] + dy)
            if n == came or n in ends:
                continue
            if g.at(*n) in "pP" or n in foreign:
                return True
        return False

    for _ in range(tries):
        G = nx.DiGraph()
        todo = []
        for s in starts:
            G.add_edge("S", (s[0], s[1], None, "p"), weight=0, kind="start")
            todo.append((s[0], s[1], None, "p"))
        seen = set()
        while todo:
            node = todo.pop()
            if node in seen:
                continue
            seen.add(node)
            x, y, din, kind = node
            if (x, y) == goal:
                G.add_edge(node, "G", weight=0, kind="end")
                continue
            for nd, (dx, dy) in DIRS.items():
                if kind == "u" and nd != din:
                    continue
                t = (x + dx, y + dy)
                if not ok(t) or touches_foreign(t, (x, y)):
                    continue
                nxt = (t[0], t[1], nd, "p")
                G.add_edge(node, nxt, weight=1 + (2 if g.at(*t) == "t" else 0), kind="pipe")
                todo.append(nxt)
                axis = "v" if dx == 0 else "h"
                if t in spans[axis] or t == goal:
                    continue
                for k in range(2, ptg_gap + 2):
                    if (t[0] + dx * (k - 1), t[1] + dy * (k - 1)) in spans[axis]:
                        break
                    e = (t[0] + dx * k, t[1] + dy * k)
                    if not ok(e) or e in spans[axis] or (e == goal and nd != goal_dir):
                        continue
                    nxt2 = (e[0], e[1], nd, "u")
                    G.add_edge(node, nxt2, weight=4 + k, kind="ptg", entrance=t, d=nd)
                    todo.append(nxt2)
        if "G" not in G:
            return None
        gx, gy = goal
        try:
            path = nx.astar_path(G, "S", "G", heuristic=lambda u, v: 0 if u in ("S", "G") else abs(u[0] - gx) + abs(u[1] - gy),
                                 weight="weight")
        except nx.NetworkXNoPath:
            return None
        places, tiles = [], []
        for u, v in zip(path, path[1:]):
            e = G.edges[u, v]
            if e["kind"] == "start":
                places.append({"item": "pipe", "x": v[0] + .5, "y": v[1] + .5, "source": True})
                tiles.append(v[:2])
            elif e["kind"] == "pipe":
                places.append({"item": "pipe", "x": v[0] + .5, "y": v[1] + .5})
                tiles.append(v[:2])
            elif e["kind"] == "ptg":
                t = e["entrance"]
                places.append({"item": "pipe-to-ground", "x": t[0] + .5, "y": t[1] + .5, "direction": e["d"], "end": "entrance"})
                places.append({"item": "pipe-to-ground", "x": v[0] + .5, "y": v[1] + .5, "direction": e["d"], "end": "exit"})
                tiles += [None, None]  # pieces join only along their axis: they cannot touch the path sideways
        # self-touch check: a plain pipe must not be beside a plain pipe of the path other than its neighbours in order
        bad = set()
        used = {}
        for q in places:  # a tile used twice (a pipe-to-ground end laid over the path's own pipe)
            t = (int(q["x"] // 1), int(q["y"] // 1))
            if t in used:
                bad.add(t)
            used[t] = 1
        pipe_idx = {t: i for i, t in enumerate(tiles) if t}
        for t, i in pipe_idx.items():
            for dx, dy in DIRS.values():
                j = pipe_idx.get((t[0] + dx, t[1] + dy))
                if j is not None and abs(i - j) > 1:
                    bad.add(t)
        if not bad:
            return places
        banned |= bad
    return None
