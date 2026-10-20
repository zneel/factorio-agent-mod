"""Stage 1: the map of a seed, from the game itself (scratch server, a throwaway surface).

    python -m auto.mapdata --seed 1060 [--radius 256]   -> runtime/auto/<seed>/map.json

map.json: {"seed", "x0", "y0", "w", "h",
           "terrain": [row strings: "." free, "~" water, "t" tree/rock (removable), "C" cliff],
           "ore":     [row strings: "." none, "i" iron, "c" copper, "k" coal, "s" stone, "u" uranium],
           "amount":  {"i": total, ...}, "wells": [[x, y, amount], ...], "spawn": [x, y]}
Row r is y = y0 + r, column c is x = x0 + c (tile = the square from (x, y) to (x+1, y+1)).
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import rcon  # noqa: E402

ROOT = HERE.parent.parent
SCRATCH = ("127.0.0.1", 27115, "scratchpw")
ORE = {"iron-ore": "i", "copper-ore": "c", "coal": "k", "stone": "s", "uranium-ore": "u"}


def q(lua):
    out = rcon.Rcon(*SCRATCH).command("/silent-command " + lua).strip()
    if out.startswith("Cannot execute"):
        raise RuntimeError(out[:300])
    return out


def out_dir(seed):
    d = ROOT / "runtime" / "auto" / str(seed)
    d.mkdir(parents=True, exist_ok=True)
    return d


def extract(seed, radius=256, rows_per_call=16):
    q("rcon.print(1)")
    q("rcon.print(1)")
    name = f"auto{seed}"
    q(f"if not game.surfaces['{name}'] then local mg=game.surfaces[1].map_gen_settings; mg.seed={seed}; "
      f"game.create_surface('{name}',mg) end; local s=game.surfaces['{name}']; "
      f"s.request_to_generate_chunks({{0,0}},{radius // 32 + 1}); s.force_generate_chunk_requests(); rcon.print('ok')")
    x0, y0, w, h = -radius, -radius, 2 * radius, 2 * radius
    terrain, ore = rows(name, x0, y0, w, h, rows_per_call)
    wells = json.loads(q("local s=game.surfaces['%s'] local o={} for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},name='crude-oil'}) do "
                         "o[#o+1]={e.position.x,e.position.y,e.amount} end rcon.print(helpers.table_to_json(o))" % (name, x0, y0, x0 + w, y0 + h)) or "[]")
    amount = json.loads(q("local s=game.surfaces['%s'] local a={} for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},type='resource'}) do "
                          "a[e.name]=(a[e.name] or 0)+e.amount end rcon.print(helpers.table_to_json(a))" % (name, x0, y0, x0 + w, y0 + h)))
    spawn = json.loads(q("local p=game.forces.player.get_spawn_position(game.surfaces[1]) rcon.print(helpers.table_to_json({p.x,p.y}))"))
    return {"seed": seed, "x0": x0, "y0": y0, "w": w, "h": h, "terrain": terrain, "ore": ore,
            "amount": {ORE.get(k, k): v for k, v in amount.items()}, "wells": wells, "spawn": spawn}


def rows(surface, x0, y0, w, h, rows_per_call=16, qf=None):
    """Terrain and ore row strings of the box (x0, y0, w, h) on a surface (by name), as map.json holds them."""
    qf = qf or q
    terrain, ore = [], []
    lua_rows = ("local s=game.surfaces['%s'] local x0,y0,w=%d,%d,%d local out={} "
                "local ORE={['iron-ore']='i',['copper-ore']='c',coal='k',stone='s',['uranium-ore']='u'} "
                "for r=%d,%d do local y=y0+r local t={} local o={} for c=1,w do t[c]='.' o[c]='.' end "
                "for _,tl in pairs(s.find_tiles_filtered{area={{x0,y},{x0+w,y+1}},collision_mask='water_tile'}) do t[tl.position.x-x0+1]='~' end "
                "for _,e in pairs(s.find_entities_filtered{area={{x0,y},{x0+w,y+1}},type={'tree','simple-entity','cliff','resource'}}) do "
                "local c=math.floor(e.position.x)-x0+1 if c>=1 and c<=w then "
                "if e.type=='resource' then if ORE[e.name] then o[c]=ORE[e.name] end "
                "elseif e.type=='cliff' then local bb=e.bounding_box for cc=math.floor(bb.left_top.x)-x0+1,math.ceil(bb.right_bottom.x)-x0 do if cc>=1 and cc<=w then t[cc]='C' end end "
                "elseif t[c]=='.' then t[c]='t' end end end "
                "out[#out+1]=table.concat(t)..'|'..table.concat(o) end rcon.print(table.concat(out,'\\n'))")
    for r0 in range(0, h, rows_per_call):
        res = qf(lua_rows % (surface, x0, y0, w, r0, min(h, r0 + rows_per_call) - 1)).strip()
        for line in res.splitlines():
            t, o = line.split("|")
            terrain.append(t)
            ore.append(o)
    # cliffs span several rows: mark their full boxes
    cl = qf("local s=game.surfaces['%s'] local o={} for _,e in pairs(s.find_entities_filtered{area={{%d,%d},{%d,%d}},type='cliff'}) do "
            "local b=e.bounding_box o[#o+1]=math.floor(b.left_top.x)..','..math.floor(b.left_top.y)..','..math.ceil(b.right_bottom.x)..','..math.ceil(b.right_bottom.y) end "
            "rcon.print(table.concat(o,';'))" % (surface, x0, y0, x0 + w, y0 + h)).strip()
    rws = [list(r) for r in terrain]
    for t in cl.split(";"):
        if t:
            ax, ay, bx, by = (int(v) for v in t.split(","))
            for y in range(ay, by):
                for x in range(ax, bx):
                    if 0 <= y - y0 < h and 0 <= x - x0 < w:
                        rws[y - y0][x - x0] = "C"
    return ["".join(r) for r in rws], ore


class Map:
    """Read access to map.json."""

    def __init__(self, d):
        self.__dict__.update(d)

    @classmethod
    def load(cls, seed):
        return cls(json.loads((out_dir(seed) / "map.json").read_text()))

    ext = ()          # scouted boxes beyond the seed's map (auto.scout): [{"x0","y0","w","h","terrain","ore"}]

    def t(self, x, y):
        r, c = y - self.y0, x - self.x0
        if 0 <= r < self.h and 0 <= c < self.w:
            return self.terrain[r][c]
        for e in self.ext:
            r, c = y - e["y0"], x - e["x0"]
            if 0 <= r < e["h"] and 0 <= c < e["w"]:
                return e["terrain"][r][c]
        return "#"

    def o(self, x, y):
        r, c = y - self.y0, x - self.x0
        if 0 <= r < self.h and 0 <= c < self.w:
            return self.ore[r][c]
        for e in self.ext:
            r, c = y - e["y0"], x - e["x0"]
            if 0 <= r < e["h"] and 0 <= c < e["w"]:
                return e["ore"][r][c]
        return "."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--radius", type=int, default=256)
    a = ap.parse_args()
    t = time.time()
    d = extract(a.seed, a.radius)
    (out_dir(a.seed) / "map.json").write_text(json.dumps(d))
    n = {k: sum(r.count(k) for r in d["terrain"]) for k in ".~tC"}
    print(f"seed {a.seed}: {d['w']}x{d['h']} tiles, terrain {n}, ore {d['amount']}, wells {len(d['wells'])}, "
          f"{time.time() - t:.0f}s")


if __name__ == "__main__":
    main()


def pump_sites(seed, radius=256):
    """Offshore pump spots the game accepts (manual build check), with the direction that puts its output on land."""
    name = f"auto{seed}"
    out = q("local s=game.surfaces['%s'] local o={} local seen={} for _,t in pairs(s.find_tiles_filtered{area={{%d,%d},{%d,%d}},collision_mask='water_tile'}) do "
            "for dx=-1,1 do for dy=-1,1 do local x,y=t.position.x+dx,t.position.y+dy local k=x..','..y if not seen[k] then seen[k]=true "
            "for _,d in pairs({'north','east','south','west'}) do if s.can_place_entity{name='offshore-pump',position={x+0.5,y+0.5},direction=defines.direction[d],force='player',build_check_type=defines.build_check_type.manual} then "
            "o[#o+1]={x+0.5,y+0.5,d} end end end end end end rcon.print(helpers.table_to_json(o))"
            % (name, -radius, -radius, radius, radius))
    return json.loads(out or "[]")
