"""Stage 2: what the rocket costs and at what rates, from the game's own recipes and tech tree.

    python -m auto.demand --seed N --hours 8 [--rocket-hours 2] [--out demand.json]
                                                        -> runtime/auto/<seed>/demand.json

A run has two phases, and the factory is sized for both (plan_lp.solve_phases, scipy/HiGHS multi-period LP):
  research  every research pack for the techs the plan needs (closure of rocket-silo plus the techs that unlock
            each recipe the solution uses) + the mall: every entity the characters place (counted from the seed's
            previous plan.json, else BUILD_PRIOR), made by machines into chests; hours - rocket_hours long
  rocket    after rocket-silo research: the silo, ROCKET_PARTS rocket parts and the satellite (the rocket's cargo);
            rocket_hours long. The rocket-silo tech's recipes run in this phase only.
Machines per recipe = the most either phase needs (max over phases, not the average over the run). Fluids are exact
in every phase: each output of advanced oil processing has an outlet (cracking, plastic, solid fuel, ...) or the
refinery stops. Crude comes from pumpjacks whose wells decline (exponential in pumped time, floor 20%): the LP buys
pumpjacks at the yield a well still has at each phase's end.
"tier" per recipe = the most science-pack kinds any tech on the way to unlocking it costs (0: no research).
The LP is re-solved until the tech set stops growing. Machines = crafts x energy / speed, rounded up per module later.
"""
import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import plan_lp  # noqa: E402
from auto import gamedata  # noqa: E402
from auto.mapdata import out_dir  # noqa: E402

PACKS = ["automation-science-pack", "logistic-science-pack", "chemical-science-pack", "production-science-pack",
         "utility-science-pack"]
SPEED = {"crafting": 0.75, "advanced-crafting": 0.75, "crafting-with-fluid": 0.75, "smelting": 1.0,
         "chemistry": 1.0, "oil-processing": 1.0, "rocket-building": 1.0}
FLUIDS = ("heavy-oil", "light-oil", "petroleum-gas", "lubricant", "sulfuric-acid")
EXTRA_TECHS = ["logistics", "fast-inserter", "automation-2", "steel-processing", "electric-energy-distribution-1",
               "advanced-material-processing", "logistics-2"]
# not used by the plan (needs alternative bootstraps or makes nothing the rocket needs)
BAN = ("coal-liquefaction", "kovarex", "uranium", "nuclear", "barrel", "empty-", "fill-", "recycling",
       "basic-oil-processing", "military")
ROCKET_PARTS = 100
ROCKET_HOURS = 2.0     # rocket phase (silo researched -> launch); seed 1060: the knee of machines vs hours (0.75 h 301, 1 h 242,
#                        1.5 h 185, 2 h 174, 3 h 169 machines)
CARGO = {"satellite"}     # the rocket's cargo: made into a mall chest, put into the silo at launch (auto.growth.upkeep.LAUNCH_LUA)
ONE_OFF = {"rocket-silo"}   # placed once: no build margin (the mall makes exactly what the plan places)
# crude: a well yields amount / 30000 crude/s (300000 = 100% = 10/s); each pumping cycle takes 10 off its amount, so
# amount decays as exp(-t / WELL_TAU_S) while pumped, down to the floor (20% of 300000)
WELL_TAU_S = 30000
WELL_FLOOR = 60000
WELL_NOMINAL = 300000


# entity counts of a finished plan (seed 1060, 2026-10-18) when the seed has no plan yet; the closed loop corrects
BUILD_PRIOR = {"transport-belt": 8900, "pipe": 3100, "underground-belt": 900, "medium-electric-pole": 350,
               "inserter": 300, "pipe-to-ground": 210, "stone-furnace": 110, "electric-mining-drill": 90,
               "long-handed-inserter": 70, "splitter": 55, "assembling-machine-2": 50, "steam-engine": 32,
               "fast-inserter": 25, "boiler": 16, "burner-inserter": 16, "chemical-plant": 13, "lab": 9,
               "pumpjack": 4, "oil-refinery": 2, "offshore-pump": 2, "pump": 1, "rocket-silo": 1, "iron-chest": 40}
# small poles need wood (only characters can get it): the mall makes medium poles, which cover more
BUILD_SWAP = {"small-electric-pole": "medium-electric-pole"}


def build_counts(seed, margin=1.1):
    """Entities the characters will place (mall goal): the seed's last plan, else the prior."""
    f = out_dir(seed) / "plan.json"
    if not f.exists():
        return dict(BUILD_PRIOR)
    from collections import Counter
    p = json.loads(f.read_text())
    c = Counter()
    for e in p["complex"] + p["world"]:
        c[BUILD_SWAP.get(e["item"], e["item"])] += 1
    c["iron-chest"] += 40          # mall terminals and the depot
    c["assembling-machine-1"] += 0
    # + margin in whole pieces only: one pump, one silo stay one
    return {k: v if k in ONE_OFF else max(1, v + math.floor(v * (margin - 1))) for k, v in c.items()}


def tiers(techs, unlock):
    """recipe -> most pack kinds on its tech path (trigger techs inherit their prerequisites' tier)."""
    memo = {}

    def tt(n, depth=0):
        if n in memo or n not in techs or depth > 60:
            return memo.get(n, 0)
        v = techs[n]
        own = len(v["ing"]) if isinstance(v["ing"], list) else 0
        pre = v["pre"] if isinstance(v["pre"], list) else []
        memo[n] = max([own] + [tt(p, depth + 1) for p in pre])
        return memo[n]
    return {r: tt(t) for r, t in unlock.items()}


def tech_closure(techs, roots):
    seen = []

    def visit(n):
        if n in seen or n not in techs:
            return
        pre = techs[n]["pre"] if isinstance(techs[n]["pre"], list) else []
        for p in pre:
            visit(p)
        seen.append(n)  # prerequisites first: a valid research order
    for r in roots:
        visit(r)
    return seen


def well_yield(amounts, hours):
    """crude/min one pumpjack gives after `hours` of pumping, averaged over the wells (amounts at the start)."""
    amounts = amounts or [WELL_NOMINAL]
    return 60 * sum(max(WELL_FLOOR, a * math.exp(-hours * 3600 / WELL_TAU_S)) for a in amounts) / len(amounts) / 30000


def solve(hours, gd, build=None, rocket_hours=ROCKET_HOURS, wells=None):
    """hours: the whole run (research phase = hours - rocket_hours); build: mall goal {item: count};
    wells: initial amounts of the seed's crude wells (map.json)."""
    rec, techs = gd["recipes"], gd["techs"]
    unlock = {r: t for t, v in techs.items() for r in (v["unlocks"] if isinstance(v["unlocks"], list) else [])}
    usable = {n: v for n, v in rec.items() if v["cat"] in SPEED and not any(b in n for b in BAN)}
    recipes = {n: (v["cat"], v["energy"], [(i[0], i[1]) for i in v["ing"]], [(p[0], p[1]) for p in v["prod"]])
               for n, v in usable.items()}
    roots = ["rocket-silo"] + EXTRA_TECHS
    late = set(techs["rocket-silo"]["unlocks"])           # recipes of the rocket phase only
    h_res = hours - rocket_hours
    crude_yield = {"research": well_yield(wells, h_res), "rocket": well_yield(wells, hours)}
    for _ in range(6):
        order = tech_closure(techs, roots)
        packs = {}
        for t in order:
            v = techs[t]
            if v.get("trigger"):
                continue
            for name, amt in (v["ing"] if isinstance(v["ing"], list) else []):
                packs[name] = packs.get(name, 0) + v["count"] * amt
        mall = {it: n for it, n in (build or {}).items() if it not in late}
        g_res = dict(packs)
        for it, n in mall.items():
            g_res[it] = g_res.get(it, 0) + n
        g_rock = {"rocket-part": ROCKET_PARTS, "rocket-silo": 1, **{c: 1 for c in CARGO}}
        phases = [{"name": "research", "hours": h_res, "goal": g_res, "ban": late},
                  {"name": "rocket", "hours": rocket_hours, "goal": g_rock}]
        sol = plan_lp.solve_phases(recipes, phases, crude_yield, exact=FLUIDS, speed=SPEED)
        used = [r for ph in sol["phases"] for j, r in enumerate(sol["recipes"]) if ph["x"][j] > 1e-6]
        need = {unlock[r] for r in used if r in unlock}
        new = [t for t in need if t not in order]
        if not new:
            break
        roots += new
    names = sol["recipes"]
    rate = lambda x, j: x[j] * recipes[names[j]][1] / (60 * SPEED[recipes[names[j]][0]])   # noqa: E731
    on = [j for j in range(len(names)) if sol["n"][j] > 1e-6]
    machines = {names[j]: sol["n"][j] for j in on}
    cpm = {names[j]: max(ph["x"][j] for ph in sol["phases"]) for j in on}
    raw = {}
    for ph in sol["phases"]:
        for k, it in enumerate(sol["raws"]):
            if ph["s"][k] > 1e-6:
                raw[it] = max(raw.get(it, 0), ph["s"][k])
    out_ph = []
    for p, ph in enumerate(sol["phases"]):
        out_ph.append({"name": phases[p]["name"], "hours": phases[p]["hours"], "goal": phases[p]["goal"],
                       "crafts_per_min": {names[j]: ph["x"][j] for j in on if ph["x"][j] > 1e-6},
                       "machines": {names[j]: rate(ph["x"], j) for j in on if ph["x"][j] > 1e-6},
                       "raw_per_min": {it: ph["s"][k] for k, it in enumerate(sol["raws"]) if ph["s"][k] > 1e-6},
                       "crude_per_pumpjack": crude_yield[phases[p]["name"]]})
    goal = dict(g_res)
    for it, n in g_rock.items():
        goal[it] = goal.get(it, 0) + n
    lab_s = sum(techs[t]["count"] * techs[t]["time"] for t in order if not techs[t].get("trigger"))
    tier = tiers(techs, unlock)
    return {"hours": hours, "rocket_hours": rocket_hours, "techs": order, "packs": packs, "goal": goal,
            "mall": sorted(set(build or {}) | CARGO), "tier": {r: tier.get(r, 0) for r in machines},
            "research_labs": lab_s / (h_res * 3600),
            "crafts_per_min": cpm, "machines": machines, "raw_per_min": raw,
            "pumpjacks": sol["pumpjacks"], "pumpjacks_at_floor": raw.get("crude-oil", 0) / (60 * WELL_FLOOR / 30000),
            "phases": out_ph,
            "recipes": {r: {"cat": recipes[r][0], "energy": recipes[r][1], "ing": recipes[r][2], "prod": recipes[r][3]}
                        for r in machines}}


def seed_wells(seed):
    f = out_dir(seed) / "map.json"
    return [w[2] for w in json.loads(f.read_text()).get("wells", [])] if f.exists() else None


def oil_balance(d):
    """Per phase: {fluid: [made, used]} per minute (every product needs an outlet)."""
    out = {}
    for ph in d["phases"]:
        bal = {}
        for r, x in ph["crafts_per_min"].items():
            for it, n in d["recipes"][r]["ing"]:
                if it in FLUIDS:
                    bal.setdefault(it, [0, 0])[1] += n * x
            for it, n in d["recipes"][r]["prod"]:
                if it in FLUIDS:
                    bal.setdefault(it, [0, 0])[0] += n * x
        out[ph["name"]] = bal
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--hours", type=float, default=8)
    ap.add_argument("--rocket-hours", type=float, default=ROCKET_HOURS)
    ap.add_argument("--out", default="demand.json", help="file name in runtime/auto/<seed>/")
    a = ap.parse_args()
    d = solve(a.hours, gamedata.load(), build_counts(a.seed), a.rocket_hours, seed_wells(a.seed))
    (out_dir(a.seed) / a.out).write_text(json.dumps(d, indent=1))
    report(d)


def report(d):
    print(f"{len(d['techs'])} techs, packs {d['packs']}")
    print("raw/min", {k: round(v) for k, v in d["raw_per_min"].items()})
    print(f"pumpjacks {d['pumpjacks']:.1f} (crude/min each: " +
          ", ".join(f"{p['name']} {p['crude_per_pumpjack']:.0f}" for p in d["phases"]) +
          f"); {d['pumpjacks_at_floor']:.1f} once the wells are at their floor")
    names = [p["name"] for p in d["phases"]]
    print(f"  {'recipe':34s} " + " ".join(f"{n:>9s}" for n in names) + "      max -> built")
    tot = 0
    for r, m in sorted(d["machines"].items(), key=lambda kv: -kv[1]):
        per = [p["machines"].get(r, 0) for p in d["phases"]]
        print(f"  {r:34s} " + " ".join(f"{v:9.2f}" for v in per) + f" {m:8.2f} -> {math.ceil(m - 1e-6)}")
        tot += math.ceil(m - 1e-6)
    print("machines (rounded up):", tot)
    for ph, bal in oil_balance(d).items():
        print(f"oil {ph}: " + ", ".join(f"{it} {mk:.0f}/{us:.0f}" for it, (mk, us) in sorted(bal.items())))


if __name__ == "__main__":
    main()
