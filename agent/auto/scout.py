"""Scouting: what the rest of the run will need more of than the known patches hold, radars that reveal the map
beyond the base, the patches they find, and the outpost that taps one.

    d = scout.deficits(sched, dem, plan, q)          {resource: amount short}  (need to the rocket - left in reach)
    ps = scout.patches(q)                            resource patches on every charted chunk (live, clustered)
    site, info = scout.radar_site(plan, live, m, q)  a free spot whose radar reveals the most uncharted chunks
    scout.extend(m, out, box)                        the map (terrain, ore) of a scouted box, merged into m

How the map grows: only in game. A radar (built by the characters like any piece) scans one sector at a time out to
14 chunks and the game generates what it scans; nothing reads the map generator. The seed's map.json covers the
start; every box the planners need beyond it comes from the live surface once charted (mapdata.rows), and is kept
in runtime/auto/<seed>/scout-map.json so the next start sees it too.
Patches: per known chunk (generated, or charted: a server with no player connected keeps no chart, while a
radar scan still generates what it scans) the resource amounts (one Lua pass), chunks joined into patches by 8-neighbourhood
(networkx connected components). The outpost itself is a remine plan in outpost mode (auto.remine.plan(outpost=)):
drill columns on the patch (auto.mines) and an A* belt route (plan_nx) to the side of a working mine's belt.
"""
import json
import math

import networkx as nx

from auto import geom, mapdata

CHUNK = 32
RADAR_REACH = 14            # chunks a radar's long-range scan reveals around its own chunk
DRILL_RATE = 0.5            # ore/s of an electric mining drill
RAW = ("coal", "iron-ore", "copper-ore", "stone")

CHUNKS_LUA = r"""
local S=game.surfaces[1] local F=game.forces.player local ch={} local res={}
for c in S.get_chunks() do if S.is_chunk_generated(c) or F.is_chunk_charted(S,c) then ch[#ch+1]=c.x..','..c.y
  local a={} for _,e in pairs(S.find_entities_filtered{area=c.area,type='resource'}) do
    local r=a[e.name] if not r then r={0,0,0,0} a[e.name]=r end
    r[1]=r[1]+1 r[2]=r[2]+e.amount r[3]=r[3]+e.position.x*e.amount r[4]=r[4]+e.position.y*e.amount end
  for n,r in pairs(a) do res[#res+1]={c.x,c.y,n,r[1],r[2],r[3]/math.max(1,r[2]),r[4]/math.max(1,r[2])} end end end
rcon.print(helpers.table_to_json({c=table.concat(ch,';'),r=res}))
"""


# A radar scans one sector (chunk) every ~33 s, nearest dark ones first, up to 14 chunks out, and the game generates
# what it scans. On a server with no player connected the game neither keeps a chart nor lets radars sweep (measured:
# 3 radars, 502 -> 509 chunks in 10 game hours), so the controller does the radar's sweep at the radar's pace:
# per working radar, one sector per SECTOR_TICKS since the last call, the nearest ungenerated chunks in its reach.
SECTOR_TICKS = 2000
SWEEP_LUA = r"""
local S=game.surfaces[1] local n,R=%d,%d local got=0
for _,r in pairs(S.find_entities_filtered{type='radar',force='player'}) do
  if r.status==defines.entity_status.working then
    local cx,cy=math.floor(r.position.x/32),math.floor(r.position.y/32) local c={}
    for dx=-R,R do for dy=-R,R do if not S.is_chunk_generated{cx+dx,cy+dy} then c[#c+1]={cx+dx,cy+dy,dx*dx+dy*dy} end end end
    table.sort(c,function(a,b) return a[3]<b[3] end)
    for k=1,math.min(n,#c) do S.request_to_generate_chunks({c[k][1]*32+16,c[k][2]*32+16},0) got=got+1 end
  end end
rcon.print(got)
"""


def sweep(q, state):
    """The radars' sweep for the ticks since the last call (see SWEEP_LUA); returns chunks requested."""
    tick = int(q("/silent-command rcon.print(game.tick)") or 0)
    last = state.get("sweep_tick") or tick
    n = (tick - last) // SECTOR_TICKS
    if n <= 0:
        state.setdefault("sweep_tick", tick)
        return 0
    state["sweep_tick"] = last + n * SECTOR_TICKS
    return int(q("/silent-command " + (SWEEP_LUA % (min(n, 200), RADAR_REACH)).replace(chr(10), " ")) or 0)


def scan(q):
    """(charted chunk set, [(cx, cy, name, tiles, amount, mean x, mean y)]) from the live game."""
    d = json.loads(q("/silent-command " + CHUNKS_LUA.replace("\n", " ")) or "{}")
    charted = {tuple(int(v) for v in t.split(",")) for t in (d.get("c") or "").split(";") if t}
    return charted, [tuple(r) for r in d.get("r") or []]


def patches(q, rows=None):
    """Resource patches on the charted map: [{"name", "amount", "tiles", "center", "box", "chunks"}], the chunks of
    one resource joined by 8-neighbourhood (networkx connected components). Crude oil counts wells, not tiles."""
    if rows is None:
        rows = scan(q)[1]
    out = []
    by = {}
    for cx, cy, n, k, amt, mx, my in rows:
        by.setdefault(n, {})[(int(cx), int(cy))] = (k, amt, mx, my)
    for n, cells in by.items():
        G = nx.Graph()
        G.add_nodes_from(cells)
        for (cx, cy) in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if (dx or dy) and (cx + dx, cy + dy) in cells:
                        G.add_edge((cx, cy), (cx + dx, cy + dy))
        for comp in nx.connected_components(G):
            amt = sum(cells[c][1] for c in comp)
            if amt <= 0:
                continue
            xs = [c[0] for c in comp]
            ys = [c[1] for c in comp]
            out.append({"name": n, "amount": int(amt), "tiles": sum(cells[c][0] for c in comp),
                        "center": (sum(cells[c][2] * cells[c][1] for c in comp) / amt,
                                   sum(cells[c][3] * cells[c][1] for c in comp) / amt),
                        "box": (min(xs) * CHUNK, min(ys) * CHUNK, (max(xs) + 1) * CHUNK, (max(ys) + 1) * CHUNK),
                        "chunks": sorted(comp)})
    return out


def mined(plan, live, ps, own=True):
    """The patches the plan's (or, own=True, our own) drills stand on: what the run already draws from."""
    pts = {}
    for i, p in enumerate(plan.p):
        if p["item"] == "electric-mining-drill" and p.get("grp", "").startswith("mine:"):
            ore = p["grp"].split(":")[1]
            pts.setdefault("iron-ore" if ore == "raw-iron-ore" else ore, []).append((p["x"], p["y"]))
    for p in live.own if own else ():
        if p["item"] == "electric-mining-drill":
            pts.setdefault("?", []).append((p["x"], p["y"]))
    used = []
    for k, pc in enumerate(ps):
        cs = set(pc["chunks"])
        for n in (pc["name"], "?"):
            if any((math.floor(x / CHUNK), math.floor(y / CHUNK)) in cs for x, y in pts.get(n, ())):
                used.append(k)
                break
    return used


def deficits(sched, dem, plan, live, ps, margin=1.2, crude=None):
    """{resource: amount short}: what the rest of the run needs (coal: the schedule's fuel row; ores: the LP's raw
    rate over the schedule's remaining makespan) beyond what the patches already mined still hold, with a margin
    for what the drills cannot reach. Crude is a rate (wells never run dry, they decline to 20%): crude =
    crude_rates(q), short = the built refineries' crude/min beyond what the pumpjacks give now, in well-amount units
    (amount / 30000 = crude/s) so it compares with a patch's amount."""
    out = {}
    if crude and crude["refineries"]:
        need = crude["refineries"] * REFINERY_CRUDE * CRUDE_MARGIN
        if need > crude["per_min"]:
            out["crude-oil"] = int((need - crude["per_min"]) * 30000 / 60)
    if not sched:
        return out
    used = mined(plan, live, ps)
    left = {}
    for k in used:
        left[ps[k]["name"]] = left.get(ps[k]["name"], 0) + ps[k]["amount"]
    mk = (sched.get("makespan") or 0) / 60.0           # game minutes to the rocket
    for n in RAW:
        if n == "coal" and sched.get("coal"):
            need = sched["coal"].get("need") or 0
            have = sched["coal"].get("left") or left.get(n, 0)
        else:
            need = (dem.get("raw_per_min") or {}).get(n, 0) * mk
            have = left.get(n, 0)
        if need * margin > have:
            out[n] = int(need * margin - have)
    return out


CRUDE_LUA = r"""
local S=game.surfaces[1] local n,a=0,0
for _,e in pairs(S.find_entities_filtered{name='pumpjack',force='player'}) do
  local w=S.find_entities_filtered{position=e.position,name='crude-oil'}[1]
  if w then n=n+1 a=a+w.amount end end
rcon.print(n..','..a..','..S.count_entities_filtered{name='oil-refinery',force='player'})
"""
WELLS_LUA = r"""
local S=game.surfaces[1] local o={} local b=helpers.json_to_table('%s')
for _,w in pairs(S.find_entities_filtered{area={{b[1],b[2]},{b[3],b[4]}},name='crude-oil'}) do
  if S.count_entities_filtered{position=w.position,name='pumpjack'}==0 then o[#o+1]={w.position.x,w.position.y,w.amount} end end
rcon.print(helpers.table_to_json(o))
"""
JACK_OUT = {"north": (1, -2), "east": (2, 1), "south": (-1, 2), "west": (-2, -1)}   # pumpjack output by facing
REFINERY_CRUDE = 1200     # crude/min of a refinery on advanced oil processing (100 per 5 s)
CRUDE_MARGIN = 1.1


def crude_rates(q):
    """{"pumpjacks", "per_min": crude/min they give at their wells' current yield, "refineries"} (live)."""
    n, a, r = (float(v) for v in (q("/silent-command " + CRUDE_LUA.replace(chr(10), " ")) or "0,0,0").split(","))
    return {"pumpjacks": int(n), "per_min": a / 30000 * 60, "refineries": int(r)}


def free_wells(q, box):
    """[(x, y, amount)] of the crude wells in box without a pumpjack (live)."""
    return [tuple(w) for w in json.loads(q("/silent-command " + (WELLS_LUA % json.dumps(list(box))).replace(chr(10), " "))
                                         or "[]")]


def pumpjack_outpost(m, plan, live, wells, short, sources=("pipe:crude", "oil-wells"), most=12):
    """Pumpjacks on the richest free wells until their yield covers short (well-amount units), each piped (A*,
    plan_nx.route_pipe) into the crude network (the plan's built pieces of the source groups, and the outpost's
    earlier pipes), pumps where a segment would pass the game's pipeline extent (fluidseg.split_route).
    Returns (places, info) or (None, reason)."""
    import plan_nx
    from auto import fluidseg
    from auto.remine import TerrainGrid
    T = lambda x, y: (math.floor(x), math.floor(y))     # noqa: E731
    built = [plan.p[i] for i in live.built]
    net = {T(p["x"], p["y"]) for i in live.built for p in [plan.p[i]]
           if p.get("grp") in sources and p["item"] in ("pipe", "pipe-to-ground")}
    # joins on plain pipes only (a pipe-to-ground connects on its one side)
    crude = {T(p["x"], p["y"]) for i in live.built for p in [plan.p[i]] if p.get("grp") in sources and p["item"] == "pipe"}
    if not crude:
        return None, "no built crude pipe to join"
    taken = set(plan.tiles)
    for p in built + list(live.own):
        taken |= geom.footprint(p)
    for (x, y), n in live.extra.items():
        taken |= geom.footprint({"item": n, "x": x, "y": y})
    foreign = {T(p["x"], p["y"]) for p in built + list(live.own)
               if p["item"] in ("pipe", "pipe-to-ground", "pump", "offshore-pump", "storage-tank")} - net
    spans = geom.pipe_spans([p for p in built + list(live.own) if p["item"] == "pipe-to-ground"])
    places, got, why = [], 0, []
    for wx, wy, amt in sorted(wells, key=lambda w: -w[2]):
        if got >= short or sum(1 for p in places if p["item"] == "pumpjack") >= most:
            break
        pj = {"item": "pumpjack", "x": wx, "y": wy}
        fp = geom.footprint(pj)
        if fp & taken or any(m.t(*t) not in ".t" for t in fp):
            why.append(f"well ({wx},{wy}) blocked")
            continue
        goal = min(crude, key=lambda t: abs(t[0] - wx) + abs(t[1] - wy))
        # the output (+1, -2 facing north, turned with the pumpjack) on a free tile, the side facing the network first
        outs = [(d, T(wx + ox, wy + oy)) for d, (ox, oy) in JACK_OUT.items()]
        outs = [(d, o) for d, o in sorted(outs, key=lambda do: abs(do[1][0] - goal[0]) + abs(do[1][1] - goal[1]))
                if o not in taken and m.t(*o) in ".t"]
        if not outs:
            why.append(f"well ({wx},{wy}): no free output tile")
            continue
        pj["direction"], out = outs[0]
        r = None   # the 3 nearest joins, in a small box first (a failed search over a big box costs minutes)
        for goal in sorted(crude, key=lambda t: abs(t[0] - out[0]) + abs(t[1] - out[1]))[:3]:
            for mg in (12, 30):
                box = (min(out[0], goal[0]) - mg, min(out[1], goal[1]) - mg, max(out[0], goal[0]) + mg + 1,
                       max(out[1], goal[1]) + mg + 1)
                g = TerrainGrid(box, (taken | fp) - crude - {out}, m)
                r = plan_nx.route_pipe(g, {out}, goal, foreign=foreign - {goal}, own=crude | {out}, spans=spans,
                                       goal_dir="none", tries=8)
                if r:
                    break
            if r:
                break
        if not r:
            why.append(f"well ({wx},{wy}): no pipe route")
            continue
        r = [q for q in r if not (q["item"] == "pipe" and T(q["x"], q["y"]) in crude)]   # the network's own pieces stay
        r = [{"item": "pipe", "x": out[0] + .5, "y": out[1] + .5}] + [q for q in r if T(q["x"], q["y"]) != out]
        seg = set()   # the segment the route joins: the crude network as built
        near = {goal} | {(goal[0] + a, goal[1] + b) for a, b in ((0, 1), (1, 0), (0, -1), (-1, 0))}
        for s_ in fluidseg.segments(built + list(live.own) + places):
            if s_ & near:
                seg |= s_
        r2 = fluidseg.split_route(r, seg, attach_end="goal")
        if r2 is None:
            why.append(f"well ({wx},{wy}): no straight stretch for a pump")
            continue
        new = [pj] + [dict(q, grp="out:crude") for q in r2]
        for q in new:
            q.setdefault("grp", "out:crude")
            taken |= geom.footprint(q)
        geom.pipe_spans(r2, spans)
        crude |= {T(q["x"], q["y"]) for q in r2 if q["item"] == "pipe"}
        net |= {T(q["x"], q["y"]) for q in r2 if q["item"] in ("pipe", "pipe-to-ground")}
        places += new
        got += amt
    n = sum(1 for p in places if p["item"] == "pumpjack")
    if not n:
        return None, "; ".join(why[:4]) or "no free well"
    return places, (f"{n} pumpjacks on wells of {got} ({got / 30000 * 60:.0f} crude/min now) for {short} short, "
                    f"{sum(1 for p in places if p['item'] == 'pump')} pumps" + (f"; {'; '.join(why[:3])}" if why else ""))


def crude_patch(wells_in, ps, short, base):
    """The crude patch whose free wells (wells_in(box) -> [(x, y, amount)]) cover short, nearest to the base, else
    the one with the most free yield; its free wells ride along as "wells". None: no free well charted."""
    cands = []
    for p in ps:
        if p["name"] == "crude-oil":
            w = wells_in(p["box"])
            if w:
                cands.append(dict(p, wells=w, free=sum(a for _, _, a in w)))
    if not cands:
        return None
    cover = [p for p in cands if p["free"] >= short]
    return min(cover or cands, key=lambda p: math.dist(p["center"], base) if cover else -p["free"])


def outpost_patch(ps, used, name, short, base):
    """The patch of `name` not mined yet that covers the deficit, nearest to the base (or the biggest one)."""
    cands = [p for k, p in enumerate(ps) if p["name"] == name and k not in used and p["amount"] >= min(short, 50000)]
    if not cands:
        return None
    cover = [p for p in cands if p["amount"] >= short]
    return min(cover or cands, key=lambda p: math.dist(p["center"], base) if cover else -p["amount"])


def drills_for(short, sched, lo=4, hi=12):
    """Drills that mine the deficit within the remaining makespan (at 0.5 ore/s each)."""
    s = max(3600.0, sched.get("makespan") or 0)
    return max(lo, min(hi, math.ceil(short / (DRILL_RATE * s))))


def radar_site(plan, live, m, charted, radars, step=12):
    """A free 3x3 spot (with a free ring) on the plan's border whose radar reveals the most uncharted chunks, away
    from the radars built before. Returns ((x, y), uncharted chunks it reveals) or (None, 0)."""
    x1, y1, x2, y2 = plan.box([i for i in range(len(plan.p))], 4)
    taken = set(plan.tiles)
    for i in live.built:
        taken |= geom.footprint(plan.p[i])
    for (x, y), n in live.extra.items():
        taken |= geom.footprint({"item": n, "x": x, "y": y})
    for p in live.own:
        taken |= geom.footprint(p)
    cand = []
    for x in range(int(x1), int(x2), step):
        cand += [(x, int(y1)), (x, int(y2))]
    for y in range(int(y1), int(y2), step):
        cand += [(int(x1), y), (int(x2), y)]
    best, bn = None, 0
    for cx, cy in cand:
        ring = [(cx + a, cy + b) for a in range(-2, 3) for b in range(-2, 3)]
        if any(t in taken or m.t(*t) not in ".t" or m.o(*t) != "." for t in ring):
            continue
        c0 = (math.floor(cx / CHUNK), math.floor(cy / CHUNK))
        if any(max(abs(c0[0] - r[0]), abs(c0[1] - r[1])) < RADAR_REACH // 2 for r in radars):
            continue
        n = sum(1 for a in range(-RADAR_REACH, RADAR_REACH + 1) for b in range(-RADAR_REACH, RADAR_REACH + 1)
                if (c0[0] + a, c0[1] + b) not in charted)
        if n > bn:
            best, bn = (cx + .5, cy + .5), n
    return best, bn


def load(m, out):
    """Scouted boxes kept from earlier runs, merged into m."""
    f = out / "scout-map.json"
    if f.exists():
        m.ext = json.loads(f.read_text())
    return m


def extend(m, out, box, qf=None):
    """Terrain and ore of box from the live surface (charted ground), outside the seed's map, merged into m and kept
    in scout-map.json. Strips of 64 rows; boxes already held are skipped."""
    x1, y1, x2, y2 = (int(math.floor(v)) for v in box)
    ext = list(getattr(m, "ext", ()) or ())
    have = {(e["x0"], e["y0"], e["w"], e["h"]) for e in ext}
    new = 0
    for y in range(y1, y2, 64):
        h = min(64, y2 - y)
        k = (x1, y, x2 - x1, h)
        if k in have or (m.x0 <= x1 and x2 <= m.x0 + m.w and m.y0 <= y and y + h <= m.y0 + m.h):
            continue
        t, o = mapdata.rows("nauvis", x1, y, x2 - x1, h, qf=qf)
        ext.append({"x0": x1, "y0": y, "w": x2 - x1, "h": h, "terrain": t, "ore": o})
        new += 1
    if new:
        # newest first: a strip scanned again later (ore mined, trees cleared) wins over the old one
        m.ext = ext[-new:][::-1] + ext[:-new]
        (out / "scout-map.json").write_text(json.dumps(m.ext))
    return new
