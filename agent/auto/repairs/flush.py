"""flush:<group> — jammed belts picked up (their load comes along) and laid again."""
from auto.growth.core import materialize
from auto.growth.sensors import flushes
from auto.repairs.common import done, planned


def run(ctx, g, why):
    plan, L, state = ctx.plan, ctx.L, ctx.state
    k = ctx.begin()
    h = g[6:]
    bad = state.get("jam", {}).get(h, [])
    if ctx.args.dry:          # planned: counted as flushed out (jammed() leaves h until the map changes)
        planned(ctx, g, why, f"{len(bad)} belts", [dict(plan.p[i], remove=True) for i in bad] +
                [materialize(plan.p[i], L.researched, ctx.pole_item)[0] for i in bad], k=k)
        state.setdefault("flushed", {})[h] = 3
        state.setdefault("flush_at", {})[h] = len(L.built)
        return
    ctx.log(f"#{k} {g}: {len(bad)} belts — {why}")
    # two steps: pick up, then lay again (one plan with both would pick the new belts up again); one builder, the
    # one that carries the belts
    ctx.build([dict(plan.p[i], remove=True) for i in bad], one=True)
    L.refresh(plan.box(bad, 6))
    pl_ = [materialize(plan.p[i], L.researched, ctx.pole_item)[0] for i in bad]
    gv = ctx.gate(g, pl_)                 # the belts came back with the pickup
    if gv[0] != "build":
        ctx.log(f"#{k} {g}: the belts did not all come back: {gv[2] if gv[0] == 'skip' else gv[1]}")
    res = ctx.build(pl_, one=True)        # lays what it has either way
    ctx.supply.deposit(ctx.Bridge(ctx.builders[0], autostart=False))
    state.setdefault("flushed", {})[h] = flushes(state, h, L) + 1
    state.setdefault("flush_at", {})[h] = len(L.built)
    L.refresh(plan.box(bad, 6))
    done(ctx, k, g, why, f"flushed {len(bad)} belts of {h}: {str(res)[:160]}", {"pieces": len(bad)})
