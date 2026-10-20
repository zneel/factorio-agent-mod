from auto import verify
L='''local S=game.surfaces['verify1060'] local o={} local n=0
for _,b in pairs(S.find_entities_filtered{type={'transport-belt','underground-belt','splitter'}}) do local cu,oth=0,{}
 for l=1,b.get_max_transport_line_index() do for _,it in pairs(b.get_transport_line(l).get_contents()) do if it.name=='copper-ore' then cu=cu+it.count elseif it.name~='coal' then oth[#oth+1]=it.name end end end
 if cu>0 and #oth>0 then n=n+1 if n<15 then o[#o+1]=b.name..'@'..b.position.x..','..b.position.y..' d'..b.direction..' cu'..cu..' '..table.concat(oth,'+') end end end
rcon.print(game.tick..' '..n..' | '..table.concat(o,' | '))'''
print(verify.q(L.replace(chr(10),' ')).replace(' | ','\n'))
