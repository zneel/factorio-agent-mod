import json,math,sys
from auto import geom
x1,y1,x2,y2=map(int,sys.argv[1:5])
p=json.load(open('../runtime/auto/1060/plan.json'))
pl=p['complex']+p['world']
G={}
A={'pipe':'+','pipe-to-ground':None,'chemical-plant':'C','oil-refinery':'R','small-electric-pole':'e','transport-belt':'b','underground-belt':'u','inserter':'i','assembling-machine-2':'A','storage-tank':'T','pump':'P','offshore-pump':'O','pumpjack':'J','splitter':'s','long-handed-inserter':'l','fast-inserter':'f'}
D={'north':'^','south':'v','east':'>','west':'<'}
for q in pl:
    if q.get('remove'): continue
    for t in geom.footprint(q):
        if x1<=t[0]<x2 and y1<=t[1]<y2:
            c=A.get(q['item'],'?')
            if q['item']=='pipe-to-ground': c={'north':'n','south':'s','east':'e','west':'w'}[q.get('direction','north')]
            if q['item'] in('chemical-plant','oil-refinery','assembling-machine-2') and t==(math.floor(q['x']),math.floor(q['y'])): c=(q.get('recipe') or '?')[0].upper()
            G[t]=c
print('    '+''.join(str(abs(x)%10) for x in range(x1,x2)))
for y in range(y1,y2):
    print(f'{y:4d}'+''.join(G.get((x,y),'.') for x in range(x1,x2)))
