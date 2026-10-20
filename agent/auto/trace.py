"""Debug helper: follow belt lines on the verify surface (through undergrounds and splitters)."""
import sys
from auto import verify

TRACE = r"""
local S=game.surfaces['%s']
local function nxt(cur)
  if cur.type=='underground-belt' and cur.belt_to_ground_type=='input' then return cur.neighbours end
  return cur.belt_neighbours.outputs[1]
end
local out={}
for _,pt in pairs(%s) do
  local b=S.find_entities_filtered{position=pt,type={'transport-belt','underground-belt','splitter'},limit=1}[1]
  if b then
    local cur=b local n=0
    while true do local x=nxt(cur) if not x or n>2000 then break end cur=x n=n+1 end
    local c={} for l=1,2 do for _,it in pairs(cur.get_transport_line(l).get_contents()) do c[#c+1]=l..it.name..it.count end end
    local dv=({[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}})[cur.direction]
    local ah={} for _,a in pairs(S.find_entities_filtered{position={cur.position.x+dv[1],cur.position.y+dv[2]},radius=0.45}) do if a.type~='resource' then ah[#ah+1]=a.name end end
    out[#out+1]=string.format('%%.1f,%%.1f -> %%s %%.1f,%%.1f dir %%d after %%d [%%s] ahead:%%s',pt[1],pt[2],cur.name,cur.position.x,cur.position.y,cur.direction,n,table.concat(c,' '),table.concat(ah,'/'))
  end
end
rcon.print(table.concat(out,' | '))
"""


def trace(points, surface="verify1060"):
    pts = "{" + ",".join("{%s,%s}" % p for p in points) + "}"
    return verify.q((TRACE % (surface, pts)).replace("\n", " ")).split(" | ")
