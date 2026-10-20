import json,sys,math
from auto import verify
from auto.site import rot_tile
p=json.load(open('../runtime/auto/1060/plan.json'))
import pickle
cx=pickle.load(open('../runtime/auto/1060/complex.pkl','rb'))
item=sys.argv[1]; col=cx['bus']['columns'][item]
k,(dx,dy)=p['rotation'],p['offset']
pts=[]
for y in range(0,cx['bus']['length'],6):
    w=rot_tile((col,y),k); pts.append((y,w[0]+dx+.5,w[1]+dy+.5))
L='''local S=game.surfaces['verify1060'] local o={}
for _,q in pairs(%s) do local b=S.find_entities_filtered{position={q[2],q[3]},type={'transport-belt','underground-belt','splitter'},limit=1}[1]
 local s='-' if b then local c={} for l=1,2 do for _,it in pairs(b.get_transport_line(l).get_contents()) do c[#c+1]=l..it.name:sub(1,10)..it.count end end s=table.concat(c,' ') end
 o[#o+1]=q[1]..':'..s end rcon.print(table.concat(o,' | '))''' % ("{"+",".join("{%d,%s,%s}"%q for q in pts)+"}")
print(verify.q(L.replace('\n',' ')).replace(' | ','\n'))
for bd in cx['bus']['bands']:
    if item in bd['items']: print('band',bd['module'],bd['kind'],bd['y'])
