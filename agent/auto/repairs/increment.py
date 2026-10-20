"""A plan group (the schedule's next, a half-built group's repair, the gate's alt) or a repair that only selects
plan pieces (restore, repower, reconnect, gap, rears, upgrade, finish, link: see growth.increments.increment):
its pieces, the transport it needs and its poles; built, then judged after a game minute (machines powered and
not broken, rates before and after)."""
import collections
import re
import time

from auto.growth.core import POLE_ITEMS, techs_of
from auto.growth.increments import increment
from auto.growth.mall import mall_ready
from auto.growth.upkeep import launched
from auto.growth.world import hut_missing, plan_poles, removals, validate
from auto.repairs.common import OWN_KEYS, keep_own


def run(ctx, g, why):
    """True once the rocket went up."""
    r = prepare(ctx, g, why)
    if r is None:
        return
    g, why, places, groups, idx, pl = r
    errs = validate(ctx.plan, ctx.L, places + pl)
    k = ctx.begin()
    ctx.log(f"#{k} {'+'.join(groups)}: {len(places)} pieces + {len(pl)} poles — {why}" +
            (f" | CHECK {errs}" if errs else ""))
    if ctx.args.dry:
        for h in groups:
            for i in ctx.plan.groups.get(h, []):
                ctx.L.built.add(i)
        for i in idx:
            ctx.L.built.add(i)
        ctx.record({"n": k, "groups": groups, "why": why, "pieces": len(places), "poles": len(pl), "errs": errs})
        return
    if errs and any("on live" in e for e in errs):
        ctx.ch.skip[g] = 3
        ctx.record({"n": k, "groups": groups, "why": why, "errs": errs, "result": "invalid"})
        return
    return build(ctx, k, g, why, places, groups, idx, pl)


def prepare(ctx, g, why):
    """The increment's pieces once its techs are researched and the gate passes it (or the alt the gate named):
    (g, why, places, groups, idx, poles), else None (g waits)."""
    plan, L, state, ch, gate = ctx.plan, ctx.L, ctx.state, ctx.ch, ctx.gate
    places, groups, idx = increment(plan, L, g, L.researched, ctx.pole_item)
    if g.startswith("power:") and ctx.hut and state["handcraft"] and hut_missing(L, ctx.hut):
        places = places + [dict(ctx.hut)]     # the bootstrap lab rides with the first power unit (its poles)
    # every item placed must be craftable (a splitter needs logistics)
    need_t = (set().union(*(plan.techs.get(h, set()) for h in groups)) |
              techs_of({p["item"] for p in places if p["item"] not in POLE_ITEMS}, ctx.gd)) - L.researched
    if need_t and not ctx.args.dry:     # e.g. the first steam plant's coal drills: research them first
        ch.want_techs = sorted(need_t) + [t for t in ch.want_techs if t not in need_t]
        ctx.rstate["want"] = ch.want_techs + [t for t in ctx.dem["techs"] if t not in ch.want_techs]
        ctx.log(f"round {ctx.rnd}: {g} waits for research {sorted(need_t)}")
        ch.skip[g] = 4            # build something else meanwhile
        time.sleep(5)
        return None
    pl = plan_poles(plan, L, places, ctx.m)
    if not ctx.args.dry:      # from what the builders can reach (the mall), unless hand-crafting is on for g
        gv = gate(g, places + pl)
        if gv[0] == "alt":
            h, w = gv[1], gv[2]
            if h.startswith("drain:"):
                ch.pending.insert(0, (h, w))     # a repair of its own branch: first thing next round
                ctx.log(f"round {ctx.rnd}: {g} waits for {h}: {w}")
                return None
            g, why = h, w
            places, groups, idx = increment(plan, L, g, L.researched, ctx.pole_item)
            pl = plan_poles(plan, L, places, ctx.m)
            ctx.macros.HANDCRAFT = state["handcraft"] or ctx.grid_dead or g in gate.hand
            gv = gate(g, places + pl, depth=3)   # the alt passed without its poles: once more with them
        if gv[0] != "build":
            # a schedule group passed choose()'s gate (without poles): short only of poles or by a race, so it
            # waits just 2 rounds; a repair keeps its own counter
            ctx.held(g, gv, 2 if why.startswith("schedule") else 4)
            time.sleep(2)
            return None
    return g, why, places, groups, idx, pl


def build(ctx, k, g, why, places, groups, idx, pl):
    """Build #k, a game minute of running, then judge it: True once the rocket went up."""
    from auto import control
    plan, L, state, ch = ctx.plan, ctx.L, ctx.state, ctx.ch
    snap = ctx.snap
    before = {it: round(snap["made"].get(it, 0), 1) for it in plan.made.get(g.removeprefix("link:"), ())
              if it in snap["made"]}
    t0 = time.time()
    state.setdefault("tried", [])
    state["tried"] += [h for h in groups if h in plan.groups and h not in state["tried"]]
    if g == "reconnect":       # orphan underground entrances are picked up before the routes are laid
        ctx.build([p_ for p_ in places if p_.get("remove")], one=True)
        places = [p_ for p_ in places if not p_.get("remove")]
        keep_own(ctx, places, OWN_KEYS)
    rm = removals(plan, L, places + pl)
    if rm:
        ctx.log(f"#{k} picks up {len(rm)} burner-phase entities in the way")
        ctx.build(rm, one=True)
    res = ctx.build(places + pl)
    for p_ in places:
        if p_.get("final"):
            state.setdefault("pending_recipes", {})[f"{p_['x']},{p_['y']}"] = p_["final"]
    sh = collections.Counter()
    for r_ in res:
        sh.update({k_: v_ for k_, v_ in (r_.get("short") or {}).items() if isinstance(v_, (int, float))})
    state["short"] = dict(sh)
    L.refresh(plan.box(idx, 10) if idx else None)
    left = [i for i in idx if i not in L.built]
    ctx.settle()
    cnt, bad = L.statuses(idx)
    snap2 = control.measure(ctx.q, ctx.track)
    after = {it: round(snap2["made"].get(it, 0), 1) for it in plan.made.get(g.removeprefix("link:"), ())
             if it in snap2["made"]}
    if bad and any(s in ("no_power", "low_power") for *_, s, _g in bad):
        more = plan_poles(plan, L, [plan.p[i] for i in idx], ctx.m)
        gv = ctx.gate(g, more) if more else None
        if gv and gv[0] == "build":
            ctx.build(more)
            cnt, bad = L.statuses(idx)
        elif gv:
            if gv[0] == "alt":
                ch.pending.append((gv[1], gv[2]))
            ctx.log(f"#{k} more poles wait: {gv[2] if gv[0] == 'skip' else gv[1:]}")
    # belt ends of this increment that feed a plan piece not built yet (by design only where a trunk waits for the
    # bus or a fuel line runs past future boilers; anything else is a bug to look at)
    open_ends = collections.Counter()
    for i in idx:
        if i in L.built and plan.p[i]["item"] in ("transport-belt", "underground-belt", "splitter"):
            for j in plan.flow.feeds[i]:
                if j not in L.built:
                    open_ends[plan.p[j].get("grp")] += 1
    ok = not left and not [b for b in bad if b[3] in ("no_power", "low_power", "missing") or
                           (b[3] == "no_fuel" and b[0] != "boiler")]   # the tender fuels boilers at first
    ctx.log(f"#{k} {'OK' if ok else 'NOT OK'} in {(time.time() - t0) / 60:.1f} min; unbuilt {len(left)}; "
            f"status {dict(cnt)}; made {before} -> {after}" +
            (f"; open belt ends into {dict(open_ends)}" if open_ends else "") + (f"; bad {bad[:4]}" if bad else "") +
            (f"; build {str(res)[:200]}" if left else ""))
    ctx.record({"n": k, "tick": snap2.get("tick"), "groups": groups, "why": why, "pieces": len(places),
                "poles": len(pl), "unbuilt": len(left), "status": dict(cnt), "bad": bad[:20],
                "open_ends": dict(open_ends),
                "before": before, "after": after, "minutes": round((time.time() - t0) / 60, 2)})
    give_up(ctx, k, res, left)
    if left:
        ch.skip[g] = 2
    elif not ok and g.startswith(("repower:", "link:", "restore", "rears", "finish:")):
        ch.skip[g] = 6           # a repair that did not take: try other work before the next attempt
    if state["handcraft"] and mall_ready(plan, L):
        state["handcraft"] = False
        ctx.log("the early mall stands: hand-crafting only where the schedule still says so")
    ctx.save()
    if launched(ctx.q):
        ctx.log("ROCKET LAUNCHED")
        return True


def give_up(ctx, k, res, left):
    """A plan piece whose spot is taken by something that stands (e.g. our own route over a mine's old drill row),
    or that no character can walk to (walled in by the build), fails the same way every time: after 3 tries it is
    given up (dead), not retried forever."""
    plan, L, state = ctx.plan, ctx.L, ctx.state
    blk = state.setdefault("blocked", {})
    for r_ in res if isinstance(res, list) else []:
        for f_ in (r_ or {}).get("failed") or []:
            mt = re.match(r"\S+ (-?[\d.]+),(-?[\d.]+): .*(blocked by|no path to)", str(f_))
            i_ = plan.key.get((round(float(mt[1]), 2), round(float(mt[2]), 2))) if mt else None
            if i_ is not None and i_ in left:
                blk[str(i_)] = blk.get(str(i_), 0) + 1
                if blk[str(i_)] >= 3:
                    L.dead.add(i_)
                    state["dead"] = sorted(L.dead)
                    ctx.log(f"#{k} gives up {plan.p[i_]['item']} {mt[1]},{mt[2]}: its spot stays blocked")
