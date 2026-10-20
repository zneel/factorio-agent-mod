"""outpost:<resource> — a far patch tapped (auto.scout): pumpjacks piped into the built crude network, or a mine
(auto.remine) whose belt route feeds a built mine's network; its pole chain runs along the route. stand() places
and judges what a scouting repair planned (radar.py too)."""
import math

from auto.growth.world import plan_poles, route_poles
from auto.repairs.common import OWN_KEYS, around, done, keep_own, look, planned, step, xy


def run(ctx, g, why):
    stock = dict(ctx.gate.have())
    n_, pc, nd = ctx.L.outpost
    if g.startswith("outpost:crude-oil"):
        r = crude(ctx, g, pc, nd)
    else:
        r = ore(ctx, g, n_, pc, nd, stock)
    if r is None:
        return
    new_, cor, h, info = r
    # an outpost's long chain uses small poles: the builders make them from wood by hand (the one item always
    # hand-made), while medium poles need steel from a factory that may be starving for the very resource the
    # outpost brings (measured: a coal outpost held for 41 medium poles while coal ran out)
    with ctx.poles("small-electric-pole"):
        pp = plan_poles(ctx.plan, ctx.L, new_, ctx.m, corridor=cor)
        pp += route_poles(ctx.plan, ctx.L, new_ + pp, ctx.q, ctx.m)

    def kept(here, cnt, dr):
        if cnt.get("missing", 0) <= len(dr) // 2:
            # it stands: recorded, so the power repair (outpower) can finish its chain when drills lack power
            ctx.state.setdefault("outposts", []).append({"name": n_, "group": h, "box": list(pc["box"]), "drills": nd})
            return True
    stand(ctx, g, why, new_, pp, info, kept)


def crude(ctx, g, pc, nd):
    """Pumpjacks on the patch's free wells, piped into the built crude network (scout.pumpjack_outpost):
    (pieces, pole corridor, group fed, info), or None (logged, g skipped)."""
    from auto import geom, scout
    plan, L, q, bx = ctx.plan, ctx.L, ctx.q, pc["box"]
    h = "pipe:crude"
    ci = [i_ for i_ in L.built if plan.p[i_].get("grp") in ("pipe:crude", "oil-wells")]
    cb = plan.box(ci, 0) if ci else bx
    rb = (min(bx[0], cb[0]) - 24, min(bx[1], cb[1]) - 24, max(bx[2], cb[2]) + 24, max(bx[3], cb[3]) + 24)
    try:
        nx_ = scout.extend(ctx.m, ctx.out, rb, qf=lambda s_: q("/silent-command " + s_))
        new_, info = scout.pumpjack_outpost(ctx.m, plan, L, pc.get("wells") or scout.free_wells(q, bx), nd)
        info = f"{info}; map +{nx_} strips"
    except Exception as e:
        new_, info = None, f"pumpjack outpost planning failed: {str(e)[:160]}"
    if not new_:
        ctx.log(f"round {ctx.rnd}: {g}: {info}")
        ctx.ch.skip[g] = 40
        return None
    cor = {(t_[0] + a_, t_[1] + b_) for t_ in geom.tiles(new_) for a_ in range(-3, 4) for b_ in range(-3, 4)}
    return new_, cor, h, info


def ore(ctx, g, n_, pc, nd, stock):
    """The mine to feed: one whose own ore is gone first (its network starves: a dry coal column's belt carries
    the fuel lane of its smelters), one outpost per mine; then the nearest. (pieces, pole corridor, group fed,
    info), or None (logged, g skipped, the patch left out of the mine plan from now on)."""
    from auto import geom, live as lv, remine, scout
    plan, L, q, state, bx = ctx.plan, ctx.L, ctx.q, ctx.state, pc["box"]
    fed = {o_["group"] for o_ in state.get("outposts", [])}
    tg = [h_ for h_ in plan.prod if h_.startswith("mine:") and L.group_done(h_) and h_ not in fed
          and {"raw-iron-ore": "iron-ore"}.get(h_.split(":")[1], h_.split(":")[1]) == n_]
    try:
        st_ = lv.status(q, plan.box([i_ for h_ in tg for i_ in plan.groups[h_]], 4)) if tg else []
    except Exception:
        st_ = []
    at_ = {(round(x, 2), round(y, 2)): s_ for n__, x, y, s_ in st_ if n__ == "electric-mining-drill"}

    def dry_(h_):
        ss = [at_.get(xy(plan.p[i_]), "gone") for i_ in plan.groups[h_]
              if plan.p[i_]["item"] == "electric-mining-drill"]
        return sum(1 for x_ in ss if x_ in ("no_minable_resources", "gone")) / max(1, len(ss))
    tg.sort(key=lambda h_: (-round(dry_(h_), 1), math.dist(pc["center"], plan.box(plan.groups[h_], 0)[:2])))
    new_, info, h = None, "no working mine of that resource to feed", None
    for h in tg[:3]:
        hb = plan.box(plan.groups[h], 0)
        rb = (min(bx[0], hb[0]) - 24, min(bx[1], hb[1]) - 24, max(bx[2], hb[2]) + 24, max(bx[3], hb[3]) + 24)
        try:
            nx_ = scout.extend(ctx.m, ctx.out, rb, qf=lambda s_: q("/silent-command " + s_))
            pl_, info = remine.plan(q, ctx.m, plan, L, h, stock=stock, outpost=bx, n_new=nd)
        except Exception as e:
            pl_, info = None, f"outpost planning failed: {str(e)[:160]}"
        if pl_:
            new_ = [p_ for p_ in pl_ if not p_.get("remove")]
            info = f"{info}; map +{nx_} strips"
            break
    if not new_:
        ctx.log(f"round {ctx.rnd}: {g}: {info}")
        ctx.ch.skip[g] = 10
        # the mine plan leaves this patch out from now on: the next pass picks the next best one
        state.setdefault("outpost_failed", []).append(list(bx))
        return None
    # the pole chain runs along the route: tiles within 3 of every new piece
    cor = {(t_[0] + a_, t_[1] + b_) for t_ in geom.tiles(new_) for a_ in range(-3, 4) for b_ in range(-3, 4)}
    return new_, cor, h, info


def stand(ctx, g, why, new_, pp, info, kept):
    """Gate (held 60 rounds: the scout pass runs every 20, the mall catches up) -> build -> every piece ours to
    keep standing (restore retries what did not place this time) -> the radars, drills and pumpjacks' statuses ->
    kept(here, statuses, those pieces) says whether it stands (else g rests 10 rounds) -> record. A dry run: planned
    and taken as standing (kept), so the scouting goes on to the next site; nothing built."""
    if ctx.args.dry:
        planned(ctx, g, why, f"{info} + {len(pp)} poles", new_ + pp, rec={"info": info})
        kept({xy(p_) for p_ in new_ + pp}, {},
             [p_ for p_ in new_ if p_["item"] in ("radar", "electric-mining-drill", "pumpjack")])
        return
    r = step(ctx, g, why, new_ + pp, f"{info} + {len(pp)} poles", 60)
    if r is None:
        return
    k, res = r
    ctx.L.refresh(around(new_ + pp, 4))
    here = {(round(x, 2), round(y, 2)) for (x, y) in ctx.L.extra}
    keep_own(ctx, new_ + pp, OWN_KEYS, new_only=True)
    stood = sum(1 for p_ in new_ + pp if xy(p_) in here)
    dr = [p_ for p_ in new_ if p_["item"] in ("radar", "electric-mining-drill", "pumpjack")]
    cnt = look(ctx, dr, 4)
    done(ctx, k, g, why, f"{g}: {stood}/{len(new_ + pp)} placed; {dict(cnt)}; {str(res)[:160]}",
         {"info": info, "status": dict(cnt)}, skip=None if kept(here, cnt, dr) else 10)
