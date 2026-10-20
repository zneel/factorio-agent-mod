"""burner:<plate> — more burner smelting while plates (or stone) are what the builders lack (bootstrap)."""
from auto.growth.upkeep import start_tender
from auto.growth.world import burner_pairs
from auto.repairs.common import done, planned


def run(ctx, g, why):
    plan, L, state = ctx.plan, ctx.L, ctx.state
    k = ctx.begin()
    pl_ = burner_pairs(plan, L, ctx.m, g[7:], ctx.boot_dir, k)
    if ctx.args.dry:     # planned: counted as built (the rule's cap), no tender restart
        planned(ctx, g, why, f"{len(pl_)} pieces", pl_, k=k)
        state["short"] = {}
        state.setdefault("burners", {})[g[7:]] = state.get("burners", {}).get(g[7:], 0) + len(pl_) // 2
        return
    ctx.log(f"#{k} {g}: {len(pl_)} pieces — {why}")
    res = ctx.build(pl_) if pl_ else []
    L.refresh(plan.box(range(len(plan.p)), 8))
    if ctx.tender:
        ctx.tender.terminate()
    ctx.tender = start_tender(ctx.py, ctx.chars[-1], ctx.boot_dir, ctx.out / "grow-power.json", ctx.env) \
        if len(ctx.chars) > 1 else None
    state["short"] = {}
    state.setdefault("burners", {})[g[7:]] = state.get("burners", {}).get(g[7:], 0) + len(pl_) // 2
    done(ctx, k, g, why, f"built {sum(1 for p in pl_ if p['item'] == 'burner-mining-drill')} burner drills; "
         f"tender restarted; {str(res)[:160]}", {"pieces": len(pl_), "build": str(res)[:300]})
