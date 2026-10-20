"""Gate: build a placement plan on the scratch server, run it in realtime with test inputs, and check it.

    .venv/Scripts/python gate.py runtime/blocks/new/X.json [--refresh] [--seconds 60] [--speed 4] [--keep]

The plan file is {"place": [{item,x,y,direction,belt_type?,recipe?}], "gate": {...}} where gate holds the test:
    "feeds":   [{"x","y","lanes": {"north": item, "south": item}}]   belt tile(s) kept full, per lane
               (compass side of the lane: north/south on east-west belts, west/east on north-south belts)
    "fluids":  [{"x","y","fluid"}]          infinity pipe at a tile outside the plan, next to its pipe input
    "sinks":   [{"x","y"}]                  belt tile whose contents are deleted (an export's far end)
    "chests":  [{"x","y","items": {item: n}}]  fill a planned chest (an input buffer) once
    "fuel":    "coal"                        fuel put into every burner entity in the plan
    "research": [tech, ...]                  techs to unlock on scratch first (recipes the block needs)
    "allow":   [item, ...]                    items the plan is meant to deliver into existing belts (not spill)
    "targets": [[x,y], ...]                  machines that must work (default: every crafting machine/drill)
    "power":   true                          add an electric-energy-interface beside the first planned pole
    "load":    {"x","y","watts"}             add an electric consumer there (for testing generators)
Checks (all must pass, else exit code 1):
    working   every target reports working in at least one of the last samples
    spill     no belt outside the plan gains an item type it did not carry before
    lanes     a planned belt with "lanes" carries only those items on those lanes
    p2g       every pipe-to-ground has 2 fluidbox connections (above ground + its underground partner)
--refresh first saves the live map and restarts scratch from it, so scratch matches what is built.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import rcon  # noqa: E402

SCRATCH = ("127.0.0.1", 27115, "scratchpw")
PY = sys.executable


def q(lua):
    """One /sc line on scratch; returns rcon.print output."""
    out = rcon.Rcon(*SCRATCH).command("/silent-command " + lua.replace("\n", " ")).strip()
    if out.startswith("Cannot execute command"):
        raise RuntimeError(out[:400])
    return out


def refresh():
    live = json.loads((ROOT / "runtime" / "rcon.json").read_text())
    rcon.Rcon(live["host"], live["port"], live["password"]).command("/server-save")
    time.sleep(2)
    for c in ("stop", "setup", "start"):
        subprocess.run([PY, str(HERE / "fa_scratch.py"), c], check=c != "stop", capture_output=True)
    q("rcon.print(1)")  # the first console command only warns about achievements
    q("rcon.print(1)")


LUA_LIB = r"""
local S=game.surfaces[1]; local F=game.forces.player
local D={north=defines.direction.north,east=defines.direction.east,south=defines.direction.south,west=defines.direction.west}
"""


def lua_str(v):
    return json.dumps(v)


def build(plan, gate):
    """Cheat-build the plan; returns created unit numbers and errors."""
    lines = [LUA_LIB, "local made,err={}, {}"]
    lines.append("local function clear(name,p,dir) local pr=prototypes.entity[name]; local b=pr.collision_box; "
                 "for _,e in pairs(S.find_entities_filtered{area={{p[1]+b.left_top.x-0.01,p[2]+b.left_top.y-0.01},"
                 "{p[1]+b.right_bottom.x+0.01,p[2]+b.right_bottom.y+0.01}},type={'tree','simple-entity','simple-entity-with-owner','simple-entity-with-force','fish','container'}}) do if e.type~='container' or e.name:find('crash') then e.destroy() end end end")
    lines.append("local function mk(item,x,y,dir,bt,recipe) local name=prototypes.item[item].place_result.name; clear(name,{x,y},dir); "
                 "local same=S.find_entities_filtered{position={x,y},name=name,limit=1}[1]; "
                 "if same and same.position.x==x and same.position.y==y and (not same.supports_direction or same.direction==D[dir or 'north']) then return end; "
                 "local a={name=name,position={x,y},direction=D[dir or 'north'],force=F,raise_built=true}; "
                 "if bt then a.type=bt end; if recipe then a.recipe=recipe end; "
                 "local e=S.create_entity(a); if e then made[#made+1]=e.unit_number else "
                 "local bl={} for _,o in pairs(S.find_entities_filtered{position={x,y},radius=1}) do if o.type~='resource' then bl[#bl+1]=o.name..'@'..o.position.x..','..o.position.y end end; "
                 "err[#err+1]=item..' '..x..','..y..' blocked: '..table.concat(bl,' ') end end")
    script = " ".join(lines)
    calls = []
    import macros
    for p in plan:
        calls.append("mk(%s,%s,%s,%s,%s,%s)" % (lua_str(p["item"]), p["x"], p["y"], lua_str(macros.game_dir(p)),
                                               lua_str(p["belt_type"]) if p.get("belt_type") else "nil",
                                               lua_str(p["recipe"]) if p.get("recipe") else "nil"))
    made, errs = [], []
    for i in range(0, len(calls), 60):  # keep each console line short enough
        out = q(script + " " + " ".join(calls[i:i + 60]) + " rcon.print(helpers.table_to_json({made=made,err=err}))")
        r = json.loads(out)
        made += r.get("made") or []
        errs += r.get("err") or []
    return made, errs


def setup_test(plan, gate):
    out = []
    lib = LUA_LIB
    for f in gate.get("fluids", []):
        q(lib + "local e=S.create_entity{name='infinity-pipe',position={%s,%s},force=F}; "
                "e.set_infinity_pipe_filter{name=%s,percentage=1,temperature=%s,mode='at-least'}; rcon.print(e.unit_number)"
          % (f["x"], f["y"], lua_str(f["fluid"]), f.get("temperature", 15)))
    for c in gate.get("chests", []):
        for it, n in c["items"].items():
            q(lib + "local e=S.find_entities_filtered{position={%s,%s},type='container',limit=1}[1]; e.insert{name=%s,count=%d}"
              % (c["x"], c["y"], lua_str(it), n))
    fuel = gate.get("fuel")
    if fuel:
        xs = [p["x"] for p in plan]
        ys = [p["y"] for p in plan]
        q(lib + "for _,e in pairs(S.find_entities_filtered{area={{%s,%s},{%s,%s}},force=F}) do local i=e.get_fuel_inventory and e.get_fuel_inventory(); "
                "if i and i.valid then i.insert{name=%s,count=20} end end" % (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2, lua_str(fuel)))
    ld = gate.get("load")
    if ld:  # an electric consumer, so generators have something to run for
        r = q(lib + "local p=S.find_non_colliding_position('electric-energy-interface',{%s,%s},20,1); "
                    "local e=S.create_entity{name='electric-energy-interface',position=p,force=F}; "
                    "e.power_production=0; e.power_usage=%s/60; e.electric_buffer_size=%s; "
                    "local pp=S.find_non_colliding_position('medium-electric-pole',p,4,1); "
                    "local pl=S.create_entity{name='medium-electric-pole',position=pp,force=F}; "
                    "rcon.print(e.unit_number..' '..pl.unit_number)" % (ld["x"], ld["y"], ld["watts"], ld["watts"]))
        out += [int(v) for v in r.split()]
    if gate.get("power"):
        pole = next((p for p in plan if "electric-pole" in p["item"] or p["item"] == "substation"), None)
        if pole:
            r = q(lib + "local p=S.find_non_colliding_position('electric-energy-interface',{%s,%s},12,1); "
                        "local e=S.create_entity{name='electric-energy-interface',position=p,force=F}; "
                        "local pp=S.find_non_colliding_position('medium-electric-pole',p,4,1); "
                        "local pl=S.create_entity{name='medium-electric-pole',position=pp,force=F}; "
                        "rcon.print(e.unit_number..' '..pl.unit_number)" % (pole["x"], pole["y"]))
            out += [int(v) for v in r.split()]
    return out


def feed_lua(gate):
    """Lua that tops up every feed's lanes and empties every sink (run each sample)."""
    parts = [LUA_LIB, "local LEFT={[defines.direction.east]='north',[defines.direction.west]='south',"
                      "[defines.direction.north]='west',[defines.direction.south]='east'}"]
    for f in gate.get("feeds", []):
        parts.append("do local e=S.find_entities_filtered{position={%s,%s},type={'transport-belt','underground-belt'},limit=1}[1]; "
                     "local lanes=helpers.json_to_table(%s); if lanes.both then lanes.north=lanes.both lanes.south=lanes.both lanes.east=lanes.both lanes.west=lanes.both end local n=0; "
                     "while e and n<%d do n=n+1; local left=LEFT[e.direction]; "
                     "for li=1,2 do local side=(li==1) and left or ({north='south',south='north',east='west',west='east'})[left]; "
                     "local it=lanes[side]; if it then local l=e.get_transport_line(li); "
                     "for k=1,4 do if l.can_insert_at_back() then l.insert_at_back({name=it,count=1}) end end end end; "
                     "e=e.belt_neighbours.outputs[1] end end"
                     % (f["x"], f["y"], lua_str(json.dumps(f["lanes"])), f.get("tiles", 4)))
    for s in gate.get("sinks", []):
        parts.append("do local e=S.find_entities_filtered{position={%s,%s},type={'transport-belt','underground-belt'},limit=1}[1]; "
                     "if e then for li=1,2 do e.get_transport_line(li).clear() end end end" % (s["x"], s["y"]))
    return " ".join(parts)


def status_lua(targets):
    pts = json.dumps(targets).replace("[", "{").replace("]", "}")
    return (LUA_LIB + "local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end; local out={} "
            "for i,t in pairs(%s) do local e=S.find_entities_filtered{position=t,limit=1,type={'assembling-machine','furnace',"
            "'mining-drill','lab','boiler','generator','rocket-silo','offshore-pump','inserter'}}[1]; "
            "out[i]=e and (SN[e.status] or 'nil') or 'missing' end; rcon.print(helpers.table_to_json(out))" % pts)


def belts_lua(area):
    (x1, y1), (x2, y2) = area
    return (LUA_LIB + "local out={} for _,e in pairs(S.find_entities_filtered{area={{%s,%s},{%s,%s}},"
                      "type={'transport-belt','underground-belt','splitter'}}) do local its={} "
                      "for li=1,e.get_max_transport_line_index() do for _,c in pairs(e.get_transport_line(li).get_contents()) do "
                      "its[#its+1]=li..':'..c.name end end; out[#out+1]={e.position.x,e.position.y,e.unit_number,its} end; "
                      "rcon.print(helpers.table_to_json(out))" % (x1, y1, x2, y2))


def p2g_lua(area):
    (x1, y1), (x2, y2) = area
    return (LUA_LIB + "local out={} for _,e in pairs(S.find_entities_filtered{area={{%s,%s},{%s,%s}},name='pipe-to-ground'}) do "
                      "local n=0 for _,c in pairs(e.fluidbox.get_pipe_connections(1)) do if c.target then n=n+1 end end; "
                      "out[#out+1]={e.position.x,e.position.y,n} end; rcon.print(helpers.table_to_json(out))" % (x1, y1, x2, y2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--seconds", type=float, default=60, help="game seconds to run")
    ap.add_argument("--speed", type=float, default=4)
    ap.add_argument("--keep", action="store_true", help="leave the test build on scratch")
    a = ap.parse_args()
    p = Path(a.plan)
    if not p.is_absolute():
        p = ROOT / a.plan
    spec = json.loads(p.read_text())
    plan, gate = (spec, {}) if isinstance(spec, list) else (spec["place"], spec.get("gate", {}))
    if a.refresh:
        refresh()
    q("rcon.print(1)")
    if not plan:
        raise SystemExit("empty plan")
    xs = [e["x"] for e in plan]
    ys = [e["y"] for e in plan]
    area = ((min(xs) - 4, min(ys) - 4), (max(xs) + 4, max(ys) + 4))
    before = {(b[0], b[1]): set(b[3]) for b in json.loads(q(belts_lua(area)) or "[]")}
    for t in gate.get("research", []):  # scratch only: unlock recipes the live game will have by build time
        q(LUA_LIB + "F.technologies[%s].researched=true" % lua_str(t))
    for r in [e for e in plan if e.get("remove")]:
        q(LUA_LIB + "for _,e in pairs(S.find_entities_filtered{position={%s,%s},name=prototypes.item[%s].place_result.name}) do e.destroy() end"
          % (r["x"], r["y"], lua_str(r["item"])))
    plan = [e for e in plan if not e.get("remove")]
    made, errs = build(plan, gate)
    if errs:
        print("BUILD ERRORS:", "; ".join(errs[:8]))
    made += setup_test(plan, gate)
    planned = {(e["x"], e["y"]) for e in plan}
    machines = ("assembling-machine", "furnace", "mining-drill", "chemical-plant", "oil-refinery", "lab", "centrifuge",
                "boiler", "steam-engine", "pumpjack", "offshore-pump", "rocket-silo")
    targets = gate["targets"] if "targets" in gate else [[e["x"], e["y"]] for e in plan if any(m in e["item"] for m in machines)]
    feed = feed_lua(gate)
    q(LUA_LIB + "game.tick_paused=false; game.speed=%s" % a.speed)
    rcon.Rcon(*SCRATCH).command('/agent {"cmd":"set_mode","agent":"beltprobe","mode":"realtime"}')
    samples = []
    t_end = time.time() + a.seconds / a.speed
    while time.time() < t_end:
        if feed:
            q(feed)
        samples.append(json.loads(q(status_lua(targets)) or "[]"))
        time.sleep(0.25)
    q(LUA_LIB + "game.tick_paused=true; game.speed=1")
    rcon.Rcon(*SCRATCH).command('/agent {"cmd":"set_mode","agent":"beltprobe","mode":"auto"}')
    ok = not errs
    # working: any of the last quarter of samples
    tail = samples[-max(4, len(samples) // 4):]
    bad = []
    for i, t in enumerate(targets):
        sts = [s[i] if isinstance(s, list) and i < len(s) else "?" for s in tail]
        if "working" not in sts and "normal" not in sts:
            bad.append(f"{t[0]},{t[1]}={sts[-1]}")
    print(f"working: {len(targets) - len(bad)}/{len(targets)}" + (" NOT: " + " ".join(bad[:12]) if bad else ""))
    ok &= not bad
    after = json.loads(q(belts_lua(area)) or "[]")
    spill = []
    lane_bad = []
    flowing = {i.split(":", 1)[1] for its in before.values() for i in its}  # already moving here before the test
    lanes_of = {(e["x"], e["y"]): e["lanes"] for e in plan if e.get("lanes")}
    for x, y, _, its in after:
        its = set(its)
        if (x, y) not in planned:
            new = {i.split(":", 1)[1] for i in its} - {i.split(":", 1)[1] for i in before.get((x, y), set())} - set(gate.get("allow", [])) - flowing
            if new:
                spill.append(f"{x},{y}+{','.join(sorted(new))}")
        elif (x, y) in lanes_of:
            allowed = {v for v in lanes_of[(x, y)].values() if v}
            extra = {i.split(":", 1)[1] for i in its} - allowed
            if extra:
                lane_bad.append(f"{x},{y}:{','.join(sorted(extra))}")
    print("spill:", " ".join(spill[:10]) if spill else "none")
    if lanes_of:
        print("lanes:", " ".join(lane_bad[:10]) if lane_bad else "ok")
    ok &= not spill and not lane_bad
    p2g = json.loads(q(p2g_lua(area)) or "[]")
    p2g_bad = [f"{x},{y}={n}" for x, y, n in p2g if n < 2]
    if p2g:
        print(f"p2g: {len(p2g) - len(p2g_bad)}/{len(p2g)} connected" + (" NOT: " + " ".join(p2g_bad[:10]) if p2g_bad else ""))
    ok &= not p2g_bad
    # grid: every planned pole must share an electric network with a pole that was there before (the live grid)
    pp = [[e["x"], e["y"]] for e in plan if "electric-pole" in e["item"] or e["item"] == "substation"]
    if pp and gate.get("grid", True):
        r = q(LUA_LIB + "local plan={} for _,t in pairs(%s) do local e=S.find_entities_filtered{position=t,type='electric-pole',limit=1}[1] "
                        "if e then plan[e.unit_number]=e.electric_network_id end end; local old={} "
                        "for _,e in pairs(S.find_entities_filtered{type='electric-pole'}) do if not plan[e.unit_number] then old[e.electric_network_id]=true end end; "
                        "local bad=0 for u,n in pairs(plan) do if not old[n] then bad=bad+1 end end; rcon.print(bad)"
          % json.dumps(pp).replace("[", "{").replace("]", "}"))
        print(f"grid: {len(pp) - int(r or 0)}/{len(pp)} planned poles on the existing grid")
        ok &= int(r or 0) == 0
    if not a.keep:
        ids = json.dumps(made).replace("[", "{").replace("]", "}")
        q(LUA_LIB + "local want={} for _,u in pairs(%s) do want[u]=true end; "
                    "for _,e in pairs(S.find_entities_filtered{area={{%s,%s},{%s,%s}},force=F}) do "
                    "if e.unit_number and want[e.unit_number] then e.destroy() end end; "
                    "for _,e in pairs(S.find_entities_filtered{area={{%s,%s},{%s,%s}},name='infinity-pipe'}) do e.destroy() end"
          % (ids, area[0][0] - 12, area[0][1] - 12, area[1][0] + 12, area[1][1] + 12,
             area[0][0] - 12, area[0][1] - 12, area[1][0] + 12, area[1][1] + 12))
    print("GATE PASS" if ok else "GATE FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
