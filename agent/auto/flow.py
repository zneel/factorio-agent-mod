"""Entity-level flow graph of a plan: which placement feeds which, for items (belts, inserters, drills) and fluids.

    g = Flow(places)          places: plan placements (complex + world), each with a "grp" tag
    g.feeds[i]                set of placement indices item i hands items to (belt fronts, inserter drops, drills)
    g.fed_by[j]               the reverse
    g.fluid[i]                placements sharing a fluid connection with i (pipes, pipe-to-ground pairs, fluid boxes)
    g.transport(i)            belts, undergrounds, splitters, inserters, pipes: they carry, they don't make

The growth controller (auto.grow) uses it to cut an increment to exactly the transport it needs: the paths from a
group's inputs back to built producers, and from its outputs on to what already stands.
"""
import math
from collections import deque

from auto import geom

DV = geom.DV4
TRANSPORT = ("transport-belt", "underground-belt", "splitter", "inserter", "pipe", "pipe-to-ground", "electric-pole")
BELTISH = ("transport-belt", "underground-belt", "splitter")


def T(x, y):
    return (math.floor(x), math.floor(y))


def is_transport(p):
    return any(k in p["item"] for k in TRANSPORT) or p["item"] == "pump"   # a pump carries (not offshore pumps)


class Flow:
    def __init__(self, places, ports=None):
        """ports(entity, recipe, direction) -> [(fluid, io, dx, dy)] (auto.ports.ports); None: no machine fluid boxes."""
        self.p = places
        self.at = {}
        for i, q in enumerate(places):
            if q.get("remove"):
                continue
            for t in geom.footprint(q):
                self.at[t] = i
        n = len(places)
        self.feeds = [set() for _ in range(n)]
        self.fed_by = [set() for _ in range(n)]
        self.fluid = [set() for _ in range(n)]
        self.fio = {}      # (machine, neighbour) -> the machine's port flow there: input / output / input-output
        self._items()
        self._fluids(ports)

    def _link(self, a, b):
        if a is not None and b is not None and a != b:
            self.feeds[a].add(b)
            self.fed_by[b].add(a)

    def _items(self):
        ug_out = {}
        for i, q in enumerate(self.p):
            if q["item"] == "underground-belt" and q.get("belt_type") == "output":
                ug_out[T(q["x"], q["y"])] = i
        for i, q in enumerate(self.p):
            if q.get("remove"):
                continue
            it, d = q["item"], DV.get(q.get("direction", "north"))
            t = T(q["x"], q["y"])
            if it == "transport-belt" or (it == "underground-belt" and q.get("belt_type") == "output"):
                self._link(i, self.at.get((t[0] + d[0], t[1] + d[1])))
            elif it == "underground-belt":
                for k in range(1, 7):   # its exit: the first output piece along the direction
                    j = ug_out.get((t[0] + d[0] * k, t[1] + d[1] * k))
                    if j is not None and self.p[j].get("direction") == q.get("direction"):
                        self._link(i, j)
                        break
            elif it == "splitter":
                for s in geom.footprint(q):
                    self._link(i, self.at.get((s[0] + d[0], s[1] + d[1])))
            elif "inserter" in it:   # plan direction = drop side
                r = 2 if it == "long-handed-inserter" else 1
                src = self.at.get((t[0] - d[0] * r, t[1] - d[1] * r))
                dst = self.at.get((t[0] + d[0] * r, t[1] + d[1] * r))
                self._link(src, i)
                self._link(i, dst)
            elif it in ("electric-mining-drill", "burner-mining-drill"):
                w = geom.size(q)[0]
                ox, oy = q["x"] + d[0] * (w / 2 + .5), q["y"] + d[1] * (w / 2 + .5)
                self._link(i, self.at.get(T(ox, oy)))

    def _fluids(self, ports):
        find, pieces, sides = geom.pipe_components(self.p)
        idx = {T(q["x"], q["y"]): i for i, q in enumerate(self.p) if q["item"] in ("pipe", "pipe-to-ground")}
        for t, q in pieces.items():
            for d in sides(q):
                n = (t[0] + d[0], t[1] + d[1])
                if n in pieces and (-d[0], -d[1]) in sides(pieces[n]):
                    self.fluid[idx[t]].add(idx[n])
        ptg = [i for i, q in enumerate(self.p) if q["item"] == "pipe-to-ground" and not q.get("remove")]
        for a, b in zip(ptg[0::2], ptg[1::2]):
            self.fluid[a].add(b)
            self.fluid[b].add(a)
        for i, q in enumerate(self.p):   # a pump joins the pieces at its two ends (it splits fluid segments)
            if q["item"] != "pump" or q.get("remove"):
                continue
            d = DV[q.get("direction", "north")]
            fp = geom.footprint(q)
            rear = next(t for t in fp if (t[0] + d[0], t[1] + d[1]) in fp)
            for n, e in (((rear[0] + 2 * d[0], rear[1] + 2 * d[1]), (-d[0], -d[1])), ((rear[0] - d[0], rear[1] - d[1]), d)):
                if n in pieces and e in sides(pieces[n]):   # the piece there opens toward the pump
                    self.fluid[i].add(idx[n])
                    self.fluid[idx[n]].add(i)
        if ports is None:
            return
        for i, q in enumerate(self.p):
            if q.get("remove") or is_transport(q) or q["item"] in ("iron-chest", "wooden-chest"):
                continue
            try:
                pts = ports(q["item"], q.get("recipe"), q.get("direction", "north"))
            except Exception:
                pts = []
            mine = geom.footprint(q)
            for _f, io, dx, dy in pts:
                t = T(q["x"] + dx, q["y"] + dy)
                j = self.at.get(t)
                if j is None or j == i:
                    continue
                o = self.p[j]
                if o["item"] in ("pipe", "pipe-to-ground"):
                    if geom.port_joined(pieces, sides, t, mine):
                        self.fluid[i].add(j)
                        self.fluid[j].add(i)
                        self.fio[(i, j)] = io
                else:   # machine to machine (boiler -> engine, engine -> engine): both ports face each other
                    if is_transport(o) or o.get("remove"):
                        continue          # a belt beside a steam port carries no steam
                    try:
                        back = ports(o["item"], o.get("recipe"), o.get("direction", "north")) or []
                    except Exception:
                        back = []
                    if not any(T(o["x"] + bx, o["y"] + by) in mine for _g, _io, bx, by in back):
                        continue
                    self.fluid[i].add(j)
                    self.fluid[j].add(i)
                    self.fio[(i, j)] = io

    def transport(self, i):
        return is_transport(self.p[i])

    # ---- closures used by the growth controller
    def upstream(self, start, built):
        """Reverse BFS from the placement set `start` over item links and fluid connections. Transport placements
        are walked; a branch ends at a built placement or at a producer: a non-transport placement handing items in,
        or one whose fluid port there is an output (a fluid consumer on the same pipe is no dependency).
        Returns (paths, producers): the transport placements on the paths back to built ends (pieces of groups
        built in the same increment count as built), and every producer reached (built or not)."""
        start = set(start)
        parent = {i: None for i in start}
        dq = deque(start)
        ends, prods = set(), set()
        while dq:
            i = dq.popleft()
            for j in list(self.fed_by[i]) + [j for j in self.fluid[i] if j not in self.fed_by[i]]:
                if j in parent:
                    continue
                via_item = j in self.fed_by[i]
                parent[j] = i
                if not self.transport(j):
                    if via_item or self.fio.get((j, i)) == "output":
                        prods.add(j)
                        ends.add(j)
                    elif j in built:
                        ends.add(j)
                    continue
                if j in built:
                    ends.add(j)
                    continue
                dq.append(j)
        paths = set()
        for e in ends:
            if e not in built:          # an unbuilt producer is no connection (the group waits for it instead)
                continue
            j = parent[e]
            while j is not None and j not in start:
                paths.add(j)
                j = parent[j]
        return paths, prods

    def downstream(self, start, built, walk_groups):
        """Forward BFS from start through transport placements of walk_groups (or of start's own groups) until a
        producer/consumer or a built piece. Returns the transport placements on those paths."""
        start = set(start)
        own = {self.p[i].get("grp") for i in start}
        parent = {i: None for i in start}
        dq = deque(start)
        ends = set()
        while dq:
            i = dq.popleft()
            for j in self.feeds[i]:
                if j in parent:
                    continue
                parent[j] = i
                q = self.p[j]
                if j in built or not self.transport(j):
                    ends.add(j)
                    continue
                if q.get("grp") not in walk_groups and q.get("grp") not in own:
                    ends.add(j)       # stop at the edge of the walked groups (the bus is built from its readers)
                    continue
                dq.append(j)
                ends.add(j)           # a dangling end is fine: the belt just fills up
        paths = set()
        for e in ends:
            j = e if (self.transport(e) and e not in built and self.p[e].get("grp") in walk_groups | own) else parent[e]
            while j is not None and j not in start:
                paths.add(j)
                j = parent[j]
        return paths
