"""Stage 3: modules from the demand: what is built where in kind (not yet where on the map).

    python -m auto.modules --seed N     -> runtime/auto/<seed>/modules.json

Kinds
  mine/smelt   per ore: drills (ore/min / 30) and furnaces; steel and bricks are smelt modules fed by plates/stone
  oil          refinery + crackers + water (fluids out by pipe)
  block        CP-SAT assembly blocks: the remaining recipes in dependency order, cut into blocks the solver can lay
               out (<= MAX_MACHINES machines, <= MAX_BUNDLES input bus belts); fluid machines sit at row ends
  labs, silo   fixed generators
The bus: every item that crosses modules rides a bus belt lane; belts carry two items (pairs chosen so that
blocks tap few belts). A block taps the belts holding its inputs (each tap is a CP-SAT input bundle) and exports
its products onto clean lanes.
"""
import argparse
import itertools
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from auto.mapdata import out_dir  # noqa: E402

FLUIDS = {"water", "crude-oil", "heavy-oil", "light-oil", "petroleum-gas", "lubricant", "sulfuric-acid", "steam"}
SMELT = {"iron-plate", "copper-plate", "steel-plate", "stone-brick"}
OIL = {"advanced-oil-processing", "basic-oil-processing", "heavy-oil-cracking", "light-oil-cracking", "lubricant"}
ACID = {"sulfuric-acid"}
ONE_OFF = set()   # the mall makes every entity (rocket silo, landing pad and labs included)
SILO = {"rocket-part"}
MAX_MACHINES = 8
MAX_BUNDLES = 2
BIG_MACHINES = 4    # a block with a third input belt holds at most this many machines
MAX_FLUIDS = 2
MAX_FLUID_MACHINES = 4   # fluid machines sit at the west row ends (blocks.spec_for), one per row, at most 4 rows
LANE = 7.5          # items/s on one lane of a yellow belt (cheatsheet: 15/s per belt)
MARGIN = 1.15     # build this much above the LP's machine count
APART = set()     # {(recipe, recipe)} that may not share a block (plan2: their block had no buildable layout)
BUMP = {}         # {recipe: extra machines} (plan2: a block of that recipe alone had no layout; 3 PU fail, 4 lay out)


def plan(demand):
    rec = demand["recipes"]
    mach = demand["machines"]
    assembly = [r for r in rec if r not in SMELT | OIL | ACID | ONE_OFF | SILO and rec[r]["cat"] != "smelting"]
    prod_of = {p: r for r in rec for p, _ in rec[r]["prod"]}
    # dependency order: a recipe after the recipes making its ingredients
    order, seen = [], set()

    def visit(r):
        if r in seen:
            return
        seen.add(r)
        for i, _ in rec[r]["ing"]:
            if i in prod_of and prod_of[i] in assembly:
                visit(prod_of[i])
        order.append(r)
    for r in sorted(assembly):
        visit(r)
    count = {r: max(1, math.ceil(mach[r] * MARGIN - 1e-6)) + BUMP.get(r, 0) for r in assembly}
    # items/s each recipe reads of each ingredient (LP crafts with the build margin)
    reads = {(r, i): demand["crafts_per_min"].get(r, 0) * n / 60 * MARGIN for r in assembly for i, n in rec[r]["ing"]}
    # a block's recipes all need the same science (tier: pack kinds on the way to their techs; red-only and none
    # share a class): else a block waits for research that its own packs, or later ones, have to pay for
    tier = demand.get("tier", {})
    mall = set(demand.get("mall", []))
    klass = {r: max(1, tier.get(r, 0)) for r in assembly}
    blocks = partition(assembly, rec, count, reads, klass=klass)
    out = []
    for k, bl in enumerate(blocks):
        made = {p for r in bl for p, _ in rec[r]["prod"]}
        ins = sorted({i for r in bl for i, _ in rec[r]["ing"]} - made - FLUIDS)
        fl_in = sorted({i for r in bl for i, _ in rec[r]["ing"]} & FLUIDS)
        used_inside = {i for r in bl for i, _ in rec[r]["ing"]}
        consumers_elsewhere = {i for r in rec if r not in bl for i, _ in rec[r]["ing"]}
        exports = sorted(p for p in made if p in consumers_elsewhere or p in demand["goal"])
        cnt = {r: count[r] for r in bl}
        for r in bl:   # exported and also used inside: its exit belt can't feed the block, so one more machine
            if any(p in exports and p in used_inside for p, _ in rec[r]["prod"]):   # hands it over directly
                cnt[r] += 1
        chest = sorted(p for p in exports if p in mall and p not in consumers_elsewhere)
        out.append({"name": f"B{k + 1}", "kind": "block", "recipes": cnt,
                    "inputs": ins, "fluids": fl_in, "exports": exports, "chest": chest,
                    "mall": sorted(p for p in exports if p in mall)})
    mods = []
    raw = demand["raw_per_min"]
    for ore, plate in (("iron-ore", "iron-plate"), ("copper-ore", "copper-plate"), ("stone", "stone-brick")):
        if plate in rec or ore in raw:
            fur = math.ceil(mach.get(plate, 0) * MARGIN)
            mods.append({"name": f"mine-{ore}", "kind": "mine", "ore": ore,
                         "drills": max(2, math.ceil(raw.get(ore, 0) / 30 * MARGIN))})
            if fur:
                mods.append({"name": f"smelt-{plate}", "kind": "smelt", "input": ore, "output": plate, "furnaces": fur})
    if "steel-plate" in rec:
        mods.append({"name": "smelt-steel-plate", "kind": "smelt", "input": "iron-plate", "output": "steel-plate",
                     "furnaces": math.ceil(mach["steel-plate"] * MARGIN)})
    if "coal" in raw or True:
        mods.append({"name": "mine-coal", "kind": "mine", "ore": "coal", "drills": 0})  # sized with power later
    oil = {r: max(1, math.ceil(mach[r] * MARGIN)) for r in rec if r in OIL}
    if oil:
        mods.append({"name": "oil", "kind": "oil", "recipes": oil,
                     "crude_per_s": sum(mach[r] for r in oil if "oil-processing" in r) * 20 * MARGIN})
    mods += out
    mods.append({"name": "labs", "kind": "labs", "labs": math.ceil(demand.get("research_labs", mach.get("lab", 1)) * MARGIN),
                 "inputs": [p for p in demand["packs"]]})
    mods.append({"name": "silo", "kind": "silo", "inputs": ["processing-unit", "low-density-structure", "rocket-fuel"]})
    return mods


def partition(assembly, rec, count, reads=None, seconds=30, klass=None):
    """CP-SAT: recipes -> ordered blocks. Producer's block <= consumer's block (the bus runs one way), at most
    MAX_MACHINES machines and 2 fluid kinds per block; minimise the external inputs summed over blocks (each one is
    a bus tap), then the number of blocks. klass: recipe -> class; a block holds one class only."""
    from ortools.sat.python import cp_model
    # every class may leave one block part-filled: room for one more block per class
    K = max(2, math.ceil(sum(count.values()) / MAX_MACHINES) + 4 + len(set((klass or {}).values())))
    md = cp_model.CpModel()
    b = {r: md.NewIntVar(0, K - 1, f"b_{r}") for r in assembly}
    x = {(r, k): md.NewBoolVar(f"x_{r}_{k}") for r in assembly for k in range(K)}
    for r in assembly:
        md.AddExactlyOne(x[r, k] for k in range(K))
        md.Add(b[r] == sum(k * x[r, k] for k in range(K)))
    prod_of = {p: r for r in assembly for p, _ in rec[r]["prod"]}
    items = sorted({i for r in assembly for i, _ in rec[r]["ing"]})
    makers = {}   # item -> every recipe making it (solid fuel has three): each one before every reader
    for r in assembly:
        for p, _ in rec[r]["prod"]:
            makers.setdefault(p, []).append(r)
    for r in assembly:
        for i, _ in rec[r]["ing"]:
            for m in makers.get(i, ()):
                if m != r:
                    md.Add(b[m] <= b[r])
    for r1, r2 in APART:
        if r1 in b and r2 in b:
            for k in range(K):
                md.AddBoolOr([x[r1, k].Not(), x[r2, k].Not()])
    if klass:
        for k in range(K):
            has = {c: md.NewBoolVar("") for c in set(klass.values())}
            for r in assembly:
                md.AddImplication(x[r, k], has[klass[r]])
            md.Add(sum(has.values()) <= 1)
    used = [md.NewBoolVar(f"u{k}") for k in range(K)]
    for k in range(K):
        md.Add(sum(count[r] * x[r, k] for r in assembly) <= MAX_MACHINES)
        md.Add(sum(count[r] * x[r, k] for r in assembly
                   if any(i in FLUIDS for i, _ in rec[r]["ing"] + rec[r]["prod"])) <= MAX_FLUID_MACHINES)
        for r in assembly:
            md.AddImplication(x[r, k], used[k])
    cost = []
    for k in range(K):
        nfl, nsol = [], []
        for i in items:
            readers = [r for r in assembly if any(j == i for j, _ in rec[r]["ing"])]
            need = md.NewBoolVar("")   # block k reads i from outside (from the bus or a pipe)
            for r in readers:
                if i in prod_of and prod_of[i] == r:
                    continue
                if i in prod_of:  # produced inside k -> no tap
                    inside = x[prod_of[i], k]
                    md.AddBoolOr([need, inside, x[r, k].Not()])
                else:
                    md.AddImplication(x[r, k], need)
            if i in FLUIDS:
                nfl.append(need)
            else:
                cost.append(need)
                nsol.append(need)
        md.Add(sum(nfl) <= MAX_FLUIDS)
        # a third input belt only for a small block (a recipe with 5 solid ingredients: rocket silo, refinery)
        big = md.NewBoolVar("")
        md.Add(sum(nsol) <= 2 * MAX_BUNDLES + 2 * big)
        md.Add(sum(count[r] * x[r, k] for r in assembly) <= BIG_MACHINES).OnlyEnforceIf(big)
        cost.append(big)
    # an ingredient read faster than one belt lane carries (a block export rides one lane, a bus tap delivers one
    # lane) must be made in the reader's own block: the layout then hands it over directly
    split = []
    for (r, i), v in (reads or {}).items():
        if v > LANE and i in prod_of and prod_of[i] in assembly and prod_of[i] != r:
            sep = md.NewBoolVar("")
            for k in range(K):
                md.AddImplication(x[r, k], x[prod_of[i], k]).OnlyEnforceIf(sep.Not())
            split.append(sep)
    md.Minimize(10 * sum(cost) + sum(used) + 100 * sum(split))
    sv = cp_model.CpSolver()
    sv.parameters.random_seed = 1
    sv.parameters.num_workers = 1
    sv.parameters.max_deterministic_time = seconds
    st = sv.Solve(md)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise SystemExit("partition: no solution")
    blocks = [[r for r in assembly if sv.Value(x[r, k])] for k in range(K)]
    return [bl for bl in blocks if bl]


def bus_items(mods):
    """Items that ride the bus, in the order they join it."""
    order = []
    for m in mods:
        for i in m.get("output", None) and [m["output"]] or m.get("exports", []):
            if i not in order:
                order.append(i)
    return order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    a = ap.parse_args()
    d = json.loads((out_dir(a.seed) / "demand.json").read_text())
    mods = plan(d)
    (out_dir(a.seed) / "modules.json").write_text(json.dumps(mods, indent=1))
    for m in mods:
        if m["kind"] == "block":
            print(m["name"], sum(m["recipes"].values()), "machines", list(m["recipes"]), "| in", m["inputs"],
                  "| fluids", m["fluids"], "| out", m["exports"])
        else:
            print(m["name"], {k: v for k, v in m.items() if k not in ("name",)})


if __name__ == "__main__":
    main()
