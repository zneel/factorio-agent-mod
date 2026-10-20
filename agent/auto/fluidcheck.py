"""Debug: walk fluid connections from each pumpjack / offshore pump on the verify surface."""
from auto import verify

LUA = r"""
local S=game.surfaces['verify1060'] local out={}
for _,src in pairs(S.find_entities_filtered{name={'pumpjack','offshore-pump'}}) do
  local seen={} local q={src} seen[src.unit_number]=true local n=0 local found={} local dead={}
  while #q>0 and n<8000 do
    local e=table.remove(q,1) n=n+1
    if e.type~='pipe' and e.type~='pipe-to-ground' and e~=src then found[e.name]=(found[e.name] or 0)+1 end
    local k=0
    for i=1,#e.fluidbox do for _,c in pairs(e.fluidbox.get_connections(i)) do k=k+1 local o=c.owner if o and not seen[o.unit_number] then seen[o.unit_number]=true q[#q+1]=o end end end
    if k<=1 and (e.type=='pipe' or e.type=='pipe-to-ground') then dead[#dead+1]=e.name..'@'..e.position.x..','..e.position.y end
  end
  local f={} for k,v in pairs(found) do f[#f+1]=k..'='..v end
  out[#out+1]=string.format('%s %.1f,%.1f: %d in network; reaches %s; dead ends %s', src.name, src.position.x,src.position.y,n,table.concat(f,','),table.concat(dead,' '))
end
rcon.print(table.concat(out,' | '))
"""


def run():
    return verify.q(LUA.replace("\n", " ")).split(" | ")


if __name__ == "__main__":
    for l in run():
        print(l[:600])
