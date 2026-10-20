import rcon,json;r=rcon.Rcon('127.0.0.1',27115,'scratchpw')
L=r'''local S=game.surfaces['verify1060'] local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local out={}
for _,e in pairs(S.find_entities_filtered{force='player',type={'assembling-machine','furnace','rocket-silo'}}) do
  local st=SN[e.status] or '?'
  if st~='working' and e.get_recipe() then
    local rc=e.get_recipe().name local miss={}
    local inv=e.get_inventory(defines.inventory.assembling_machine_input) or e.get_inventory(defines.inventory.furnace_source)
    for _,ing in pairs(e.get_recipe().ingredients) do
      if ing.type=='item' then if inv.get_item_count(ing.name)<ing.amount then miss[#miss+1]=ing.name end
      else local have=0 for i=1,#e.fluidbox do local f=e.fluidbox[i] if f and f.name==ing.name then have=have+f.amount end end if have<ing.amount then miss[#miss+1]=ing.name end end
    end
    local k=rc..'|'..st..'|'..table.concat(miss,'+') out[k]=(out[k] or 0)+1
  end
end
rcon.print(helpers.table_to_json(out))'''
o=json.loads(r.command('/silent-command '+L))
for k,v in sorted(o.items()): print(v,k)
