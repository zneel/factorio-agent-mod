"""Fluid port offsets of (entity, recipe, direction), measured on the scratch server once and cached.

ports(entity, recipe, direction) -> [(fluid or None, "input"|"output"|"input-output", dx, dy)]
dx, dy: offset of the tile where a pipe must sit, from the entity centre.
"""
import json

from auto.mapdata import ROOT, q

CACHE = ROOT / "runtime" / "auto" / "ports.json"
_c = None

# a force of its own: researching everything on the player force would change the game being played
LUA = ("local S=game.surfaces[1] local F=game.forces['portprobe'] or game.create_force('portprobe') "
       "for _,t in pairs(F.technologies) do t.researched=true end "
       "local e=S.create_entity{name=%s,position={1000.5,1000.5},direction=defines.direction[%s],force=F%s} "
       "local o={} for i=1,#e.fluidbox do local f=e.fluidbox.get_filter(i) for _,c in pairs(e.fluidbox.get_pipe_connections(i)) do "
       "o[#o+1]={f and f.name or false,c.flow_direction,c.target_position.x-e.position.x,c.target_position.y-e.position.y} end end "
       "e.destroy() rcon.print(helpers.table_to_json(o))")


def ports(entity, recipe, direction):
    global _c
    if _c is None:
        _c = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    k = f"{entity}|{recipe}|{direction}"
    if k not in _c:
        q("rcon.print(1)")
        rec = f",recipe={json.dumps(recipe)}" if recipe else ""
        out = q(LUA % (json.dumps(entity), json.dumps(direction), rec))
        _c[k] = [[f or None, d, dx, dy] for f, d, dx, dy in json.loads(out or "[]")]
        CACHE.write_text(json.dumps(_c))
    return _c[k]
