"""Things kept up every round or in threads: recipes set once researched, oil cracking, trigger techs, the
research runner, the burner tender, the rocket launch."""
import json

from auto.growth.core import AGENT, log, recipe_ok


TRIG_LUA = r"""
local F=game.forces.player local o={}
for _,n in pairs(helpers.json_to_table('%s')) do local t=F.technologies[n]
  if t and not t.researched then local tr=t.prototype.research_trigger
    local ok=true for _,p in pairs(t.prerequisites) do if not p.researched then ok=false end end
    if tr and ok then
      local ent=tr.entity and (type(tr.entity)=='string' and tr.entity or tr.entity.name) or nil
      local it=tr.item and (type(tr.item)=='string' and tr.item or tr.item.name) or nil
      o[#o+1]={name=n, type=tr.type, entity=ent, item=it} end end end
rcon.print(helpers.table_to_json(o))
"""


# cracking follows demand, as a player's circuit condition would: a refinery stops whole when one output has no
# room (petroleum 55 per cycle), so light oil cracked into petroleum that backs up starves rocket fuel and solid fuel;
# light-oil cracking is switched off (its recipe taken out) while the refineries' petroleum has no room for a cycle,
# and back on when petroleum has room again
CRACK_LUA = r"""
local S=game.surfaces[1] local full,room=0,0
for _,e in pairs(S.find_entities_filtered{name='oil-refinery',force='player'}) do
  local r=e.get_recipe()
  if r and r.name=='advanced-oil-processing' then
    for k=1,#e.fluidbox do local fl=e.fluidbox.get_filter(k)
      if fl and fl.name=='petroleum-gas' then local f=e.fluidbox[k]
        if f and e.fluidbox.get_capacity(k)-f.amount<55 then full=full+1 else room=room+1 end end end end end
local off,on=0,0
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},name='chemical-plant',force='player'}[1]
  if e then local r=e.get_recipe()
    if full>0 and room==0 and r and r.name=='light-oil-cracking' then e.set_recipe(nil) off=off+1
    elseif room>0 and full==0 and not r then e.set_recipe('light-oil-cracking') on=on+1 end end
end
rcon.print(off..','..on..','..full..','..room)
"""


def balance_cracking(plan, L, q):
    """Light-oil cracking plants of the plan switched off while the refineries' petroleum is backed up, back on when
    it has room. Returns (switched off, switched on, refineries full, refineries with room)."""
    pos = [[plan.p[i]["x"], plan.p[i]["y"]] for i in L.built if plan.p[i].get("recipe") == "light-oil-cracking"]
    if not pos:
        return 0, 0, 0, 0
    r = (q("/silent-command " + (CRACK_LUA % json.dumps(pos)).replace(chr(10), " ")) or "0,0,0,0").strip()
    return tuple(int(v) for v in r.split(","))


SETREC_LUA = r"""
local S=game.surfaces[1] local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type={'assembling-machine'},force='player'}[1]
  if e and (not e.get_recipe() or e.get_recipe().name~=c[3]) then e.set_recipe(c[3]) o[#o+1]=c[3] end
end
rcon.print(helpers.table_to_json(o))
"""


def oil_upkeep(plan, L, q):
    try:
        off, on, full, room = balance_cracking(plan, L, q)
        if off or on:
            log(f"cracking: {off} light-oil crackers off, {on} on (refineries: {full} petroleum-full, {room} with room)")
    except Exception as e:
        log("cracking:", str(e)[:120])


def set_pending_recipes(q, state, researched, log):
    """Machines placed idle or on a stand-in recipe get their planned recipe once it is researched."""
    pend = state.get("pending_recipes") or {}
    ready = [[float(k.split(",")[0]), float(k.split(",")[1]), r] for k, r in pend.items() if recipe_ok(r, researched)]
    if not ready:
        return
    try:
        r = q("/silent-command " + (SETREC_LUA % json.dumps(ready)).replace(chr(10), " "))
        log(f"recipes set now that they are researched: {r}")
        for x, y, _ in ready:
            pend.pop(f"{x},{y}", None)
    except Exception as e:
        log("set recipes:", str(e)[:120])


def trigger_techs(q, techs):
    """Trigger techs whose prerequisites are researched: [{name, type, entity, item}]."""
    try:
        r = json.loads(q("/silent-command " + (TRIG_LUA % json.dumps(techs)).replace(chr(10), " ")) or "[]")
        return r if isinstance(r, list) else []
    except Exception:
        return []


def start_tender(py, char, boot_dir, power_file, env):
    import subprocess
    files = [str(f) for f in sorted(boot_dir.glob("boot-*.json"))] + ([str(power_file)] if power_file.exists() else [])
    return subprocess.Popen([py, str(AGENT / "bootstrap.py"), char, ",".join(files), "--tend", "--minutes", "100000"],
                            env=env, cwd=str(AGENT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def grid_dead(q):
    """No generator has steam (the boilers ran out of fuel): nothing electric runs, the mall included."""
    from auto import live as lv
    try:
        return lv.power(q).get("cap", 0) <= 0
    except Exception:
        return False


# a finished rocket waits in its silo; 2.0 will not send it empty: the satellite (the plan's cargo, made into a mall
# chest) goes into the cargo slot, else one of our characters puts an item it carries there (a satellite if it has
# one, else what it holds most of), then launch is pressed
LAUNCH_LUA = ("local n=0 for _,e in pairs(game.surfaces[1].find_entities_filtered{type='rocket-silo',force='player'}) do "
              "if e.status==defines.entity_status.waiting_to_launch_rocket then "
              "local ri=e.get_inventory(defines.inventory.rocket_silo_rocket) "
              "if ri and ri.is_empty() then for _,c in pairs(game.surfaces[1].find_entities_filtered{name='iron-chest',force='player'}) do "
              "if c.get_item_count('satellite')>0 and ri.insert{name='satellite',count=1}>0 then c.remove_item{name='satellite',count=1} break end end end "
              "if ri and ri.is_empty() then for _,ch in pairs(game.surfaces[1].find_entities_filtered{type='character',force='player'}) do "
              "local inv=ch.get_main_inventory() local best,bc=nil,0 "
              "for _,it in pairs(inv.get_contents()) do local c=(it.name=='satellite' and 1e9 or it.count) if c>bc then best,bc=it.name,c end end "
              "if best and ri.insert{name=best,count=1}>0 then inv.remove{name=best,count=1} break end end end "
              "if e.launch_rocket() then n=n+1 end end end rcon.print(n)")


def launched(q):
    """True once a rocket has gone up. A finished rocket waits in its silo for the launch button (2.0): pressed here."""
    try:
        n = int(q("/silent-command " + LAUNCH_LUA) or 0)
        if n:
            log(f"launch pressed on {n} silo(s)")
        return int(q("/silent-command rcon.print(game.forces.player.rockets_launched)") or 0) > 0
    except ValueError:
        return False


SMELTED = {"iron-plate", "copper-plate", "steel-plate", "stone-brick"}


def research_loop(b, q, rstate, hut, state, stop, gate=None):
    """Keep the queue filled with what the next increments need (rstate["want"]); do trigger techs; during the
    bootstrap run the hut lab on hand-crafted red packs. The hut lab is placed only when the gate passes it (this
    runner's own inventory counts; no alt search from this thread)."""
    from auto import research
    import supply

    def hut_ok():
        return hut is not None and (gate is None or gate("hut", [hut], depth=3, who=(b.agent,))[0] == "build")
    while not stop.is_set():
        try:
            want = rstate["want"]
            r = json.loads(q("/silent-command " + (research.TICK_LUA % json.dumps(want)).replace("\n", " ")))
            if r.get("queued"):
                log("research queued", r["queued"])
            st = json.loads(q("/silent-command " + (research.STATUS_LUA % (json.dumps(want), hut["x"], hut["y"]))
                              .replace("\n", " ")))
            for t in st.get("triggers") or []:
                if t["type"] == "craft-item" and t.get("item") in SMELTED:
                    continue          # plates count when the furnaces make them
                if t["type"] == "craft-item" and t.get("item"):
                    log(f"trigger {t['name']}: craft {t['count']} {t['item']}")
                    supply.craft_items(b, t["item"], t["count"])
                    if t["item"] == "lab" and not st.get("lab") and hut_ok():
                        supply.build_plan(b, [hut], minutes=5)
            if state.get("handcraft") and st.get("current") and set(st.get("ing") or []) <= research.HAND:
                if not st.get("lab"):
                    if hut_ok():
                        supply.build_plan(b, [hut], minutes=5)
                elif st.get("lab_packs", 0) < 5:
                    supply.feed_labs(b, [hut], "automation-science-pack", 10)
        except Exception as e:
            log("research error", str(e)[:200])
        stop.wait(20)
