"""outpower:<k> — an outpost's pole chain along its route to the powered network (its drills stand unpowered)."""
import collections

from auto.growth.world import outpost_pieces, route_poles
from auto.repairs.common import done, keep_own, step


def run(ctx, g, why):
    from auto import live as lv
    o = ctx.state["outposts"][int(g[9:])]
    pcs = outpost_pieces(ctx.state, o)
    with ctx.poles("small-electric-pole"):     # hand-made from wood (see the outpost repair)
        pp = route_poles(ctx.plan, ctx.L, pcs, ctx.q, ctx.m, box=o["box"])
    if not pp:
        ctx.log(f"round {ctx.rnd}: {g}: no pole chain along {len(pcs)} pieces of its route")
        ctx.ch.skip[g] = 40
        return
    r = step(ctx, g, why, pp, f"{len(pp)} poles along {len(pcs)} route pieces", 20)
    if r is None:
        return
    k, res = r
    keep_own(ctx, pp, ("item", "x", "y"))
    ctx.settle()
    st_ = lv.status(ctx.q, tuple(o["box"]))
    cnt = collections.Counter(s_ for n_, *_x, s_ in st_ if n_ == "electric-mining-drill")
    done(ctx, k, g, why, f"{g}: drills {dict(cnt)}; {str(res)[:160]}", {"pieces": len(pp), "status": dict(cnt)},
         skip=10)
