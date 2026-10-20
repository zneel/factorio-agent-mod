import rcon,json;r=rcon.Rcon('127.0.0.1',27115,'scratchpw')
L=r'''local S=game.surfaces['verify1060'] local o={}
for _,e in pairs(S.find_entities_filtered{type={'assembling-machine','furnace'}}) do
 local rc=e.get_recipe() if rc and (rc.name:find('crack') or rc.name=='sulfur' or rc.name=='lubricant' or rc.name=='plastic-bar' or rc.name=='sulfuric-acid' or rc.name=='solid-fuel-from-light-oil') then
  local fb={} for i=1,#e.fluidbox do local f=e.fluidbox[i] local con=e.fluidbox.get_connections(i) local pc=e.fluidbox.get_pipe_connections(i)
   local ps={} for _,c in pairs(pc) do ps[#ps+1]=c.flow_direction..'@'..c.target_position.x..','..c.target_position.y..(c.target and ('>'..c.target.owner.name) or '>none') end
   fb[#fb+1]=(e.fluidbox.get_filter(i) and e.fluidbox.get_filter(i).name or '?')..'='..(f and math.floor(f.amount) or 0)..' ['..table.concat(ps,' ')..']' end
  o[#o+1]=rc.name..'@'..e.position.x..','..e.position.y..' '..e.direction..' :: '..table.concat(fb,' | ')
 end end
for _,e in pairs(S.find_entities_filtered{name='oil-refinery'}) do
  local fb={} for i=1,#e.fluidbox do local f=e.fluidbox[i] local pc=e.fluidbox.get_pipe_connections(i)
   local ps={} for _,c in pairs(pc) do ps[#ps+1]=c.target_position.x..','..c.target_position.y..(c.target and ('>'..c.target.owner.name) or '>none') end
   fb[#fb+1]=(f and (f.name..'='..math.floor(f.amount)) or 'empty')..' ['..table.concat(ps,' ')..']' end
  o[#o+1]='refinery@'..e.position.x..','..e.position.y..' :: '..table.concat(fb,' | ') end
rcon.print(table.concat(o,'\n'))'''
print(r.command('/silent-command '+L))
