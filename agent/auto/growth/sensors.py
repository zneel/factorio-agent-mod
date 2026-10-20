"""Live measurements behind the repairs: dry drills and mines, unpowered machines, jammed belts, stuck inserters,
misfaced machines, starving smelters, measured made rates, coal left."""
import collections
import json
import math
import time

from auto.growth.core import FUEL, log, materialize
from auto.growth.increments import DEAD
from auto.growth.mall import maker_pieces


# a machine's facing sticks only once its recipe has fluid boxes (an assembler placed idle or on a stand-in
# recipe comes up north): every built plan machine is turned to its planned facing (a player's R, nothing else)
DIR_NUM = {"north": 0, "east": 4, "south": 8, "west": 12}
FACE_LUA = r"""
local S=game.surfaces[1] local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type={'assembling-machine','furnace'},force='player'}[1]
  if e and e.direction~=c[3] then
    if c[4]==1 then e.direction=c[3]
      if e.direction==c[3] then o[#o+1]=c[1]..','..c[2] else o[#o+1]='!'..c[1]..','..c[2] end
    else o[#o+1]=c[1]..','..c[2] end end
end
rcon.print(table.concat(o,';'))
"""


def misfaced(plan, L, q, fix=False):
    """Built plan machines (with a planned facing) facing another way live; fix=True turns them. Returns the
    positions found ('!' prefix: the game would not turn it, e.g. a recipe without fluids)."""
    idx = [i for i in L.built if plan.p[i]["item"].startswith(("assembling-machine", "chemical-plant", "oil-refinery"))
           and plan.p[i].get("direction")]
    pos = [[plan.p[i]["x"], plan.p[i]["y"], DIR_NUM.get(plan.p[i].get("direction", "north"), 0), int(fix)] for i in idx]
    out = []
    for k in range(0, len(pos), 300):
        r = q("/silent-command " + (FACE_LUA % json.dumps(pos[k:k + 300])).replace(chr(10), " ")) or ""
        out += [t for t in r.strip().split(";") if t]
    return out


def starving_fuel(plan, L, q):
    """True when half the furnaces of some built smelting group report no fuel."""
    from auto import live as lv
    try:
        st = lv.status(q, plan.box(sorted(L.built), 2)) if L.built else []
    except Exception:
        return False
    at = {(round(x, 2), round(y, 2)): s_ for n, x, y, s_ in st if "furnace" in n}
    for g in plan.prod:
        if g.startswith("smelt") and L.group_done(g):
            fur = [plan.p[i] for i in plan.groups[g] if "furnace" in plan.p[i]["item"]]
            nf = sum(1 for p in fur if at.get((round(p["x"], 2), round(p["y"], 2))) == "no_fuel")
            if fur and nf >= 0.5 * len(fur):
                return True
    return False


def dry_drills(plan, L, q, state, skip=None):
    """(group, [dry drills]) for the first mine group with a drill (plan piece or our own one beside its belts)
    reporting no minable resources; groups where relocation found no spot are left to the re-mine."""
    from auto import live as lv
    skip = skip or {}
    try:
        st = lv.status(q, L.plan.box(sorted(L.built), 2) if L.built else (0, 0, 1, 1))
        own_d = [p for p in L.own if p["item"] == "electric-mining-drill"]
        if own_d:
            st += lv.status(q, (min(p["x"] for p in own_d) - 2, min(p["y"] for p in own_d) - 2,
                                max(p["x"] for p in own_d) + 2, max(p["y"] for p in own_d) + 2))
    except Exception:
        return None
    dry = {(round(x, 2), round(y, 2)) for n, x, y, s_ in st
           if n == "electric-mining-drill" and s_ == "no_minable_resources"}
    off = {tuple(t) for t in state.get("dry_own", [])}     # own drills already written off (no spot to move to)
    for g in sorted(plan.prod):
        if not g.startswith("mine:") or g in L.retired or skip.get("relocate:" + g, 0) > 0 \
                or g in state.get("no_relocate", []):
            continue
        idx = plan.groups[g]
        ds = [plan.p[i] for i in idx if plan.p[i]["item"] == "electric-mining-drill" and i in L.built
              and i not in L.dead and (round(plan.p[i]["x"], 2), round(plan.p[i]["y"], 2)) in dry]
        ds += [p for p in own_beside(plan, L, g) if (round(p["x"], 2), round(p["y"], 2)) in dry - off]
        if ds:
            return g, ds
    return None


DV4 = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def own_beside(plan, L, g):
    """Our own (relocated) drills that drop onto group g's belts."""
    bt = {(math.floor(plan.p[i]["x"]), math.floor(plan.p[i]["y"])) for i in plan.groups[g]
          if plan.p[i]["item"] == "transport-belt"}
    out = []
    for p in L.own:
        if p["item"] == "electric-mining-drill":
            f = DV4.get(p.get("direction", "north"), (0, -1))
            if (math.floor(p["x"]) + 2 * f[0], math.floor(p["y"]) + 2 * f[1]) in bt:
                out.append(p)
    return out


def dry_mine(plan, L, q, state, skip=None):
    """A built, not retired mine group whose dry drills found no spot to move to (no_relocate), with >= 70% of its
    standing drills (plan drills and our own beside its belt) reporting no minable resources: measured live every
    time. A group found dry before goes on to the remine only while that holds, or when its drills are picked up
    already; one that measures below 70% is not dry any more (one dry drill never retires a mining group)."""
    from auto import live as lv
    try:
        st = lv.status(q, plan.box(sorted(L.built), 2)) if L.built else []
        own_d = [p for p in L.own if p["item"] == "electric-mining-drill"]
        if own_d:
            st += lv.status(q, (min(p["x"] for p in own_d) - 2, min(p["y"] for p in own_d) - 2,
                                max(p["x"] for p in own_d) + 2, max(p["y"] for p in own_d) + 2))
    except Exception:
        return None
    at = {(round(x, 2), round(y, 2)): s_ for n, x, y, s_ in st if n == "electric-mining-drill"}
    for g in sorted(plan.prod):
        if not g.startswith("mine:") or g in L.retired or (skip or {}).get("remine:" + g, 0) > 0:
            continue
        if g not in state.get("no_relocate", []):
            continue                      # drills are moved one by one first (dry_drills / auto.relocate)
        dr = [i for i in plan.groups[g] if plan.p[i]["item"] == "electric-mining-drill"]
        if g in state.get("dry", ()) and dr and not any(i in L.built for i in dr):
            return g                      # found dry before and its drills are picked up already: remine on
        if not L.group_done(g):
            continue
        stand = [plan.p[i] for i in dr if i in L.built] + own_beside(plan, L, g)
        nd = sum(1 for p in stand if at.get((round(p["x"], 2), round(p["y"], 2))) == "no_minable_resources")
        if stand and nd >= 0.7 * len(stand):
            if g not in state.setdefault("dry", []):
                state["dry"].append(g)
            return g
        if g in state.get("dry", ()):
            state["dry"].remove(g)
            log(f"{g}: {nd} of {len(stand)} standing drills dry (< 70%): not dry, it keeps supplying")
    return None


def unpowered(plan, L, q, state):
    """A built group with an electric machine reporting no power while the plant has capacity."""
    from auto import live as lv
    try:
        if not L.built or not lv.power(q).get("cap"):
            return None
        st = lv.status(q, plan.box(range(len(plan.p)), 8))     # the whole plan area: our own machines too
    except Exception:
        return None
    for n, x, y, s_ in st:
        if s_ == "no_power":
            i = plan.key.get((round(x, 2), round(y, 2)))
            if i is not None and i in L.built:
                return plan.p[i].get("grp")
            if i is None and n != "inserter":       # a machine of our own (the bootstrap lab): poles for it alone
                return f"extra@{n}@{x}@{y}"
    return None


def flushes(state, g, L):
    """Flushes of g since the map last changed: a jam that comes back on the same belts is not flushed forever,
    but once something new stands (the missing side of a lane merge) it may be flushed again."""
    if state.get("flush_at", {}).get(g) != len(L.built):
        return 0
    return state.get("flushed", {}).get(g, 0)


def jammed(plan, L, q, state):
    """A built smelting group with most furnaces starved and belts that carry items but none of its ore."""
    from auto import live as lv
    try:
        st = lv.status(q, plan.box(sorted(L.built), 2)) if L.built else []
    except Exception:
        return None
    at = {(round(x, 2), round(y, 2)): s for n, x, y, s in st}
    for g in sorted(plan.prod):
        if not g.startswith("smelt") or not L.group_done(g) or flushes(state, g, L) >= 3:
            continue
        fur = [plan.p[i] for i in plan.groups[g] if "furnace" in plan.p[i]["item"]]
        starved = sum(1 for p in fur if at.get((round(p["x"], 2), round(p["y"], 2))) in ("no_ingredients",
                                                                                        "item_ingredient_shortage"))
        if not fur or starved < 0.6 * len(fur):
            continue
        ore = plan.need[g] - FUEL
        belts = [i for i in plan.groups[g] if plan.p[i]["item"] == "transport-belt"]
        # backed up (a moving belt at start-up is sparse) and still so a few minutes later: stale load, not start-up
        lanes = lv.belt_lanes(q, [(plan.p[i]["x"], plan.p[i]["y"]) for i in belts])
        # stale fuel on the ore lane: packed, the same item on both lanes, no ore (one lane of coal and an empty ore
        # lane is a mine that delivers nothing, not a jam)
        bad = [i for i, (a_, b_) in zip(belts, lanes) if sum(a_.values()) + sum(b_.values()) >= 5 and
               len(a_) == 1 and set(a_) == set(b_) and not set(a_) & ore]
        seen = state.setdefault("jam_seen", {})
        if len(bad) >= 3:
            seen.setdefault(g, time.time())
            if time.time() - seen[g] >= 180:
                state.setdefault("jam", {})[g] = bad
                seen.pop(g, None)
                return g
        else:
            seen.pop(g, None)
    return jammed_block(plan, L, q, state, at)


def jammed_block(plan, L, q, state, at):
    """An assembler starved of an ingredient whose input belt is packed (>= 6 items a tile) without that
    ingredient: stale load from a lane mix-up blocks the lane. The jammed stretch = the belts of that line in the
    group, both ways from the read tile, that are packed and lack the ingredient."""
    from auto import live as lv
    f = plan.flow
    rec = plan.gd["recipes"]
    seen = state.setdefault("jam_seen", {})
    for g in sorted(plan.prod):
        if not L.group_done(g) or flushes(state, g, L) >= 6:
            continue
        mach = [i for i in plan.groups[g] if plan.p[i].get("recipe") and
                at.get((round(plan.p[i]["x"], 2), round(plan.p[i]["y"], 2))) == "item_ingredient_shortage"]
        for m in mach[:6]:
            ings = {x[0] for x in rec.get(plan.p[m]["recipe"], {}).get("ing", [])}
            reads = [j for ins in f.fed_by[m] if "inserter" in plan.p[ins]["item"] for j in f.fed_by[ins]
                     if plan.p[j]["item"] == "transport-belt"]
            if not reads:
                continue
            cont = lv.belt_items(q, [(plan.p[j]["x"], plan.p[j]["y"]) for j in reads])
            have = set().union(*[set(c) for c in cont])
            packed = [j for j, c in zip(reads, cont) if sum(c.values()) >= 6]
            missing = ings - have
            if not packed or not missing:
                continue
            # the belt line through the packed read tile, inside the group, both directions
            line, fr = set(packed), list(packed)
            while fr:
                nx_ = []
                for i in fr:
                    for j in list(f.feeds[i]) + list(f.fed_by[i]):
                        if j not in line and plan.p[j]["item"] == "transport-belt" and plan.p[j].get("grp") == g:
                            line.add(j)
                            nx_.append(j)
                fr = nx_
            line = sorted(line)
            lc = lv.belt_lanes(q, [(plan.p[j]["x"], plan.p[j]["y"]) for j in line])
            # the mix-up signature: packed, one and the same item on both lanes, none of what the machine lacks
            bad = [j for j, (a_, b_) in zip(line, lc) if sum(a_.values()) + sum(b_.values()) >= 6 and
                   len(a_) == 1 and set(a_) == set(b_) and not set(a_) & missing]
            key = f"{g}@{m}"
            if len(bad) >= 3:
                seen.setdefault(key, time.time())
                if time.time() - seen[key] >= 180:
                    state.setdefault("jam", {})[g] = bad
                    seen.pop(key, None)
                    return g
            else:
                seen.pop(key, None)
    return None


# inserters waiting for space in an assembler while holding an item its recipe does not take (they never drop it)
STUCK_LUA = r"""
local S=game.surfaces[1] local o={} local W=defines.entity_status.waiting_for_space_in_destination
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},type='inserter',force='player'}) do
  local h=e.held_stack local t=e.drop_target
  if e.status==W and h and h.valid_for_read and t and t.type=='assembling-machine' then
    local r=t.get_recipe() local ok=false
    if r then for _,g in pairs(r.ingredients) do if g.name==h.name then ok=true end end end
    if r and not ok then o[#o+1]=e.name..'|'..e.position.x..'|'..e.position.y..'|'..h.name..'|'..r.name end
  end
end
rcon.print(table.concat(o,';'))
"""


def stuck_inserters(plan, L, q, state, tries=2):
    """[(place, held item, recipe)] for stuck inserters we know how to lay again (plan pieces, our own pieces),
    each at most `tries` times."""
    from auto import live as lv
    x1, y1, x2, y2 = plan.box(range(len(plan.p)), 8)
    own = {_k(p["x"], p["y"]): p for p in state.get("extra_places", []) if p["item"].endswith("inserter")}
    done = state.get("unstuck", {})
    out = []
    for n, x, y, held, rec in lv._run(q, STUCK_LUA, (x1, y1, x2, y2)):
        x, y = float(x), float(y)
        if done.get(f"{x},{y}", 0) >= tries:
            continue
        i = plan.key.get(_k(x, y))
        if i is not None and plan.p[i]["item"] == n:
            p = materialize(plan.p[i], L.researched, "small-electric-pole")[0]     # an inserter: no pole kind
        elif _k(x, y) in own and own[_k(x, y)]["item"] == n:
            p = {k: v for k, v in own[_k(x, y)].items() if k in ("item", "x", "y", "direction")}
        else:
            continue
        out.append((dict(p, x=x, y=y), held, rec))
    return out


FULL = {"full_output", "waiting_for_space_in_destination", "output_full"}
ITEM_STARVED = {"item_ingredient_shortage", "no_ingredients"}


def made_rates(plan, L, snap, status=None):
    """{group: {item: per second}} for built producing groups: the measured 10-minute made rate of each tracked
    item, split over its built makers by their machine count (the game's statistics are per force, not per group).
    status ((x, y) -> live status): a group whose makers of the item stand full is output-bound, its rate measures
    the demand, not what it can make: left out (the schedule takes the nameplate rate there). So is a group whose
    makers starve of items off the bus (assemblers, the steel furnaces): its rate measures the plates the bus brings,
    which the schedule's item levels model themselves (passed on, a frozen bus reads 0/min for every mall item until
    the next solve). A starved ore smelter stays measured: its ore comes over its own mine's belt, which the levels
    do not see."""
    made = (snap or {}).get("made") or {}
    status = status or {}
    out = collections.defaultdict(dict)
    for it, per_min in made.items():
        mk = [h for h in plan.makers.get(it, []) if L.group_done(h) and h not in DEAD]
        idx = {h: [i for i in maker_pieces(plan, [h], it) if i in L.built] for h in mk}
        tot = sum(len(v) for v in idx.values())
        for h in mk:
            st = [status.get(_k(plan.p[i]["x"], plan.p[i]["y"])) for i in idx[h]]
            full = sum(1 for s in st if s in FULL)
            ore_fed = h.startswith("smelt") and all(any(m.startswith("mine:") for m in plan.makers.get(x, ()))
                                                     for x in plan.need.get(h, set()) - FUEL)
            starved = 0 if ore_fed else sum(1 for s in st if s in ITEM_STARVED)
            if idx[h] and full * 2 < len(idx[h]) and starved * 2 < len(idx[h]):
                out[h][it] = round(per_min / 60 * len(idx[h]) / tot, 5)
    return dict(out) or None


COAL_LUA = ("local a=0 for _,r in pairs(game.surfaces[1].find_entities_filtered{area={{%d,%d},{%d,%d}},"
            "type='resource',name='coal'}) do a=a+r.amount end rcon.print(a)")


def coal_left(q, plan):
    """Coal left in the ground under the plan's coal drills (the box of every mine:coal group, mining area in)."""
    idx = [i for g in plan.groups if g.startswith("mine:coal") for i in plan.groups[g]
           if plan.p[i]["item"] == "electric-mining-drill"]
    if not idx:
        return None
    x1, y1, x2, y2 = plan.box(idx, 3)
    return int(float(q("/silent-command " + COAL_LUA % (math.floor(x1), math.floor(y1), math.ceil(x2), math.ceil(y2)))
                     or 0))


def _k(x, y):
    return round(x, 2), round(y, 2)
