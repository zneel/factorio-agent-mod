import pickle,math,sys
cx=pickle.load(open('../runtime/auto/1060/complex.pkl','rb'))
DV={'north':(0,-1),'south':(0,1),'east':(1,0),'west':(-1,0)}
B={}
for q in cx['places']:
    if q['item'] in('transport-belt','underground-belt','splitter') and not q.get('remove'):
        B[(math.floor(q['x']),math.floor(q['y']))]=q
t=(int(sys.argv[1]),int(sys.argv[2]))
seen=0
while t in B and seen<400:
    q=B[t]; d=DV[q['direction']]
    nxt=(t[0]+d[0],t[1]+d[1])
    if q['item']=='underground-belt' and q.get('belt_type')=='input':
        k=1
        while k<7:
            n2=(t[0]+d[0]*k,t[1]+d[1]*k)
            if n2 in B and B[n2]['item']=='underground-belt' and B[n2].get('belt_type')=='output': nxt=n2;break
            k+=1
    if seen<3 or nxt not in B or (nxt in B and B[nxt]['direction']!=q['direction']) or q['item']!='transport-belt':
        print(seen,t,q['item'],q['direction'],q.get('belt_type',''),'->',nxt, B.get(nxt,{}).get('item'),B.get(nxt,{}).get('direction'))
    t=nxt; seen+=1
