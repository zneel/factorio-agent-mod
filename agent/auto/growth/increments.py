"""What an increment places: a group plus the transport from its built suppliers and on to the world's trunks."""
from auto.growth.core import ELECTRIC, WORLD_TRANSPORT, materialize


DEAD = set()   # groups whose drills found no ore left (dry or retired): standing, but supplying nothing


def supplied(plan, built):
    """Built pieces that something built actually feeds: built producers and the built transport downstream of
    them (items forward, fluids either way). A built belt stub that nothing feeds is no connection, and neither is
    a mine that has run dry."""
    f = plan.flow
    out = {i for i in built if not f.transport(i) and plan.p[i].get("grp") not in DEAD}
    fr = list(out)
    while fr:
        nx_ = []
        for i in fr:
            for j in list(f.feeds[i]) + list(f.fluid[i]):
                if j in built and j not in out:
                    out.add(j)
                    if f.transport(j):
                        nx_.append(j)
        fr = nx_
    return out


def link_paths(plan, built, g):
    """Unbuilt transport on the cheapest flow path from a built supplier of each item g needs to g (networkx
    Dijkstra over the plan's item and fluid links: entering a built piece costs ~0, an unbuilt one 1). The upstream
    walk stops at the first fed belt, so a branch side-loading into a working trunk (blue packs into the red/green
    lab belt) stays invisible to it; this walk starts at the suppliers."""
    import networkx as nx
    f = plan.flow
    G = getattr(plan, "_fg", None)
    if G is None:
        G = nx.DiGraph()
        for i in range(len(plan.p)):
            for j in list(f.feeds[i]) + list(f.fluid[i]):
                G.add_edge(i, j)
        plan._fg = G
    own = set(plan.groups[g])
    out = set()
    for it in plan.need.get(g, ()):
        rec = plan.gd["recipes"]

        def makes(i, h):                      # this machine (not just its group) makes it
            r = plan.p[i].get("recipe")
            if r in rec:
                return it in {x[0] for x in rec[r]["prod"]}
            return not r and it in plan.made.get(h, ())   # furnaces, drills, pumpjacks: the group's product
        src = {i for h in plan.deps.get(g, ()) if it in plan.made.get(h, ()) and h not in DEAD
               and all(i in built for i in plan.groups[h]) for i in plan.groups[h]
               if not f.transport(i) and makes(i, h)}
        if not src:
            continue

        def w(u, v, _d):
            if v == "T" or v in own or v in src:
                return 0.001
            if not f.transport(v):
                return None                   # paths run through transport only, not through other machines
            return 0.001 if v in built else 1.0
        # shortest path to any piece of g: a virtual sink "T" joined from g's pieces
        G.add_edges_from((i, "T") for i in own)
        try:
            ln, pth = nx.multi_source_dijkstra(G, src, target="T", weight=w)
        except nx.NetworkXNoPath:
            continue
        finally:
            G.remove_node("T")
        if ln >= 1:
            out |= {i for i in pth[1:-1] if i not in built and f.transport(i)}
    return out


def rear_belts(plan, idx, built):
    """idx plus the belt straight behind every belt of idx (or built) that takes a side-load: without its rear
    belt a side-loaded belt turns into a curve and the side input lands on both lanes (coal on the ore lane)."""
    f = plan.flow
    out = set(idx)
    for i in list(idx) + [j for j in built if j in idx or any(k in idx for k in f.fed_by[j])]:
        p = plan.p[i]
        if p["item"] != "transport-belt":
            continue
        d = p.get("direction", "north")
        behind = None
        sides = []
        for j in f.fed_by[i]:
            q = plan.p[j]
            if q["item"] in ("transport-belt", "underground-belt") and q.get("direction") == d:
                behind = j
            elif q["item"] in ("transport-belt", "underground-belt", "splitter"):
                sides.append(j)
        if sides and behind is not None and behind not in built:
            out.add(behind)
        # a T merge (two side inputs, one item per lane, nothing behind): with only one side standing the belt is a
        # curve and that side's item takes both lanes (pipes over the circuit lane into B5); the other side's last
        # belt keeps it a side-load
        if behind is None and len(sides) == 2 and any(j in out or j in built for j in sides):
            out |= {j for j in sides if j not in built}
    return out


def gaps(plan, built, limit=80):
    """Runs of unbuilt plan transport that a working producer's output runs into and that end at a built piece:
    a missing piece between two built stretches (an underground entrance a builder picked up, a failed placement).
    Returns the first gap's pieces."""
    f = plan.flow
    start = [i for i in built if not f.transport(i) and plan.p[i].get("grp") not in DEAD]
    seen, fr = set(start), list(start)
    while fr:
        i = fr.pop()
        for j in f.feeds[i]:
            if j in seen:
                continue
            seen.add(j)
            if j in built:
                if f.transport(j):
                    fr.append(j)
                continue
            if not f.transport(j):
                continue
            run, cur, ok = [j], j, False   # follow the unbuilt run forward until something built
            for _ in range(limit):
                nxt = [k for k in f.feeds[cur] if k not in run]
                if not nxt:
                    break
                k = nxt[0]
                if k in built:
                    ok = True
                    break
                if not f.transport(k):
                    break
                run.append(k)
                cur = k
            if ok:
                return run
    return []


def increment(plan, live, g, researched, pole_item):
    """Placements for group g plus the transport it needs (see module doc). Returns (places, groups covered)."""
    built = supplied(plan, live.built)
    if g == "restore":
        idx = [i for i in live.state_gone if i not in live.built]
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)] +             [dict(p) for p in getattr(live, "gone_extra", [])], [g], idx
    if g.startswith("repower:extra@"):   # a machine of our own: only its poles
        _, n_, x_, y_ = g[8:].split("@")
        return [{"item": n_, "x": float(x_), "y": float(y_), "upgrade": True}], [g], []
    if g.startswith("repower:"):     # the group's own machines, already standing: only poles get placed
        idx = [i for i in plan.groups[g[8:]] if any(k in plan.p[i]["item"] for k in ELECTRIC)]
        return [dict(q, upgrade=True) for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g == "reconnect":
        return [dict(p, upgrade=True) for p in live.reconnect], [g], []
    if g == "gap":
        idx = gaps(plan, live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g == "rears":
        bb = {i for i in live.built if plan.p[i]["item"] == "transport-belt"}
        idx = sorted(rear_belts(plan, bb, live.built) - live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g.startswith("upgrade:"):   # stand-ins replaced by the real thing now that it can be made
        idx = sorted(live.standin)
        return [dict(q, upgrade=True) for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g.startswith("finish:"):  # a half-built transport group whose machines all stand: the rest of it
        h = g[7:]
        idx = sorted(rear_belts(plan, {i for i in plan.groups[h] if i not in live.built and i not in live.dead},
                                live.built) - live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g.startswith("link:"):    # a built group's missing connection to suppliers that now stand
        h = g[5:]
        own = set(plan.groups[h])
        idx = sorted(rear_belts(plan, plan.flow.upstream(own, built - own)[0] | link_paths(plan, live.built, h),
                                live.built) - live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    groups = [g]
    if g.startswith("power:") and not live.group_done("power"):
        groups.append("power")
    start = set()
    for h in groups:
        start |= set(plan.groups[h])
    up = set()
    for h in groups:     # each group's inputs back to what stands or comes with this increment
        own = set(plan.groups[h])
        up |= plan.flow.upstream(own, (built | start) - own)[0]
    walk = {h for h in plan.groups if WORLD_TRANSPORT.match(h or "")}
    down = plan.flow.downstream(set(plan.groups[g]), built, walk)
    idx = sorted(rear_belts(plan, (start | up | down), live.built) - live.built)
    places = [q for i in idx for q in materialize(plan.p[i], researched, pole_item)]
    return places, groups, idx
