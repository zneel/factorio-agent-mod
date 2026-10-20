"""pairs — own undergrounds the game paired with plan ones: picked up, so the plan pair pairs again."""
from auto.growth.core import materialize
from auto.growth.gate import bad_pairs, pair_check
from auto.repairs.common import around, done, xy


def run(ctx, g, why):
    plan, L, state = ctx.plan, ctx.L, ctx.state
    k = ctx.begin()
    rm_ = L.pairs
    ctx.log(f"#{k} pairs: picks up {len(rm_)} own undergrounds {[(p_['x'], p_['y']) for p_ in rm_]} — {why}")
    ctx.build([dict(p_, remove=True) for p_ in rm_], one=True)
    L.refresh(around(rm_, 8))
    # only what is really gone leaves our list (restore would put it back); one still standing stays ours, so the
    # next scan finds it again
    gone_ = {xy(p_) for p_ in rm_ if L.names.get(xy(p_)) != p_["item"]}
    state["extra_places"] = [p_ for p_ in state.get("extra_places", []) if xy(p_) not in gone_]
    L.own = state["extra_places"]
    # the robbed plan underground should pair with its planned partner again by itself; one left unpaired while
    # that partner stands is laid again (a placed underground pairs with the nearest in reach). Both stay "built",
    # so no other repair would see the cut line
    near_ = [plan.p[i] for i in L.built if plan.p[i]["item"].endswith("underground-belt") and
             any(abs(plan.p[i]["x"] - p_["x"]) + abs(plan.p[i]["y"] - p_["y"]) <= 12 for p_ in rm_)]
    lone_ = {xy_ for xy_, r_, w_ in (pair_check(ctx.q, plan, near_, near_) if near_ else [])
             if r_ is None and w_ is not None}
    re_ = [materialize(p_, L.researched, ctx.pole_item)[0] for p_ in near_ if (p_["x"], p_["y"]) in lone_]
    if re_:
        ctx.log(f"#{k} pairs: {len(re_)} plan undergrounds stand unpaired: laid again {sorted(lone_)}")
        ctx.build([dict(p_, remove=True) for p_ in re_], one=True)
        ctx.build(re_, one=True)
        L.refresh(around(re_, 8))
    ctx.ch.skip.pop("reconnect", None)    # the cut own route is laid again by reconnect (with spans)
    left_ = bad_pairs(plan, L, ctx.q, state)
    ctx.ch.skip["pairs_fix"] = 10
    done(ctx, k, g, why, f"pairs: {len(left_)} plan undergrounds still paired with own ones",
         {"removed": sorted(gone_), "left": len(left_)})
