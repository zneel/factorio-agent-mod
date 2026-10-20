"""face — built machines turned to their planned facing (growth.sensors.misfaced)."""
from auto.growth.sensors import misfaced
from auto.repairs.common import planned


def run(ctx, g, why):
    if ctx.args.dry:          # the rule found them (L.misfaced): planned, none turned
        planned(ctx, g, why, f"would turn {len(ctx.L.misfaced)} machines to their planned facing", [])
        return
    k = ctx.begin()
    r_ = misfaced(ctx.plan, ctx.L, ctx.q, fix=True)
    ok_ = [t for t in r_ if not t.startswith("!")]
    ctx.log(f"#{k} face: turned {len(ok_)} machines to their planned facing, {len(r_) - len(ok_)} would not "
            f"turn — {why}")
    ctx.record({"n": k, "groups": [g], "why": why, "pieces": len(ok_)})
    ctx.save()
