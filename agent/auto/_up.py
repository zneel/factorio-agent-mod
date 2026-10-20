import json,sys
from auto import verify
x,y=float(sys.argv[1]),float(sys.argv[2])
L='''local S=game.surfaces['verify1060'] local o={}
local function show(b) local c={} for l=1,2 do for _,it in pairs(b.get_transport_line(l).get_contents()) do c[#c+1]=l..it.name:sub(1,10)..it.count end end return b.type:sub(1,5)..'@'..b.position.x..','..b.position.y..' d'..b.direction..' ['..table.concat(c,' ')..']' end
local cur=S.find_entities_filtered{position={%s,%s},type={'transport-belt','underground-belt','splitter'},limit=1}[1]
for n=1,600 do local ins=cur.belt_neighbours.inputs local nx=nil
 if cur.type=='underground-belt' and cur.belt_to_ground_type=='output' then nx=cur.neighbours
 elseif #ins>0 then nx=ins[1] for _,i in pairs(ins) do if i.direction==cur.direction then nx=i end end
   if #ins>1 or (#ins==1 and ins[1].direction~=cur.direction and cur.type=='transport-belt') then local s={} for _,i in pairs(ins) do s[#s+1]=show(i) end o[#o+1]=n..' JOIN at '..show(cur)..' <= '..table.concat(s,' ; ') end end
 if n%%25==0 then o[#o+1]=n..' '..show(cur) end
 if not nx then o[#o+1]='START '..show(cur) break end cur=nx end
rcon.print(table.concat(o,' | '))''' % (x,y)
print(verify.q(L.replace('\n',' ')).replace(' | ','\n'))
