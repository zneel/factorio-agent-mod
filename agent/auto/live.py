"""What stands in the running game: entities, machine statuses, the power network, research, stock.

All reads are one Lua call per 128x128 area (q(lua) runs `/silent-command` on the target server) and come back
in compact text, so a whole base reads in a few calls.
"""
import json

SCAN_LUA = r"""
local S=game.surfaces[1] local o={}
local dn={} for k,v in pairs(defines.direction) do dn[v]=k end
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},force='player'}) do
  if e.type~='character' then
    local bt='' if e.type=='underground-belt' then bt=e.belt_to_ground_type end
    o[#o+1]=e.name..'|'..e.position.x..'|'..e.position.y..'|'..(dn[e.direction] or '')..'|'..bt
  end
end
rcon.print(table.concat(o,';'))
"""

OBST_LUA = r"""
local S=game.surfaces[1] local o={}
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},type={'tree','simple-entity','cliff'}}) do
  local b=e.bounding_box
  o[#o+1]=(e.type=='cliff' and 'C' or 't')..'|'..b.left_top.x..'|'..b.left_top.y..'|'..b.right_bottom.x..'|'..b.right_bottom.y
end
rcon.print(table.concat(o,';'))
"""

STATUS_LUA = r"""
local S=game.surfaces[1] local o={} local sn={} for k,v in pairs(defines.entity_status) do sn[v]=k end
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},force='player',
    type={'assembling-machine','furnace','mining-drill','lab','boiler','generator','offshore-pump','rocket-silo','inserter'}}) do
  o[#o+1]=e.name..'|'..e.position.x..'|'..e.position.y..'|'..(sn[e.status or 0] or 'none')
end
rcon.print(table.concat(o,';'))
"""

NET_LUA = r"""
local S=game.surfaces[1] local ids={} local o={poles={},engines=0}
for _,e in pairs(S.find_entities_filtered{type='generator',force='player'}) do
  o.engines=o.engines+1 if e.electric_network_id then ids[e.electric_network_id]=true end end
for _,p in pairs(S.find_entities_filtered{type='electric-pole',force='player'}) do
  o.poles[#o.poles+1]={p.position.x,p.position.y,ids[p.electric_network_id] and 1 or 0,p.name} end
rcon.print(helpers.table_to_json(o))
"""

POWER_LUA = r"""
local S=game.surfaces[1] local o={cap=0,use=0}
for _,e in pairs(S.find_entities_filtered{type='generator',force='player'}) do
  local fb=e.fluidbox[1]
  if fb and fb.amount>0 then o.cap=o.cap+e.prototype.get_max_power_output(e.quality)*60 end
  o.use=o.use+(e.energy_generated_last_tick or 0)*60 end
rcon.print(helpers.table_to_json(o))
"""


def _areas(box, step=128):
    x1, y1, x2, y2 = box
    for x in range(int(x1) - 1, int(x2) + 2, step):
        for y in range(int(y1) - 1, int(y2) + 2, step):
            yield x, y, min(x + step, int(x2) + 2), min(y + step, int(y2) + 2)


def _run(q, lua, box):
    out = []
    for a in _areas(box):
        r = q("/silent-command " + (lua % a).replace("\n", " ")) or ""
        out += [t.split("|") for t in r.strip().split(";") if t]
    return out


def scan(q, box):
    """Player entities in box: [(name, x, y, direction, belt_type)]; an area seam can list one twice (deduped)."""
    seen = {}
    for n, x, y, d, bt in _run(q, SCAN_LUA, box):
        seen[(n, float(x), float(y))] = (n, float(x), float(y), d, bt)
    return list(seen.values())


def obstacles(q, box):
    """Tiles under trees/rocks ('t', removable) and cliffs ('C')."""
    out = {}
    for k, x1, y1, x2, y2 in _run(q, OBST_LUA, box):
        import math
        for tx in range(math.floor(float(x1)), math.ceil(float(x2))):
            for ty in range(math.floor(float(y1)), math.ceil(float(y2))):
                if out.get((tx, ty)) != "C":
                    out[(tx, ty)] = k
    return out


def status(q, box):
    """[(name, x, y, status)] for machines, drills, labs, boilers, engines, pumps, inserters."""
    return [(n, float(x), float(y), s) for n, x, y, s in _run(q, STATUS_LUA, box)]


def network(q):
    """Poles and whether each is on the steam engines' network: {"poles": [[x, y, on]], "engines": n}."""
    return json.loads(q("/silent-command " + NET_LUA.replace("\n", " ")))


def power(q):
    """Generator capacity and output (W)."""
    return json.loads(q("/silent-command " + POWER_LUA.replace("\n", " ")))


def researched(q):
    lua = ("local o={} for n,t in pairs(game.forces.player.technologies) do if t.researched then o[#o+1]=n end end "
           "rcon.print(helpers.table_to_json(o))")
    return set(json.loads(q("/silent-command " + lua)) or [])


BELT_LUA = r"""
local S=game.surfaces[1] local o={}
for _,p in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={p[1],p[2]},type={'transport-belt','underground-belt','splitter'}}[1]
  local r={} if e then for k=1,e.get_max_transport_line_index() do
    for _,it in pairs(e.get_transport_line(k).get_contents()) do r[it.name]=(r[it.name] or 0)+it.count end end end
  o[#o+1]=r
end
rcon.print(helpers.table_to_json(o))
"""


def belt_items(q, positions):
    """Items on each belt at positions [(x, y)] -> [{item: count}] (all lanes)."""
    out = []
    for i in range(0, len(positions), 200):
        chunk = [[x, y] for x, y in positions[i:i + 200]]
        r = json.loads(q("/silent-command " + (BELT_LUA % json.dumps(chunk)).replace(chr(10), " ")) or "[]")
        out += [d if isinstance(d, dict) else {} for d in r]
    return out


LANES_LUA = r"""
local S=game.surfaces[1] local o={}
for _,p in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={p[1],p[2]},type='transport-belt'}[1]
  local r={{},{}} if e then for k=1,2 do
    for _,it in pairs(e.get_transport_line(k).get_contents()) do r[k][it.name]=it.count end end end
  o[#o+1]=r
end
rcon.print(helpers.table_to_json(o))
"""


PARTNER_LUA = r"""
local S=game.surfaces[1] local o={}
for _,p in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={p[1],p[2]},type='underground-belt'}[1]
  local n=e and e.neighbours
  if not e then o[#o+1]=false elseif n then o[#o+1]={n.position.x,n.position.y} else o[#o+1]=0 end
end
rcon.print(helpers.table_to_json(o))
"""


def underground_partners(q, positions):
    """Per (x, y): the (x, y) of the underground-belt the game paired with the one standing there (entity.neighbours),
    None when it stands unpaired, False when no underground-belt stands there. The game pairs by proximity, not by
    plan: a new piece on the same axis can steal a planned partner."""
    out = []
    for i in range(0, len(positions), 200):
        chunk = [[x, y] for x, y in positions[i:i + 200]]
        r = json.loads(q("/silent-command " + (PARTNER_LUA % json.dumps(chunk)).replace(chr(10), " ")) or "[]")
        out += [tuple(d) if isinstance(d, list) else (False if d is False else None) for d in r]   # False == 0
    return out


UG_LUA = r"""
local S=game.surfaces[1] local o={}
local dn={} for k,v in pairs(defines.direction) do dn[v]=k end
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},type='underground-belt',force='player'}) do
  local n=e.neighbours
  o[#o+1]=e.position.x..'|'..e.position.y..'|'..dn[e.direction]..'|'..e.belt_to_ground_type..'|'..
    (n and (n.position.x..'|'..n.position.y) or '|')
end
rcon.print(table.concat(o,';'))
"""


def undergrounds(q, box):
    """Underground belts standing in box with the game's pairing: [(x, y, direction, belt_type, (px, py) | None)]."""
    seen = {}
    for x, y, d, bt, px, py in _run(q, UG_LUA, box):
        seen[(float(x), float(y))] = (float(x), float(y), d, bt, (float(px), float(py)) if px else None)
    return list(seen.values())


def belt_lanes(q, positions):
    """Per belt at positions: [{item: count} lane 1, {item: count} lane 2]."""
    out = []
    for i in range(0, len(positions), 200):
        chunk = [[x, y] for x, y in positions[i:i + 200]]
        r = json.loads(q("/silent-command " + (LANES_LUA % json.dumps(chunk)).replace(chr(10), " ")) or "[]")
        for d in r:
            d = d if isinstance(d, list) else [{}, {}]
            out.append([x if isinstance(x, dict) else {} for x in (d + [{}, {}])[:2]])
    return out
