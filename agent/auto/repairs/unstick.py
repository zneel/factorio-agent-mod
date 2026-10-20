"""unstick — inserters holding what their machine cannot take: picked up and laid again."""
from auto.repairs.common import around, done


def run(ctx, g, why):
    L = ctx.L
    k = ctx.begin()
    pl_ = [p_ for p_, _held, _rec in L.unstick]
    ctx.log(f"#{k} unstick: {[(p_['x'], p_['y'], h_, r_) for p_, h_, r_ in L.unstick][:6]} — {why}")
    ctx.build([dict(p_, remove=True) for p_ in pl_], one=True)
    L.refresh(around(pl_, 4))
    gv = ctx.gate(g, pl_)                 # the inserters came back with the pickup
    if gv[0] != "build":
        ctx.log(f"#{k} unstick: not all inserters came back: {gv[2] if gv[0] == 'skip' else gv[1]}")
    res = ctx.build(pl_, one=True)
    tries = ctx.state.setdefault("unstuck", {})
    for p_ in pl_:
        kk = f"{p_['x']},{p_['y']}"
        tries[kk] = tries.get(kk, 0) + 1
    L.refresh(around(pl_, 4))
    done(ctx, k, g, why, f"unstuck {len(pl_)} inserters; {str(res)[:160]}", {"pieces": len(pl_)}, skip=10)
