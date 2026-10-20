import sys,json
from auto import verify, trace
rc=sys.argv[1]
L=r'''local S=game.surfaces['verify1060'] local o={}
for _,e in pairs(S.find_entities_filtered{type='assembling-machine'}) do if e.get_recipe() and e.get_recipe().name=='%s' then
 for _,ins in pairs(S.find_entities_filtered{type='inserter',area={{e.position.x-3,e.position.y-3},{e.position.x+3,e.position.y+3}}}) do
  if ins.pickup_target==e then local d=ins.drop_target o[#o+1]={e.position.x,e.position.y,ins.drop_position.x,ins.drop_position.y,d and d.name or 'ground'} end end end end
rcon.print(helpers.table_to_json(o))''' % rc
o=json.loads(verify.q(L.replace("\n"," ")))
for m in o: print(m)
pts=[(m[2],m[3]) for m in o if 'belt' in m[4]]
for t in trace.trace(pts): print('  ',t)
