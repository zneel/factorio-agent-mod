"""Game data from the prototypes (seed independent): recipes, technologies, machine stats.

    python -m auto.gamedata     -> runtime/auto/gamedata.json
"""
import json
from pathlib import Path

from auto.mapdata import ROOT, q

LUA_RECIPES = r"""
local out={}
for name,r in pairs(prototypes.recipe) do
  if not r.hidden and not r.parameter then
    local ing,prod={},{}
    for _,i in pairs(r.ingredients) do ing[#ing+1]={i.name,i.amount,i.type} end
    for _,p in pairs(r.products) do prod[#prod+1]={p.name,p.amount or ((p.amount_min+p.amount_max)/2)*(p.probability or 1),p.type} end
    out[name]={cat=r.category,energy=r.energy,ing=ing,prod=prod}
  end
end
rcon.print(helpers.table_to_json(out))
"""
LUA_TECHS = r"""
local out={}
for name,t in pairs(prototypes.technology) do
  if not t.hidden then
    local ing={} for _,i in pairs(t.research_unit_ingredients or {}) do ing[#ing+1]={i.name,i.amount} end
    local pre={} for p,_ in pairs(t.prerequisites) do pre[#pre+1]=p end
    local un={} for _,e in pairs(t.effects or {}) do if e.type=='unlock-recipe' then un[#un+1]=e.recipe end end
    local trig=nil if t.research_trigger then trig=t.research_trigger.type..':'..tostring(t.research_trigger.item or t.research_trigger.entity or '') end
    local cnt=0 local ok=pcall(function() cnt=t.research_unit_count end)
    out[name]={count=cnt,time=t.research_unit_energy/60,ing=ing,pre=pre,unlocks=un,trigger=trig}
  end
end
rcon.print(helpers.table_to_json(out))
"""


def load():
    p = ROOT / "runtime" / "auto" / "gamedata.json"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        q("rcon.print(1)")
        q("rcon.print(1)")
        d = {"recipes": json.loads(q(LUA_RECIPES)), "techs": json.loads(q(LUA_TECHS))}
        p.write_text(json.dumps(d))
    return json.loads(p.read_text())


if __name__ == "__main__":
    d = load()
    print(len(d["recipes"]), "recipes", len(d["techs"]), "techs")
