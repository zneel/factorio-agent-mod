"""The mall: which chests and makers stand, their caps (what the unbuilt plan still needs), the chest reads, the
blocks nothing needs any more and the chests a full item locks."""
import collections
import json
import math
import re

from auto.growth.core import materialize


EARLY_MALL = ["transport-belt", "inserter", "pipe", "electric-mining-drill", "stone-furnace",
              "assembling-machine-1", "underground-belt", "splitter", "steam-engine", "boiler", "offshore-pump",
              "pipe-to-ground", "lab", "iron-chest", "burner-inserter", "long-handed-inserter", "fast-inserter"]


def mall_groups(plan, item):
    """Groups that put item into a mall chest: its maker plus the chest (mall:<item> on the bus, or the maker's own)."""
    out = [g for g in plan.groups if g.startswith("mall:") and item in g[5:].split("+")]
    return out



def mall_ready(plan, L):
    for it in EARLY_MALL:
        gs = mall_groups(plan, it) + [g for g in plan.makers.get("stock:" + it, [])]
        if not gs or not any(L.group_done(g) for g in gs):
            return False
    return True


# c = [x, y, [[item, cap], ...]]: slots per item ceil(cap/stack) (at least 1), the bar after their sum. A chest fed
# two items reserves each its slots: slot filters where the chest supports them; an iron chest does not (its
# inventory is "with_bar" only, measured), so there the inserter that fills it keeps a whitelist of the items still
# under their cap, and one item can no longer fill all slots and lock the other out. The filters are written first
# and use_filters last: a write failing half-way must never leave an empty whitelist on (the chest would take
# nothing at all); on a failure the inserter goes back to unfiltered. (No Lua comments: newlines become spaces.)
LIMIT_LUA = r"""
local S=game.surfaces[1] local n,f,w=0,0,0
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},name='iron-chest'}[1]
  if e then local inv=e.get_inventory(defines.inventory.chest) local want={} local slots=0
    for _,it in pairs(c[3]) do local pr=prototypes.item[it[1]]
      if pr then local k=math.max(1,math.ceil(it[2]/pr.stack_size)) want[#want+1]={it[1],k,it[2]} slots=slots+k end end
    slots=math.max(1,math.min(#inv,slots))
    if #want>1 and inv.supports_filters() then
      local left={} for _,it in pairs(want) do left[it[1]]=it[2] end
      local free={}
      for s=1,#inv do inv.set_filter(s,nil) end
      for s=1,slots do local st=inv[s]
        if st.valid_for_read then if (left[st.name] or 0)>0 then inv.set_filter(s,st.name) left[st.name]=left[st.name]-1 end
        else free[#free+1]=s end end
      for _,it in pairs(want) do while left[it[1]]>0 and #free>0 do inv.set_filter(table.remove(free,1),it[1]) left[it[1]]=left[it[1]]-1 end end
      f=f+1
    elseif #want>1 then
      for _,i in pairs(S.find_entities_filtered{area={{c[1]-3,c[2]-3},{c[1]+3,c[2]+3}},type='inserter',force='player'}) do
        if i.drop_target==e and i.filter_slot_count>0 then
          local l={} for _,it in pairs(want) do
            if inv.get_item_count(it[1])<it[3] and #l<i.filter_slot_count then l[#l+1]=it[1] end end
          local ok=pcall(function()
            for s=1,i.filter_slot_count do i.set_filter(s,l[s]) end
            i.inserter_filter_mode='whitelist' i.use_filters=true end)
          if ok then w=w+1 else pcall(function() i.use_filters=false end) end
        end end
    end
    inv.set_bar(slots+1) n=n+1 end
end
rcon.print(n..' chests, '..f..' slot-filtered, '..w..' inserters whitelisted')
"""


RESERVE = 10                   # stock every mall item keeps beyond the plan: repairs, restores, own routes


def mall_caps(plan, L, researched, pole_item, want=None):
    """{(x, y): {item: cap}} for every built mall chest: what the unbuilt plan still needs of the item (+10%) plus a
    reserve of 10% of the plan's total (at most RESERVE: one silo, one pump keep none); pieces of retired groups and
    written-off drills are not coming. The rocket's cargo (demand.CARGO) is needed once. want ({item: n}, the
    controller's Context.want): short for builds outside the plan, added to the caps."""
    from auto.demand import CARGO
    rem, tot = collections.Counter(), collections.Counter()
    for i, p in enumerate(plan.p):
        for m in materialize(p, researched, pole_item):
            tot[m["item"]] += 1
            if i not in L.built and i not in L.dead and p.get("grp") not in L.retired:
                rem[m["item"]] += 1
    for c_ in (rem, tot):
        c_["assembling-machine-1"] += c_.get("assembling-machine-2", 0)   # asm2 is made from asm1
        c_.update({it: 1 for it in CARGO})
        c_.update(want or {})    # builds outside the plan (outposts, remines) the gate holds for want of these
    return {(p["x"], p["y"]): {it: math.ceil(rem.get(it, 0) * 1.1) + min(RESERVE, tot.get(it, 0) // 10)
                               for it in p["mall"]}
            for i, p in enumerate(plan.p) if p["item"] == "iron-chest" and p.get("mall") and i in L.built}


def mall_limits(plan, L, q, researched, pole_item, limits, want=None):
    """Cap every mall chest at what the unbuilt plan still needs of its items (+10%): the mall stops taking plates
    off the bus for stock nobody will place. Chests fed two items cap each item (LIMIT_LUA), never uncapped.
    A chest's bar keeps one slot even at cap 0, and a maker loads the ingredients of its next craft while it crafts:
    the makers of mall-only items are switched off as well (mall_makers) once their chests hold the cap.
    limits ({"cmd", "makers"}): the commands are kept there (a thread sends them again every 10 s)."""
    caps = mall_caps(plan, L, researched, pole_item, want)
    chests = [[x, y, [[it, c] for it, c in cs.items()]] for (x, y), cs in caps.items()]
    if not chests:
        return "0 chests"
    limits["cmd"] = "/silent-command " + (LIMIT_LUA % json.dumps(chests)).replace(chr(10), " ")
    limits["makers"] = mall_makers(plan, L, caps)
    r = q(limits["cmd"])
    if limits["makers"]:
        r = f"{r}; makers {q(limits['makers'])}"
    return r


# a mall maker (its product only fills mall chests) runs while its item is short of the chests' cap: stock in the
# chests + its output + the craft under way; off (with the inserters loading it) once nothing still needs the item,
# so it stops pulling a silo's 1000 concrete off the bus for a second silo; back on when the cap grows again
MAKER_LUA = r"""
local S=game.surfaces[1] local d=helpers.json_to_table('%s') local stock={}
for _,c in pairs(d.chests) do
  local e=S.find_entities_filtered{position={c[1],c[2]},name='iron-chest'}[1]
  if e then for _,it in pairs(e.get_inventory(defines.inventory.chest).get_contents()) do
    stock[it.name]=(stock[it.name] or 0)+it.count end end end
local on,off=0,0
for _,m in pairs(d.makers) do
  local e=S.find_entities_filtered{position={m[1],m[2]},type='assembling-machine',force='player'}[1]
  if e then
    local out=e.get_inventory(defines.inventory.assembling_machine_output)
    local left=m[4]-(stock[m[3]] or 0)-(out and out.get_item_count(m[3]) or 0)-(e.is_crafting() and m[5] or 0)
    local act=left>0
    if e.active~=act then e.active=act if act then on=on+1 else off=off+1 end end
    for _,i in pairs(S.find_entities_filtered{area={{m[1]-4,m[2]-4},{m[1]+4,m[2]+4}},type='inserter',force='player'}) do
      if i.drop_target==e and i.active~=act then i.active=act end end
  end
end
rcon.print(off..' off, '..on..' on')
"""


def mall_makers(plan, L, caps):
    """MAKER_LUA command for the built makers of mall-only items (no recipe of the plan reads them), or None."""
    cap = {}
    for cs in caps.values():
        for it, c in cs.items():
            cap[it] = max(cap.get(it, 0), c)
    rec = plan.dem.get("recipes") or {}        # demand.json's recipes: which recipes read which items
    read = {i for v in rec.values() for i, _ in v["ing"]}
    makers = []
    for i in L.built:
        p = plan.p[i]
        r = rec.get(p.get("recipe"))
        if not r or "assembling-machine" not in p["item"]:
            continue
        for it, k in r["prod"]:
            if it in cap and it not in read:
                makers.append([p["x"], p["y"], it, cap[it], k])
    if not makers:
        return None
    ch = [[x, y] for (x, y) in caps]
    return "/silent-command " + (MAKER_LUA % json.dumps({"chests": ch, "makers": makers})).replace(chr(10), " ")


CHEST_LUA = r"""
local S=game.surfaces[1] local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type='container',force='player'}[1] local r={} local f=0
  if e then local inv=e.get_inventory(defines.inventory.chest)
    for _,it in pairs(inv.get_contents()) do r[it.name]=(r[it.name] or 0)+it.count end
    f=inv.count_empty_stacks(false,false) end
  o[#o+1]={r,f}
end
rcon.print(helpers.table_to_json(o))
"""


def chest_contents(q, positions, room=False):
    """[{item: count}] of the chest at each (x, y) ({} where none stands); room=True: [({item: count}, empty slots
    below the bar)]."""
    out = []
    for i in range(0, len(positions), 200):
        r = json.loads(q("/silent-command " + (CHEST_LUA % json.dumps([[x, y] for x, y in positions[i:i + 200]]))
                         .replace(chr(10), " ")) or "[]")
        for d in (r if isinstance(r, list) else []):
            c, f = (d + [{}, 0])[:2] if isinstance(d, list) else ({}, 0)    # an empty Lua table prints as []
            out.append((c if isinstance(c, dict) else {}, int(f or 0)))
    out += [({}, 0)] * (len(positions) - len(out))
    return out if room else [c for c, _ in out]


# status of each crafting machine / furnace / drill at the positions, and the recipe ingredients an assembler lacks
MACH_LUA = r"""
local S=game.surfaces[1] local sn={} for k,v in pairs(defines.entity_status) do sn[v]=k end local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type={'assembling-machine','furnace','mining-drill'},force='player'}[1]
  local r={s='missing',m={}}
  if e then r.s=sn[e.status or 0] or 'none'
    if e.type=='assembling-machine' then local rc=e.get_recipe()
      if rc then local inv=e.get_inventory(defines.inventory.assembling_machine_input)
        for _,g in pairs(rc.ingredients) do
          if g.type=='item' and inv.get_item_count(g.name)<g.amount then r.m[#r.m+1]=g.name end
          if g.type=='fluid' and (e.get_fluid_count(g.name) or 0)<g.amount then r.m[#r.m+1]=g.name end
        end end end end
  o[#o+1]=r
end
rcon.print(helpers.table_to_json(o))
"""
STARVED = {"item_ingredient_shortage", "no_ingredients", "fluid_ingredient_shortage", "no_input_fluid",
           "missing_required_fluid"}


def machine_state(q, pieces):
    """[(status, [missing ingredients])] for each machine piece ('missing' where none stands)."""
    out = []
    for i in range(0, len(pieces), 200):
        r = json.loads(q("/silent-command " + (MACH_LUA % json.dumps([[p["x"], p["y"]] for p in pieces[i:i + 200]]))
                         .replace(chr(10), " ")) or "[]")
        for d in (r if isinstance(r, list) else []):
            d = d if isinstance(d, dict) else {}
            out.append((d.get("s", "missing"), list(d.get("m") or []) if isinstance(d.get("m"), list) else []))
    return out + [("missing", [])] * (len(pieces) - len(out))


def maker_pieces(plan, groups, item):
    """Plan indices of the machines in groups that make item: an assembler on a recipe with it among the
    products, a furnace or a drill of a group that makes it."""
    rec = plan.gd["recipes"]
    out = []
    for h in groups:
        for i in plan.groups.get(h, []):
            p = plan.p[i]
            r = p.get("recipe")
            if r and item in {x[0] for x in rec.get(r, {}).get("prod", [])}:
                out.append(i)
            elif not r and ("furnace" in p["item"] or "mining-drill" in p["item"]) and item in plan.made.get(h, ()):
                out.append(i)
    return out


RECLAIM = ("transport-belt", "underground-belt", "inserter", "fast-inserter", "long-handed-inserter",
           "burner-inserter", "assembling-machine-1", "assembling-machine-2", "assembling-machine-3")


def spent_blocks(plan, L, caps, researched, pole_item):
    """Built assembler blocks (B<n>) whose every product nothing needs any more: no unbuilt plan piece is one, no mall
    chest's cap wants one (mall_caps: the unbuilt plan + reserve + want), no machine of another live group takes it
    as an ingredient (the silo, labs, smelters, other blocks), and it is not the rocket's cargo. Measured (seed 1060,
    after the silo stood): B13, the silo + landing pad makers, kept 129 belt tiles of steel, concrete, pipe,
    processing units and electric engines forever, its silo maker switched off by the mall caps.
    Returns [(group, [piece indices to pick up])]: the block's and its band:/conn: feeders' belts, undergrounds,
    inserters and machines that stand (no splitter: a tap sits on the bus; no pole, pipe or chest)."""
    from auto.demand import CARGO
    rem = collections.Counter()
    for i, p in enumerate(plan.p):
        if i not in L.built and i not in L.dead and p.get("grp") not in L.retired:
            for m in materialize(p, researched, pole_item):
                rem[m["item"]] += 1
    capped = {it for cs in caps.values() for it, c in cs.items() if c > 0}
    live = {g for g in plan.prod if g not in L.retired and any(i in L.built for i in plan.groups[g])}
    takes = {}             # group -> what its machines eat (mall chests left out: their caps speak for them)
    for g in live:
        ents = [plan.p[i] for i in plan.groups[g] if not plan.p[i].get("mall")]
        takes[g] = plan._io(g, ents)[1]
    out = []
    for g in sorted(live):
        if not re.match(r"^B\d+$", g):
            continue
        made = {m for m in plan.made[g] if not m.startswith("stock:")}
        if not made or made & CARGO:
            continue
        if any(rem[it] > 0 or it in capped or any(it in takes[h] for h in live if h != g) for it in made):
            continue
        idx = [i for h in (g, "band:" + g, "conn:" + g) for i in plan.groups.get(h, ())
               if i in L.built and i not in L.dead and plan.p[i]["item"] in RECLAIM]
        if idx:
            out.append((g, idx))
    return out


def drain_due(plan, L, q, caps, item=None, skip=None):
    """A built mall chest fed two items that holds more of one than its cap while another (item, if given) is under
    its cap and locked out (no empty slot below the bar) with its maker full_output: (x, y, over item, locked-out
    item), or None. Draining it lets the other item in. Locked out, not only absent: 10 steam engines left in a chest
    full of burner inserters lock out the 14 more the power units need, and nobody takes the 10 while the unit
    waits for all of them. skip: the controller's skip counters (a chest drained lately rests)."""
    mixed = [(xy, cs) for xy, cs in caps.items() if len(cs) > 1 and (item is None or item in cs)
             and (skip or {}).get(f"drain:{xy[0]},{xy[1]}", 0) <= 0]
    if not mixed:
        return None
    cont = chest_contents(q, [xy for xy, _ in mixed], room=True)
    for (xy, cs), (have, room) in zip(mixed, cont):
        over = [it for it in cs if have.get(it, 0) > cs[it]]
        locked = [it for it in cs if have.get(it, 0) < cs[it] and (item is None or it == item)] if room <= 0 else []
        if not over or not locked:
            continue
        for it in locked:
            mk = [i for i in maker_pieces(plan, [h for h in plan.makers.get(it, []) if L.group_done(h)], it)
                  if i in L.built]
            if mk and any(s == "full_output" for s, _ in machine_state(q, [plan.p[i] for i in mk])):
                return xy[0], xy[1], over[0], it
    return None
