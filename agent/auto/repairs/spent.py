"""spent:<block> — a block nothing needs any more: picked up, its load to the depot, its pieces written off."""
import collections

from auto.repairs.common import done, xy


def run(ctx, g, why):
    plan, L, state, supply = ctx.plan, ctx.L, ctx.state, ctx.supply
    k = ctx.begin()
    h = g[6:]
    idx = L.spent
    b0 = ctx.Bridge(ctx.builders[0], autostart=False)
    # inserters first (they hold an item each), then the machines (inputs and outputs), the belts last
    def rank(i):
        return 0 if "inserter" in plan.p[i]["item"] else 1 if "assembling" in plan.p[i]["item"] else 2
    ctx.log(f"#{k} {g}: picks up {dict(collections.Counter(plan.p[i]['item'] for i in idx))} — {why}")
    res, dep = None, {}
    for _ in range(3):              # a full inventory stops a pickup: empty it into the depot, go on
        left_ = [i for i in sorted(idx, key=rank) if i in L.built]
        if not left_:
            break
        supply.unload(b0, collections.Counter(), low=40)
        res = ctx.build([dict(plan.p[i], remove=True, item=L.names.get(xy(plan.p[i]), plan.p[i]["item"]))
                         for i in left_], one=True)
        dep = supply.deposit(b0)
        L.refresh(plan.box(idx, 4))
    gone_ = [i for i in idx if i not in L.built]
    L.dead |= set(gone_)
    state["dead"] = sorted(L.dead)
    state["placed"] = [i for i in state.get("placed", []) if i not in set(gone_)]
    if len(gone_) == len(idx):
        L.retired.add(h)          # supplies nothing: repairs, the schedule and the mall caps leave it
        state.setdefault("retired", []).append(h)
    done(ctx, k, g, why, f"{g}: {len(gone_)} of {len(idx)} picked up, load to the depot (last deposit {dep}); "
         f"{str(res)[:120]}", {"pieces": len(gone_)}, skip=None if len(gone_) == len(idx) else 20)
