"""radar:<x>,<y> — a radar that charts more of the map (auto.scout picked the site)."""
from auto.growth.world import plan_poles
from auto.repairs.outpost import stand


def run(ctx, g, why):
    ctx.gate.have()          # the stock read up front, as for an outpost (cached for the gate)
    x_, y_ = (float(v_) for v_ in g[6:].split(","))
    new_ = [{"item": "radar", "x": x_, "y": y_}]
    pp = plan_poles(ctx.plan, ctx.L, new_, ctx.m)

    def kept(here, cnt, dr):
        if (round(x_, 2), round(y_, 2)) in here:   # live.status has no radars: standing is all
            ctx.state.setdefault("radars", []).append([x_, y_])
            ctx.state.pop("radar_site", None)
            return True
    stand(ctx, g, why, new_, pp, "radar", kept)
