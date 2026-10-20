"""relocate:<mine> — dry drills moved onto ore that is left beside the same belt (auto.relocate, CP-SAT)."""
from auto.growth.world import plan_poles
from auto.repairs.common import done, look, step, xy


def run(ctx, g, why):
    from auto import geom, relocate
    plan, L, state = ctx.plan, ctx.L, ctx.state
    h = g[9:]
    _, ds = L.relocate
    occ = set()
    for i in L.built:
        occ |= geom.footprint(plan.p[i])
    for (x_, y_), n_ in L.extra.items():
        occ |= geom.footprint({"item": n_, "x": x_, "y": y_})
    # plan pieces still to come stay free, except this mine's own drill slots
    occ |= {t for i, p_ in enumerate(plan.p) if i not in L.built and i not in L.dead
            and p_.get("grp") not in L.retired and p_.get("grp") != h for t in geom.footprint(p_)}
    belts = [plan.p[i] for i in plan.groups[h] if plan.p[i]["item"] == "transport-belt" and i in L.built]
    ore = {"raw-iron-ore": "iron-ore"}.get(h.split(":")[1], h.split(":")[1])
    pl_, info = relocate.plan(ctx.q, ctx.m, plan, L, ds, belts, ore, occ, both_sides=h.startswith("mine:coal"))
    if not pl_:
        # no spot for them: only these drills are written off (they stand, mining nothing); the group keeps
        # supplying and is dry only when the live measurement says so (dry_mine, >= 70%)
        for d_ in ds:
            i_ = plan.key.get(xy(d_))
            if i_ is not None and plan.p[i_]["item"] == "electric-mining-drill":
                L.dead.add(i_)
            else:
                state.setdefault("dry_own", []).append([round(d_["x"], 2), round(d_["y"], 2)])
        state["dead"] = sorted(L.dead)
        if h not in state.setdefault("no_relocate", []):
            state["no_relocate"].append(h)
        ctx.log(f"round {ctx.rnd}: {g}: {info}; {len(ds)} dry drills written off, {h} mines on")
        ctx.save()
        return
    new_ = [p_ for p_ in pl_ if not p_.get("remove")]
    pp = plan_poles(plan, L, new_, ctx.m)
    gone = [p_ for p_ in pl_ if p_.get("remove")]      # picked up first: the moved drills come back
    r = step(ctx, g, why, new_ + pp, f"{info} + {len(pp)} poles", 6, back=gone, first=gone)
    if r is None:
        return
    k, res = r
    # moved plan drills are dead (never put back); moved own drills leave our list
    for d_ in ds:
        i_ = plan.key.get(xy(d_))
        if i_ is not None and plan.p[i_]["item"] == "electric-mining-drill":
            L.dead.add(i_)
    gone_own = {xy(d_) for d_ in ds}
    state["extra_places"] = [p_ for p_ in state.get("extra_places", []) if xy(p_) not in gone_own] + \
        [{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y", "direction")} for p_ in new_ + pp]
    L.own = state["extra_places"]
    state["dead"] = sorted(L.dead)
    L.refresh()
    cnt = look(ctx, new_, 3)
    done(ctx, k, g, why, f"relocated {h}: new drills {dict(cnt)}; {str(res)[:160]}",
         {"info": info, "drills": dict(cnt)}, skip=6 if cnt.get("missing", 0) else None)
