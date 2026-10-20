"""drain:<x>,<y> — a mall chest full of one item locks its other item out: picked up with its contents (to the
depot, where every build can still fetch them) and placed again empty."""
import collections

from auto.growth.mall import chest_contents
from auto.repairs.common import done


def run(ctx, g, why):
    supply = ctx.supply
    k = ctx.begin()
    x_, y_ = (float(v_) for v_ in g[6:].split(","))
    b0 = ctx.Bridge(ctx.builders[0], autostart=False)
    before_ = chest_contents(ctx.q, [(x_, y_)])[0]
    ctx.log(f"#{k} {g}: holds {before_} — {why}")
    # the builder picks the chest up with its contents (room first: up to 32 stacks come along) and places it
    # again empty; the contents go to the depot
    supply.unload(b0, collections.Counter(), low=40)
    ctx.build([{"item": "iron-chest", "x": x_, "y": y_, "remove": True}], one=True)
    res = ctx.build([{"item": "iron-chest", "x": x_, "y": y_}], one=True)
    supply.unload(b0, collections.Counter(), low=70)
    dep = supply.deposit(b0)
    try:
        ctx.mall_limits()     # the new chest has no bar yet
    except Exception as e:
        ctx.log("mall limits:", str(e)[:120])
    after_ = chest_contents(ctx.q, [(x_, y_)])[0]
    ctx.L.refresh((x_ - 6, y_ - 6, x_ + 6, y_ + 6))
    done(ctx, k, g, why, f"drained {g}: {sum(before_.values())} items -> depot (last deposit {dep}); chest now "
         f"{after_}; {str(res)[:120]}", {"before": before_, "after": after_}, skip=30)
