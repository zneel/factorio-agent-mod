import rcon,json;r=rcon.Rcon('127.0.0.1',27115,'scratchpw')
L=r'''local S=game.surfaces['verify1060']
local PIPE={pipe=1,['pipe-to-ground']=1,pump=1,['storage-tank']=1}
local lab={} local q={} local src={}
local function nm(e) return (e.type=='assembling-machine' and e.get_recipe() and e.get_recipe().name) or e.name end
for _,e in pairs(S.find_entities_filtered{force='player',type={'assembling-machine','offshore-pump','mining-drill','rocket-silo'}}) do
 if e.fluidbox and #e.fluidbox>0 then for i=1,#e.fluidbox do local f=e.fluidbox.get_filter(i)
  if f then for _,c in pairs(e.fluidbox.get_connections(i)) do local p=c.owner if PIPE[p.type] then
   if lab[p.unit_number] and lab[p.unit_number]~=f.name then rcon.print('PORTCLASH '..f.name..'/'..lab[p.unit_number]..' at '..p.position.x..','..p.position.y..' by '..nm(e))
   elseif not lab[p.unit_number] then lab[p.unit_number]=f.name src[p.unit_number]=nm(e) q[#q+1]=p end end end end end end end
local qi=1 local clash={} local nc=0
while qi<=#q do local p=q[qi] qi=qi+1 local L=lab[p.unit_number]
 for _,c in pairs(p.fluidbox.get_connections(1)) do local w=c.owner if PIPE[w.type] then
  local l2=lab[w.unit_number]
  if not l2 then lab[w.unit_number]=L src[w.unit_number]=src[p.unit_number] q[#q+1]=w
  elseif l2~=L then local k=math.floor(p.position.x)..','..math.floor(p.position.y) if not clash[k] then clash[k]=1 nc=nc+1 if nc<=12 then rcon.print('MIX '..L..'('..src[p.unit_number]..') meets '..l2..'('..src[w.unit_number]..') at '..p.name..'@'..p.position.x..','..p.position.y..' / '..w.name..'@'..w.position.x..','..w.position.y) end end end end end end
rcon.print('clashes '..nc)'''
print(r.command('/silent-command '+L))
