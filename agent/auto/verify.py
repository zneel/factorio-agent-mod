"""Stage 6: build the whole planned factory on a fresh surface of the seed (scratch server) and run it.

    python -m auto.verify --seed N [--minutes 30] [--speed 20]

Every entity of plan.json is created (trees and rocks under it removed, recipes set, all techs researched on the
scratch force), boilers get one load of coal, then the game runs for --minutes of game time. Checks: entities that
could not be placed, machine statuses after the run (working / starved / blocked / no power), crafting machines
that never finished a craft in the whole run (the STARVED ALL RUN line: short of ingredients, whatever their needed
rate), and per-surface production against the demand. Report -> runtime/auto/<seed>/verify.txt
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import rcon  # noqa: E402
from auto import mapdata  # noqa: E402
from auto.geom import ROT  # noqa: E402

SCRATCH = ("127.0.0.1", 27115, "scratchpw")
OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}


def q(lua):
    out = rcon.Rcon(*SCRATCH).command("/silent-command " + lua).strip()
    if out.startswith("Cannot execute"):
        raise RuntimeError(out[:400])
    return out


def surface(seed, radius):
    name = f"verify{seed}"
    q("rcon.print(1)")
    q(f"if game.surfaces['{name}'] then game.delete_surface('{name}') end rcon.print(1)")
    time.sleep(1)
    q(f"local mg=game.surfaces[1].map_gen_settings; mg.seed={seed}; local s=game.create_surface('{name}',mg); "
      f"s.request_to_generate_chunks({{0,0}},{radius // 32 + 1}); s.force_generate_chunk_requests(); "
      f"s.always_day=true; rcon.print('ok')")
    # a force of its own: the scratch server may be running a growth game on the player force
    q("local F=game.forces['verify'] or game.create_force('verify') "
      "for _,t in pairs(F.technologies) do t.researched=true end rcon.print(1)")
    # keep the labs busy like a real run: un-research late techs that cost only the 5 base packs and unlock no
    # recipe the plan uses, and queue them
    used = json.dumps(sorted(USED_RECIPES))
    q("local used={} for _,r in pairs(helpers.json_to_table('%s')) do used[r]=true end "
      "local ok={['automation-science-pack']=1,['logistic-science-pack']=1,['chemical-science-pack']=1,"
      "['production-science-pack']=1,['utility-science-pack']=1} local F=game.forces['verify'] local n=0 "
      "for name,t in pairs(F.technologies) do local fine=#t.research_unit_ingredients>=4 "
      "for _,i in pairs(t.research_unit_ingredients) do if not ok[i.name] then fine=false end end "
      "for _,e in pairs(t.prototype.effects) do if e.type=='unlock-recipe' and used[e.recipe] then fine=false end end "
      "if fine and n<7 and t.prototype.max_level==1 then t.researched=false F.add_research(t) n=n+1 end end "
      "rcon.print(n)" % used.replace("'", ""))
    return name


BUILD_LUA = r"""
local S=game.surfaces['%s'] local F=game.forces['verify']
local D={north=defines.direction.north,east=defines.direction.east,south=defines.direction.south,west=defines.direction.west}
local items=helpers.json_to_table(%s) local err={} local n=0
for _,p in pairs(items) do
  local proto=prototypes.item[p[1]] local name=proto and proto.place_result and proto.place_result.name
  if not name then err[#err+1]='noentity '..p[1] else
    local pr=prototypes.entity[name] local b=pr.collision_box
    local w,h=b.right_bottom.x-b.left_top.x,b.right_bottom.y-b.left_top.y
    if p[4]=='east' or p[4]=='west' then w,h=h,w end
    for _,e in pairs(S.find_entities_filtered{area={{p[2]-w/2,p[3]-h/2},{p[2]+w/2,p[3]+h/2}},type={'tree','simple-entity','simple-entity-with-owner','fish'}}) do e.destroy() end
    local a={name=name,position={p[2],p[3]},direction=D[p[4] or 'north'],force=F}
    if p[5] then a.type=p[5] end
    if p[6] then a.recipe=p[6] end
    local e=S.create_entity(a)
    if e then n=n+1 else
      local bl={} for _,o in pairs(S.find_entities_filtered{position={p[2],p[3]},radius=0.6}) do if o.type~='resource' then bl[#bl+1]=o.name end end
      err[#err+1]=p[1]..'@'..p[2]..','..p[3]..' '..table.concat(bl,'/') end
  end
end
rcon.print(helpers.table_to_json({n=n,err=err}))
"""


def build(name, places, chunk=150):
    rows = []
    for p in places:
        if p.get("remove"):
            continue
        d = p.get("direction")
        if d and "inserter" in p["item"]:
            d = OPP[d]               # plans say where items go; the engine direction is the pickup side
        rows.append([p["item"], p["x"], p["y"], d, p.get("belt_type"), p.get("recipe")])
    # big entities first (machines claim their tiles before belts squeeze in)
    rows.sort(key=lambda r: 0 if r[0] in ("assembling-machine-2", "chemical-plant", "oil-refinery", "rocket-silo",
                                         "lab", "stone-furnace", "electric-mining-drill", "boiler", "steam-engine",
                                         "pumpjack", "offshore-pump") else 1)
    n, errs = 0, []
    for i in range(0, len(rows), chunk):
        r = json.loads(q(BUILD_LUA % (name, json.dumps(json.dumps(rows[i:i + chunk])))))
        n += r["n"]
        errs += r["err"] or []
    return n, errs


STATUS_LUA = r"""
local S=game.surfaces['%s'] local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end
local c={} local bad={} local never={}
for _,e in pairs(S.find_entities_filtered{force='verify',type={'assembling-machine','furnace','mining-drill','lab','boiler','generator','rocket-silo','offshore-pump'}}) do
  local r=e.name if (e.type=='assembling-machine' or e.type=='furnace') and e.get_recipe() then r=e.get_recipe().name end
  local st=SN[e.status] or '?' local k=r..':'..st c[k]=(c[k] or 0)+1
  local at=r..'@'..math.floor(e.position.x)..','..math.floor(e.position.y)..':'..st
  if st~='working' and st~='normal' then bad[#bad+1]=at end
  if (e.type=='assembling-machine' or e.type=='furnace' or e.type=='rocket-silo') and e.products_finished==0 then never[#never+1]=at end
end
rcon.print(helpers.table_to_json({c=c,bad=bad,never=never}))
"""
PROD_LUA = r"""
local st=game.forces['verify'].get_item_production_statistics('%s') local o={}
for name,_ in pairs(st.input_counts) do o[name]=st.get_flow_count{name=name,category='input',precision_index=defines.flow_precision_index.ten_minutes,count=false}*60 end
rcon.print(helpers.table_to_json(o))
"""


USED_RECIPES = set()
STARVED = {"item_ingredient_shortage", "no_ingredients"}   # never crafting for these is reported loudly


def run(seed, minutes, speed):
    out = mapdata.out_dir(seed)
    USED_RECIPES.update(json.loads((out / "demand.json").read_text())["recipes"])
    plan = json.loads((out / "plan.json").read_text())
    places = plan["complex"] + plan["world"]
    xs = [abs(p["x"]) for p in places] + [abs(p["y"]) for p in places]
    q("game.tick_paused=true rcon.print(1)")   # nothing may run while lines are half built (drills would spill)
    name = surface(seed, int(max(xs)) + 40)
    t = time.time()
    n, errs = build(name, places)
    lines = [f"built {n} of {len(places)} entities in {time.time() - t:.0f}s; {len(errs)} failed"]
    lines += ["  FAIL " + e for e in errs[:40]]
    # one load of coal in every boiler (the coal belt takes over), then run
    q("local S=game.surfaces['%s'] for _,e in pairs(S.find_entities_filtered{name='boiler'}) do e.insert{name='coal',count=20} end rcon.print(1)" % name)
    rcon.Rcon(*SCRATCH).command('/agent {"cmd":"set_mode","agent":"beltprobe","mode":"realtime"}')
    q(f"game.speed={speed} game.tick_paused=false rcon.print(1)")
    t_end = time.time() + minutes * 60 / speed
    while time.time() < t_end:
        time.sleep(15)
    q("game.speed=1 game.tick_paused=true rcon.print(1)")
    rcon.Rcon(*SCRATCH).command('/agent {"cmd":"set_mode","agent":"beltprobe","mode":"auto"}')
    st = json.loads(q(STATUS_LUA % name))
    # crafting machines that finished nothing in the whole run (built at its start), whatever their needed rate
    never = st.get("never") or []
    starved = [n for n in never if n.rsplit(":", 1)[1] in STARVED]
    lines.insert(1, f"STARVED ALL RUN: {len(starved)} crafting machines never finished a craft for lack of ingredients"
                    f" ({len(never)} never crafted in all)")
    lines += ["  STARVED " + n for n in starved] + ["  never crafted " + n for n in never if n not in starved]
    lines.append("machine status counts:")
    for k, v in sorted(st["c"].items()):
        lines.append(f"  {k} = {v}")
    lines.append(f"not working ({len(st['bad'])}):")
    lines += ["  " + b for b in st["bad"][:80]]
    prod = json.loads(q(PROD_LUA % name) or "{}")
    dem = json.loads((out / "demand.json").read_text())
    lines.append("made per minute (10 min window) vs LP demand:")
    for r, c in sorted(dem["crafts_per_min"].items()):
        for p, k in dem["recipes"][r]["prod"]:
            need = c * k
            lines.append(f"  {p:32s} made {prod.get(p, 0):8.1f}  need {need:8.1f}")
    (out / "verify.txt").write_text("\n".join(lines))
    print("\n".join(lines[:12]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--speed", type=float, default=20)
    a = ap.parse_args()
    run(a.seed, a.minutes, a.speed)


if __name__ == "__main__":
    main()
