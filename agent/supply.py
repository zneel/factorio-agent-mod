"""Material logistics for algorithm jobs: the depot, raw-material expansion, fetching, and building a plan.

    python supply.py <character> build <plan.json> [--minutes 8]    fetch what the plan needs, craft, build
    python supply.py <character> deposit                           put surplus plates/stone/coal in the depot

The depot is a pair of iron chests at DEPOT. Scripts deposit surplus there and fetch from it first, then from
any chest or furnace holding the item (bridge `stock`), so characters never hoard what others need.
"""
import argparse
import collections
import json
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from bridge import Bridge, BridgeError  # noqa: E402
import macros  # noqa: E402

DEPOT = [(-60.5, -55.5), (-59.5, -55.5), (-58.5, -55.5), (-57.5, -55.5)]
if os.environ.get("FA_DEPOT"):   # "x,y": the depot's first chest for this map (the staged builder picks a free spot)
    _dx, _dy = (float(v) for v in os.environ["FA_DEPOT"].split(","))
    DEPOT = [(_dx + i, _dy) for i in range(4)]
KEEP = {"iron-plate": 60, "copper-plate": 40, "stone": 20, "coal": 30, "steel-plate": 50, "stone-brick": 0,
        "iron-gear-wheel": 40, "electronic-circuit": 40, "copper-cable": 40, "wood": 20}
BASE = {"iron-plate", "copper-plate", "stone", "wood", "coal", "steel-plate", "stone-brick", "plastic-bar", "sulfur",
        "solid-fuel", "iron-ore", "copper-ore", "advanced-circuit", "processing-unit", "engine-unit", "battery",
        "low-density-structure", "rocket-fuel", "electric-engine-unit", "flying-robot-frame", "uranium-ore"}
RECIPES = json.loads((ROOT / "runtime" / "recipes.json").read_text())


def inv(b):
    return collections.Counter(b.call("state")["agent"].get("inventory") or {})


def expand(need, have):
    """Item counts -> base materials still missing, using what is carried (intermediates count)."""
    have = collections.Counter(have)
    base = collections.Counter()
    todo = list(need.items())
    while todo:
        it, n = todo.pop()
        use = min(n, have[it])
        have[it] -= use
        n -= use
        if n <= 0:
            continue
        r = RECIPES.get(it)
        if it in BASE or not r:
            base[it] += n
            continue
        ings, prods, _ = r
        out = next((c for p, c in prods if p == it), 1)
        crafts = math.ceil(n / out)
        extra = crafts * out - n
        if extra:
            have[it] += extra
        for ing, c in ings:
            todo.append((ing, c * crafts))
    return base


def depot_ok(b):
    for x, y in DEPOT:
        ents = b.call("probe", x=x, y=y).get("entities") or []
        if not any(e["name"] in ("iron-chest", "wooden-chest") for e in ents):
            return False
    return True


def ensure_depot(b):
    if depot_ok(b):
        return True
    have = inv(b)
    if have["iron-plate"] < 16 and have["iron-chest"] < 2:
        return False
    r = macros.build(b, [{"item": "iron-chest", "x": x, "y": y} for x, y in DEPOT])
    return bool(r.get("placed") or r.get("already"))


def deposit(b, keep=None, everything=True):
    """Put surplus in the depot: plates above KEEP, and (everything=True) every crafted part, so the next build
    by any character can fetch it instead of crafting it again."""
    keep = dict(KEEP, **(keep or {}))
    have = inv(b)
    extra = {k: have[k] - v for k, v in keep.items() if have[k] > v + 10}
    if everything:
        extra.update({k: n for k, n in have.items() if k not in keep and n > 0})
    extra = {k: v for k, v in extra.items() if v > 0}
    if not extra or not ensure_depot(b):
        return {}
    pos = b.call("state")["agent"]["position"]
    done = {}
    for x, y in DEPOT:
        if not extra:
            break
        try:
            pos = macros._near(b, pos, (x, y))
            for k in list(extra):
                r = b.call("insert", item=k, count=extra[k], x=x, y=y)
                n = (r.get("inserted") if isinstance(r, dict) else 0) or 0
                done[k] = done.get(k, 0) + n
                extra[k] -= n
                if extra[k] <= 0:
                    extra.pop(k)
        except BridgeError:
            continue
    return done


HOLD_LUA = ("local it=%s local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{force='player',"
            "type={'container','furnace','assembling-machine'}}) do local inv=(e.type=='container' and e.get_inventory(defines.inventory.chest)) "
            "or (e.type=='furnace' and e.get_inventory(defines.inventory.furnace_result)) "
            "or (e.type=='assembling-machine' and e.get_inventory(defines.inventory.assembling_machine_output)) "
            "if inv then local c=inv.get_item_count(it) if c>0 then o[#o+1]=e.position.x..','..e.position.y..','..c end end end "
            "rcon.print(table.concat(o,';'))")


HOLD_IN_LUA = ("local it=%s local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{force='player',"
               "type={'assembling-machine'}}) do local inv=e.get_inventory(defines.inventory.assembling_machine_input) "
               "if inv then local c=inv.get_item_count(it) if c>0 then o[#o+1]=e.position.x..','..e.position.y..','..c end end end "
               "rcon.print(table.concat(o,';'))")


STOCK_LUA = ("local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{force='player',"
             "type={'container','furnace','assembling-machine'}}) do local inv=(e.type=='container' and e.get_inventory(defines.inventory.chest)) "
             "or (e.type=='furnace' and e.get_inventory(defines.inventory.furnace_result)) "
             "or (e.type=='assembling-machine' and e.get_inventory(defines.inventory.assembling_machine_output)) "
             "if inv then for _,it in pairs(inv.get_contents()) do o[it.name]=(o[it.name] or 0)+it.count end end end "
             "rcon.print(helpers.table_to_json(o))")


def reachable_stock(q):
    """{item: count} the builders' fetch can take: the inventories HOLD_LUA reads (chests, furnace results,
    assembler outputs of force player), whole surface. q: a raw RCON callable (Bridge(...).raw)."""
    try:
        r = json.loads(q("/silent-command " + STOCK_LUA).strip() or "{}")
    except ValueError:
        return {}
    return {k: int(v) for k, v in r.items()} if isinstance(r, dict) else {}   # an empty Lua table prints as []


def game_tick(b):
    """game.tick, or None when unreadable (build limits are game time: at 20x a wall minute is 20 game minutes)."""
    try:
        return int(b.raw("/sc rcon.print(game.tick)").strip())
    except ValueError:
        return None


def holders(b, item, inputs=False):
    """(x, y, count) of every chest / furnace output / assembler output holding item; inputs=True: assembler
    inputs instead (last resort for an item the mall does not make and no machine puts out, e.g. engine units)."""
    out = b.raw("/sc " + (HOLD_IN_LUA if inputs else HOLD_LUA) % json.dumps(item)).strip()
    res = []
    for t in out.split(";"):
        try:
            x, y, c = t.split(",")
            res.append((float(x), float(y), int(float(c))))
        except ValueError:
            pass
    return res


FROM_INPUTS = {"engine-unit", "electric-engine-unit", "advanced-circuit", "processing-unit"}   # not hand-craftable


def fetch(b, item, n):
    """Take up to n of item from wherever it sits (depot and other chests, furnace and assembler outputs),
    nearest first, preferring big stacks."""
    got = 0
    pos = b.call("state")["agent"]["position"]
    spots = holders(b, item) or (holders(b, item, inputs=True) if item in FROM_INPUTS else [])
    spots.sort(key=lambda s: ((s[0] - pos["x"]) ** 2 + (s[1] - pos["y"]) ** 2) ** 0.5 - 2 * min(s[2], n))
    for x, y, c in spots[:30]:
        if got >= n:
            break
        before = inv(b)[item]
        try:
            pos = macros._near(b, pos, (x, y))
            b.call("take", x=x, y=y, item=item, count=n - got)
        except BridgeError:
            continue
        got += inv(b)[item] - before
    return got


STACK = {"coal": 50, "stone": 50, "iron-ore": 50, "copper-ore": 50, "wood": 100, "iron-chest": 50,
         "electric-mining-drill": 50, "stone-furnace": 50, "assembling-machine-1": 50, "assembling-machine-2": 50,
         "lab": 10, "boiler": 50, "steam-engine": 10, "offshore-pump": 20, "splitter": 50, "underground-belt": 50,
         "chemical-plant": 10, "oil-refinery": 10, "pumpjack": 20, "rocket-silo": 1}


def free_slots(b):
    """Free main-inventory slots (stack sizes: STACK, else 100), of 80."""
    have = inv(b)
    return 80 - sum(math.ceil(n / STACK.get(k, 100)) for k, n in have.items() if n > 0)


def unload(b, need, low=12):
    """Too full to pick up what a build needs (a flush brings the belts' load along): everything the build does
    not need goes to the depot, which grows a chest at a time when it is full."""
    if free_slots(b) >= low:
        return 0
    keep = {k: need.get(k, 0) for k in inv(b)}
    keep.update({k: max(keep.get(k, 0), v) for k, v in (("coal", 20), ("wood", 20)) })
    for extra in range(len(DEPOT), 13):
        deposit(b, keep=keep)
        if free_slots(b) >= low:
            break
        x0, y0 = DEPOT[0]
        nxt = (x0 + extra, y0)          # the depot row grows east
        if not inv(b)["iron-chest"]:
            fetch(b, "iron-chest", 1)
        if not inv(b)["iron-chest"]:
            break
        if macros.build(b, [{"item": "iron-chest", "x": nxt[0], "y": nxt[1]}]).get("placed"):
            DEPOT.append(nxt)
    return free_slots(b)


def build_plan(b, plan, minutes=8):
    """Fetch the base materials for every unbuilt entry, craft, build. Returns the build result + shortages.
    The limit is game time (minutes*3600 ticks: at 20x, 15 wall minutes were 5 game hours), capped at minutes*60
    wall seconds (a paused game). Mall phase (not macros.HANDCRAFT): never waits for an item no reachable source
    holds; it places what it can and returns done=False, short={item: n missing} at once."""
    items = macros._plan_list(plan)
    t0, wall_end = game_tick(b), time.time() + minutes * 60

    def late():
        t = game_tick(b) if t0 is not None else None
        return time.time() > wall_end or (t is not None and t > t0 + minutes * 3600)
    while True:
        dry = macros.build(b, items, dry=True)
        if dry["to_place"]:
            unload(b, collections.Counter(dry.get("need") or {}))
        if not dry["to_place"]:
            return {"done": True, "already": dry["already"]}
        need = collections.Counter(dry.get("need") or {})
        have = inv(b)
        for it, n in need.items():  # finished parts first (depot, chests), then raw materials for the rest
            if have[it] < n:
                fetch(b, it, n - have[it])
        if not macros.HANDCRAFT:   # mall phase: finished parts only (poles from wood are still hand-made)
            hand = collections.Counter({k: v for k, v in need.items() if k in macros.ALWAYS_HAND})
            base = expand(hand, inv(b))
            have = inv(b)
            short = {k: v - have[k] for k, v in need.items() if k not in macros.ALWAYS_HAND and have[k] < v}
        else:
            base = expand(need, inv(b))
            short = {}
        for it, n in base.items():
            if it in ("iron-ore", "copper-ore"):
                continue
            got = fetch(b, it, n)
            if it == "wood" and got < n:  # wood comes from trees: 4 per tree
                try:
                    b.run("mine", name="tree", count=math.ceil((n - got) / 4), timeout=600)
                except BridgeError:
                    pass
                got = n if inv(b)["wood"] >= n else got
            if got < n:
                short[it] = n - got
        r = macros.build(b, items)
        if not r.get("short") and not r.get("failed"):
            dry = macros.build(b, items, dry=True)
            if not dry["to_place"]:
                deposit(b)
                return {"done": True, "placed": r.get("placed"), "already": dry["already"]}
        if r.get("failed") or late():
            deposit(b)
            return {"done": False, "result": r, "short": short or r.get("short")}
        if not macros.HANDCRAFT:
            # nothing waits for the mall: another pass only fetches what some chest or machine output still holds
            # (fetch takes the 30 nearest holders), and only while passes still place pieces
            lack = short or r.get("short") or {}
            held = reachable_stock(b.raw) if lack and r.get("placed") else {}   # one surface scan, not one per item
            if not r.get("placed") or (lack and not any(held.get(k, 0) > 0 for k in lack)):
                deposit(b)
                return {"done": False, "result": r, "short": lack or None}
        elif short:  # bootstrap: wait for the furnaces, then try again
            b.run("wait", seconds=20, timeout=120)


def craft_items(b, item, n):
    """Hand-craft n of item, fetching the base materials first."""
    for it, k in expand(collections.Counter({item: n}), inv(b)).items():
        fetch(b, it, k)
    try:
        b.run("craft", recipe=item, count=n, wait=True, timeout=900)
    except BridgeError as e:
        return str(e)[:120]
    for _ in range(240):
        a = b.call("state")["agent"]
        if not a.get("crafting_queue"):
            break
        time.sleep(0.5)
    return inv(b)[item]


def feed_labs(b, plan, item, n):
    """Bootstrap only (before assemblers exist): craft n packs and split them over the plan's labs."""
    have = craft_items(b, item, n)
    labs = [p for p in macros._plan_list(plan) if p["item"] == "lab"]
    pos = b.call("state")["agent"]["position"]
    per = max(1, inv(b)[item] // max(1, len(labs)))
    for p in labs:
        pos = macros._near(b, pos, (p["x"], p["y"]))
        try:
            b.call("insert", item=item, count=per, x=p["x"], y=p["y"])
        except BridgeError:
            pass
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("char")
    ap.add_argument("cmd")
    ap.add_argument("plan", nargs="?")
    ap.add_argument("item", nargs="?")
    ap.add_argument("n", nargs="?", type=int)
    ap.add_argument("--minutes", type=float, default=8)
    a = ap.parse_args()
    b = Bridge(a.char)
    if a.cmd == "labs":
        print("crafted", feed_labs(b, a.plan, a.item, a.n), a.item, "into the labs of", a.plan)
    elif a.cmd == "deposit":
        print("deposited", deposit(b))
    elif a.cmd == "build":
        r = build_plan(b, a.plan, a.minutes)
        if r.get("done"):
            print(f"DONE {a.plan}: placed {r.get('placed', 0)}, already {r.get('already')}")
        else:
            res = r.get("result") or {}
            print(f"PARTIAL {a.plan}: placed {res.get('placed', 0)}, short {r.get('short')}, failed {res.get('failed')}")


if __name__ == "__main__":
    main()
