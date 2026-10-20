"""Build and research order as one CP-SAT scheduling model (OR-Tools): what to build next, what to research next.

    s = schedule.solve(plan, built, researched, gd, dem, sec_per_piece=3.5, labs=9,
                       stock0=None, rates=None, coal_left=None)    # optional: the flow phase, the coal budget
    s["order"]        unbuilt groups by start time;  s["research"]  techs by start time
    s["hand"]         groups the solver lets the characters hand-craft (only before the mall is ready)
    s["status"]       "OPTIMAL" / "FEASIBLE" / "INFEASIBLE" (a cycle: something waits for itself)
    s["flow_status"]  the flow phase's status (None: no stock0; "SKIPPED": no schedule to start it from);
                      s["flow"] = {"late": groups past the grid, "unmade": items no plan group makes and the stock
                      lacks}
    s["coal"]         {"need": coal burned until the launch, "left": coal_left} (coal_left given)

Model (game seconds; built groups and researched techs are fixed at time 0):
  build task per group    interval, size = its pieces x sec_per_piece; one at a time (AddNoOverlap: the crew builds
                          one increment at a time)
  research task per tech  two alternative optional intervals: the labs (after the labs group, packs from their
                          makers) or the bootstrap lab (red-only techs, hand-made packs, 9x slower); exactly one;
                          AddNoOverlap per lab; after its prerequisites; trigger techs after their action
  ready[g]                AddMaxEquality(end of g, techs of its recipes or their stand-ins): when g produces
  inputs                  a group starts after, per input item, the earliest ready maker (AddMinEquality)
  techs                   a group starts after the techs of its entities
  mall                    a group starts after the mall makers of its entities, unless hand[g]; hand[g] => it ends
                          before the early mall is ready (rule 2: hand-crafting only in the bootstrap)
  power                   AddReservoirConstraintWithActive: each boiler unit adds its MW when built (optional,
                          in water-chain order), each electric group draws its MW from when it is built; >= 0
  flow (with stock0)      item levels on a time grid TAU: L[it][k] = stock0 + what the makers made in the slots before
                          k - the entities of the builds started by the end of slot k (their pieces and the transport
                          their increments bring; transport several bring goes with the first) - the packs of the
                          techs ended by then, >= 0. A maker makes x[h,r,k] <= rate x slot length while on[h][k]
                          (ready by the slot start), and takes its ingredients in proportion: the plates come from
                          smelters and mines like every other item, so the mall is bound by plates and smelters come
                          first. Machines never take the stock (it is in chests: the builders' and the labs'). Rates
                          are nameplate (machines x speed / recipe time) or measured (rates). Every tech lands on the
                          grid; a build may start past it and then takes nothing (with the mall modelled every
                          unbuilt group has a hand[g], and only the second solve pulls it in as its items allow);
                          when a tech cannot (packs that never come) the phase is re-solved with techs past the grid
                          too (s["flow"]["late"]: the builds that start past it). Fuel is a budget, not a level
  objective               when the silo is ready (rocket launch) + a small cost per hand-crafted piece; in the flow
                          phase + every tech as early as it can be, then (second solve at that rocket time) every
                          piece as early as its items allow
"""
import collections
import math
import time
import types

from ortools.sat.python import cp_model

UNIT_KW = 1800                      # one boiler + two steam engines
KW = {"assembling-machine-1": 75, "assembling-machine-2": 150, "chemical-plant": 210, "oil-refinery": 420,
      "electric-mining-drill": 90, "lab": 60, "inserter": 13, "fast-inserter": 46, "long-handed-inserter": 20,
      "pumpjack": 90, "rocket-silo": 250, "electric-furnace": 180, "pump": 30}
RED = "automation-science-pack"
HAND_COST = 20          # seconds of rocket time one hand-crafted piece is worth avoiding (rule 2: build from the mall)
OK = (cp_model.OPTIMAL, cp_model.FEASIBLE)
# flow grid (game s): fine where the next builds land, hourly where the rocket does, then coarse to 48 h (a finer
# grid is more exact but CP-SAT does not finish it in a minute)
TAU = [0, 600, 1200, 1800, 2700, 3600, 5400, 7200, 9000, 10800, 14400] + [3600 * h for h in range(5, 17)] + [
    72000, 86400, 115200, 172800]
SPEED = {"assembling-machine-1": 0.5, "assembling-machine-2": 0.75, "assembling-machine-3": 1.25, "chemical-plant": 1,
         "oil-refinery": 1, "stone-furnace": 1, "steel-furnace": 2, "electric-furnace": 2, "centrifuge": 1}
DRILL = 0.5             # electric mining drill: ore/s (mining time 1)
FURNACE_KW = 90         # a burner furnace burns 90 kW while it crafts
COAL_KJ = 4000          # boilers and furnaces: 4 MJ per coal, 100% efficient
FUEL = {"coal", "wood"}  # fuel is a budget (coal_left), not a level


def solve(plan, built, researched, gd, dem, sec_per_piece=3.5, labs=9, seconds=20.0, retired=(),
          sizes=None, early_mall=(), parts=("power", "mall", "crew"), hint=None, stock0=None, rates=None,
          coal_left=None):
    """See the module doc. The full model is solved from a hint: first the precedences and the crew alone (fast),
    its schedule then seeds the search with power and mall (CP-SAT AddHint), and that one the flow (stock0 given:
    item levels; its result replaces the previous one only when the flow phase is FEASIBLE/OPTIMAL in time).
    Each phase has up to `seconds` as before and the flow phase `seconds` of its own after them, only on a schedule
    (a precedence cycle or none in time: "SKIPPED"): an order without levels beats none (grow builds nothing without
    one), and an early game (60 groups to place) needs all of `seconds` for its first (measured: with 15 s none).
    rates: {group: {item: per s}} measured for built groups (None: nameplate). coal_left: coal the factory can still
    mine -> s["coal"] = {"need", "left"} (reported, never a constraint)."""
    args = (plan, built, researched, gd, dem, sec_per_piece, labs)
    rest = (retired, sizes, early_mall)
    if hint is None:      # phases: each solution seeds the next, richer model
        steps = [p_ for p_ in (("crew",), ("crew", "mall")) if set(p_) < set(parts)]
        for st_ in steps:
            r = _solve(*args, seconds, *rest, st_, hint)
            hint = r.get("values") or hint
    res = _solve(*args, seconds, *rest, parts, hint)
    res["flow_status"] = None
    if stock0 is not None and res["status"] not in ("OPTIMAL", "FEASIBLE"):
        res["flow_status"] = "SKIPPED"
    elif stock0 is not None:
        end = time.time() + seconds
        fp = tuple(parts) + ("flow",)
        fl = _solve(*args, seconds, *rest, fp, res.get("values") or hint, stock0=stock0, rates=rates)
        if fl["status"] == "INFEASIBLE":    # some packs never come within the grid: let those techs land past it
            fl = _solve(*args, max(1.0, end - time.time()), *rest, fp + ("late",), res.get("values") or hint,
                        stock0=stock0, rates=rates)
        if fl["status"] in ("OPTIMAL", "FEASIBLE"):
            fl["flow_status"] = fl["status"]
            res = fl
        else:      # no flow schedule in time: the previous phase's order stands
            res["flow_status"] = fl["status"]
    if coal_left is not None:
        res["coal"] = {"need": int(_coal_need(plan, res, gd, retired, built, researched, stock0 or {},
                                              (dem.get("goal") or {}).get("rocket-part", 0))),
                       "left": int(coal_left)}
    return res


def _placed(p, gd):
    """The item a plan piece is placed as: an assembler 2 on a crafting recipe stands as an assembler 1 (grow)."""
    from auto import grow
    it = p["item"]
    if it == "assembling-machine-2" and gd["recipes"].get(p.get("recipe") or "", {}).get("cat") in grow.ASM1_CATS:
        return "assembling-machine-1"
    return it


def makers(plan, g, gd, rates=None):
    """What group g makes at full speed: {recipe: (crafts/s, solid ingredients, solid products, coal per craft)}.
    Machines x speed / recipe time (assemblers as placed); a smelt group's stone furnaces on its plate recipe;
    a mine group's drills as recipe "mine" (0.5 ore/s each). rates[g] = {item: per s} (measured) replaces the
    nameplate rate of the recipes making that item."""
    rec = gd["recipes"]
    spd = collections.Counter()
    smelt = [it for it in plan.made.get(g, ()) if rec.get(it, {}).get("cat") == "smelting"]
    for i in plan.groups[g]:
        p = plan.p[i]
        if p.get("recipe") in rec:
            spd[p["recipe"]] += SPEED.get(_placed(p, gd), 1)
        elif p["item"] in ("stone-furnace", "steel-furnace") and smelt:
            spd[smelt[0]] += SPEED[p["item"]]
        elif p["item"] == "electric-mining-drill" and g.startswith("mine:"):
            spd["mine"] += DRILL
    out = {}
    for r, s in spd.items():
        if r == "mine":
            ore = sorted(plan.made.get(g, ()))
            if ore:
                out[r] = (s, {}, {ore[0]: 1}, 0.0)
            continue
        R = rec[r]
        ing = {a: n for a, n, ty in R["ing"] if ty != "fluid"}
        prod = {a: n for a, n, ty in R["prod"] if ty != "fluid"}
        fuel = FURNACE_KW * R["energy"] / COAL_KJ if r in smelt else 0.0    # stone furnace: speed 1
        out[r] = (s / R["energy"], ing, prod, fuel)
    for it, v in ((rates or {}).get(g) or {}).items():     # measured: split over the recipes making it by nameplate
        rs = [r for r in out if out[r][2].get(it)]
        tot = sum(out[r][0] * out[r][2][it] for r in rs)
        for r in rs:
            share = out[r][0] * out[r][2][it] / tot if tot else 1 / len(rs)
            out[r] = (max(0.0, v) * share / out[r][2][it],) + out[r][1:]
    return out


def _coal_need(plan, res, gd, retired, built, researched, stock0, parts=0):
    """Coal burned until the rocket launches: the furnaces smelting the plates that the unbuilt pieces, the techs and
    the `parts` rocket parts left take (less the stock, through the plan's recipes; plastic's coal too) and the
    boilers feeding every electric group from when it stands at 60% of nameplate (as the power reservoir counts it)
    until the launch. The launch is not res["makespan"] (the silo standing) but that + the time the plan's makers
    take for the parts' slowest ingredient (nameplate; until the silo stands it backs up on a belt, a few dozen).
    On the 23 h game: rocket fuel at 3/min, 1000 of it = 5.6 h of boilers (~90k coal) after the silo stands."""
    from auto import grow
    B = (res.get("values") or {}).get("B") or {}
    span = res.get("makespan") or 0
    how = {}                  # item -> (crafts per item, solid ingredients, coal per craft) as the plan makes it
    rate = collections.Counter()
    for g in sorted(B):
        for r, (cps, ing, prod, fuel) in makers(plan, g, gd).items():
            for it, n in prod.items():
                how.setdefault(it, (1 / n, ing, fuel))
                rate[it] += cps * n
    R = gd["recipes"].get("rocket-part") if parts else None
    todo = collections.Counter({a: parts * n for a, n, ty in R["ing"] if ty != "fluid"} if R else {})
    launch = span + max([(n - stock0.get(a, 0)) / rate[a] for a, n in todo.items() if rate[a] > 0] + [0])
    left = collections.Counter(stock0)
    coal = 0.0
    for g in res.get("order") or ():        # what gets built before the rocket (unused boiler units are not in it)
        if (res.get("start") or {}).get(g, 0) < span:
            todo.update(_placed(plan.p[i], gd) for i in plan.groups[g] if i not in built)
    for it in grow.POLE_ITEMS:
        todo.pop(it, None)
    for t in (res.get("values") or {}).get("T") or {}:
        if t not in researched and not gd["techs"][t].get("trigger"):
            todo.update({pk: gd["techs"][t]["count"] * n for pk, n in gd["techs"][t]["ing"]})
    for _ in range(12):       # down the recipe tree, level by level (it is shallow)
        nxt = collections.Counter()
        for it, n in todo.items():
            u = min(left[it], n)
            left[it] -= u
            n -= u
            if n <= 0 or it not in how:
                continue
            per, ing, fuel = how[it]
            coal += n * per * (fuel + ing.get("coal", 0))
            nxt.update({a: n * per * m for a, m in ing.items() if a not in FUEL})
        todo = nxt
    for g, (_, e) in B.items():
        if g not in retired and not g.startswith("power"):
            coal += 0.6 * sum(KW.get(plan.p[i]["item"], 0) for i in plan.groups[g]) * max(0, launch - e) / COAL_KJ
    return coal


def _flow(md, plan, gd, groups, done, built, B, T, ready, hand, use, tlist, researched, stock0, rates, hint, late,
          dur):
    """The flow part (module doc): levels per item on the grid TAU. Every tech lands on the grid unless late (then
    one past it takes nothing: its packs never come); a build may always start past it, taking nothing. A build takes
    its unbuilt pieces and the transport its increment brings (grow.increment: the belts, undergrounds and pipes to
    what stands); what several increments bring goes with the first of them to start. Returns what the result needs
    to read back."""
    from auto import grow
    techs = gd["techs"]
    K = len(TAU) - 1
    DT = [TAU[k + 1] - TAU[k] for k in range(K)]
    need = {}                 # unbuilt group -> the items its unbuilt pieces are placed as
    brings = collections.defaultdict(set)     # unbuilt piece -> the unbuilt groups whose increment places it
    lv = types.SimpleNamespace(built=built, state_gone=[], reconnect=[], standin=set(),
                               group_done=lambda h: all(i in built for i in plan.groups[h]))
    for g in groups:
        if g in done:
            continue
        put = [i for i in plan.groups[g] if i not in built]
        c = collections.Counter(_placed(plan.p[i], gd) for i in put)
        need[g] = c - collections.Counter({it: c[it] for it in grow.POLE_ITEMS})
        try:      # the gate counts the increment (most belts, undergrounds and pipes left are transport, not groups)
            _, gs, idx = grow.increment(plan, lv, g, researched, "small-electric-pole")
            gp = {j for h in gs for j in plan.groups[h]}          # (power:k with the power group: that one's)
            put += [i for i in idx if i not in built and i not in gp]
        except Exception:     # (an increment that fails here fails in the gate too: its own pieces still count)
            pass
        for i in put:
            brings[i].add(g)
    bund = collections.defaultdict(collections.Counter)     # what the same groups bring, by item
    for i, gs in brings.items():
        it = _placed(plan.p[i], gd)
        if it not in grow.POLE_ITEMS:
            bund[frozenset(gs)][it] += 1
    packs = {}                # tech on the labs -> packs it takes (all at its end: the labs run while they come)
    for t in tlist:
        if t not in researched and len(T[t]) == 4:
            packs[t] = collections.Counter({pk: techs[t]["count"] * n for pk, n in techs[t]["ing"]})
    mk = {(h, r): v for h in groups for r, v in makers(plan, h, gd, rates).items()}
    made = {it for v in mk.values() for it in v[2]}
    want = collections.Counter()
    for c in list(bund.values()) + list(packs.values()):
        want.update(c)
    for v in mk.values():
        want.update({it: 0 for it in v[1]})
    items = {it for it in want if it in made and it not in FUEL}
    # an item nothing in the plan makes is no level (it would only push its builds past the grid): reported
    unmade = {it: n - stock0.get(it, 0) for it, n in want.items()
              if it not in made and it not in FUEL and n > stock0.get(it, 0)}
    while True:   # nor is one the stock covers for every build and tech while no tracked maker takes it (smaller model)
        mk = {hr: v for hr, v in mk.items() if set(v[2]) & items}
        used = {it for v in mk.values() for it in v[1]}
        cov = {it for it in items if it not in used and stock0.get(it, 0) >= want[it]}
        if not cov:
            break
        items -= cov
    items = sorted(items)

    def ready0(h):            # built and a recipe runs: producing from t=0 (no variable)
        def ok(r):
            return all(t in researched for t in grow.techs_of([r], gd) if t in T)
        rs = plan.recipes(h)
        return h in done and (not rs or any(ok(r) or (grow.RECIPE_SUB.get(r) and ok(grow.RECIPE_SUB[r])) for r in rs))

    def by(var, name, hv=None):
        """b[k] <=> var < TAU[k+1]: started (or ended) by the end of slot k; monotone in k."""
        b = []
        for k in range(K):
            v = md.NewBoolVar(f"{name}{k}")
            md.Add(var <= TAU[k + 1] - 1).OnlyEnforceIf(v)
            md.Add(var >= TAU[k + 1]).OnlyEnforceIf(v.Not())
            if b:
                md.AddImplication(b[-1], v)
            if hv is not None:
                md.AddHint(v, int(hv < TAU[k + 1]))
            b.append(v)
        return b
    on = {}                   # on[h][k] <=> h makes things through slot k (ready by its start)
    for h in sorted({h for h, _ in mk}):
        if ready0(h):
            on[h] = [1] * K
            continue
        on[h] = []
        for k in range(K):
            o = md.NewBoolVar(f"on_{h}_{k}")
            md.Add(ready[h] <= TAU[k]).OnlyEnforceIf(o)
            md.Add(ready[h] >= TAU[k] + 1).OnlyEnforceIf(o.Not())
            if on[h]:
                md.AddImplication(on[h][-1], o)
            on[h].append(o)
    hb, ht = (hint or {}).get("B", {}), (hint or {}).get("T", {})
    st, sb = {}, {}           # st[g][k]: g's build started by the end of slot k (its entities taken then)
    takers = set().union(*bund)
    for g in need:
        if g not in takers:
            continue
        b = sb[g] = by(B[g][0], f"st_{g}_", hb[g][0] if g in hb else None)
        for k in range(K if g in on else 0):     # it makes things only once its build started (a tight LP row)
            md.AddImplication(on[g][k], b[k])
        # hand-made (bootstrap): its entities come from the characters, not the levels; an unused boiler unit takes
        # nothing. Every unbuilt group has a hand[g] once the mall is modelled: none is held on the grid (past it, it
        # takes nothing), so `late` only lets the techs off it
        if g in hand or g in use:
            e = [md.NewBoolVar("") for _ in range(K)]
            for k in range(K):
                md.Add(e[k] >= b[k] - (hand[g] if g in hand else 0) - (1 - use[g] if g in use else 0))
            b = e
        elif not late:
            md.Add(b[-1] == 1)
        st[g] = b
    takes = []                # (items, taken by the end of slot k): a shared bundle by the first of its groups
    for gs, c in bund.items():
        sg = [st[g] for g in sorted(gs)]
        if len(sg) > 1:
            v = [md.NewBoolVar("") for _ in range(K)]
            for k in range(K):
                for s_ in sg:
                    md.AddImplication(s_[k], v[k])
            sg = [v]
        takes.append((c, sg[0]))
    rt, eb = {}, {}           # rt[t][k]: tech t ended by the end of slot k (its packs taken then)
    for t in packs:
        s, e, on_labs, ing = T[t]
        b = eb[t] = by(e, f"rt_{t}_", ht[t][1] if t in ht else None)
        if ing and set(ing) <= {RED}:      # may run on the bootstrap lab (hand-made packs): takes packs on labs only
            f = [md.NewBoolVar("") for _ in range(K)]
            for k in range(K):
                md.Add(f[k] >= b[k] + on_labs - 1)
            b = f
        elif not late:
            md.Add(b[-1] == 1)
        rt[t] = b
    for t in eb:              # a tech ends after its prerequisites (a tight LP row)
        for p_ in techs[t]["pre"] if isinstance(techs[t]["pre"], list) else []:
            for k in range(K if p_ in eb else 0):
                md.AddImplication(eb[t][k], eb[p_][k])
    # redundant rows for the LP (it sees neither NoOverlap nor what the makers can do at most): a tech ends no sooner
    # than all makers together make its packs and its prerequisites'; what the labs finished and the crew started by
    # the end of slot k fits in the time up to then (the crew's last one may run past it). Only the techs held on the
    # labs count (a red-only one may run on the bootstrap lab on hand-made packs), and the bound stops at the grid
    # (past it nothing is counted; a slow measured pack maker would put it past H: infeasible even when late)
    lab = [t for t in eb if not set(T[t][3]) <= {RED}]
    rate = collections.Counter()
    for v in mk.values():
        rate.update({it: v[0] * n for it, n in v[2].items()})
    for t in lab:
        pre, todo = {t}, [t]
        while todo:
            for p_ in techs[todo.pop()]["pre"] or []:
                if p_ in packs and p_ not in pre:
                    pre.add(p_)
                    todo.append(p_)
        tot = sum((packs[p_] for p_ in pre if p_ in lab), collections.Counter())
        lb = max([(n - stock0.get(pk, 0)) / rate[pk] for pk, n in tot.items() if rate[pk] > 0] + [0])
        md.Add(T[t][1] >= min(int(lb), TAU[-1]))
    crew = [g for g in sb if not g.startswith("power:")]
    top = max([dur["g"].get(g, 0) for g in crew] + [0])
    for k in range(K):
        if lab:
            md.Add(sum(dur["t"][t] * eb[t][k] for t in lab) <= TAU[k + 1] - 1)
        if crew and top:
            md.Add(sum(dur["g"].get(g, 0) * sb[g][k] for g in crew) <= TAU[k + 1] - 1 + top)
    x = {}                    # x[h, r][k]: crafts of r by h in slot k
    for (h, r), (cps, ing, prod, fuel) in mk.items():
        row = []
        for k in range(K):
            ub = int(cps * DT[k])
            v = md.NewIntVar(0, ub, "") if ub > 0 else 0
            if ub > 0 and not isinstance(on[h][k], int):
                md.Add(v <= ub * on[h][k])
            row.append(v)
        x[h, r] = row
    for it in items:
        made_by = [(n, hr) for hr, v in mk.items() if (n := v[2].get(it))]
        used_by = [(n, hr) for hr, v in mk.items() if (n := v[1].get(it))]
        users = [(c[it], b) for c, b in takes if c.get(it)]
        users += [(p[it], rt[t]) for t, p in packs.items() if p.get(it)]
        s0 = int(stock0.get(it, 0))
        cap = s0 + sum(n * int(mk[hr][0] * DT[k]) for n, hr in made_by for k in range(K))
        F = s0                # stock + made - used by machines so far; machines never take the stock (chests and
        for k in range(K + 1):    # outputs are the builders' and the labs'), so F >= stock
            if users:
                md.Add(F >= sum(n * b[min(k, K - 1)] for n, b in users))
            if k == K:
                break
            f = md.NewIntVar(s0, cap, "")
            md.Add(f == F + sum(n * x[hr][k] for n, hr in made_by) - sum(n * x[hr][k] for n, hr in used_by))
            F = f
    return {"x": x, "mk": mk, "need": need, "unmade": unmade, "DT": DT}


def _solve(plan, built, researched, gd, dem, sec_per_piece, labs, seconds, retired, sizes, early_mall, parts, hint,
           stock0=None, rates=None):
    from auto import grow
    md = cp_model.CpModel()
    lits = {}

    def why(name):
        """An assumption literal per constraint family: an infeasible model names the conflicting ones."""
        if name not in lits:
            lits[name] = md.NewBoolVar(name)
        return lits[name]
    techs = gd["techs"]
    groups = [g for g in plan.prod if g not in retired]
    done = {g for g in groups if all(i in built for i in plan.groups[g])}
    sizes = sizes or {}
    pieces = {g: (0 if g in done else sizes.get(g, sum(1 for i in plan.groups[g] if i not in built))) for g in groups}
    need_t = set(dem.get("techs", []))
    for g in groups:
        need_t |= plan.techs.get(g, set())
        for r in plan.recipes(g):
            need_t |= grow.techs_of([r], gd)
    tlist = sorted(t for t in need_t if t in techs)
    dur_lab = {t: math.ceil(techs[t]["count"] * techs[t]["time"] / labs) for t in tlist}
    H = sum(pieces.values()) * sec_per_piece + sum(dur_lab.values()) * labs + 10 ** 5
    H = int(H) + (TAU[-1] if "flow" in parts else 0)     # room after the grid for what the flow cannot supply

    def iv(name, size, fixed=False):
        s = md.NewIntVar(0, 0 if fixed else H, f"s_{name}")
        e = md.NewIntVar(0, 0 if fixed else H, f"e_{name}")
        return s, e, md.NewIntervalVar(s, int(size), e, f"i_{name}")

    # ---- research: labs or bootstrap lab, one of them
    T = {}
    lab_iv, hut_iv = [], []
    for t in tlist:
        fixed = t in researched
        s, e, _ = iv("t_" + t, 0, fixed) if fixed else (md.NewIntVar(0, H, f"s_{t}"), md.NewIntVar(0, H, f"e_{t}"), None)
        T[t] = (s, e)
        if fixed:
            continue
        trig = techs[t].get("trigger")
        if trig:                      # no lab time: the action itself (see triggers below)
            md.Add(e >= s)
            continue
        ing = [i[0] for i in techs[t]["ing"]] if isinstance(techs[t]["ing"], list) else []
        on_labs = md.NewBoolVar(f"labs_{t}")
        a = md.NewOptionalIntervalVar(s, dur_lab[t], e, on_labs, f"L_{t}")
        lab_iv.append(a)
        if ing and set(ing) <= {RED}:
            on_hut = on_labs.Not()
            b = md.NewOptionalIntervalVar(s, dur_lab[t] * labs, e, on_hut, f"H_{t}")
            hut_iv.append(b)
        else:
            md.Add(on_labs == 1)
        T[t] = (s, e, on_labs, ing)
    md.AddNoOverlap(lab_iv)
    md.AddNoOverlap(hut_iv)
    for t in tlist:
        pre = techs[t]["pre"] if isinstance(techs[t]["pre"], list) else []
        for p in pre:
            if p in T:
                md.Add(T[t][0] >= T[p][1]).OnlyEnforceIf(why(f"tech {t} after {p}"))

    # ---- build tasks (boiler units are optional: built only if the power balance needs them)
    B, use = {}, {}
    for g in groups:
        nm = "g_" + g.replace(":", "_")
        if g.startswith("power:") and g not in done:
            use[g] = md.NewBoolVar("use_" + nm)
            s_, e_ = md.NewIntVar(0, H, "s_" + nm), md.NewIntVar(0, H, "e_" + nm)
            B[g] = (s_, e_, md.NewOptionalIntervalVar(s_, int(pieces[g] * sec_per_piece), e_, use[g], "i_" + nm))
        else:
            B[g] = iv(nm, int(pieces[g] * sec_per_piece), g in done)
    if "crew" in parts:
        md.AddNoOverlap([B[g][2] for g in groups if g not in done])

    def tend(t):
        return T[t][1] if t in T else 0

    ready = {}
    for g in groups:
        r_end = []
        for r in plan.recipes(g):
            a = [tend(t) for t in grow.techs_of([r], gd) if t in T]
            sub = grow.RECIPE_SUB.get(r)
            b = [tend(t) for t in grow.techs_of([sub], gd) if t in T] if sub else None
            ra = md.NewIntVar(0, H, "")
            md.AddMaxEquality(ra, a + [0])
            if b is not None:
                rb = md.NewIntVar(0, H, "")
                md.AddMaxEquality(rb, b + [0])
                rr = md.NewIntVar(0, H, "")
                md.AddMinEquality(rr, [ra, rb])
                r_end.append(rr)
            else:
                r_end.append(ra)
        rd = md.NewIntVar(0, H, f"ready_{g}")
        # producing once built and one recipe can run (the first one researched is enough)
        first = md.NewIntVar(0, H, "")
        md.AddMinEquality(first, r_end or [md.NewConstant(0)])
        md.AddMaxEquality(rd, [B[g][1], first])
        ready[g] = rd

    # entity techs; inputs from the earliest ready maker of each needed item
    for g in groups:
        if g in done:
            continue
        for t in plan.techs.get(g, set()):
            if t in T:
                md.Add(B[g][0] >= T[t][1]).OnlyEnforceIf(why(f"{g} after tech {t}"))
        needs = [[it] for it in plan.need[g]]
        if g == "labs":                       # a lab runs on any pack: the first pack maker is enough
            needs = [sorted(plan.need[g])]
        for its in needs:
            it = "/".join(its)
            makers = [h for h in plan.deps.get(g, ()) if set(its) & plan.made.get(h, set()) and h in ready]
            if not makers:
                continue
            if g.startswith("power") and it == "coal":
                continue                      # the tender hand-feeds the first boilers
            m_ = md.NewIntVar(0, H, "")
            md.AddMinEquality(m_, [ready[h] for h in makers])
            md.Add(B[g][0] >= m_).OnlyEnforceIf(why(f"{g} after a maker of {it}"))

    # research needs its packs: labs path after the labs group and each pack's maker
    for t in tlist:
        if t in researched or len(T[t]) < 4:
            continue
        s, e, on_labs, ing = T[t]
        if "labs" in ready:
            md.Add(s >= ready["labs"]).OnlyEnforceIf([on_labs, why(f"tech {t} after labs")])
        for pk in ing:
            mk = [h for h in groups if pk in plan.made.get(h, ())]
            if mk:
                m_ = md.NewIntVar(0, H, "")
                md.AddMinEquality(m_, [ready[h] for h in mk])
                md.Add(s >= m_).OnlyEnforceIf([on_labs, why(f"tech {t} after a maker of {pk}")])
    # trigger techs: their action
    for t in tlist:
        trig = techs[t].get("trigger") or ""
        if t in researched or not trig:
            continue
        if trig.startswith("mine-entity"):
            mk = [h for h in groups if "crude-oil" in plan.made.get(h, ())]   # the only mine trigger the plan uses
            if mk:
                m_ = md.NewIntVar(0, H, "")
                md.AddMinEquality(m_, [ready[h] for h in mk])
                md.Add(T[t][1] >= m_).OnlyEnforceIf(why(f"tech {t} after crude oil mined"))

    # mall: entities come from the mall makers once they stand, or are hand-made before the early mall is ready
    stock = collections.defaultdict(list)
    for h in groups:
        for it in plan.made.get(h, ()):
            if it.startswith("stock:"):
                stock[it[6:]].append(h)
    for h in [x for x in plan.groups if x.startswith("mall:")]:
        for it in h[5:].split("+"):
            stock[it].append(h)
    em = [ready[h] for it in early_mall for h in stock.get(it, []) if h in ready]
    mall_ready = md.NewIntVar(0, H, "mall_ready")
    md.AddMaxEquality(mall_ready, em or [md.NewConstant(0)])
    hand = {}
    for g in groups:
        if g in done or "mall" not in parts:
            continue
        hand[g] = md.NewBoolVar(f"hand_{g}")
        md.Add(B[g][1] <= mall_ready).OnlyEnforceIf([hand[g], why(f"{g} hand-made only before the mall")])
        # what is placed first: assembler 1 for crafting recipes (upgraded later)
        items = {_placed(plan.p[i], gd) for i in plan.groups[g]} - set(grow.POLE_ITEMS)
        for it in items:
            mk = [h for h in stock.get(it, []) if h in ready and h != g]
            if not mk:
                continue
            m_ = md.NewIntVar(0, H, "")
            md.AddMinEquality(m_, [ready[h] for h in mk])
            md.Add(B[g][0] >= m_).OnlyEnforceIf([hand[g].Not(), why(f"{g} after the mall makes {it}")])

    # power: boiler units (optional, chain order) supply; electric groups draw from when built
    units = sorted((g for g in groups if g.startswith("power:")), key=lambda g: int(g.split(":")[1]))
    times, deltas, actives = [], [], []
    prev = None
    for g in units:
        pres = md.NewConstant(1) if g in done else use[g]
        if prev is not None and g not in done:
            md.AddImplication(pres, prev)          # water runs boiler to boiler: in chain order
            md.Add(B[g][1] >= B[units[units.index(g) - 1]][1]).OnlyEnforceIf(pres)
        prev = pres
        times.append(B[g][1])
        deltas.append(UNIT_KW)
        actives.append(pres)
    # average draw ~ 60% of nameplate; what stands already may run browned out: that is the starting level
    draw = {g: int(0.6 * sum(KW.get(plan.p[i]["item"], 0) for i in plan.groups[g])) for g in groups}
    have = sum(UNIT_KW for g in units if g in done) - sum(draw[g] for g in done if not g.startswith("power"))
    times.append(md.NewConstant(0))
    deltas.append(max(0, -have))
    actives.append(md.NewConstant(1))
    for g in groups:
        if draw[g] and not g.startswith("power"):
            times.append(B[g][1])
            deltas.append(-draw[g])
            actives.append(md.NewConstant(1))
    if "power" in parts:
        md.AddReservoirConstraintWithActive(times, deltas, actives, 0, 10 ** 9)
    fl = None
    if "flow" in parts and stock0 is not None:
        # (the crew's and the labs' rows hold only under their NoOverlap: no crew part, no crew row)
        fl = _flow(md, plan, gd, groups, done, built, B, T, ready, hand, use, tlist, researched, stock0, rates, hint,
                   "late" in parts, {"g": {g: int(pieces[g] * sec_per_piece) for g in groups} if "crew" in parts
                                     else {}, "t": dur_lab})

    # objective: the silo producing (rocket parts flow), then fewest hand-made pieces
    goal = ready["silo"] if "silo" in ready else md.NewConstant(0)
    obj = goal + HAND_COST * sum(pieces[g] * hand[g] for g in hand)
    if fl:   # + every tech as early as its packs allow: a slope on the rocket's plateaus (the grid makes them) for LNS
        rs = [T[t][1] for t in tlist if t not in researched]
        md.Minimize((len(rs) + 1) * obj + sum(rs))
    else:
        md.Minimize(obj)
    if hint:   # start the search from an earlier schedule: same task names, same times
        for g in groups:
            if g in hint.get("B", {}) and g not in done:
                md.AddHint(B[g][0], hint["B"][g][0])
                md.AddHint(B[g][1], hint["B"][g][1])
                if g in use:
                    md.AddHint(use[g], 1)
        for t in tlist:
            if t in hint.get("T", {}) and t not in researched:
                md.AddHint(T[t][0], hint["T"][t][0])
                md.AddHint(T[t][1], hint["T"][t][1])
    if fl:      # assumptions force one search thread; the flow phase needs them all (cycles are found earlier)
        for v in lits.values():
            md.Add(v == 1)
    else:
        md.AddAssumptions(list(lits.values()))
    t0 = time.time()
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = seconds * (0.8 if fl else 1)
    sv.parameters.num_workers = 8
    if fl:
        # the levels are linear rows: their LP relaxation is tight (its bound is the optimum here), so the LP workers
        # find the schedule; the core / no-LP / fixed ones did not (measured on the 23 h game: with this portfolio
        # and the techs' slope 10 of 10 runs reached the best rocket time in 13-40 s, without them 2 of 4)
        sv.parameters.linearization_level = 2
        sv.parameters.relative_gap_limit = 0.01     # < a slot of rocket time: the goal is as good as the grid allows
        sv.parameters.ignore_subsolvers.extend(["core", "no_lp", "fixed"])
        sv.parameters.extra_subsolvers.extend(["max_lp", "reduced_costs", "pseudo_costs"])
    st = sv.Solve(md)
    name = sv.StatusName(st)
    rsv = sv                      # the research order: as the first solve has it (each tech as early as it can be)
    if fl and st not in OK:       # the caller keeps the previous phase's schedule (precedence cycles are found there)
        return {"status": name}
    if fl and seconds - (time.time() - t0) > 3:
        # second, at that rocket time: every piece as early as the items allow (the order among the groups off the
        # critical path is otherwise arbitrary), from the first solution (a full hint). The techs stay where the first
        # put them: the labs research as soon as packs come (the research list grow follows), so a build order that
        # delays research to save the plates would never happen
        r_ = sv.ResponseProto()
        md.ClearHints()
        md.Proto().solution_hint.vars.extend(range(len(r_.solution)))
        md.Proto().solution_hint.values.extend(list(r_.solution))
        md.Add(obj <= sv.Value(obj))
        for t in tlist:
            if t not in researched:
                md.Add(T[t][1] == sv.Value(T[t][1]))
        md.Minimize(sum(pieces[g] * B[g][0] for g in groups if g not in done and g not in use))
        sv2 = cp_model.CpSolver()     # it settles in ~10 s (measured)
        sv2.parameters.max_time_in_seconds = seconds - (time.time() - t0)
        sv2.parameters.num_workers = 8
        sv2.parameters.linearization_level = 2
        sv2.parameters.relative_gap_limit = 0.005
        if sv2.Solve(md) in OK:
            sv = sv2
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # the fewest rules to drop for a schedule to exist = the cycle (CP-SAT minimises the relaxation)
        md.ClearAssumptions()
        md.Minimize(sum(v.Not() for v in lits.values()))
        sv2 = cp_model.CpSolver()
        sv2.parameters.max_time_in_seconds = seconds
        sv2.parameters.num_workers = 8
        st2 = sv2.Solve(md)
        ok2 = st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        core = [k for k, v in lits.items() if ok2 and not sv2.Value(v)]
        if not ok2:
            return {"status": name, "order": [], "research": [], "hand": [], "makespan": None, "units": [],
                    "conflict": core}
        sv = sv2     # the relaxed schedule still orders the work; the conflict says what it had to drop
    order = sorted((g for g in groups if g not in done and (g not in use or sv.Value(use[g]))),
                   key=lambda g: sv.Value(B[g][0]))
    rsv = rsv if fl else sv
    research = sorted((t for t in tlist if t not in researched), key=lambda t: rsv.Value(T[t][0]))
    core = locals().get("core")
    values = {"B": {g: (sv.Value(B[g][0]), sv.Value(B[g][1])) for g in groups},
              "T": {t: (rsv.Value(T[t][0]), rsv.Value(T[t][1])) for t in tlist}}
    out = {"status": name, "values": values, "order": order, "start": {g: sv.Value(B[g][0]) for g in order},
           "research": research, "hand": [g for g in hand if sv.Value(hand[g])],
           "makespan": sv.Value(goal), "units": [g for g in units if g in use and sv.Value(use[g])],
           "conflict": core}
    if fl:
        out["flow"] = {"late": [g for g in order if out["start"][g] >= TAU[-1]], "unmade": fl["unmade"]}
    return out
