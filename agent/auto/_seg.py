import rcon,json;r=rcon.Rcon('127.0.0.1',27115,'scratchpw')
L=r'''local S=game.surfaces['verify1060']
local PIPE={pipe=1,['pipe-to-ground']=1,pump=1,['storage-tank']=1}
local seen={} local o={}
local function lbl(e) return (e.type=='assembling-machine' and e.get_recipe() and e.get_recipe().name) or e.name end
for _,e in pairs(S.find_entities_filtered{force='player',type={'assembling-machine','offshore-pump','mining-drill'}}) do
 if e.fluidbox and #e.fluidbox>0 and e.type~='boiler' then
  for i=1,#e.fluidbox do
   local f=e.fluidbox.get_filter(i) local fname=f and f.name or '?'
   local start={} for _,c in pairs(e.fluidbox.get_connections(i)) do if PIPE[c.owner.type] then start[#start+1]=c.owner end end
   if #start>0 and not seen[start[1].unit_number] then
    local q,qi,mach,n,ends=start,1,{},0,{}
    for _,s in pairs(start) do seen[s.unit_number]=true end
    while qi<=#q do local p=q[qi] qi=qi+1 n=n+1 local deg=0
     for _,c in pairs(p.fluidbox.get_connections(1)) do local w=c.owner deg=deg+1
      if PIPE[w.type] then if not seen[w.unit_number] then seen[w.unit_number]=true q[#q+1]=w end
      else local k=lbl(w) mach[k]=(mach[k] or 0)+1 end end
     if deg<=1 and p.type~='storage-tank' then ends[#ends+1]=p.name..'@'..p.position.x..','..p.position.y end
    end
    local parts={} for k,v in pairs(mach) do parts[#parts+1]=k..'x'..v end table.sort(parts)
    o[#o+1]=fname..' from '..lbl(e)..' pipes='..n..' :: '..table.concat(parts,', ')..' | deadends '..table.concat(ends,' ',1,math.min(#ends,4))
   end
  end end end
rcon.print(table.concat(o,'\n'))'''
print(r.command('/silent-command '+L))
