"""Decision report: what the rocket still needs, against what the factory can do and does.

    python plan.py decide [--hours 6] [--top 20]

1. Goal: remaining research packs to the silo (crew.silo_path) + the rocket silo + 100 rocket parts.
2. Expand through the game's recipes (plan.ratio's tree) into totals per item, stopping at plates and
   fluids, so every intermediate shows up.
3. Required rate = total / hours. Compare with capacity (machines x speed, bridge `capacity`; furnaces
   from crew.furnace_capacity; ore from the drills) and with what is made and used (production, 10 min).
4. Rank by the gap: the top lines are what to build next; 'OK' lines are what not to touch.
"""
import json

PACK_ITEM = {"red": "automation-science-pack", "green": "logistic-science-pack", "blue": "chemical-science-pack",
             "purple": "production-science-pack", "yellow": "utility-science-pack", "military": "military-science-pack"}
STOP = {"iron-plate", "copper-plate", "stone", "coal", "water", "crude-oil", "petroleum-gas", "light-oil", "heavy-oil",
        "lubricant", "sulfuric-acid", "iron-ore", "copper-ore", "uranium-ore", "wood"}
ORE_OF = {"iron-plate": "iron-ore", "copper-plate": "copper-ore", "stone-brick": "stone"}


def totals(recipes, goal):
    """recipes: name -> (category, energy, ing, prod). goal: {item: count}. Returns {item: count}, {recipe: crafts}."""
    by_product = {}
    for name, (cat, energy, ing, prod) in recipes.items():
        for p, n in prod:
            cur = by_product.get(p)
            if cur is None or name == p:
                by_product[p] = name
    need, crafts = {}, {}
    stack = list(goal.items())
    while stack:
        item, n = stack.pop()
        need[item] = need.get(item, 0) + n
        if item in STOP or item not in by_product:
            continue
        rname = by_product[item]
        cat, energy, ing, prod = recipes[rname]
        out = next(a for p, a in prod if p == item)
        c = n / out
        crafts[rname] = crafts.get(rname, 0) + c
        for i, a in ing:
            stack.append((i, c * a))
    return need, crafts, by_product


def report(b, crew, hours=6.0, top=20):
    data = json.loads(b.raw("/sc " + __import__("plan").RECIPE_LUA))
    recipes = {n: (cat, e, ing, prod) for n, cat, e, ing, prod, en in data}
    path = crew.silo_path()
    goal = {PACK_ITEM.get(k, k): v for k, v in path["packs"].items()}
    if not path["silo_done"]:
        goal["rocket-silo"] = 1
    goal["rocket-part"] = 100
    need, crafts, by_product = totals(recipes, goal)
    cap = crew._dict_of(crew._dict_of(b.call("capacity")).get("recipes"))
    furn = crew.furnace_capacity(b)
    prod = crew._dict_of(b.call("production", window="ten_minutes", min_rate=0))
    made = {k: v.get("made", 0) for kind in ("items", "fluids") for k, v in crew._dict_of(prod.get(kind)).items()}
    ore = crew._dict_of(crew._dict_of(b.call("ore")).get("ore"))
    rows = []
    for item, total in need.items():
        if item in goal and item.endswith("science-pack") is False and item not in ("rocket-part", "rocket-silo"):
            pass
        req = total / (hours * 60)
        if req < 0.2:
            continue
        rname = by_product.get(item)
        st = crew._dict_of(cap.get(rname, {}).get("st")) if rname else {}
        full = st.get("full_output", 0) + st.get("waiting_for_space_in_destination", 0)
        busy = sum(st.values()) or 0
        c = None
        if item in furn:
            c = furn[item]
        elif rname and rname in cap:
            c = cap[rname].get("per_min")
        elif item in STOP and item not in ORE_OF:
            c = None
        m = made.get(item, 0)
        ore_note = ""
        if item in ORE_OF and ORE_OF[item] in ore:
            o = ore[ORE_OF[item]]
            ore_note = f" | ore {o.get('per_min', 0):.0f}/min from {o.get('drills', 0)} drills"
        if c is None:
            verdict = "NO MACHINES" if item not in STOP else "input"
            gap = req
        else:
            gap = req - c
            verdict = "OK" if c >= req else f"BUILD x{req / max(c, 1e-9):.1f} capacity"
            if busy and full * 2 >= busy and c < req:
                verdict += " LATER: machines are output-blocked now, build the consumers first"
        rows.append((gap / max(req, 1e-9), item, total, req, c, m, verdict, ore_note))
    rows.sort(key=lambda r: -r[0])
    out = [f"goal in {hours:g} h: {', '.join(f'{k} {v:g}' for k, v in goal.items())}",
           "item                         total    need/min  capacity/min  made/min  verdict"]
    for _, item, total, req, c, m, verdict, note in rows[:top]:
        out.append(f"{item[:28]:28s} {total:8.0f} {req:10.1f} {('-' if c is None else f'{c:.0f}'):>12s} {m:9.1f}  {verdict}{note}")
    return "\n".join(out)
