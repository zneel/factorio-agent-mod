"""Layout search (OR-Tools CP-SAT) on top of plan_block: choose each machine's row and column, each belt's
lanes and flow direction, so that the compiled block is correct by construction.

    .venv/Scripts/python plan.py layout blocks/rocketY.layout.json [--out blocks/rocketY.json]

Template (period 7 in y):   belt 2r (long for row r) / belt 2r+1 (regular) / inserters / machine row r (3 tall)
                            / inserters / belt 2r+2 (regular) / belt 2r+3 (long) ...
Columns are 4 apart (one gap tile for a direct-insertion inserter).
Constraints
  ingredient   every solid ingredient comes from a neighbour in the same row that makes it, or from a lane of
               one of the 4 belts the machine's row reaches (regular = nearest belt on that side, long = next)
  far lane     an output dropped onto a belt above lands on its north lane, below on its south lane
  lanes        one item per lane; a lane holding an item made here must be fed by one of its producers;
               input bundles (two items on one belt, as they arrive) keep their left/right pairing; a bundle
               may run on a second belt the other way (a U-turn loop at the block's end, like block X B2 -> B5)
  flow         every machine reading a lane has at least one of that lane's producers upstream of it
               (the purple-block T1 bug: circuits made downstream of their consumers)
  inserters    at most 3 per machine side (belt reads + outputs)
  repair       "pinned_belts" [{j, id, dir, lanes, x1, x2, cols}] keep built belts; machines may be "pin"ned
               [row, col] or carry "was" [row, col] (moving one costs more than anything else)
  fluids       machines with a fluid ingredient/product sit at a row end, ports facing out (pipes come from outside);
               a machine's "end": "west"/"east" picks the end (the side its pipe arrives from)
Objective   fewest belts, then fewest long-handed reads, then narrowest.
Spec: {"name", "origin": [x0, y0], "rows": R, "cols": C, "machines": [{"id","recipe","entity"?}],
       "inputs": [{"left": item, "right": item|null, "name": ...}], "exports": [items]}
"""
import json

from ortools.sat.python import cp_model

FLUIDS = {"water", "steam", "crude-oil", "heavy-oil", "light-oil", "petroleum-gas", "lubricant", "sulfuric-acid"}


def solve(spec, recipes, seconds=30.0):
    """recipes: name -> (ing [(item, n)], prod [(item, n)], energy). Returns a plan_block spec or raises."""
    R, C = spec["rows"], spec["cols"]
    NB = 2 * R + 2
    ms = spec["machines"]
    ms_by = {m["id"]: m for m in ms}
    ids = [m["id"] for m in ms]
    ing = {m["id"]: [i for i, _ in recipes[m["recipe"]][0] if i not in FLUIDS] for m in ms}
    out = {m["id"]: recipes[m["recipe"]][1][0][0] for m in ms}
    fluid = {m["id"]: any(i in FLUIDS for i, _ in recipes[m["recipe"]][0] + recipes[m["recipe"]][1]) for m in ms}
    made = {out[k] for k in ids if out[k] not in FLUIDS}
    import plan_rules
    LONG_CAP = 1.15 * 1.05  # long-handed inserter items/s (+5%: a target is an estimate); faster -> normal inserter
    crafts = {k: plan_rules.MACHINE_SPEED.get(ms_by[k].get("entity", "assembling-machine-2"), 0.75)
              / max(recipes[ms_by[k]["recipe"]][2], 1e-9) for k in ids}  # full speed ...
    if spec.get("target"):  # ... or what the block's target output needs (spec "target": {item: per s})
        need = plan_rules.block_crafts(ms_by, recipes, spec["target"])
        crafts = {k: min(crafts[k], need.get(k, 0)) for k in ids}
    rate_in = {(k, i): n * crafts[k] for k in ids for i, n in recipes[ms_by[k]["recipe"]][0]}
    rate_out = {k: recipes[ms_by[k]["recipe"]][1][0][1] * crafts[k] for k in ids}
    bundles = spec.get("inputs", [])
    pinned = {pb["j"]: pb for pb in spec.get("pinned_belts", [])}  # repair mode: belts already built
    ext = {it for bd in bundles for it in (bd.get("left"), bd.get("right")) if it}
    ext |= {it for pb in pinned.values() for it in pb["lanes"].values() if it and it not in made}
    items = sorted(made | ext | {i for k in ids for i in ing[k]})
    exports = set(spec.get("exports", []))
    missing = {i for k in ids for i in ing[k]} - made - ext
    if missing:
        raise ValueError(f"no source for {sorted(missing)}: add an input bundle")

    md = cp_model.CpModel()
    row = {k: md.NewIntVar(0, R - 1, f"row_{k}") for k in ids}
    col = {k: md.NewIntVar(0, C - 1, f"col_{k}") for k in ids}
    slot = {k: md.NewIntVar(0, R * C - 1, f"slot_{k}") for k in ids}
    for k in ids:
        md.Add(slot[k] == row[k] * C + col[k])
    md.AddAllDifferent(slot.values())
    L = {(j, l, i): md.NewBoolVar(f"L_{j}_{l}_{i}") for j in range(NB) for l in (0, 1) for i in items}  # l 0=N 1=S
    east = {j: md.NewBoolVar(f"east_{j}") for j in range(NB)}
    for j in range(NB):
        for l in (0, 1):
            md.AddAtMostOne(L[j, l, i] for i in items)
    for j, pb in pinned.items():  # built belts keep their direction and lane items
        md.Add(east[j] == (1 if pb["dir"] == "east" else 0))
        for l, nm in ((0, "north"), (1, "south")):
            if pb["lanes"].get(nm):
                md.Add(L[j, l, pb["lanes"][nm]] == 1)
            elif pb.get("closed", True):
                md.Add(sum(L[j, l, i] for i in items) == 0)
    used = {j: md.NewBoolVar(f"used_{j}") for j in range(NB)}
    for j in range(NB):
        for l in (0, 1):
            for i in items:
                md.AddImplication(L[j, l, i], used[j])

    def enforce_reach(lit, k, j, above):
        if above:  # belts 2r (long) and 2r+1 (regular)
            md.Add(2 * row[k] <= j).OnlyEnforceIf(lit)
            md.Add(j <= 2 * row[k] + 1).OnlyEnforceIf(lit)
        else:      # belts 2r+2 (regular) and 2r+3 (long)
            md.Add(2 * row[k] + 2 <= j).OnlyEnforceIf(lit)
            md.Add(j <= 2 * row[k] + 3).OnlyEnforceIf(lit)

    def is_long(k, j):
        b = md.NewBoolVar("")
        # long when j == 2r or j == 2r+3
        e0, e3 = md.NewBoolVar(""), md.NewBoolVar("")
        md.Add(j == 2 * row[k]).OnlyEnforceIf(e0)
        md.Add(j != 2 * row[k]).OnlyEnforceIf(e0.Not())
        md.Add(j == 2 * row[k] + 3).OnlyEnforceIf(e3)
        md.Add(j != 2 * row[k] + 3).OnlyEnforceIf(e3.Not())
        md.AddBoolOr([e0, e3]).OnlyEnforceIf(b)
        md.AddBoolAnd([e0.Not(), e3.Not()]).OnlyEnforceIf(b.Not())
        return b

    # reads: rd[k, j, side] = machine k has an inserter reading belt j; u[k, i, j, l] = it takes item i from lane l
    rd, u = {}, {}
    longs = []
    lg_of = {}
    for k in ids:
        for j in range(NB):
            for above in (True, False):
                rd[k, j, above] = md.NewBoolVar("")
                enforce_reach(rd[k, j, above], k, j, above)
            for i in ing[k]:
                for l in (0, 1):
                    u[k, i, j, l] = md.NewBoolVar("")
                    md.AddImplication(u[k, i, j, l], L[j, l, i])
                    md.AddBoolOr([rd[k, j, True], rd[k, j, False]]).OnlyEnforceIf(u[k, i, j, l])
            lg = is_long(k, j)
            lg_of[k, j] = lg
            for i in ing[k]:  # fast items never on a long-handed inserter
                if rate_in.get((k, i), 0) > LONG_CAP:
                    for l in (0, 1):
                        md.AddImplication(u[k, i, j, l], lg.Not())
            x = md.NewBoolVar("")
            md.AddBoolAnd([lg]).OnlyEnforceIf(x)
            md.AddBoolOr([rd[k, j, True], rd[k, j, False]]).OnlyEnforceIf(x)  # x -> some read
            md.AddBoolOr([x, lg.Not(), rd[k, j, True].Not()])
            md.AddBoolOr([x, lg.Not(), rd[k, j, False].Not()])
            longs.append(x)
    # direct insertion: d[p, k] = p hands its product to neighbour k
    d = {}
    for k in ids:
        for p in ids:
            if p != k and out[p] in ing[k]:
                d[p, k] = md.NewBoolVar(f"d_{p}_{k}")
                md.Add(row[p] == row[k]).OnlyEnforceIf(d[p, k])
                diff = md.NewIntVar(-C, C, "")
                md.Add(diff == col[p] - col[k])
                ad = md.NewIntVar(0, C, "")
                md.AddAbsEquality(ad, diff)
                md.Add(ad == 1).OnlyEnforceIf(d[p, k])
    for k in ids:
        for i in ing[k]:
            md.AddBoolOr([d[p, k] for p in ids if (p, k) in d and out[p] == i] +
                         [u[k, i, j, l] for j in range(NB) for l in (0, 1)])
    # outputs: oN[p, j] drops onto belt j above (north lane), oS below (south lane)
    oN, oS = {}, {}
    for p in ids:
        it = out[p]
        if it in FLUIDS:
            continue
        for j in range(NB):
            oN[p, j], oS[p, j] = md.NewBoolVar(""), md.NewBoolVar("")
            enforce_reach(oN[p, j], p, j, True)
            enforce_reach(oS[p, j], p, j, False)
            md.AddImplication(oN[p, j], L[j, 0, it])
            md.AddImplication(oS[p, j], L[j, 1, it])
            if rate_out[p] > LONG_CAP:
                md.AddImplication(oN[p, j], lg_of[p, j].Not())
                md.AddImplication(oS[p, j], lg_of[p, j].Not())
        # the product goes somewhere
        md.AddBoolOr([oN[p, j] for j in range(NB)] + [oS[p, j] for j in range(NB)] +
                     [d[p, k] for k in ids if (p, k) in d])
    for i in items:
        prods = [p for p in ids if out[p] == i]
        for j in range(NB):
            for l in (0, 1):
                if i in made and i not in ext:  # a lane of a made item must be fed by a producer
                    feeders = [(oN if l == 0 else oS)[p, j] for p in prods]
                    md.AddBoolOr(feeders).OnlyEnforceIf(L[j, l, i])
        if i in exports:
            md.AddBoolOr([L[j, l, i] for j in range(NB) for l in (0, 1)])
    # input bundles: each placed on one belt, lanes paired by direction (east: left=N, west: left=S)
    bp = {}
    loops = []
    for bi, bd in enumerate(bundles):
        lits = []
        for j in range(NB):
            v = md.NewBoolVar(f"bundle{bi}_{j}")
            lits.append(v)
            bp[bi, j] = v
            for side, it in (("left", bd.get("left")), ("right", bd.get("right"))):
                lane_east = 0 if side == "left" else 1
                lane_west = 1 - lane_east
                if it:
                    md.AddBoolOr([L[j, lane_east, it]]).OnlyEnforceIf([v, east[j]])
                    md.AddBoolOr([L[j, lane_west, it]]).OnlyEnforceIf([v, east[j].Not()])
        md.Add(sum(lits) >= 1)
        md.Add(sum(lits) <= 2)  # a second belt = a U-turn loop of the first: it runs the other way
        for j in range(NB):
            for j2 in range(j + 1, NB):
                md.AddBoolOr([east[j], east[j2]]).OnlyEnforceIf([bp[bi, j], bp[bi, j2]])
                md.AddBoolOr([east[j].Not(), east[j2].Not()]).OnlyEnforceIf([bp[bi, j], bp[bi, j2]])
        loops.append(sum(lits) - 1)
    for j in range(NB):
        md.AddAtMostOne(bp[bi, j] for bi in range(len(bundles)))
        for i in ext:  # an external item on a lane means a bundle (or a built belt) brought it there
            for l in (0, 1):
                if j in pinned and pinned[j]["lanes"].get(("north", "south")[l]) == i:
                    continue
                md.AddBoolOr([bp[bi, j] for bi, bd in enumerate(bundles) if i in (bd.get("left"), bd.get("right"))]
                             ).OnlyEnforceIf(L[j, l, i])
    # a built belt only spans some columns: machines using it must stand within them
    for j, pb in pinned.items():
        c0, c1 = pb.get("cols", [0, C - 1])
        for k in ids:
            lits = [rd[k, j, True], rd[k, j, False]]
            if (k, j) in oN:
                lits += [oN[k, j], oS[k, j]]
            for lit in lits:
                md.Add(col[k] >= c0).OnlyEnforceIf(lit)
                md.Add(col[k] <= c1).OnlyEnforceIf(lit)
    for r_, c_ in spec.get("blocked_slots", []):  # slots taken by something else (pipes, poles)
        for k in ids:
            md.Add(slot[k] != r_ * C + c_)
    # pinned machines (e.g. piped ones) stay; machines with "was" pay for moving
    moves = []
    for k in ids:
        m = ms_by[k]
        if m.get("pin"):
            md.Add(row[k] == m["pin"][0])
            md.Add(col[k] == m["pin"][1])
        if m.get("was"):
            mv = md.NewBoolVar(f"move_{k}")
            same = md.NewBoolVar("")
            md.Add(slot[k] == m["was"][0] * C + m["was"][1]).OnlyEnforceIf(same)
            md.Add(slot[k] != m["was"][0] * C + m["was"][1]).OnlyEnforceIf(same.Not())
            md.Add(mv == 1 - same)
            moves.append(mv)
    # flow: a machine reading a lane of a made item has at least one producer of that lane upstream of it
    for (k, i, j, l), uv in u.items():
        if i not in made:
            continue
        ws = []
        for p in ids:
            if out[p] != i or p == k:
                continue
            o = (oN if l == 0 else oS)[p, j]
            w = md.NewBoolVar("")
            md.AddImplication(w, o)
            md.Add(col[p] <= col[k]).OnlyEnforceIf([w, east[j]])
            md.Add(col[p] >= col[k]).OnlyEnforceIf([w, east[j].Not()])
            ws.append(w)
        md.AddBoolOr(ws).OnlyEnforceIf(uv)
    # inserters per side <= 3
    for k in ids:
        for above in (True, False):
            outs = [] if out[k] in FLUIDS else [(oN if above else oS)[k, j] for j in range(NB)]
            md.Add(sum(rd[k, j, above] for j in range(NB)) + sum(outs) <= 3)
    # fluid machines at a row end
    for k in ids:
        if fluid[k] and not ms_by[k].get("pin") and not ms_by[k].get("ptg"):  # "ptg": acid via pipe-to-ground
            e0, e1 = md.NewBoolVar(""), md.NewBoolVar("")
            md.Add(col[k] == 0).OnlyEnforceIf(e0)
            md.Add(col[k] == C - 1).OnlyEnforceIf(e1)
            md.AddBoolOr([e0, e1])
            side = ms_by[k].get("end")  # optional: "west"/"east" = which row end (where its pipes come from)
            if side:
                md.AddBoolOr([e0 if side == "west" else e1])
    width = md.NewIntVar(0, C - 1, "width")
    md.AddMaxEquality(width, list(col.values()))
    md.Minimize(200 * sum(moves) + 60 * sum(loops) + 100 * sum(used.values()) + 10 * sum(longs) + 5 * width +
                sum(L.values()) + sum(rd.values()))
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = seconds
    sv.parameters.num_workers = 8
    st = sv.Solve(md)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError(f"no layout ({sv.StatusName(st)}) with {R} rows x {C} cols")
    return _to_block(spec, sv, row, col, L, east, used, items, ids, fluid, R, C, NB, oN, oS, out), sv.StatusName(st)


def _to_block(spec, sv, row, col, L, east, used, items, ids, fluid, R, C, NB, oN, oS, out):
    x0, y0 = spec["origin"]
    ms = {m["id"]: m for m in spec["machines"]}
    machines, outputs = [], {}
    for k in ids:
        r, c = sv.Value(row[k]), sv.Value(col[k])
        m = {"id": k, "recipe": ms[k]["recipe"], "x": x0 + 4 * c, "y": y0 + 4 + 7 * r}
        if ms[k].get("entity"):
            m["entity"] = ms[k]["entity"]
        if ms[k].get("direction"):
            m["direction"] = ms[k]["direction"]
        elif fluid[k]:
            m["direction"] = "west" if c == 0 else "east"
        machines.append(m)
    belts = []
    spec_pinned = {pb["j"]: pb for pb in spec.get("pinned_belts", [])}
    yb = lambda j: y0 + 7 * (j // 2) + (j % 2)  # noqa: E731
    xa, xb = x0 - 2, x0 + 4 * (C - 1) + 2
    for j in range(NB):
        if not sv.Value(used[j]):
            continue
        lanes = {}
        for l, nm in ((0, "north"), (1, "south")):
            lanes[nm] = next((i for i in items if sv.Value(L[j, l, i])), None)
        e = sv.Value(east[j])
        pb = spec_pinned.get(j)
        if pb:
            belts.append({"id": pb.get("id", f"K{j}"), "y": yb(j), "x1": pb["x1"], "x2": pb["x2"], "dir": pb["dir"],
                          "lanes": lanes, "build": False})
            continue
        belts.append({"id": f"K{j}", "y": yb(j), "x1": xa if e else xb, "x2": xb if e else xa,
                      "dir": "east" if e else "west", "lanes": lanes})
    names = {j: spec_pinned[j].get("id", f"K{j}") if j in spec_pinned else f"K{j}" for j in range(NB)}
    for k in ids:
        for j in range(NB):
            if (k, j) in oN and (sv.Value(oN[k, j]) or sv.Value(oS[k, j])):
                outputs[k] = names[j]
    return {"name": spec.get("name", "layout"), "machines": machines, "belts": belts, "outputs": outputs,
            "target": spec.get("target")}


def report(b, spec_path, out_path=None, seconds=30.0):
    import plan
    import plan_block
    spec = json.load(open(spec_path, encoding="utf-8"))
    rec = plan_block.recipes_from_game(b, plan.RECIPE_LUA)
    tried = []
    sizes = sorted({(r, c) for r in range(spec["rows"], spec["rows"] + 2) for c in range(spec["cols"], spec["cols"] + 4)
                    if r * c >= len(spec["machines"])}, key=lambda rc: (rc[0] * rc[1], rc[0]))
    for r, c in sizes:  # smallest grid first: rows x cols from the spec upward
        try:
            spec = dict(spec, rows=r, cols=c)
            blk, status = solve(spec, rec, seconds)
            break
        except ValueError as e:
            tried.append(f"{r}x{c}")
    else:
        raise SystemExit("no layout in " + ", ".join(tried))
    res = plan_block.compile_block(blk, rec)
    lines = [f"CP-SAT {status}: {len(blk['belts'])} belts, rows {spec['rows']} x cols {spec['cols']}"]
    for r in range(spec["rows"]):
        ms = sorted((m for m in blk["machines"] if m["y"] == spec["origin"][1] + 4 + 7 * r), key=lambda m: m["x"])
        lines.append(f"  row {r}: " + "  ".join(f"{m['id']}@{m['x']:g}" for m in ms))
    for bl in blk["belts"]:
        lines.append(f"  {bl['id']} y={bl['y']:g} {bl['dir']}: N={bl['lanes']['north']} S={bl['lanes']['south']}")
    lines += [f"  ERROR {e}" for e in res["errors"]] + [f"  RATE {w}" for w in res["rates"]]
    if out_path:
        json.dump(blk, open(out_path, "w", encoding="utf-8"), indent=1)
        lines.append(f"  -> {out_path}")
    return "\n".join(lines)
