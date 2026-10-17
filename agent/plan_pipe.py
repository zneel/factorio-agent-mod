"""Pipe router (fluids). Unlike belts, a plain pipe joins every neighbouring pipe and every machine port it
touches, so a route must keep clear of other fluid networks ('p' tiles in the snapshot) and of other
machines' port tiles ('P'), and must not touch itself except tile-to-tile along the path.
Pipe-to-ground pairs join only along their axis and hop up to 10 tiles.
"""
import heapq

DIRS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
PTG_GAP = 10


def route_pipe(g, start, goal, extra_blocked=(), own=(), spans=None, goal_dir=None, foreign=()):
    """spans: {"v": set(tiles), "h": set(tiles)} covered by existing or planned pipe-to-ground pairs (ends
    included). A new pair may not lie on, or run along, a span on the same axis: underground pipes pair
    with the nearest piece in line, so overlapping pairs would join the wrong pieces (and mix fluids)."""
    spans = spans or {"v": set(), "h": set()}
    """start/goal: integer tiles that touch the source and destination ports (the ports' target tiles).
    Returns placements [{"item": "pipe"|"pipe-to-ground", x, y, direction?, end?}] or None."""
    blocked = set(extra_blocked) - set(own)   # obstacles (machines, chests): no pipe on them
    foreign = set(foreign) - set(own)          # other networks' pipes and ports: no pipe on or beside them
    own = set(own)  # tiles of the same fluid network: touching them is fine, and a branch may start from any
    ends = {start, goal} | own

    def free(t):
        return (t in ends) or (t not in blocked and t not in foreign and g.free(*t))

    def foreign_next_to(t, came_from):
        for dx, dy in DIRS.values():
            n = (t[0] + dx, t[1] + dy)
            if n == came_from or n in ends:
                continue
            if g.at(*n) in "pP" or n in foreign:
                return True
        return False

    def h(t):
        return abs(t[0] - goal[0]) + abs(t[1] - goal[1])

    # state: (tile, last direction, arrived by pipe-to-ground exit?)
    sources = own if (start is None and own) else {start}
    best, came, q = {}, {}, []
    for src in sources:
        s0 = (src, None, False)
        best[s0] = 0
        q.append((h(src), 0, s0))
    heapq.heapify(q)
    while q:
        _, cost, st = heapq.heappop(q)
        if best.get(st, 1e18) < cost:
            continue
        t, d, is_exit = st
        if t == goal:
            return _unwind(came, st)
        mine = _path_tiles(came, st)
        for nd, (dx, dy) in DIRS.items():
            if is_exit and nd != d:
                continue  # leave an exit straight on
            n = (t[0] + dx, t[1] + dy)
            if not free(n) or n in mine or foreign_next_to(n, t):
                continue
            # the new pipe must not touch our own path except the tile it comes from
            if any((n[0] + ex, n[1] + ey) in mine and (n[0] + ex, n[1] + ey) != t for ex, ey in DIRS.values()):
                continue
            c = g.at(*n)
            step = 1 + (2 if c == "t" else 0) + (0.3 if (d and nd != d) else 0)
            _push(q, best, came, st, (n, nd, False), cost + step, h(n), ("pipe", None))
            # pipe-to-ground: entrance on n, exit k tiles further on (only the ends need to be free)
            if not foreign_next_to(n, t):
                axis = "v" if dx == 0 else "h"
                if n in spans[axis]:
                    continue
                for k in range(2, PTG_GAP + 2):
                    e = (n[0] + dx * k, n[1] + dy * k)
                    if (n[0] + dx * (k - 1), n[1] + dy * (k - 1)) in spans[axis]:
                        break  # the line ahead is taken by another pair
                    if not free(e) or e in mine or e in spans[axis] or (e == goal and nd != goal_dir):
                        continue  # an exit on the goal joins the port only when it travels straight into it
                    _push(q, best, came, st, (e, nd, True), cost + 4 + k, h(e), ("ptg", n))
    return None


def _push(q, best, came, prev, st, cost, hh, how):
    if cost < best.get(st, 1e18):
        best[st] = cost
        came[st] = (prev, how)
        heapq.heappush(q, (cost + hh, cost, st))


def _path_tiles(came, st):
    tiles = set()
    while True:
        tiles.add(st[0])
        if st not in came:
            return tiles
        prev, how = came[st]
        if how[0] == "ptg":
            tiles.add(how[1])
        st = prev


def _unwind(came, st):
    out = []
    while st in came:
        prev, how = came[st]
        t, d, _ = st
        if how[0] == "ptg":
            out.append({"item": "pipe-to-ground", "x": t[0] + .5, "y": t[1] + .5, "direction": d, "end": "exit"})
            out.append({"item": "pipe-to-ground", "x": how[1][0] + .5, "y": how[1][1] + .5, "direction": d, "end": "entrance"})
        else:
            out.append({"item": "pipe", "x": t[0] + .5, "y": t[1] + .5})
        st = prev
    out.append({"item": "pipe", "x": st[0][0] + .5, "y": st[0][1] + .5, "source": True})
    out.reverse()
    return out
