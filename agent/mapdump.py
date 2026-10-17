"""Serializable snapshot of the whole factory: the data form of a screenshot.

    .venv/Scripts/python mapdump.py            -> runtime/map/snapshot.json.gz + snapshot.png

snapshot.json:
  tick, bbox [x1,y1,x2,y2]
  entities[]  n name, t type, x, y, d direction, r recipe, s status, plus by type:
              belts L1/L2 {item: n} per lane, ug in|out + p partner; inserters pu/dr pickup/drop;
              machines/chests inv {item: n}; fluid boxes f [[fluid, amount, segment]]; poles net
  raster      one string per row over bbox: . free  ~ water  i/c/s/k/u ore (iron/copper/stone/coal/oil)
  graphs      belt edges (tile -> next tile, incl. undergrounds and splitters), pipe segments,
              electric networks: built with networkx and summarised (components, sizes)
Rendering uses Pillow (3 px per tile).
"""
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "map")

CHUNKS_LUA = ("/sc local s=game.surfaces[1] local o={} for ch in s.get_chunks() do "
              "if s.count_entities_filtered{area=ch.area,force='player'}>0 then o[#o+1]=ch.x..','..ch.y end end "
              "rcon.print(table.concat(o,';'))")

ENT_LUA = "/sc " + r"""local s=game.surfaces[1] local cx,cy=%d,%d local A={{cx*32,cy*32},{cx*32+32,cy*32+32}}
local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local DN={[0]='N',[4]='E',[8]='S',[12]='W'}
local out={}
local function inv(e)
  local r={} local any=false
  for i=1,(e.get_max_inventory_index() or 0) do local v=e.get_inventory(i) if v and v.valid then
    for _,it in pairs(v.get_contents()) do r[it.name]=(r[it.name] or 0)+it.count any=true end end end
  return any and r or nil
end
for _,e in pairs(s.find_entities_filtered{area=A,force='player'}) do
  local p=e.position
  if p.x>=A[1][1] and p.x<A[2][1] and p.y>=A[1][2] and p.y<A[2][2] and e.type~='character' then
    local v={n=e.name,t=e.type,x=p.x,y=p.y}
    if e.supports_direction then v.d=DN[e.direction] end
    local ok,st=pcall(function() return e.status end) if ok and st then v.s=SN[st] end
    local t=e.type
    if t=='assembling-machine' or t=='furnace' then local r=e.get_recipe() v.r=r and r.name or nil v.inv=inv(e)
    elseif t=='container' or t=='logistic-container' or t=='lab' or t=='mining-drill' or t=='rocket-silo' then v.inv=inv(e)
    elseif t=='transport-belt' or t=='underground-belt' or t=='splitter' then
      for i=1,math.min(2,e.get_max_transport_line_index()) do local c={} local any=false
        for _,it in pairs(e.get_transport_line(i).get_contents()) do c[it.name]=it.count any=true end
        if any then v['L'..i]=c end end
      if t=='underground-belt' then v.ug=e.belt_to_ground_type local nb=e.neighbours if nb then v.p={nb.position.x,nb.position.y} end end
    elseif t=='inserter' then v.pu={e.pickup_position.x,e.pickup_position.y} v.dr={e.drop_position.x,e.drop_position.y}
    elseif t=='electric-pole' then local ok2,id=pcall(function() return e.electric_network_id end) if ok2 then v.net=id end end
    local okf,fb=pcall(function() return e.fluidbox end)
    if okf and fb and #fb>0 then local f={} for i=1,#fb do local x=fb[i] local seg=nil
      pcall(function() seg=fb.get_fluid_segment_id(i) end)
      f[#f+1]={x and x.name or nil, x and math.floor(x.amount*10)/10 or 0, seg} end v.f=f end
    out[#out+1]=v
  end
end
rcon.print(helpers.table_to_json(out))
"""

RASTER_LUA = "/sc " + r"""local s=game.surfaces[1] local x1,y1,x2,y2=%d,%d,%d,%d local W=x2-x1
local g={} for i=1,W*(y2-y1) do g[i]=46 end
local C={['iron-ore']=105,['copper-ore']=99,['stone']=115,['coal']=107,['crude-oil']=117,['uranium-ore']=85}
for _,t in pairs(s.find_tiles_filtered{area={{x1,y1},{x2,y2}},collision_mask='water_tile'}) do
  g[(t.position.y-y1)*W+(t.position.x-x1)+1]=126 end
for _,r in pairs(s.find_entities_filtered{area={{x1,y1},{x2,y2}},type='resource'}) do
  local x,y=math.floor(r.position.x),math.floor(r.position.y)
  if x>=x1 and x<x2 and y>=y1 and y<y2 then g[(y-y1)*W+(x-x1)+1]=C[r.name] or 111 end end
local rows={} for y=0,y2-y1-1 do local r={} for x=1,W do r[x]=string.char(g[y*W+x]) end rows[#rows+1]=table.concat(r) end
rcon.print(table.concat(rows,'\n'))
"""


def _one_line(lua):
    """Factorio reads '/sc' plus the rest of the line as the command: send the chunk as one line."""
    return " ".join(line.strip() for line in lua.splitlines() if line.strip())


def dump(b):
    chunks = [tuple(int(v) for v in c.split(",")) for c in b.raw(CHUNKS_LUA).strip().split(";") if c]
    ents = []
    for cx, cy in chunks:
        ents += json.loads(b.raw(_one_line(ENT_LUA % (cx, cy))) or "[]")
    xs = [c[0] for c in chunks]
    ys = [c[1] for c in chunks]
    bbox = [min(xs) * 32, min(ys) * 32, max(xs) * 32 + 32, max(ys) * 32 + 32]
    raster = []
    for y in range(bbox[1], bbox[3], 32):  # raster in 32-row bands
        raster += b.raw(_one_line(RASTER_LUA % (bbox[0], y, bbox[2], y + 32))).splitlines()
    tick = b.call("ping").get("tick")
    return {"tick": tick, "time": int(time.time()), "bbox": bbox, "entities": ents, "raster": raster}


DV = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}


def graphs(snap):
    """Belt flow graph, pipe segments and electric networks (networkx)."""
    import networkx as nx
    belt = nx.DiGraph()
    tiles = {}
    for e in snap["entities"]:
        if e["t"] in ("transport-belt", "underground-belt"):
            tiles[(e["x"], e["y"])] = e
            belt.add_node((e["x"], e["y"]))
        elif e["t"] == "splitter":  # a splitter covers two tiles across its direction
            dx, dy = DV.get(e.get("d"), (0, 0))
            for s in (-0.5, 0.5):
                t = (e["x"] + s * abs(dy), e["y"] + s * abs(dx))
                tiles[t] = e
                belt.add_node(t)
    for (x, y), e in tiles.items():
        d = DV.get(e.get("d"))
        if not d:
            continue
        if e["t"] == "underground-belt" and e.get("ug") == "input" and e.get("p"):
            belt.add_edge((x, y), tuple(e["p"]))
            continue
        nxt = (x + d[0], y + d[1])
        if nxt in tiles:
            belt.add_edge((x, y), nxt)
    pipes = {}
    for e in snap["entities"]:
        for f in e.get("f") or []:
            if len(f) > 2 and f[2] is not None:
                pipes.setdefault(f[2], {"fluid": f[0], "entities": 0})
                pipes[f[2]]["entities"] += 1
                pipes[f[2]]["fluid"] = pipes[f[2]]["fluid"] or f[0]
    power = {}
    for e in snap["entities"]:
        if e["t"] == "electric-pole" and e.get("net") is not None:
            power[e["net"]] = power.get(e["net"], 0) + 1
    comps = list(nx.weakly_connected_components(belt))
    return belt, {"belt_lines": len(comps), "belt_tiles": belt.number_of_nodes(),
                  "fluid_segments": {str(k): v for k, v in pipes.items()},
                  "electric_networks": {str(k): v for k, v in power.items()}}


COLORS = {".": (40, 40, 40), "~": (30, 70, 140), "i": (90, 110, 130), "c": (160, 90, 50), "s": (140, 130, 100),
          "k": (20, 20, 20), "u": (110, 40, 110), "o": (90, 90, 90)}
ENT_COLOR = {"transport-belt": (230, 190, 40), "underground-belt": (200, 160, 30), "splitter": (240, 210, 60),
             "inserter": (220, 220, 220), "assembling-machine": (110, 140, 160), "furnace": (200, 120, 60),
             "mining-drill": (120, 160, 90), "electric-pole": (150, 100, 60), "pipe": (90, 120, 200),
             "pipe-to-ground": (70, 100, 180), "lab": (220, 230, 240), "container": (170, 120, 80),
             "boiler": (210, 80, 60), "generator": (180, 180, 60), "storage-tank": (80, 110, 190)}
SIZE = {"assembling-machine": 3, "furnace": 2, "mining-drill": 3, "lab": 3, "boiler": 3, "generator": 3,
        "storage-tank": 3, "oil-refinery": 5, "chemical-plant": 3, "offshore-pump": 1, "rocket-silo": 9}


def render(snap, path, px=3):
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = snap["bbox"]
    img = Image.new("RGB", ((x2 - x1) * px, (y2 - y1) * px), COLORS["."])
    dr = ImageDraw.Draw(img)
    for j, row in enumerate(snap["raster"]):
        for i, ch in enumerate(row):
            if ch != ".":
                dr.rectangle([i * px, j * px, i * px + px - 1, j * px + px - 1], fill=COLORS.get(ch, (80, 80, 80)))
    for e in snap["entities"]:
        t = e["t"]
        size = SIZE.get(e["n"], SIZE.get(t, 1))
        if e["n"] == "electric-furnace":
            size = 3
        col = ENT_COLOR.get(t, (160, 160, 160))
        if e.get("s") in ("no_power", "no_fuel", "item_ingredient_shortage", "fluid_ingredient_shortage"):
            col = (200, 60, 60)  # starved or unpowered machines in red
        h = size / 2
        X0, Y0 = (e["x"] - h - x1) * px, (e["y"] - h - y1) * px
        dr.rectangle([X0, Y0, X0 + size * px - 1, Y0 + size * px - 1], fill=col)
    img.save(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    b = Bridge("planner", autostart=False)
    t0 = time.time()
    snap = dump(b)
    _, summary = graphs(snap)
    snap["summary"] = summary
    p = os.path.join(OUT, "snapshot.json.gz")
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    render(snap, os.path.join(OUT, "snapshot.png"))
    print(f"{len(snap['entities'])} entities, bbox {snap['bbox']}, {summary['belt_lines']} belt lines, "
          f"{len(summary['fluid_segments'])} fluid segments, {len(summary['electric_networks'])} electric networks; "
          f"{os.path.getsize(p) // 1024} KB gz, {time.time() - t0:.1f}s -> runtime/map/")


if __name__ == "__main__":
    main()
