"""Main bus (cheat sheet: group belts in a main bus) in local coordinates; the bus flows south from row 0.

Column slot s: belt at x = 2s, gap column at x = 2s + 1. An item's column runs from the row it joins (bus head for raw
items, the producer's output band for products) to its last tap; two items whose rows don't meet may share a slot.
Slots are ordered so that few columns stand between a module and the items its bands tap or export (order_columns).
Modules stack west and east of the bus (modules with fluids on the fluid side, so no pipe crosses the bus), each
STRIDE rows per band; a west and an east band may share rows when the west band's items all lie west of the east
band's (pack: list scheduling in dependency order, then sides flipped while the area shrinks). Each module sits its
own zone (ZONES) away from the bus edge. Bands are 4 rows where belts cross the bus toward a module. Only the live
columns a band's belts cross (between the band's farthest item and the module's side) need a way past; every other
column stays on the surface.
A crossed column either dives under the band (entrance on the row above, exit on the row below; it frees the column
on every band row) or a single belt hops it with a yellow underground pair (solve_band picks the fewest pieces):
  input band (one per bundle)  a splitter two rows above sends half of each tapped belt down the gap column on the
               module's side of its column (the whole belt at the item's last tap: the column ends there unless it is
               a mall item); the two branches turn toward the module on the band's first and third rows and
               side-load a stub on the middle row at the bus edge: the left item from the stub's left side, the right
               item from its right side (a one-item stub gets a rear belt so it stays straight). One merged belt
               leaves the band.
  output band  each product's belt comes from the module on its band row, turns down its own column and becomes
               that item's bus belt below the band
With 1-tile gaps a belt can't chain hops across live columns (an exit and the next entrance need two free tiles), so
hops pay off only between dived or ended columns, and only where one row crosses.
The connector zone between bus and module then needs only one route per bundle (stub -> block port) and one per
export (block port -> output band), plus pipes on the fluid side.
"""
import math
import random

from auto import geom

ZONE = 8       # connector zone between the bus edge and a module, in tiles
ZONES = {}     # module -> its own wider zone (plan2's repair loop widens the modules whose routes fail)
BAND = 4
STRIDE = BAND + 4   # rows from a band to the next one it can't share rows with (splitter y-2 .. dive exit y+4, +1)
SPACING = 3
MALL = set()   # mall items (plan2 sets it): a mall item riding the bus ends in a chest below its last tap
PRIO = {}      # module -> bus order priority (plan2 sets it; order_modules sorts by it, pack keeps it)
ORDER_SEEDS = 6   # annealing restarts for the column order
PACK_ROUNDS = 4   # pack bands with a column order, re-order the columns for that packing, pack again ...
PACK_TRIES = 24   # packings per round: the first plain, the others with jittered priorities (the best is kept)
JITTER = 12       # rows of priority noise per module in a jittered packing
WINDOW = 2        # the scheduler picks among the next WINDOW modules of the dependency order: taps nearer the bus
                  # head get scarce items first, so modules keep about their order's priority
PAIR_COST = 40    # annealing penalty for two row-sharing bands whose columns would meet
REUSE = True      # items whose rows don't meet may share a bus column
SLOT_COST = 3     # annealing cost of one more bus column (crossings cost 1 each)
PIECE_AREA = 50   # tiles of complex area one more underground piece in the bus is worth (choosing the order)


def order_modules(mods, inner, raw, prio=None):
    """Dependency order: each module after the producers of its inputs; of the ready ones, the lowest prio[n]
    first (then the given order)."""
    prod = {}
    for n, it in inner.items():
        for p in it["ports"]:
            if p["kind"] == "out":
                prod.setdefault(p["item"], set()).add(n)
    need = {n: {i for p in it["ports"] if p["kind"] == "bundle" for i in (p.get("left"), p.get("right")) if i}
            for n, it in inner.items()}
    done, out = set(raw), []
    left = list(mods)
    while left:
        ready = [n for n in left if all(i in done or not (prod.get(i, set()) - {n}) for i in need[n])]
        if not ready:
            raise RuntimeError(f"bus: cycle among {left}")
        n = min(ready, key=lambda m: (prio or {}).get(m, 0))   # min keeps the first of equals
        out.append(n)
        left.remove(n)
        done |= {p["item"] for p in inner[n]["ports"] if p["kind"] == "out"}
    return out


def module_bands(it):
    """A module's bundles and its bands [(kind, what, items)]: input bands in the order of their ports (so their
    connectors don't cross), output bands (<= BAND products each) after them; above them a module's products would
    ride the bus through its own input bands and need more crossings."""
    bundles = [p for p in it["ports"] if p["kind"] == "bundle"]
    port_y = {}   # export item -> y of its first port (the one routed to the band; later ones side-load onto it)
    for p in it["ports"]:
        if p["kind"] == "out":
            port_y.setdefault(p["item"], p["y"])
    outs = sorted(port_y, key=port_y.get)
    groups = [outs[k:k + BAND] for k in range(0, len(outs), BAND)]
    mine = [(b["y"], "in", bi, [i for i in (b.get("left"), b.get("right")) if i]) for bi, b in enumerate(bundles)] + \
           [(sum(port_y[i] for i in g) / len(g), "out", g, g) for g in groups]
    mine.sort(key=lambda m: (m[1] == "out", m[0]))
    return bundles, [(kind, what, its) for _, kind, what, its in mine]


def compatible(a, b, col):
    """May bands a and b (other sides) share rows? Every tile a west band uses (splitters, branches, rows, dives,
    output columns) lies at or west of its farthest item's column, every tile of an east band at or east of its
    nearest item's column: they can't meet if the west band's items all lie west of the east band's."""
    if col is None:
        return False
    w, e = (a, b) if a["side"] == "west" else (b, a)
    return max(col[i] for i in w["items"]) < min(col[i] for i in e["items"])


def has_fluid(it):
    """Fluid ports or networks (oil units): the module goes on the fluid side, next to the headers; no pipe crosses
    the bus."""
    return any(p["kind"] == "fluid" for p in it["ports"]) or bool(it.get("networks"))


def pack(inner, order, fluid_side, col, sides=None, jitter=None):
    """Sides, mirroring, band rows and module tops for a column order (None: no two bands share rows).
    Each side stacks its modules; a module's bands go to the first rows below the last banded module of its side
    (beside an oil unit's body: it has no bands; the module's body comes below it) where no band of its own side
    is within STRIDE rows, every input item is on the bus already (its output band STRIDE rows up), and every band
    of the other side within STRIDE rows is compatible. List scheduling: of the modules whose inputs are all made
    by placed modules (among the next WINDOW of the order with the next one's PRIO; oil units any time), place the
    one whose bands start highest (the longest chain of bands below it first; oil units before all; jitter:
    module -> rows added to its start for this choice); a module without fluids takes sides[n], else either side.
    Returns (used interiors, {module: (side, top)}, bands, length)."""
    used, pos, bands = {}, {}, []
    y_body = {"west": 6, "east": 6}   # first row of the next module body on each side
    y_conn = {"west": 6, "east": 6}   # first row of the next module's bands (and connectors) on each side
    # item -> first row a band may tap it: its output band's row + STRIDE; a raw item's below its taps placed
    # before (they run down the bus in the order's priority: the first gets the trunk's plates first)
    ready = {}
    prod = {}
    for n in order:
        for p in inner[n]["ports"]:
            if p["kind"] == "out":
                prod.setdefault(p["item"], set()).add(n)
    need = {n: {i for p in inner[n]["ports"] if p["kind"] == "bundle" for i in (p.get("left"), p.get("right")) if i}
            for n in order}
    after = {n: {m for i in need[n] for m in prod.get(i, ()) if m != n} for n in order}   # modules n waits for
    chain = {}

    def depth(n):   # rows of bands from n's first band to the end of the longest chain of consumers below it
        if n not in chain:
            chain[n] = 0
            nb = len(module_bands(inner[n])[1])
            chain[n] = STRIDE * nb + max([depth(m) for m in order if n in after[m]], default=0)
        return chain[n]

    def fits(y, side, its):
        nb = {"side": side, "items": its}
        return all(abs(b["y"] - y) >= STRIDE or (b["side"] != side and compatible(b, nb, col)) for b in bands)

    def trial(n, side):
        it0 = inner[n]
        it = it0
        if not has_fluid(it0) and facing(mirror(it0), side) > facing(it0, side):
            it = mirror(it0)
        bundles, mine = module_bands(it)
        rows, y = [], y_conn[side]
        for kind, what, its in mine:
            if kind == "in":
                y = max([y] + [ready.get(i, 0) for i in its])
            while not fits(y, side, its):
                y += 1
            rows.append(y)
            y += STRIDE
        y0 = max(y_body[side], rows[0] - 2) if rows else y_body[side]
        h = max(y0 + it["h"], (rows[-1] + BAND) if rows else 0) - y0
        # oil units first: their networks link to the header tops (inlets), so they stay at the top
        start = min([y0] + rows[:1]) + (jitter or {}).get(n, 0)
        return (not it0.get("networks"), start, -depth(n), y0 + h, side != fluid_side), \
            (n, side, it, bundles, mine, rows, y0, h)
    left = list(order)
    while left:
        head = PRIO.get(left[0], 0)   # the window never lets a module jump ahead of one with a higher priority
        cands = [n for n in left if (n in left[:WINDOW] and PRIO.get(n, 0) == head or inner[n].get("networks"))
                 and not (after[n] - set(used))]
        if not cands:
            raise RuntimeError(f"bus: cycle among {left}")
        best = min((trial(n, side) for n in cands
                    for side in ((fluid_side,) if has_fluid(inner[n]) else (sides[n],) if sides else ("west", "east"))),
                   key=lambda t: t[0])
        n, side, it, bundles, mine, rows, y0, h = best[1]
        left.remove(n)
        used[n] = it
        pos[n] = (side, y0)
        y_body[side] = y0 + it["h"] + SPACING
        if rows:
            y_body[side] = y_conn[side] = y0 + h + SPACING
        for r, (kind, what, its) in zip(rows, mine):
            if kind == "in":
                b = bundles[what]
                bands.append({"module": n, "kind": "in", "y": r, "side": side, "bundle": what,
                              "left": b.get("left"), "right": b.get("right"), "items": its})
                for i in its:
                    if i not in prod:   # raw: the trunk's plates are the scarce ones
                        ready[i] = max(ready.get(i, 0), r + 1)
            else:
                bands.append({"module": n, "kind": "out", "y": r, "items": what, "side": side})
                for i in what:
                    ready[i] = max(ready.get(i, 0), r + STRIDE)
    length = max(list(y_body.values()) + [max((b["y"] for b in bands), default=0) + BAND + 2])
    return used, pos, bands, length


def area(lay, fluid_side, BW, headers):
    """Area estimate of the complex: length x (west modules + bus + east modules + the header strip)."""
    used, pos, _, length = lay
    w = {s: max([ZONE + used[n]["w"] for n, (s_, _) in pos.items() if s_ == s], default=0) for s in ("west", "east")}
    w[fluid_side] += headers
    return length * (w["west"] + BW + w["east"])


def best_pack(inner, order, fluid_side, col, BW, headers, jitter=None):
    """pack, then flip modules without fluids to the other side while that shrinks the area estimate."""
    lay = pack(inner, order, fluid_side, col, jitter=jitter)
    sides = {n: s for n, (s, _) in lay[1].items()}
    cur = area(lay, fluid_side, BW, headers)
    free = [n for n in order if not has_fluid(inner[n])]
    improved = True
    while improved:
        improved = False
        for n in free:
            s2 = dict(sides, **{n: "east" if sides[n] == "west" else "west"})
            lay2 = pack(inner, order, fluid_side, col, s2, jitter)
            a2 = area(lay2, fluid_side, BW, headers)
            if a2 < cur:
                lay, sides, cur, improved = lay2, s2, a2, True
    return lay


def live_rows(items, raw_items, bands):
    """start (row an item joins the bus), end (its last tap's band row) and rows: item -> (first, last) row of its
    column belt (a mall item's chest is placed below its last tap: an estimate here, for the column order)."""
    start = {it: 0 for it in raw_items}
    end = {}
    for b in bands:
        if b["kind"] == "out":
            for it in b["items"]:
                start[it] = b["y"]
        else:
            for it in b["items"]:
                end[it] = max(end.get(it, 0), b["y"])
    rows = {}
    for it in items:
        if it in end:
            y0 = start[it] + BAND if start.get(it, 0) and it not in raw_items else 0
            rows[it] = (y0, end[it] + BAND + 2 if it in MALL else end[it] - 2)
    return start, end, rows


def spans_of(items, raw_items, start, end):
    """Rows an item's column holds anything of it, from its output band (2 rows above it) to its last tap's band
    (the band's rows and the dive exits below it); a mall item's column runs on to its chest: never shared."""
    big = 1 << 20
    out = {}
    for it in items:
        lo = -big if it in raw_items else start.get(it, 0) - 2
        hi = big if it in MALL else (end[it] if it in end else start.get(it, 0)) + BAND + 1
        out[it] = (lo, hi)
    return out


def shared_rows(bands):
    """Pairs of band indices (other sides) within STRIDE rows of each other: they must stay compatible."""
    return [(i, j) for i, a in enumerate(bands) for j, b in enumerate(bands)
            if i < j and a["side"] != b["side"] and abs(a["y"] - b["y"]) < STRIDE]


def bus_width(col):
    return max(col.values()) + 2


_PLANS = {}   # packing + column order per input (plan2's repair loop re-lays the same modules with other zones)


def plan(inner, order, raw_items, fluid_side="west"):
    """Sides, mirroring, band rows, module positions and the column order; returns the bus description (no places
    yet). Rounds: pack the bands with the last column order (none at first: no shared rows), anneal column orders
    for that packing that keep its row-sharing bands apart (two items whose rows don't meet may share a column);
    keep the packing and order with the smallest area estimate + PIECE_AREA per underground piece. Module x comes
    from its zone (ZONES, else ZONE): west modules end that far west of the bus, east ones start that far east."""
    import copy
    import json
    items = list(raw_items)
    for n in order:
        for p in inner[n]["ports"]:
            if p["kind"] == "out" and p["item"] not in items:
                items.append(p["item"])
    fluids = {p["fluid"] for it in inner.values() for p in it["ports"] if p["kind"] == "fluid"}
    fluids |= {f for it in inner.values() for f in (it.get("networks") or {})} | {"water", "crude-oil"}
    headers = 3 * len(fluids)
    key = json.dumps([order, list(raw_items), fluid_side, sorted(MALL), PRIO, STRIDE, SPACING, ZONE, REUSE, SLOT_COST,
                      [(inner[n]["w"], inner[n]["h"], inner[n]["ports"]) for n in order]], sort_keys=True, default=str)
    if key not in _PLANS:
        col, best = None, None
        for _ in range(PACK_ROUNDS):
            bw = bus_width(col) if col else 2 * len(items)
            lays = []
            for t in range(PACK_TRIES):
                rng = random.Random(t)
                jit = {n: rng.randint(0, JITTER) for n in order} if t else None
                lays.append(best_pack(inner, order, fluid_side, col, bw, headers, jit))
            lay = min(lays, key=lambda l: area(l, fluid_side, bw, headers))
            used, pos, bands, length = lay
            start, end, rows = live_rows(items, raw_items, bands)
            pairs = shared_rows(bands)
            spans = spans_of(items, raw_items, start, end) if REUSE else None
            cands = [order_columns(items, bands, rows, seed=s, pairs=pairs, spans=spans) for s in range(ORDER_SEEDS)]
            if col is not None:
                cands.append(col)   # the order this packing was made for keeps it valid
            score = [area(lay, fluid_side, bus_width(c), headers) +
                     PIECE_AREA * crossing_pieces(bands, c, bus_width(c), items, rows) for c in cands]
            ok = [k for k, c in enumerate(cands) if all(compatible(bands[i], bands[j], c) for i, j in pairs)]
            if ok:
                k = min(ok, key=lambda k: score[k])
                if best is None or score[k] < best[0]:
                    best = (score[k], used, pos, bands, length, cands[k])
            col = cands[min(range(len(cands)), key=lambda k: score[k])]
        _PLANS[key] = best[1:]
    used, pos, bands, length, col = copy.deepcopy(_PLANS[key])
    BW = bus_width(col)
    pos = {n: (-ZONES.get(n, ZONE) - used[n]["w"] if side == "west" else BW + ZONES.get(n, ZONE), y, side)
           for n, (side, y) in pos.items()}
    start, end, rows = live_rows(items, raw_items, bands)
    places = []
    for it in items:
        if it not in end:
            continue
        y0, y1 = rows[it][0], end[it] - 2   # a plain column ends in the splitter of its last tap
        if it in MALL:   # past its last tap to a row no band crosses this column at: inserter + chest
            yt = end[it]
            while any(crosses(b, col[it], col) and b["y"] - 1 <= yt <= b["y"] + BAND for b in bands if b["y"] >= end[it]):
                yt += 1
            y1 = yt - 1
            places.append({"item": "inserter", "x": col[it] + .5, "y": yt + .5, "direction": "south"})
            places.append({"item": "iron-chest", "x": col[it] + .5, "y": yt + 1.5, "mall": [it]})
            length = max(length, yt + 3)
        rows[it] = (y0, y1 if it not in MALL else y1 + 2)
    for b in bands:   # the live columns this band's belts cross: each dives under it or is hopped by the belts
        solve_band(b, col, BW, crossed(b, col, items, rows))
    for it in items:
        if it in end:
            _column(places, col[it], rows[it][0], rows[it][1] - (2 if it in MALL else 0), bands, it)
    return {"places": places, "modules": pos, "columns": col, "bands": bands, "BW": BW, "length": length,
            "items": items, "start": start, "end": end, "raw": list(raw_items), "inner": used}


def crosses(b, x, col):
    """Do band b's belts cross column x? They run from the band's farthest item to the module's side."""
    xs = [col[i] for i in b["items"]]
    return x < max(xs) if b["side"] == "west" else x > min(xs)


def crossed(b, col, items, rows):
    """Items whose column band b's belts cross while it is live there (rows: item -> (first, last) belt row)."""
    return [it for it in items if it in rows and crosses(b, col[it], col)
            and rows[it][0] <= b["y"] + BAND - 1 and rows[it][1] >= b["y"]]


def crossing_pieces(bands, col, BW, items, rows):
    """Underground pieces all bands need with this column order (dives + hops, from solve_band)."""
    n = 0
    for b in bands:
        b2 = dict(b)
        solve_band(b2, col, BW, crossed(b, col, items, rows))
        n += 2 * len(b2["cross"]) + 2 * sum(len(h) for _, _, h in b2["rows"].values())
    return n


def band_rows(b, col, BW, ext=False):
    """Belts of band b that cross the bus: [(item, row, tiles in flow order, blocked tiles)] and the input band's
    merge stub column. Input rows run from beside their branch's corner to the stub, past the other branch's
    vertical; output rows from the bus edge to beside their column. ext: one more tile before the stub (west)."""
    y, west = b["y"], b["side"] == "west"
    s = -1 if west else 1
    e0 = -1 if west else BW                  # the tile past the outermost gap on the module's side
    if b["kind"] == "out":
        its = sorted(b["items"], key=lambda i: col[i], reverse=west)   # farthest column on the first row
        return [(it, y + j, list(range(e0, col[it], -s)), set()) for j, it in enumerate(its)], None
    rr = {}                                  # left item from the stub's left side
    if b.get("left"):
        rr[b["left"]] = y + 2 if west else y   # flowing west the left side is south; flowing east it is north
    if b.get("right"):
        rr[b["right"]] = y if west else y + 2
    g = {it: col[it] + s for it in rr}       # each branch runs down the gap on the module's side of its column
    if e0 in g.values():                     # a branch down the edge tile: the stub (and its rear belt) moves out
        ext = True
    last = e0 + s if ext else e0
    return [(it, r, list(range(g[it] + s, last + s, s)), {g[o] for o in rr if o != it and rr[o] > r})
            for it, r in rr.items()], last + s


def hops(tiles, blocked):
    """Fewest yellow-underground pairs (1..4 tiles between entrance and exit) that carry a belt along tiles (flow
    order) past the blocked ones: [(entrance x, exit x)], or None. An exit and the next entrance need two tiles."""
    n = len(tiles)
    big = 1 << 30
    free = [x not in blocked for x in tiles]
    belt, ex, src = [big] * n, [big] * n, [0] * n

    def before(i):
        return 0 if i == 0 else min(belt[i - 1], ex[i - 1])
    for j in range(n):
        if not free[j]:
            continue
        belt[j] = before(j)
        for i in range(max(0, j - 5), j - 1):
            if free[i] and before(i) + 1 < ex[j]:
                ex[j], src[j] = before(i) + 1, i
    if not n:
        return []
    if min(belt[-1], ex[-1]) >= big:
        return None
    out, j = [], n - 1
    while j >= 0:
        if ex[j] < belt[j]:
            out.append((tiles[src[j]], tiles[j]))
            j = src[j] - 1
        else:
            j -= 1
    return out[::-1]


def solve_band(b, col, BW, cand):
    """Which crossed live columns dive under band b and where its belts hop the others, with the fewest
    underground pieces. A dive (2 pieces) frees its column on every band row, a hop (2 pieces) passes on one row,
    so only columns that a single row crosses are worth hopping; those subsets are searched exhaustively.
    Sets b["cross"] (diving items), b["rows"] {item: (row, tiles, hops)} and b["sx"] (input stub column)."""
    xs = {col[i] for i in cand}
    for ext in (False, True):
        rows, sx = band_rows(b, col, BW, ext)
        hit = {x: sum(x in t for _, _, t, _ in rows) for x in xs}
        single = sorted(x for x in xs if hit[x] == 1)
        if len(single) > 12:
            single = []
        best = None
        for m in range(1 << len(single)):   # m = 0: every crossed column dives
            hop = {single[k] for k in range(len(single)) if m >> k & 1}
            cost, hs = 2 * len(xs - hop), []
            for _, _, tiles, vert in rows:
                h = hops(tiles, vert | hop)
                if h is None:
                    break
                cost += 2 * len(h)
                hs.append(h)
            else:
                if best is None or cost < best[0]:
                    best = (cost, xs - hop, hs)
        if best:
            b["cross"] = [i for i in cand if col[i] in best[1]]
            b["rows"] = {it: (r, t, h) for (it, r, t, _), h in zip(rows, best[2])}
            b["sx"] = sx
            return
    raise RuntimeError(f"bus: {b['module']} {b['kind']} band at row {b['y']}: no way across")


def order_columns(items, bands, rows, iters=40000, seed=0, pairs=(), spans=None):
    """Column order (item -> x) with few band crossings: simulated annealing over permutations, minimising the
    number of (band, column) pairs where a live column stands between a band's farthest item and its module, plus
    PAIR_COST per pair of row-sharing bands (pairs: band indices, other sides) whose columns would meet.
    spans (item -> (first, last) row its column holds it): two items whose spans are 2+ rows apart may share a
    column, at SLOT_COST per column used."""
    import numpy as np
    n = len(items)
    idx = {it: i for i, it in enumerate(items)}
    live = np.array([[it in rows and rows[it][0] <= b["y"] + BAND - 1 and rows[it][1] >= b["y"] for it in items]
                     for b in bands], dtype=bool)
    mem = np.zeros((len(bands), n), dtype=bool)
    for k, b in enumerate(bands):
        for i in b["items"]:
            mem[k, idx[i]] = True
    west = np.array([b["side"] == "west" for b in bands])[:, None]
    pw = np.array([i if bands[i]["side"] == "west" else j for i, j in pairs], dtype=int)
    pe = np.array([j if bands[i]["side"] == "west" else i for i, j in pairs], dtype=int)
    if spans:
        lo = np.array([spans[it][0] for it in items])
        hi = np.array([spans[it][1] for it in items])
        clash = ~((hi[:, None] + 2 < lo[None, :]) | (hi[None, :] + 2 < lo[:, None]))   # rows within 2 of each other
    big = 1 << 20

    def cost(pos):   # pos[i] = column slot of item i
        far_w = np.where(mem, pos, -1).max(1)[:, None]
        far_e = np.where(mem, pos, big).min(1)[:, None]
        c = int((live & np.where(west, pos[None, :] < far_w, pos[None, :] > far_e)).sum())
        if len(pw):   # a shared-rows pair: the west band's items must all lie west of the east band's
            c += PAIR_COST * int((far_w[pw, 0] >= far_e[pe, 0]).sum())
        if spans:
            c += SLOT_COST * len(np.unique(pos))
        return c

    def fits(q, i):   # item i alone in its column, or sharing it only with items it doesn't meet
        same = q == q[i]
        same[i] = False
        return not (clash[i] & same).any()
    rng = random.Random(seed)
    pos = np.arange(n)
    cur = best = cost(pos)
    best_pos = pos.copy()
    for k in range(iters):
        t = 2.0 * (0.01 / 2.0) ** (k / iters)
        q = pos.copy()
        i, j = rng.sample(range(n), 2)
        mv = rng.random()
        if spans and mv < .25:  # item i joins item j's column
            q[i] = q[j]
            if not fits(q, i):
                continue
        elif mv < .5 + (.125 if spans else 0):   # swap two items
            q[i], q[j] = q[j], q[i]
            if spans and not (fits(q, i) and fits(q, j)):
                continue
        elif spans:             # item i to a column of its own just west of item j's
            q = 2 * q
            q[i] = 2 * pos[j] - 1
        else:                   # move item i to slot pos[j], shifting the ones between
            a, b_ = q[i], q[j]
            if a < b_:
                q[(q > a) & (q <= b_)] -= 1
            else:
                q[(q >= b_) & (q < a)] += 1
            q[i] = b_
        if spans:
            q = np.unique(q, return_inverse=True)[1].reshape(-1)   # columns 0 .. used - 1, order kept
        c = cost(q)
        if c <= cur or rng.random() < math.exp((cur - c) / t):
            pos, cur = q, c
            if c < best:
                best, best_pos = c, q.copy()
    return {it: 2 * int(best_pos[idx[it]]) for it in items}


def _column(places, x, y0, y1, bands, item):
    """Bus belt of one item from row y0 to y1 (south), diving under the bands that cross it; a tap's splitter
    reaches into the gap column on its module's side."""
    tops = sorted(b["y"] for b in bands if item in b["cross"])
    taps = {b["y"] - 2: b["side"] for b in bands if b["kind"] == "in" and item in b["items"]}
    y = y0
    while y <= y1:
        dive = next((t for t in tops if t - 1 == y), None)
        if dive is not None:
            places.append({"item": "underground-belt", "x": x + .5, "y": y + .5, "direction": "south", "belt_type": "input"})
            places.append({"item": "underground-belt", "x": x + .5, "y": dive + BAND + .5, "direction": "south",
                           "belt_type": "output"})
            y = dive + BAND + 1
            continue
        if y in taps:
            places.append({"item": "splitter", "x": x + (0.0 if taps[y] == "west" else 1.0), "y": y + .5,
                           "direction": "south"})
        else:
            places.append({"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": "south"})
        y += 1


def _belt(x, y, d):
    return {"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": d}


def _row(places, tiles, r, d, hs):
    """Belts along tiles (flow order) on row r, underground from each hop's entrance to its exit."""
    ent = dict(hs)
    k = 0
    while k < len(tiles):
        x = tiles[k]
        if x in ent:
            places.append({"item": "underground-belt", "x": x + .5, "y": r + .5, "direction": d, "belt_type": "input"})
            places.append({"item": "underground-belt", "x": ent[x] + .5, "y": r + .5, "direction": d,
                           "belt_type": "output"})
            k = tiles.index(ent[x]) + 1
            continue
        places.append(_belt(x, r, d))
        k += 1


def bands_places(bus):
    """Surface belts inside the bands (rows and hops from solve_band). Returns (places, edges):
    edges[(module, "in", bundle)] = (tile, dir) where the merged belt leaves the band;
    edges[(module, item, "out")] = (tile, dir) where an export must arrive."""
    col, BW = bus["columns"], bus["BW"]
    places, edges = [], {}
    for b in bus["bands"]:
        geom.tag(places, prev_band) if places else None
        prev_band = "band:" + b["module"]
        y, side = b["y"], b["side"]
        west = side == "west"
        d = "west" if west else "east"
        step_ = -1 if west else 1
        if b["kind"] == "in":
            sx = b["sx"]                         # merge stub column, just outside the bus edge
            for it, (r, tiles, hs) in b["rows"].items():
                g = col[it] + step_              # the branch runs down the gap on the module's side, turns at r
                for yy in range(y - 1, r):
                    places.append(_belt(g, yy, "south"))
                places.append(_belt(g, r, d))
                _row(places, tiles, r, d, hs)
                places.append(_belt(sx, r, "south" if r < y + 1 else "north"))
            places.append(_belt(sx, y + 1, d))
            if len(b["rows"]) == 1:
                places.append(_belt(sx - step_, y + 1, d))   # rear belt keeps a one-input stub straight
            edges[(b["module"], "in", b["bundle"])] = ((sx + step_, y + 1), d)
        else:
            edge_x = -1 if west else BW
            dd = "east" if west else "west"
            for it, (r, tiles, hs) in b["rows"].items():
                _row(places, tiles, r, dd, hs)
                for yy in range(r, y + BAND):
                    places.append(_belt(col[it], yy, "south"))
                edges[(b["module"], it, "out")] = ((edge_x - 1 if west else edge_x + 1, r), dd)
    if places:
        geom.tag(places, prev_band)
    return places, edges


class Grid:
    """Occupancy for local routing (plan_nx Grid interface)."""

    def __init__(self, box, blocked, pipes=None):
        self.x1, self.y1, self.x2, self.y2 = box
        self.blocked = blocked
        self.pipes = pipes or {}

    def at(self, x, y):
        if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2):
            return "#"
        if (x, y) in self.pipes:
            return "p"
        return "#" if (x, y) in self.blocked else "."

    def free(self, x, y, ore_ok=True):
        return self.at(x, y) == "."


DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
LEFTOF = {"north": "west", "south": "east", "east": "north", "west": "south"}
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}


MIRROR = {"east": "west", "west": "east", "north": "north", "south": "south"}


def mirror(it):
    """Mirror a module interior left-right (x -> w - x). Directions east<->west; a bundle's left and right lanes swap
    (the same physical lanes, seen from the other direction)."""
    w = it["w"]
    pl = [dict(p, x=w - p["x"], direction=MIRROR[p["direction"]]) if "direction" in p else dict(p, x=w - p["x"])
          for p in it["places"]]
    ports = []
    for p in it["ports"]:
        q = dict(p, x=w - p["x"])
        if "dir" in q:
            q["dir"] = MIRROR[q["dir"]]
        if q["kind"] == "bundle":
            q["left"], q["right"] = p.get("right"), p.get("left")
        ports.append(q)
    return dict(it, places=pl, ports=ports)


def facing(it, side):
    """How many belt ports point toward the bus (east-side modules: west-flowing in / out ports on the west)."""
    want = "west" if side == "east" else "east"
    n = 0
    for p in it["ports"]:
        if p["kind"] == "bundle":
            n += p["dir"] == ("east" if side == "east" else "west")   # entry flows into the block from the bus side
        elif p["kind"] == "out":
            n += p["dir"] == want
    return n




def complex_layout(inner, order, raw_items, fluid_side="west"):
    """Bus + bands + modules + connector routes in local coordinates."""
    import plan_nx
    bus = plan(inner, order, raw_items, fluid_side)
    used = bus["inner"]
    places = geom.tag(list(bus["places"]), "bus")
    bp, edges = bands_places(bus)
    places += bp
    mods = {}
    for n, (x, y, side) in bus["modules"].items():
        it = used[n]
        pl = geom.tag(geom.translate(it["places"], x, y), n)
        ports = [dict(p, x=p["x"] + x, y=p["y"] + y) for p in it["ports"]]
        mods[n] = {"places": pl, "ports": ports, "side": side, "x": x, "y": y, "w": it["w"], "h": it["h"],
                   "networks": {f: [(a + x, b + y) for a, b in ts] for f, ts in (it.get("networks") or {}).items()}}
        places += pl
    blocked = geom.tiles(places)
    spans = {"v": set(), "h": set()}
    fails = []

    def add_spans(pl):
        for i_, q in enumerate(pl):
            if q["item"] == "underground-belt" and q.get("belt_type") == "input":
                for r_ in pl[i_ + 1:i_ + 3]:
                    if r_["item"] == "underground-belt" and r_.get("belt_type") == "output":
                        a, b = (math.floor(q["x"]), math.floor(q["y"])), (math.floor(r_["x"]), math.floor(r_["y"]))
                        if a[0] == b[0]:
                            spans["v"].update((a[0], yy) for yy in range(min(a[1], b[1]), max(a[1], b[1]) + 1))
                        else:
                            spans["h"].update((xx, a[1]) for xx in range(min(a[0], b[0]), max(a[0], b[0]) + 1))
                        break
    add_spans(places)
    reserved = set()
    for n, mo in mods.items():
        for p in mo["ports"]:
            t = (math.floor(p["x"]), math.floor(p["y"]))
            reserved.add(t)
            if p["kind"] == "out":
                reserved.add((t[0] + DV[p["dir"]][0], t[1] + DV[p["dir"]][1]))
    for (t, d) in edges.values():
        reserved.add(t)
    reserved |= geom.belt_fronts(places) - blocked   # a route there would catch what that belt end drops
    xs = [math.floor(p["x"]) for p in places]
    ys = [math.floor(p["y"]) for p in places]
    box = (min(xs) - 6, min(ys) - 6, max(xs) + 7, max(ys) + 7)

    def route(start, goal, fd, td, mine):
        for m in (6, 16, 40):
            wb = (max(box[0], min(start[0], goal[0]) - m), max(box[1], min(start[1], goal[1]) - m),
                  min(box[2], max(start[0], goal[0]) + m + 1), min(box[3], max(start[1], goal[1]) + m + 1))
            g = Grid(wb, blocked | (reserved - set(mine)))
            for _ in range(6):
                r = plan_nx.route_belt(g, start, goal, fd, td, spans=spans)
                if not r:
                    break
                tiles = [(math.floor(q["x"]), math.floor(q["y"])) for q in r]
                if len(set(tiles)) == len(tiles):
                    return r
                dup = next(t for t in tiles if tiles.count(t) > 1)
                g.blocked = g.blocked | {dup}
        return None

    cur = "conn"

    def commit(pl):
        geom.tag(pl, cur)
        add_spans(pl)
        places.extend(pl)
        blocked.update(geom.tiles(pl))

    def free(t):
        return box[0] <= t[0] < box[2] and box[1] <= t[1] < box[3] and t not in blocked and t not in reserved

    for n, mo in mods.items():
        cur = "conn:" + n
        first_out = {}
        for p in mo["ports"]:
            if p["kind"] != "out":
                continue
            t0 = (math.floor(p["x"]), math.floor(p["y"]))
            e = edges.get((n, p["item"], "out"))
            if e and p["item"] not in first_out:
                r = route(t0, e[0], p["dir"], e[1], {t0, e[0]})
                if r is None:
                    fails.append(f"{n} out {p['item']}")
                    continue
                commit(r)
                first_out[p["item"]] = r
                continue
            trunk = first_out.get(p["item"])
            if not trunk:
                continue
            ok = False   # a second export belt of the same item side-loads onto the first one's route
            for q in sorted([q for q in trunk[1:-1] if q["item"] == "transport-belt"],
                            key=lambda q: abs(q["x"] - t0[0]) + abs(q["y"] - t0[1])):
                d0 = q["direction"]
                for sd in (LEFTOF[d0], OPP[LEFTOF[d0]]):
                    tt = (math.floor(q["x"]) + DV[sd][0], math.floor(q["y"]) + DV[sd][1])
                    if tt == t0:
                        r = [{"item": "transport-belt", "x": t0[0] + .5, "y": t0[1] + .5, "direction": OPP[sd]}] \
                            if p["dir"] == OPP[sd] else None
                    elif free(tt):
                        r = route(t0, tt, p["dir"], OPP[sd], {t0, tt})
                    else:
                        r = None
                    if r:
                        commit(r)
                        ok = True
                        break
                if ok:
                    break
            if not ok:
                fails.append(f"{n} second export {p['item']}")
        bundles = [p for p in mo["ports"] if p["kind"] == "bundle"]
        for bi, p in enumerate(bundles):
            port = (math.floor(p["x"]), math.floor(p["y"]))
            e = edges.get((n, "in", bi))
            if not e:
                fails.append(f"{n} bundle {bi}: no band")
                continue
            r = route(e[0], port, e[1], p["dir"], {e[0], port})
            if r is None:
                fails.append(f"{n} bundle {bi} {p.get('left')}/{p.get('right')}")
                continue
            commit(r)
    head = {it: (bus["columns"][it], -1) for it in bus["raw"]}
    fluid_ports = [dict(p, module=n) for n, mo in mods.items() for p in mo["ports"] if p["kind"] == "fluid"]
    networks = {n: mo["networks"] for n, mo in mods.items() if mo["networks"]}
    return {"places": places, "fails": fails, "head": head, "bus": bus, "fluid_ports": fluid_ports,
            "networks": networks, "box": box, "spans": spans}


def route_fluids(cx, fluid_side="west"):
    """Pipe trees inside the complex. Water and crude come in at inlet tiles on the fluid side's outer edge."""
    import plan_nx
    places = cx["places"]
    blocked = geom.tiles(places)
    pipes = {}      # tile -> fluid, for foreign checks (pipes and fluid ports)
    real = set()    # tiles that actually hold a pipe entity
    for n, nets in cx["networks"].items():
        for f, ts in nets.items():
            for t in ts:
                pipes[tuple(t)] = f
                if len(ts) > 1:
                    real.add(tuple(t))
    ports = cx["fluid_ports"]
    for p in ports:
        pipes.setdefault((math.floor(p["x"]), math.floor(p["y"])), p["fluid"])  # port tiles: foreign to others
    box = cx["box"]
    xs = [p["x"] for p in ports] + [t[0] for nets in cx["networks"].values() for ts in nets.values() for t in ts]
    edge_x = (min(xs) - 4) if fluid_side == "west" else (max(xs) + 4)
    oil_y = min(t[1] for nets in cx["networks"].values() for ts in nets.values() for t in ts) if cx["networks"] else 0
    inlets = {"water": (int(edge_x), int(oil_y) + 2), "crude-oil": (int(edge_x), int(oil_y) + 6)}
    fails, out = [], []
    src = {}
    for n, nets in cx["networks"].items():
        for f, ts in nets.items():
            if f in ("petroleum-gas", "light-oil", "heavy-oil"):
                src.setdefault(f, set()).update(tuple(t) for t in ts)
    for p in ports:
        if p["io"] == "output":
            src.setdefault(p["fluid"], set()).add((math.floor(p["x"]), math.floor(p["y"])))
    for f, t in inlets.items():
        src.setdefault(f, set()).add(t)
        pipes[t] = f
        real.add(t)
        out.append({"item": "pipe", "x": t[0] + .5, "y": t[1] + .5})
    sinks = [p for p in ports if "input" in p["io"]]
    for n, nets in cx["networks"].items():   # oil units take water and crude
        for f in ("water", "crude-oil"):
            if nets.get(f):
                sinks.append({"fluid": f, "x": nets[f][0][0] + .5, "y": nets[f][0][1] + .5, "module": n, "io": "input"})
    by_machine = set()
    pspans = {"v": set(), "h": set()}
    for n_, it_ in cx["networks"].items():
        pass
    geom.pipe_spans([q for q in places if q["item"] == "pipe-to-ground"], pspans)
    for p in sorted(sinks, key=lambda p: (p["fluid"] != "crude-oil", p["fluid"] != "water")):
        f = p["fluid"]
        t = (math.floor(p["x"]), math.floor(p["y"]))
        key = (p["module"], round(p["x"] / 3), round(p["y"] / 3), f)   # one port per machine is enough
        net = src.get(f, set())
        if not net:
            fails.append(f"no source for {f}")
            continue
        if t in net or key in by_machine:
            continue
        if pipes.get(t) == f and t in blocked and p.get("module", "").startswith("oil"):
            # an oil unit's own network tile: aim for a free tile beside that network instead
            mnet = [tuple(q) for q in cx["networks"][p["module"]].get(f, [])]
            cands = [(a + ddx, b + ddy) for a, b in mnet for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            cands = [c for c in cands if c not in blocked and c not in pipes and
                     not any(pipes.get((c[0] + ex, c[1] + ey), f) != f for ex, ey in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
            if not cands:
                fails.append(f"no free tile beside {p['module']} {f} network")
                continue
            t = min(cands, key=lambda c: min(abs(c[0] - s_[0]) + abs(c[1] - s_[1]) for s_ in net))
        foreign = {q for q, g in pipes.items() if g != f}
        own = {q for q in real if pipes.get(q) == f} | net
        r = None
        near = min(net, key=lambda s: abs(s[0] - t[0]) + abs(s[1] - t[1]))
        for m in (8, 20, 45):
            wb = (min(near[0], t[0]) - m, min(near[1], t[1]) - m, max(near[0], t[0]) + m + 1, max(near[1], t[1]) + m + 1)
            g = Grid(wb, blocked - own - {t}, {q: g_ for q, g_ in pipes.items() if g_ != f})
            starts = {s for s in net if wb[0] <= s[0] < wb[2] and wb[1] <= s[1] < wb[3]}
            r = plan_nx.route_pipe(g, starts, t, foreign=foreign - {t}, own=own, spans=pspans, tries=30)
            if r:
                break
        if not r:
            fails.append(f"pipe {f} -> {p['module']}")
            continue
        r = [{k: v for k, v in q.items() if k in ("item", "x", "y", "direction")} for q in r
             if not ((math.floor(q["x"]), math.floor(q["y"])) in real and q["item"] == "pipe")]
        out += r
        real.update((math.floor(q["x"]), math.floor(q["y"])) for q in r)
        geom.pipe_spans(r, pspans)
        for q in r:
            tt = (math.floor(q["x"]), math.floor(q["y"]))
            blocked.add(tt)
            pipes[tt] = f
            net.add(tt)
        net.add(t)
        by_machine.add(key)
    cx["places"] += geom.tag(out, "fluid")
    cx["inlets"] = inlets
    cx["pipe_fluids"] = {t: f for t, f in pipes.items() if t in real}
    cx["fails"] += fails
    return out


def route_fluids_bus(cx, fluid_side="west"):
    """Fluid bus: one vertical header per fluid west of every fluid-side module (2 tiles apart); every fluid port
    gets a straight horizontal spur to its header, hopping other headers and obstacles with pipe-to-ground pairs
    (at most 9 tiles between). Oil-unit networks join their headers with short A* links. Water and crude enter at
    the top of their headers (the inlets); a header nothing joins at the top starts at its first port. Pumps cut a
    header whose segment would pass the game's pipeline extent (fluidseg.pump_headers)."""
    import plan_nx
    places = cx["places"]
    blocked = geom.tiles(places)
    ports = [p for p in cx["fluid_ports"]]
    nets = cx["networks"]
    fluids = sorted({p["fluid"] for p in ports} | {f for n in nets.values() for f in n} | {"water", "crude-oil"})
    west_x = [cx["bus"]["modules"][n][0] for n in cx["bus"]["modules"] if cx["bus"]["modules"][n][2] == fluid_side]
    hx0 = min(west_x) - 3
    hx = {f: hx0 - 3 * k for k, f in enumerate(fluids)}   # 2 free columns between headers
    header_x = set(hx.values())
    out, fails = [], []
    spans = {"v": set(), "h": set()}
    rows = {f: set() for f in fluids}
    occupied = set(blocked)

    def spur(t, f, x_to):
        """Tile t west to header column x_to: free runs get pipes; every obstacle run (other headers, belts) is
        passed underground: the last tile of a free run is an entrance, the first tile of the next run an exit
        (runs between obstacles need 2 tiles: exit then entrance, facing each other)."""
        x, y = t
        cols = list(range(x, x_to, -1))
        bad = [((c, y) in occupied) or (c in header_x and c != hx[f]) for c in cols]
        runs, cur = [], None
        for c, b in zip(cols, bad):
            if not b:
                if cur is None:
                    cur = [c]
                    runs.append(cur)
                else:
                    if cur[-1] != c + 1:
                        cur = [c]
                        runs.append(cur)
                    else:
                        cur.append(c)
            else:
                cur = None
        if not runs or runs[0][0] != x or runs[-1][-1] != x_to + 1:
            return None
        pl = []
        for i, run in enumerate(runs):
            first, last = run[0], run[-1]
            if i > 0 and runs[i - 1][-1] - first - 1 > 9:
                return None
            if i > 0 and i < len(runs) - 1 and len(run) < 2:
                return None
            for c in run:
                if i > 0 and c == first:
                    pl.append({"item": "pipe-to-ground", "x": c + .5, "y": y + .5, "direction": "west"})   # exit
                elif i < len(runs) - 1 and c == last:
                    pl.append({"item": "pipe-to-ground", "x": c + .5, "y": y + .5, "direction": "east"})   # entrance
                else:
                    pl.append({"item": "pipe", "x": c + .5, "y": y + .5})
        return pl
    by_machine = set()
    fluid_of = {}  # tile -> fluid of each piece placed here
    later = []    # ports a straight spur can't serve (fluid outputs facing into their block): A* below
    for p in sorted(ports, key=lambda p: (p["module"], p["y"])):
        f = p["fluid"]
        t = (math.floor(p["x"]), math.floor(p["y"]))
        key = (p["module"], round(p["x"] / 3), round(p["y"] / 3), f, p["io"])
        if key in by_machine:
            continue
        pl = spur(t, f, hx[f])
        if pl is None:
            later.append((p, key))
            continue
        by_machine.add(key)
        out += pl
        occupied |= geom.tiles(pl)
        rows[f].add(t[1])
        fluid_of.update({t_: f for t_ in geom.tiles(pl)})
    # header extents: staggered tops (a farther header starts higher) so links reach a top over nearer headers
    first = min((y for f in rows for y in rows[f]), default=0)
    first = min([first] + [math.floor(p["y"]) for p, _ in later] +
                [min(q[1] for q in ts) for nw in nets.values() for ts in nw.values()])
    top = {f: first - 3 - 2 * k for k, f in enumerate(fluids)}
    for f in fluids:   # a header no inlet and no oil unit joins at the top starts at its first port
        ys = rows[f] | {math.floor(p["y"]) for p, _ in later if p["fluid"] == f}
        if f not in ("water", "crude-oil") and ys and not any(f in nw for nw in nets.values()):
            top[f] = min(ys)
    bottom = {f: max(rows[f] | {top[f]} | {math.floor(p["y"]) for p, _ in later if p["fluid"] == f}) for f in fluids}
    for n, nw in nets.items():
        for f, ts in nw.items():
            bottom[f] = max(bottom[f], top[f])
    hdr = {f: {(hx[f], y) for y in range(top[f], bottom[f] + 1)} for f in fluids}

    mach_tiles = {}
    for q in places:
        if q["item"] in ("assembling-machine-2", "assembling-machine-3", "chemical-plant", "oil-refinery", "rocket-silo"):
            for t_ in geom.footprint(q):
                mach_tiles[t_] = True

    def face(t):
        """Direction from a port tile into its machine (a pipe-to-ground may end there facing it), else "none"."""
        return next((d for d, (ddx, ddy) in geom.DV4.items() if (t[0] + ddx, t[1] + ddy) in mach_tiles), "none")

    def link(starts, f, goal, ownset, gdir="none"):
        foreign = set().union(*[hdr[g] for g in fluids if g != f])
        foreign |= {(math.floor(q["x"]), math.floor(q["y"])) for q in ports if q["fluid"] != f}
        foreign |= {t_ for t_, g in list(same.items()) + list(fluid_of.items()) if g != f}   # pipes placed already
        for n_, nw_ in nets.items():
            for g, qs in nw_.items():
                if g != f:
                    foreign |= {tuple(z) for z in qs}
        for m in (6, 14, 30):
            xs = [q[0] for q in starts] + [goal[0]]
            ys = [q[1] for q in starts] + [goal[1]]
            wb = (min(xs) - m, min(ys) - m, max(xs) + m + 1, max(ys) + m + 1)
            g = Grid(wb, (occupied | foreign) - set(starts) - {goal})
            r = plan_nx.route_pipe(g, set(starts), goal, foreign=foreign, own=set(ownset), spans=spans, tries=30,
                                   goal_dir=gdir)   # a plain pipe, or a pipe-to-ground facing the goal's machine
            if r:
                return r
        return None
    same = {}   # tile -> fluid of every pipe placed here
    joined = {}   # fluid -> tiles of the oil networks already linked to its header
    for n, nw in nets.items():
        for f, ts in nw.items():
            ts = [tuple(q) for q in ts]
            goal = (hx[f], top[f])
            single = len(ts) == 1   # a one-tile network is a bare port: its pipe is placed by the link
            r = link(ts, f, goal, ts)
            if not r and single:    # a boxed-in port: from the header to it, ending on a piece facing its machine
                r = link([goal], f, ts[0], [goal], face(ts[0]))
            if not r and joined.get(f):   # the header top is boxed in: join this fluid where it is piped already
                laid = sorted(joined[f] | {t_ for t_, g in same.items() if g == f})
                ends = [t_ for t_ in ts if any((t_[0] + a, t_[1] + b) not in occupied for a, b in geom.DV4.values())]
                for t0 in sorted(ends, key=lambda t_: min(abs(t_[0] - s[0]) + abs(t_[1] - s[1]) for s in laid))[:4]:
                    r = link(laid, f, t0, laid + ts, face(t0) if single else "none")
                    if r:
                        break
            if not r:
                fails.append(f"oil link {n} {f}")
                continue
            joined.setdefault(f, set()).update(ts)
            # the network's own pipes are already placed: drop the route's pipes there, but keep the ones on its
            # bare port tiles (nothing placed yet), or the port stays unconnected and a pole takes the tile
            r = [{k: v for k, v in q.items() if k in ("item", "x", "y", "direction")} for q in r
                 if not ((math.floor(q["x"]), math.floor(q["y"])) in occupied and q["item"] == "pipe")
                 and (single or (math.floor(q["x"]), math.floor(q["y"])) not in ts)]
            r = [q for q in r if (math.floor(q["x"]), math.floor(q["y"])) != goal]
            geom.pipe_spans(r, spans)
            out += r
            occupied |= geom.tiles(r)
            same.update({t_: f for t_ in geom.tiles(r)})
    for p, key in later:
        f = p["fluid"]
        t = (math.floor(p["x"]), math.floor(p["y"]))
        goal = min(hdr[f], key=lambda h: abs(h[0] - t[0]) + abs(h[1] - t[1]))
        r = link([t], f, goal, [t])
        if not r:   # reversed: from the header to the port, ending on a piece that opens toward the machine
            r = link([goal], f, t, [goal], face(t))
            if r:
                r = [q for q in r if not (q["item"] == "pipe" and (math.floor(q["x"]), math.floor(q["y"])) == goal)]
        if not r:
            fails.append(f"port {f} {p['module']} {t}")
            continue
        r = [{k: v for k, v in q.items() if k in ("item", "x", "y", "direction")} for q in r]
        r = [q for q in r if (math.floor(q["x"]), math.floor(q["y"])) != goal]
        geom.pipe_spans(r, spans)
        out += r
        occupied |= geom.tiles(r)
        same.update({t_: f for t_ in geom.tiles(r)})
        by_machine.add(key)
    inlets = {"water": (hx["water"], top["water"]), "crude-oil": (hx["crude-oil"], top["crude-oil"])}   # header tops
    for f in fluids:
        for (xx, y) in sorted(hdr[f]):
            if same.get((xx, y)) == f:
                continue
            if (xx, y) in occupied:
                fails.append(f"header {f} blocked at {y}")
                continue
            out.append({"item": "pipe", "x": xx + .5, "y": y + .5})
            occupied.add((xx, y))
            fluid_of[(xx, y)] = f
    fluid_of.update(same)
    for n, nw in nets.items():
        for f, ts in nw.items():
            if len(ts) > 1:
                fluid_of.update({tuple(t_): f for t_ in ts})
    from auto import fluidseg
    pumps, taken, pf = fluidseg.pump_headers(places + out, fluid_of, hx, top, bottom, ports, nets, inlets)
    out = [q for q in out if not (q["item"] == "pipe" and (math.floor(q["x"]), math.floor(q["y"])) in taken)] + pumps
    for t_ in taken:   # a pump is no pipe: world routes of its fluid must not run through it
        fluid_of.pop(t_, None)
    fails += pf
    cx["places"] += geom.tag(out, "fluid")
    cx["inlets"] = inlets
    cx["fails"] += fails
    cx["pipe_fluids"] = fluid_of
    return out

