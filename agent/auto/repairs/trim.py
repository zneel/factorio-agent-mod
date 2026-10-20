"""trim — belt tails past the last pickup picked up (their load back to the builders) and written off."""
from auto.repairs.common import done, planned


def run(ctx, g, why):
    plan, L, state = ctx.plan, ctx.L, ctx.state
    k = ctx.begin()
    bad = L.trim
    if ctx.args.dry:
        planned(ctx, g, why, f"picks up {len(bad)} tail belts", [dict(plan.p[i], remove=True) for i in bad], k=k)
        return
    ctx.log(f"#{k} trim: picks up {len(bad)} tail belts — {why}")
    res = ctx.build([dict(plan.p[i], remove=True) for i in bad], one=True)
    ctx.supply.deposit(ctx.Bridge(ctx.builders[0], autostart=False))
    L.refresh(plan.box(bad, 4))
    gone_ = [i for i in bad if i not in L.built]
    L.dead |= set(gone_)
    state["dead"] = sorted(L.dead)
    state["placed"] = [i for i in state.get("placed", []) if i not in set(gone_)]
    done(ctx, k, g, why, f"trim: {len(gone_)} of {len(bad)} picked up; {str(res)[:160]}", {"pieces": len(gone_)})
