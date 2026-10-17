"""Supply-chain linear program (scipy.optimize.linprog, HiGHS): the cheapest set of NEW machines that
makes the rest of the rocket in H hours, given the machines and drills that already exist.

    .venv/Scripts/python plan.py lp [--hours 8] [--top 25]

Variables  x_r  crafts/min of every recipe (the solver picks among alternatives, e.g. oil cracking)
           n_r  new machines for recipe r          s_i  raw supply/min      d_i  new drills for ore i
Constraints
  balance   sum_r a_ir x_r + s_i >= demand_i      (a_ir: net items per craft; s only for raw inputs)
  machines  x_r * energy_r / (60 * speed_r) - n_r <= existing_r
  mining    s_i - 30 d_i <= drill capacity_i       (an electric drill mines 30 ore/min)
Objective  minimise sum n_r + sum d_i  (+ a tiny cost on x so no recipe runs for nothing)
The duals of the balance rows are shadow prices: new machines per extra item/min. The largest ones are
the binding constraints.
"""
import json

import numpy as np
from scipy.optimize import linprog

SPEED = {"crafting": 0.75, "advanced-crafting": 0.75, "crafting-with-fluid": 0.75, "smelting": 2.0,
         "chemistry": 1.0, "oil-processing": 1.0, "rocket-building": 1.0, "centrifuging": 1.0}
MACHINE = {"crafting": "AM2", "advanced-crafting": "AM2", "crafting-with-fluid": "AM2", "smelting": "furnace",
           "chemistry": "chem plant", "oil-processing": "refinery", "rocket-building": "rocket silo"}
RAW = {"iron-ore", "copper-ore", "stone", "coal", "crude-oil", "water", "wood", "uranium-ore"}
SKIP = ("barrel", "empty-", "fill-", "-recycling", "parameter", "kovarex", "uranium", "nuclear")
PACK_ITEM = {"red": "automation-science-pack", "green": "logistic-science-pack", "blue": "chemical-science-pack",
             "purple": "production-science-pack", "yellow": "utility-science-pack", "military": "military-science-pack"}


def solve(recipes, demand, existing, ore_cap, raw_cap=None):
    """recipes: name -> (cat, energy, ing, prod); demand: item -> per min; existing: recipe -> machines;
    ore_cap: ore -> per min. Returns dict with x, n, drills, duals."""
    raw_cap = raw_cap or {}
    names = [r for r, v in recipes.items() if not any(k in r for k in SKIP) and v[0] in SPEED]
    items = sorted({i for r in names for i, _ in recipes[r][2] + recipes[r][3]} | set(demand))
    raws = [i for i in items if i in RAW]
    R, I, K = len(names), len(items), len(raws)
    iid = {it: k for k, it in enumerate(items)}
    a = np.zeros((I, R))
    for j, r in enumerate(names):
        for it, n in recipes[r][2]:
            a[iid[it], j] -= n
        for it, n in recipes[r][3]:
            a[iid[it], j] += n
    # variable order: x (R) | n (R) | s (K) | d (K)
    nv = 2 * R + 2 * K
    c = np.concatenate([np.full(R, 1e-6), np.ones(R), np.zeros(K), np.ones(K)])
    rows, b = [], []
    for it, k in iid.items():                       # balance: -(a x) - s <= -demand
        row = np.zeros(nv)
        row[:R] = -a[k]
        if it in raws:
            row[2 * R + raws.index(it)] = -1
        rows.append(row)
        b.append(-demand.get(it, 0.0))
    nbal = len(rows)
    for j, r in enumerate(names):                    # machines: m x - n <= existing
        cat, energy = recipes[r][0], recipes[r][1]
        row = np.zeros(nv)
        row[j] = energy / (60 * SPEED[cat])
        row[R + j] = -1
        rows.append(row)
        b.append(existing.get(r, 0))
    for k, it in enumerate(raws):                    # mining: s - 30 d <= cap
        row = np.zeros(nv)
        row[2 * R + k] = 1
        if it in ("iron-ore", "copper-ore", "stone", "coal"):
            row[2 * R + K + k] = -30
            b.append(ore_cap.get(it, 0))
        else:
            b.append(raw_cap.get(it, 1e9))           # crude, water: whatever the pumps give
        rows.append(row)
    res = linprog(c, A_ub=np.array(rows), b_ub=np.array(b), bounds=[(0, None)] * nv, method="highs")
    if res.status != 0:
        raise SystemExit("LP: " + res.message)
    x, n, s, d = res.x[:R], res.x[R:2 * R], res.x[2 * R:2 * R + K], res.x[2 * R + K:]
    duals = -res.ineqlin.marginals[:nbal]
    return {"recipes": names, "x": x, "n": n, "raws": raws, "s": s, "d": d,
            "dual": dict(zip(items, duals)), "machines_needed": {r: x[j] * recipes[r][1] / (60 * SPEED[recipes[r][0]])
                                                                 for j, r in enumerate(names)}}


def report(b, crew, hours=8.0, top=25):
    import plan
    data = json.loads(b.raw("/sc " + plan.RECIPE_LUA))
    recipes = {n: (cat, e, ing, prod) for n, cat, e, ing, prod, en in data}
    path = crew.silo_path()
    goal = {PACK_ITEM.get(k, k): v for k, v in path["packs"].items()}
    if not path["silo_done"]:
        goal["rocket-silo"] = 1
    goal["rocket-part"] = 100
    demand = {k: v / (hours * 60) for k, v in goal.items()}
    cap = crew._dict_of(crew._dict_of(b.call("capacity")).get("recipes"))
    existing = {r: v.get("machines", 0) for r, v in cap.items()}
    ore = crew._dict_of(crew._dict_of(b.call("ore")).get("ore"))
    ore_cap = {k: v.get("per_min", 0) for k, v in ore.items()}
    sol = solve(recipes, demand, existing, ore_cap)
    out = [f"LP (HiGHS): rocket in {hours:g} h. New machines total {sum(sol['n']) + sum(sol['d']):.1f}",
           "recipe                          machines need  have   NEW  (machine)"]
    rows = sorted(((sol["n"][j], r) for j, r in enumerate(sol["recipes"])), reverse=True)
    for nn, r in rows[:top]:
        if nn < 0.05:
            break
        need = sol["machines_needed"][r]
        out.append(f"{r[:31]:31s} {need:12.2f} {existing.get(r, 0):5d} {nn:5.1f}  {MACHINE.get(recipes[r][0], recipes[r][0])}")
    for k, it in enumerate(sol["raws"]):
        if sol["d"][k] > 0.05:
            out.append(f"NEW DRILLS {it}: {sol['d'][k]:.1f}")
    pr = sorted(((v, k) for k, v in sol["dual"].items() if v > 1e-4), reverse=True)[:8]
    out.append("shadow prices (new machines per +1 item/min): " + ", ".join(f"{k} {v:.3f}" for v, k in pr))
    return "\n".join(out)
