"""Overseer planning algorithms: deterministic answers instead of an agent's guesswork.
Run with the project venv (networkx, scipy): E:/modding/factorio-agent/.venv/Scripts/python plan.py ...

All read-only (spawn no character). Run from this folder:

    python plan.py ratio <item> <per_min> [--raw iron-plate,copper-plate,...]
        Recipe tree from the game's own recipes: machines per recipe (AM2 / steel furnace / chem plant
        speeds), raw inputs per minute.
    python plan.py route X1,Y1 X2,Y2 [--from-dir east] [--to-dir north] [--margin 12] [--json]
        A* belt route over the live map: avoids buildings and water, crosses obstacles with
        underground belts (yellow: up to 4 tiles), trees cost extra (workers mine them). Prints
        segments for a brief and a `place` list for post_job (--json).
    python plan.py site W H --near X,Y [--radius 60] [--avoid-ore]
        Nearest fully free W x H rectangle (top-left and centre), from an integral image.
    python plan.py block spec.json [--json]
        Block compiler (plan_block.py): machines + belt lanes -> every inserter (direct or belt, normal
        or long-handed), lane checks, poles by greedy set cover, all checked on the live map.
    python plan.py layout spec.layout.json [--out block.json] [--seconds 30]
        Layout search (plan_layout.py, OR-Tools CP-SAT): rows/columns of machines, belt lanes and directions
        so that every ingredient is reachable, outputs land on the right far lane and producers are upstream
        of their readers; writes a plan_block spec.
    python plan.py grid X1,Y1 X2,Y2
        ASCII occupancy of an area: . free, # building, ~ water, t tree/rock, o ore, = belt.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge  # noqa: E402

DIRS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
UG_GAP = 4  # yellow underground: up to 4 tiles between entrance and exit


def _bridge():
    return Bridge("planner", autostart=False)


def _lua(b, code):
    out = b.raw("/sc " + code)
    return out


# ---------------------------------------------------------------- map snapshot

GRID_LUA = r"""
local s=game.surfaces[1] local x1,y1,x2,y2=%d,%d,%d,%d
local W,H=x2-x1,y2-y1 local g={}
for i=1,W*H do g[i]=46 end
local function mark(x,y,c) if x>=x1 and x<x2 and y>=y1 and y<y2 then local k=(y-y1)*W+(x-x1)+1 if g[k]==46 or (g[k]==111 and c~=111) or c==35 or c==126 then g[k]=c end end end
for _,t in pairs(s.find_tiles_filtered{area={{x1,y1},{x2,y2}},collision_mask='water_tile'}) do mark(t.position.x,t.position.y,126) end
for _,r in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}},type='resource'}) do mark(math.floor(r.position.x),math.floor(r.position.y),111) end
for _,e in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}}}) do
  local t=e.type
  if t~='resource' and t~='character' and t~='item-entity' and t~='corpse' and t~='entity-ghost' then
    local c=35
    if t=='tree' or t=='simple-entity' or t=='fish' or e.name:find('crash',1,true) then c=116 elseif t=='transport-belt' or t=='underground-belt' or t=='splitter' then c=61
    elseif t=='pipe' or t=='pipe-to-ground' or t=='storage-tank' or t=='pump' or t=='offshore-pump' then c=112 end
    local bb=e.bounding_box
    for x=math.floor(bb.left_top.x+0.01),math.ceil(bb.right_bottom.x-0.01)-1 do
      for y=math.floor(bb.left_top.y+0.01),math.ceil(bb.right_bottom.y-0.01)-1 do mark(x,y,c) end
    end
  end
end
-- the free tile in front of a belt end: a belt placed there would take that belt's items (side-load)
local DV={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}}
for _,e in pairs(s.find_entities_filtered{area={{x1-1,y1-1},{x2+1,y2+1}},type={'transport-belt','underground-belt'}}) do
  local v=DV[e.direction]
  if v and not (e.type=='underground-belt' and e.belt_to_ground_type=='input') then
    local fx,fy=math.floor(e.position.x)+v[1],math.floor(e.position.y)+v[2]
    if fx>=x1 and fx<x2 and fy>=y1 and fy<y2 then local k=(fy-y1)*W+(fx-x1)+1 if g[k]==46 or g[k]==111 or g[k]==116 then g[k]=35 end end
  end
end
-- port tiles of fluid machines: a pipe placed there would join that machine's fluid
for _,e in pairs(s.find_entities_filtered{area={{x1-3,y1-3},{x2+3,y2+3}}}) do
  if e.type~='pipe' and e.type~='pipe-to-ground' and e.type~='resource' and e.type~='tree' then
    local ok,fb=pcall(function() return e.fluidbox end)
    if ok and fb and #fb>0 then
      for i=1,#fb do
        local okc,cs=pcall(function() return fb.get_pipe_connections(i) end)
        if okc and cs then for _,c in pairs(cs) do
          local tx,ty=math.floor(c.target_position.x),math.floor(c.target_position.y)
          if tx>=x1 and tx<x2 and ty>=y1 and ty<y2 then local k=(ty-y1)*W+(tx-x1)+1 if g[k]==46 or g[k]==116 or g[k]==111 then g[k]=80 end end
        end end
      end
    end
  end
end
local rows={} for y=0,H-1 do local r={} for x=1,W do r[x]=string.char(g[y*W+x]) end rows[#rows+1]=table.concat(r) end
rcon.print(table.concat(rows,'\n'))
"""


class Grid:
    def __init__(self, x1, y1, x2, y2, rows):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.rows = rows

    @classmethod
    def load(cls, b, x1, y1, x2, y2):
        x1, y1, x2, y2 = int(x1 // 1), int(y1 // 1), int(-(-x2 // 1)), int(-(-y2 // 1))
        rows = [r for r in _lua(b, GRID_LUA % (x1, y1, x2, y2)).splitlines()]
        if len(rows) != y2 - y1:
            raise SystemExit("map snapshot failed: " + "\n".join(rows[:3]))
        return cls(x1, y1, x2, y2, rows)

    SIZE = {"assembling-machine-2": 3, "electric-furnace": 3, "electric-mining-drill": 3, "steel-furnace": 2,
            "stone-furnace": 2}

    def block(self, places):
        """Mark planned placements as blocked (so routes avoid what is about to be built)."""
        rows = [list(r) for r in self.rows]
        for p in places:
            n = self.SIZE.get(p["item"], 1)
            for dx in range(n):
                for dy in range(n):
                    x = int((p["x"] - n / 2 + 0.01) // 1) + dx
                    y = int((p["y"] - n / 2 + 0.01) // 1) + dy
                    if self.x1 <= x < self.x2 and self.y1 <= y < self.y2:
                        rows[y - self.y1][x - self.x1] = "#"
        self.rows = ["".join(r) for r in rows]

    def at(self, x, y):
        if not (self.x1 <= x < self.x2 and self.y1 <= y < self.y2):
            return "#"
        return self.rows[y - self.y1][x - self.x1]

    def free(self, x, y, ore_ok=True):
        c = self.at(x, y)
        return c in ".t" or (ore_ok and c == "o")


# ---------------------------------------------------------------- A* belt router

def route(g, start, goal, from_dir=None, to_dir=None):
    """Belt route: networkx A* over the belt state graph (plan_nx.route_belt)."""
    import plan_nx
    return plan_nx.route_belt(g, start, goal, from_dir, to_dir)


def segments(places):
    """Compress a placement list into readable runs: 'belt east 3.5,-10.5 -> 9.5,-10.5 (7)'."""
    runs, cur = [], None
    for p in places:
        if p["item"] != "transport-belt":
            if cur:
                runs.append(cur)
                cur = None
            runs.append({"kind": p["item"] + ("(exit)" if p.get("belt_type") == "output" else "(entrance)"),
                         "dir": p["direction"], "a": (p["x"], p["y"]), "b": (p["x"], p["y"]), "n": 1})
            continue
        if cur and cur["dir"] == p["direction"]:
            cur["b"], cur["n"] = (p["x"], p["y"]), cur["n"] + 1
        else:
            if cur:
                runs.append(cur)
            cur = {"kind": "transport-belt", "dir": p["direction"], "a": (p["x"], p["y"]), "b": (p["x"], p["y"]), "n": 1}
    if cur:
        runs.append(cur)
    lines = []
    for r in runs:
        a, bb = r["a"], r["b"]
        span = f"{a[0]:g},{a[1]:g}" + ("" if a == bb else f" -> {bb[0]:g},{bb[1]:g}")
        lines.append(f"{r['kind']} direction={r['dir']} {span}" + (f" ({r['n']} tiles)" if r["n"] > 1 else ""))
    return lines


# ---------------------------------------------------------------- free site

def site(g, w, h, near, radius, ore_ok=True):
    W, H = g.x2 - g.x1, g.y2 - g.y1
    S = [[0] * (W + 1) for _ in range(H + 1)]  # integral image of blocked tiles
    for j in range(H):
        row = g.rows[j]
        acc = 0
        for i in range(W):
            c = row[i]
            blocked = 0 if (c in ".t" or (ore_ok and c == "o")) else 1
            acc += blocked
            S[j + 1][i + 1] = S[j][i + 1] + acc
    best = None
    nx, ny = near
    for j in range(H - h + 1):
        for i in range(W - w + 1):
            if S[j + h][i + w] - S[j][i + w] - S[j + h][i] + S[j][i] == 0:
                cx, cy = g.x1 + i + w / 2, g.y1 + j + h / 2
                d = abs(cx - nx) + abs(cy - ny)
                if d <= radius and (best is None or d < best[0]):
                    best = (d, g.x1 + i, g.y1 + j, cx, cy)
    return best


# ---------------------------------------------------------------- ratio solver

RECIPE_LUA = r"""
local out={}
for name,r in pairs(game.forces.player.recipes) do
  if not r.hidden then
    local ing,prod={},{}
    for _,i in pairs(r.ingredients) do ing[#ing+1]={i.name,i.amount} end
    for _,p in pairs(r.products) do prod[#prod+1]={p.name,p.amount or ((p.amount_min+p.amount_max)/2)*(p.probability or 1)} end
    out[#out+1]={name,r.category,r.energy,ing,prod,r.enabled}
  end
end
rcon.print(helpers.table_to_json(out))
"""

SPEED = {"crafting": 0.75, "advanced-crafting": 0.75, "crafting-with-fluid": 0.75, "smelting": 2.0,
         "chemistry": 1.0, "oil-processing": 1.0, "rocket-building": 1.0, "centrifuging": 1.0}
MACHINE = {"crafting": "AM2", "advanced-crafting": "AM2", "crafting-with-fluid": "AM2", "smelting": "steel furnace",
           "chemistry": "chemical plant", "oil-processing": "refinery", "rocket-building": "silo"}
DEFAULT_RAW = {"iron-plate", "copper-plate", "steel-plate", "stone", "stone-brick", "coal", "plastic-bar",
               "sulfur", "water", "crude-oil", "petroleum-gas", "light-oil", "heavy-oil", "lubricant",
               "sulfuric-acid", "iron-ore", "copper-ore", "uranium-ore", "solid-fuel"}


def ratio(b, item, per_min, raw):
    data = json.loads(_lua(b, RECIPE_LUA))
    by_product = {}
    for name, cat, energy, ing, prod, enabled in data:
        for pname, amount in prod:
            # prefer the recipe named after the item, then any enabled one
            cur = by_product.get(pname)
            if cur is None or name == pname or (not cur[5] and enabled):
                by_product[pname] = (name, cat, energy, ing, prod, enabled)
    need, machines, raws = {}, {}, {}

    def walk(it, rate, depth=0):
        if it in raw or it not in by_product:
            raws[it] = raws.get(it, 0) + rate
            return
        name, cat, energy, ing, prod, enabled = by_product[it]
        out_amt = next(a for p, a in prod if p == it)
        crafts = rate / out_amt  # crafts per minute
        speed = SPEED.get(cat, 0.75)
        m = crafts * energy / 60 / speed
        machines[name] = (machines.get(name, (0, cat, enabled))[0] + m, cat, enabled)
        need[it] = need.get(it, 0) + rate
        for iname, iamt in ing:
            walk(iname, crafts * iamt, depth + 1)

    walk(item, per_min)
    return machines, raws, need


# ---------------------------------------------------------------- cli

def _xy(s):
    x, y = s.split(",")
    return float(x), float(y)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ratio")
    r.add_argument("item")
    r.add_argument("per_min", type=float)
    r.add_argument("--raw", default="")
    ro = sub.add_parser("route")
    ro.add_argument("a")
    ro.add_argument("b")
    ro.add_argument("--from-dir", choices=list(DIRS))
    ro.add_argument("--to-dir", choices=list(DIRS))
    ro.add_argument("--margin", type=int, default=12)
    ro.add_argument("--json", action="store_true")
    ro.add_argument("--pipe", action="store_true", help="route a pipe between two port tiles (fluid rules)")
    ro.add_argument("--reserve", action="append", default=[], help="JSON place list(s) whose tiles count as blocked")
    ro.add_argument("--save", help="append this route's places to a JSON file (for --reserve in the next route)")
    si = sub.add_parser("site")
    si.add_argument("w", type=int)
    si.add_argument("h", type=int)
    si.add_argument("--near", required=True)
    si.add_argument("--radius", type=float, default=60)
    si.add_argument("--avoid-ore", action="store_true")
    lp = sub.add_parser("lp")
    lp.add_argument("--hours", type=float, default=8)
    lp.add_argument("--top", type=int, default=25)
    de = sub.add_parser("decide")
    de.add_argument("--hours", type=float, default=6)
    de.add_argument("--top", type=int, default=22)
    lo = sub.add_parser("layout")
    lo.add_argument("spec")
    lo.add_argument("--out")
    lo.add_argument("--seconds", type=float, default=30.0)
    bl = sub.add_parser("block")
    bl.add_argument("spec")
    bl.add_argument("--json", action="store_true")
    gr = sub.add_parser("grid")
    gr.add_argument("a")
    gr.add_argument("b")
    # coordinates like -254.5,0.5 look like options to argparse; a leading space keeps them positional
    a = ap.parse_args([(" " + x) if len(x) > 1 and x[0] == "-" and (x[1].isdigit() or x[1] == ".") else x
                       for x in sys.argv[1:]])
    b = _bridge()

    if a.cmd == "ratio":
        raw = DEFAULT_RAW | {x for x in a.raw.split(",") if x}
        machines, raws, _ = ratio(b, a.item, a.per_min, raw)
        print(f"{a.per_min:g}/min {a.item}:")
        for name, (m, cat, enabled) in sorted(machines.items(), key=lambda kv: -kv[1][0]):
            print(f"  {name:28s} {m:6.2f} x {MACHINE.get(cat, cat)}{'' if enabled else '  (NOT RESEARCHED)'}")
        print("raw per minute: " + ", ".join(f"{k} {v:.1f}" for k, v in sorted(raws.items(), key=lambda kv: -kv[1])))
    elif a.cmd == "route":
        (ax, ay), (bx, by) = _xy(a.a), _xy(a.b)
        sx, sy, gx, gy = int(ax // 1), int(ay // 1), int(bx // 1), int(by // 1)
        m = a.margin
        g = Grid.load(b, min(sx, gx) - m, min(sy, gy) - m, max(sx, gx) + m + 1, max(sy, gy) + m + 1)
        for path in a.reserve:
            if os.path.exists(path):
                g.block(json.load(open(path, encoding="utf-8")))
        if a.pipe:
            import plan_nx
            places = plan_nx.route_pipe(g, {(sx, sy)}, (gx, gy))
            if not places:
                print("no pipe route (try a larger --margin)")
                return
            n_ptg = sum(1 for p in places if p["item"] == "pipe-to-ground")
            print(f"pipe route: {len(places)} tiles ({len(places) - n_ptg} pipes, {n_ptg // 2} pipe-to-ground pairs)")
            print("; ".join(f"{p['item']} {p['x']:g},{p['y']:g}" + (f" {p['direction']} {p['end']}" if p.get("end") else "")
                            for p in places))
            if a.json:
                print(json.dumps(places, separators=(",", ":")))
            if a.save:
                old = json.load(open(a.save, encoding="utf-8")) if os.path.exists(a.save) else []
                json.dump(old + places, open(a.save, "w", encoding="utf-8"))
            return
        places = route(g, (sx, sy), (gx, gy), a.from_dir, a.to_dir)
        if not places:
            print("no route (try a larger --margin or other end tiles)")
            return
        n_ug = sum(1 for p in places if p["item"] == "underground-belt")
        n_tree = sum(1 for p in places if g.at(int(p["x"]), int(p["y"])) == "t")
        print(f"route: {len(places)} tiles ({len(places) - n_ug} belts, {n_ug // 2} underground pairs, {n_tree} trees to mine)")
        print("\n".join(segments(places)))
        if a.json:
            print(json.dumps(places, separators=(",", ":")))
        if a.save:
            old = json.load(open(a.save, encoding="utf-8")) if os.path.exists(a.save) else []
            json.dump(old + places, open(a.save, "w", encoding="utf-8"))
    elif a.cmd == "site":
        nx, ny = _xy(a.near)
        R = int(a.radius) + max(a.w, a.h)
        g = Grid.load(b, nx - R, ny - R, nx + R, ny + R)
        best = site(g, a.w, a.h, (nx, ny), a.radius, ore_ok=not a.avoid_ore)
        if not best:
            print("no free site in range")
        else:
            d, x, y, cx, cy = best
            print(f"free {a.w}x{a.h}: top-left {x},{y}  centre {cx:g},{cy:g}  (distance {d:g})")
    elif a.cmd == "lp":
        import plan_lp
        import crew
        print(plan_lp.report(b, crew, a.hours, a.top))
    elif a.cmd == "decide":
        import plan_decide
        import crew
        print(plan_decide.report(b, crew, a.hours, a.top))
    elif a.cmd == "layout":
        import plan_layout
        print(plan_layout.report(b, a.spec, a.out, a.seconds))
    elif a.cmd == "block":
        import plan_block
        spec = json.load(open(a.spec, encoding="utf-8"))
        res = plan_block.compile_block(spec, plan_block.recipes_from_game(b, RECIPE_LUA))
        for e in res["errors"]:
            print("ERROR", e)
        for n in res["notes"]:
            print("note ", n)
        for w in res.get("rates", []):
            print("RATE ", w)
        groups = {}
        for p in res["places"]:
            groups.setdefault(p["group"], []).append(p)
        for gname, ps in groups.items():
            print(f"[{gname}] {len(ps)}: " + "; ".join(
                f"{p['item'].replace('transport-belt','belt')} {p['x']:g},{p['y']:g}"
                + (f" {p['direction']}" if p.get("direction") else "") + (f" {p['recipe']}" if p.get("recipe") else "")
                + (f" ({p['why']})" if p.get("why") else "") for p in ps[:60]))
        print(f"pole groups (wire reach 7.5): {len(res['pole_components'])}")
        print("map check:", plan_block.check_on_map(b, [p for p in res["places"]]))
        if a.json:
            print(json.dumps(res["places"], separators=(",", ":")))
    elif a.cmd == "grid":
        (ax, ay), (bx, by) = _xy(a.a), _xy(a.b)
        g = Grid.load(b, ax, ay, bx, by)
        print(f"x from {g.x1} to {g.x2 - 1}, rows from y={g.y1}")
        for j, row in enumerate(g.rows):
            print(f"{g.y1 + j:5d} {row}")


if __name__ == "__main__":
    main()
