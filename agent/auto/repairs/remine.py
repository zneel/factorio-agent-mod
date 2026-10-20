"""remine:<mine> — a dry mine replaced from live ore (auto.remine): new columns, or for a dry coal column a fuel
bridge from a live one into the network it fed."""
from auto.growth.core import techs_of
from auto.growth.increments import DEAD
from auto.growth.world import plan_poles
from auto.repairs.common import OWN_KEYS, around, done, keep_own, look, step, xy


def run(ctx, g, why):
    from auto import remine
    plan, L, state, q = ctx.plan, ctx.L, ctx.state, ctx.q
    h = g[7:]
    stock = dict(ctx.gate.have())     # routes tunnel only with undergrounds the builders can get
    pl_, info = remine.plan(q, ctx.m, plan, L, h, stock=stock)
    bridge = False
    if not pl_ and h.startswith("mine:coal") and state.get("bridged", []).count(h) < 3:
        # no ore left to mine here: feed the network this column fed from a live coal column instead
        live_src = [x for x in plan.prod if x.startswith("mine:coal") and x not in DEAD and L.group_done(x)]
        pl_, info2 = remine.fuel_bridge(q, ctx.m, plan, L, h, live_src, stock=stock, researched=L.researched)
        info = f"{info}; fuel bridge: {info2}"
        bridge = bool(pl_)
    if not pl_:
        ctx.log(f"round {ctx.rnd}: {g}: {info}")
        ctx.ch.skip[g] = 20
        return
    new_ = [p_ for p_ in pl_ if not p_.get("remove")]
    need_t = techs_of({p_["item"] for p_ in new_}, ctx.gd) - L.researched
    if need_t:
        ctx.log(f"round {ctx.rnd}: {g} waits for research {sorted(need_t)}")
        ctx.ch.skip[g] = 4
        return
    pp = plan_poles(plan, L, new_, ctx.m)
    # the dry drills are picked up only after the new column stands
    r = step(ctx, g, why, new_ + pp, f"{info} + {len(pp)} poles", 6)
    if r is None:
        return
    k, res = r
    L.refresh(around(new_ + pp, 4))
    here = {(round(x, 2), round(y, 2)) for (x, y) in L.extra}
    keep_own(ctx, [p_ for p_ in new_ + pp if xy(p_) in here])
    # a fuel bridge is judged by its inserters (whatever type fuel_bridge chose), a column by its drills
    dr = [p_ for p_ in new_ if (p_["item"].endswith("inserter") if bridge else p_["item"] == "electric-mining-drill")]
    cnt = look(ctx, dr, 4, optional=True)
    if cnt.get("missing", 0) <= len(dr) // 2:
        # the new column stands: every planned piece is ours to keep standing (restore retries what did not place,
        # e.g. an underground the mall had none of yet); the dry drills are picked up (reused) and the group retires
        keep_own(ctx, new_ + pp, OWN_KEYS, new_only=True)
        ctx.build([p_ for p_ in pl_ if p_.get("remove")], one=True)
        if bridge:
            state.setdefault("bridged", []).append(h)
        L.retired.add(h)
        state.setdefault("retired", []).append(h)
        state["placed"] = [i for i in state.get("placed", []) if plan.p[i].get("grp") != h]
        skip = None
    else:
        skip = 6
    done(ctx, k, g, why, f"remine {h}: new drills {dict(cnt)}; {str(res)[:160]}", {"info": info, "drills": dict(cnt)},
         skip=skip)
