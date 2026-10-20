"""Research driver: keeps the force researching what the next unbuilt stages need (see STAGED.md).

    research.tick(q, stages, done)   q(lua) -> str runs Lua on the target server; stages: stages.json rows;
                                     done: set of finished stage numbers. Returns what it queued.

Technologies are queued in tech-tree order (prerequisites first), at most a few stages ahead so labs work on what
the builders will need next. Trigger technologies (2.0's early ones: craft/build/mine something) finish on their
own when the builders do that; the driver just keeps them in the queue so the game tracks them.
"""
import json
import math

LOOKAHEAD = 12   # stages ahead to research for (labs idle otherwise)


def wanted(stages, done):
    out = []
    for r in stages:
        if r["stage"] in done:
            continue
        out += [t for t in r["techs"] if t not in out]
        if len([s for s in stages if s["stage"] not in done and s["stage"] <= r["stage"]]) >= LOOKAHEAD:
            break
    return out


# the queue follows the wanted order: techs whose prerequisites are all researched, most wanted first
TICK_LUA = r"""
local F=game.forces.player local want=helpers.json_to_table('%s') local o={}
local q={}
local ready={} local seen={}
local function avail(name, depth)
  local t=F.technologies[name] if not t or t.researched or seen[name] or depth>30 then return end
  seen[name]=true
  local ok=true for pn,p in pairs(t.prerequisites) do if not p.researched then ok=false avail(pn, depth+1) end end
  if ok and not t.prototype.research_trigger and #ready<7 then ready[#ready+1]=name end
end
for _,n in pairs(want) do avail(n,0) end
local cur={} for _,t in pairs(F.research_queue) do cur[#cur+1]=t.name end
if #ready>0 and cur[1]~=ready[1] then F.research_queue=ready end
for _,t in pairs(F.research_queue) do q[t.name]=true end
local function add(name, depth)
  local t=F.technologies[name] if not t or t.researched or q[name] or depth>30 then return end
  for pn,_ in pairs(t.prerequisites) do add(pn, depth+1) end
  if t.prototype.research_trigger then return end
  if not t.researched and not q[name] and #F.research_queue < 7 then F.add_research(t) q[name]=true o[#o+1]=name end
end
for _,n in pairs(want) do add(n,0) end
local cur=F.current_research
rcon.print(helpers.table_to_json({queued=o, current=cur and cur.name or nil,
  progress=F.research_progress, researched=#want}))
"""


def tick(q, stages, done):
    want = wanted(stages, done)
    return json.loads(q("/silent-command " + (TICK_LUA % json.dumps(want)).replace("\n", " ")))


def researched(q, techs):
    """Which of these techs the force has researched."""
    lua = ("local F=game.forces.player local o={} for _,n in pairs(helpers.json_to_table('%s')) do "
           "local t=F.technologies[n] if t and t.researched then o[#o+1]=n end end "
           "rcon.print(helpers.table_to_json(o))") % json.dumps(sorted(techs))
    return set(json.loads(q("/silent-command " + lua)) or [])


HAND = {"automation-science-pack"}   # packs a character can hand-craft for the hut lab


def hut_site(plan, extra=()):
    """A lab next to one of the steam plant's poles (so it has power from the first stage), on no planned tile."""
    from auto import geom
    blocked = geom.tiles(plan["complex"] + plan["world"] + list(extra))
    engines = [p for p in plan["world"] if p["item"] == "steam-engine"]
    poles = [p for p in plan["world"] if "electric-pole" in p["item"] and
             any(math.dist((p["x"], p["y"]), (e["x"], e["y"])) < 8 for e in engines)]
    for p in poles:
        for ox in range(-4, 5):
            for oy in range(-4, 5):
                lab = {"item": "lab", "x": math.floor(p["x"]) + ox + .5, "y": math.floor(p["y"]) + oy + .5}
                ring = {(a + dx, b + dy) for a, b in geom.footprint(lab) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
                if abs(lab["x"] - p["x"]) <= 3.5 and abs(lab["y"] - p["y"]) <= 3.5 and not ring & blocked:
                    return lab
    return None


STATUS_LUA = r"""
local F=game.forces.player local o={}
local cur=F.current_research
o.current=cur and cur.name or nil
o.ing={} if cur then for _,i in pairs(cur.research_unit_ingredients) do o.ing[#o.ing+1]=i.name end end
o.triggers={}
for _,n in pairs(helpers.json_to_table('%s')) do local t=F.technologies[n]
  if t and not t.researched then local tr=t.prototype.research_trigger
    local pre_ok=true for _,p in pairs(t.prerequisites) do if not p.researched then pre_ok=false end end
    if tr and pre_ok then o.triggers[#o.triggers+1]={name=n, type=tr.type, item=tr.item and tr.item.name or nil,
      count=tr.count or 1, entity=tr.entity and tr.entity.name or nil} end end end
local lab=game.surfaces[1].find_entities_filtered{name='lab', position={%s,%s}, radius=0.6}[1]
o.lab=lab and true or false
o.lab_packs=lab and lab.get_inventory(defines.inventory.lab_input).get_item_count('automation-science-pack') or 0
rcon.print(helpers.table_to_json(o))
"""


def serve(b, q, stages, done, hut, log, stop):
    """Research character loop: queue lab research, satisfy trigger techs by crafting/building, and run the hut
    lab on hand-crafted red packs while the research needs nothing else. Runs until stop.is_set()."""
    import supply
    from bridge import BridgeError
    while not stop.is_set():
        try:
            r = tick(q, stages, done)
            if r.get("queued"):
                log("research queued", r["queued"])
            want = wanted(stages, done)
            st = json.loads(q("/silent-command " + (STATUS_LUA % (json.dumps(want), hut["x"], hut["y"])).replace("\n", " ")))
            for t in st.get("triggers") or []:
                if t["type"] == "craft-item" and t.get("item"):
                    log(f"trigger {t['name']}: craft {t['count']} {t['item']}")
                    supply.craft_items(b, t["item"], t["count"])
                    if t["item"] == "lab" and not st.get("lab"):
                        supply.build_plan(b, [hut], minutes=5)
            if st.get("current") and set(st.get("ing") or []) <= HAND:
                if not st.get("lab"):
                    supply.build_plan(b, [hut], minutes=5)
                elif st.get("lab_packs", 0) < 5:
                    supply.feed_labs(b, [hut], "automation-science-pack", 10)
        except (BridgeError, Exception) as e:   # the research loop keeps going whatever one round hits
            log("research error", str(e)[:200])
        stop.wait(20)
