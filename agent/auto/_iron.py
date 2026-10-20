import rcon,json;r=rcon.Rcon('127.0.0.1',27115,'scratchpw')
L=r'''local S=game.surfaces['verify1060'] local o={}
for _,e in pairs(S.find_entities_filtered{type='assembling-machine'}) do
 local rc=e.get_recipe() if rc and e.status==defines.entity_status.item_ingredient_shortage then
  local inv=e.get_inventory(defines.inventory.assembling_machine_input)
  for _,ing in pairs(rc.ingredients) do if ing.name=='iron-plate' and inv.get_item_count('iron-plate')<ing.amount then
   local feeds={}
   for _,ins in pairs(S.find_entities_filtered{type='inserter',area={{e.position.x-3,e.position.y-3},{e.position.x+3,e.position.y+3}}}) do
     if ins.drop_target==e then local pt=ins.pickup_target local what='?'
      if pt and pt.type=='transport-belt' or (pt and pt.type=='underground-belt') then local c={} for l=1,2 do for k,v in pairs(pt.get_transport_line(l).get_contents()) do c[#c+1]=(v.name or k)..':'..(v.count or v) end end what=pt.name..'@'..pt.position.x..','..pt.position.y..'['..table.concat(c,' ')..']'
      elseif pt then what=pt.name end
      feeds[#feeds+1]=what end end
   o[#o+1]=rc.name..'@'..e.position.x..','..e.position.y..' feeds: '..table.concat(feeds,' ; ')
  end end end end
rcon.print(table.concat(o,'\n'))'''
print(r.command('/silent-command '+L))
