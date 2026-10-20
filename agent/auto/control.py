"""Production controller: real rates from the running game against targets, and what to do about the gaps.

    snap = measure(q, items)            items/min made and used per item (last 10 game minutes), plus stock in chests
    gaps = deficits(snap, targets)      targets: item -> items/min; returns [(item, made, target, ratio)] worst first
    need = build_need(stages, done)     item -> count the next unbuilt stages place (what the mall should make)
    plan = mall_assign(need, stock, slots, recipes)   recipe per mall slot: biggest shortfall first

The controller runs in the staged builder's loop; it logs a line per round so a run can be read back
(runtime/auto/<seed>/control.log).
"""
import json
from collections import Counter

MEASURE_LUA = r"""
local F=game.forces.player local S=game.surfaces[1]
local st=F.get_item_production_statistics(S) local fs=F.get_fluid_production_statistics(S) local o={made={},used={},stock={}}
for _,n in pairs(helpers.json_to_table('%s')) do
  local s_=prototypes.item[n] and st or (prototypes.fluid[n] and fs or nil)
  if s_ then
  o.made[n]=s_.get_flow_count{name=n,category='input',precision_index=defines.flow_precision_index.ten_minutes,count=true}/10
  o.used[n]=s_.get_flow_count{name=n,category='output',precision_index=defines.flow_precision_index.ten_minutes,count=true}/10
  end
end
for _,c in pairs(S.find_entities_filtered{type={'container'},force='player'}) do
  for _,it in pairs(c.get_inventory(defines.inventory.chest).get_contents()) do o.stock[it.name]=(o.stock[it.name] or 0)+it.count end
end
o.tick=game.tick
rcon.print(helpers.table_to_json(o))
"""


def measure(q, items):
    """Per-minute production and consumption (10-minute window) and the stock held in chests."""
    return json.loads(q("/silent-command " + (MEASURE_LUA % json.dumps(sorted(items))).replace("\n", " ")))


def deficits(snap, targets, floor=0.9):
    """Items made below floor x target, worst ratio first."""
    out = []
    for it, t in targets.items():
        if t <= 0:
            continue
        m = (snap.get("made") or {}).get(it, 0)
        if m < floor * t:
            out.append((it, round(m, 1), round(t, 1), round(m / t, 2)))
    return sorted(out, key=lambda r: r[3])


def lp_targets(dem):
    """items/min the finished factory must make (LP crafts x products)."""
    t = Counter()
    for r, c in dem["crafts_per_min"].items():
        for p, n in dem["recipes"][r]["prod"]:
            t[p] += c * n
    return dict(t)


def build_need(stages, done, root, lookahead=3):
    """Items the next `lookahead` unbuilt stages place, by count."""
    need = Counter()
    k = 0
    for r in stages:
        if r["stage"] in done:
            continue
        for p in json.loads((root / r["file"]).read_text())["place"]:
            if not p.get("remove"):
                need[p["item"]] += 1
        k += 1
        if k >= lookahead:
            break
    return need


def mall_assign(need, stock, slots, makeable):
    """Recipe per mall slot: the items with the biggest shortfall (need - stock) that the mall can make, one slot
    each, the biggest shortfall getting extra slots when there are more slots than items."""
    short = sorted(((it, n - stock.get(it, 0)) for it, n in need.items() if it in makeable),
                   key=lambda kv: -kv[1])
    short = [(it, s) for it, s in short if s > 0]
    if not short:
        return []
    out = [it for it, _ in short[:slots]]
    i = 0
    while len(out) < slots:      # spare slots go to the largest shortfalls in turn
        out.append(short[i % len(short)][0])
        i += 1
    return out
