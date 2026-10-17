"""Factorio 2.0 base-game rules (factoriocheatsheet.com + the game's prototypes) that plan.py enforces.
Rates are items per second without inserter-capacity research (this force has bonus 0; re-check with
force.inserter_stack_size_bonus after researching inserter capacity)."""

BELT_LANE = {"transport-belt": 7.5, "fast-transport-belt": 15, "express-transport-belt": 22.5}
UNDERGROUND_GAP = {"underground-belt": 4, "fast-underground-belt": 6, "express-underground-belt": 8}
PIPE_TO_GROUND_GAP = 10
# chest-to-chest swing rates, hand size 1; picking from a moving belt is a little slower (BELT_PICKUP)
INSERTER = {"burner-inserter": 0.6, "inserter": 0.83, "long-handed-inserter": 1.15, "fast-inserter": 2.31,
            "bulk-inserter": 2.31}
BELT_PICKUP = 0.85
UPGRADE = {"burner-inserter": "inserter", "inserter": "fast-inserter", "long-handed-inserter": "fast-inserter"}
MACHINE_SPEED = {"assembling-machine-1": 0.5, "assembling-machine-2": 0.75, "assembling-machine-3": 1.25,
                 "stone-furnace": 1, "steel-furnace": 2, "electric-furnace": 2, "chemical-plant": 1,
                 "oil-refinery": 1}
DRILL_RATE = {"electric-mining-drill": 0.5, "burner-mining-drill": 0.25}  # ore/s (x mining productivity)
# build ratios (machines of equal tier)
RATIOS = {
    "green circuits": "3 copper-cable : 2 electronic-circuit assemblers",
    "steam": "1 offshore pump : 20 boilers : 40 steam engines (36 MW)",
    "oil": "advanced oil processing: 20 refineries : 5 heavy-oil cracking : 17 light-oil cracking plants",
    "smelting": "a yellow belt of ore (15/s) feeds 48 stone or 24 steel/electric furnaces",
    "steel": "a steel furnace making steel eats 0.625 iron/s; a yellow belt of iron feeds 24 of them",
    "mining": "30 electric drills fill a yellow belt",
    "science (AM2, packs/min each)": "red 9, green 7.5, blue 3.75, purple 6.43, yellow 6.43",
}


def inserter_capacity(kind, from_belt):
    return INSERTER.get(kind, 0.83) * (BELT_PICKUP if from_belt else 1.0)


def block_crafts(machines, recipes, target):
    """Crafts/s each machine must do to make `target` {item: items/s} out of this block: walk the recipe
    tree back through the recipes the block's machines run, split each recipe's demand evenly over its
    machines, and cap at what a machine can do (that cap is itself a finding)."""
    by_out = {}
    for mid, m in machines.items():
        r = recipes.get(m["recipe"])
        if r:
            by_out.setdefault(r[1][0][0], []).append(mid)
    need = {}
    stack = list(target.items())
    while stack:
        item, rate = stack.pop()
        if item not in by_out:
            continue
        mids = by_out[item]
        r = recipes[machines[mids[0]]["recipe"]]
        crafts = rate / r[1][0][1]
        for mid in mids:
            need[mid] = need.get(mid, 0) + crafts / len(mids)
        for ing, n in r[0]:
            stack.append((ing, crafts * n))
    return need


def check_rates(places, machines, recipes, target=None):
    """Per inserter: items/s needed through it (from the block's target output, or every machine at full
    speed when no target is given), against its capacity. Returns warnings."""
    warn = []
    rate_in = {}   # (machine id, item) -> items/s the machine needs
    rate_out = {}  # machine id -> items/s it makes
    demand = block_crafts(machines, recipes, target) if target else {}
    for mid, m in machines.items():
        r = recipes.get(m["recipe"])
        if not r or len(r) < 3:
            continue
        ing, prod, energy = r[0], r[1], r[2]
        cap = MACHINE_SPEED.get(m.get("entity", "assembling-machine-2"), 0.75) / max(energy, 1e-9)
        crafts = min(demand.get(mid, cap), cap) if target else cap
        if target and demand.get(mid, 0) > cap * 1.01:
            warn.append(f"machine {mid} ({m['recipe']}) must do {demand[mid]:.2f} crafts/s, can do {cap:.2f}: add a machine")
        for item, n in ing:
            rate_in[(mid, item)] = crafts * n
        rate_out[mid] = crafts * prod[0][1]
    for p in places:
        if p.get("group") != "inserters":
            continue
        src, _, rest = p.get("why", "").partition("->")
        dst, _, items = rest.partition(" ")
        need = 0.0
        for item in items.split("+"):
            if (dst, item) in rate_in:
                need += rate_in[(dst, item)]
            elif src in rate_out:
                need = max(need, rate_out[src])
        from_belt = src.startswith(("BUS", "T", "U")) and src not in machines
        cap = inserter_capacity(p["item"], from_belt)
        if need > cap * 0.95:
            up = UPGRADE.get(p["item"])
            warn.append(f"{p['item']} {p['x']:g},{p['y']:g} ({p.get('why')}) needs {need:.2f}/s, carries {cap:.2f}/s"
                        + (f": use {up}" if up else ": add a second inserter"))
    return warn
