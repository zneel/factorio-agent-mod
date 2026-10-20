"""One round of the controller: the upkeep before choosing (live map, research, mall limits, pole kind, recipes,
cracking, radars, the schedule's re-solve), then what to build (choose) and the hand-crafting rule for it."""
import json
import threading
import time

from auto.growth import core
from auto.growth.chooser import Look, choose
from auto.growth.core import log
from auto.growth.increments import DEAD
from auto.growth.mall import EARLY_MALL, mall_groups
from auto.growth.sensors import coal_left, made_rates
from auto.growth.upkeep import grid_dead, launched, oil_upkeep, set_pending_recipes


def begin(ctx):
    """Everything a round reads and keeps up before it chooses."""
    from auto import control, live as lv, poles
    import supply
    plan, L, state, ch, q, a = ctx.plan, ctx.L, ctx.state, ctx.ch, ctx.q, ctx.args
    for kk in list(ch.skip):
        ch.skip[kk] -= 1
    L.refresh()
    ctx.gate.stale()                                  # the gate reads the stock afresh each round
    ctx.macros.ALWAYS_HAND = {"small-electric-pole"}  # orphan exceptions last one round (the gate adds them)
    state["placed"] = sorted(set(state.get("placed", ())) | L.built)
    L.state_gone = state.get("gone", [])
    L.researched = lv.researched(q)
    if not a.dry:     # every round: a two-item chest's per-item caps live on its inserter's whitelist
        try:
            r_ = ctx.mall_limits("medium-electric-pole" if not state["handcraft"] else "small-electric-pole")
            if r_ != state.get("limits"):
                state["limits"] = r_
                log(f"mall limits (chests, slot-filtered, whitelisted inserters): {r_}")
        except Exception as e:
            log("mall limits:", str(e)[:120])
    # medium poles once researched AND the mall makes them (their maker and chest stand)
    med = list(plan.makers.get("stock:medium-electric-pole", [])) + mall_groups(plan, "medium-electric-pole")
    ctx.pole_item = "medium-electric-pole" if ("electric-energy-distribution-1" in L.researched and
                                               not state["handcraft"] and med and
                                               all(L.group_done(h) for h in med)) else "small-electric-pole"
    poles.use(ctx.pole_item)
    ctx.gate.pole_item = ctx.pole_item
    if not a.dry:     # game writes: recipes, cracking, chunks generated
        set_pending_recipes(q, state, L.researched, log)
        oil_upkeep(plan, L, q)
        try:
            from auto import scout
            scout.sweep(q, state)      # the radars' sweep (a playerless server does not run it)
        except Exception as e:
            log("sweep:", str(e)[:120])
    DEAD.clear()
    DEAD.update(state.get("dry", []))
    DEAD.update(L.retired)
    if not state["handcraft"] and supply.DEPOT[0] != ctx.mall_depot:
        supply.DEPOT[:] = [(ctx.mall_depot[0] + i, ctx.mall_depot[1]) for i in range(4)]
        log(f"depot moves next to the mall: {ctx.mall_depot}")
    core.MALL_MAKES = {it for it in EARLY_MALL + ["assembling-machine-2", "medium-electric-pole", "chemical-plant",
                                                  "oil-refinery", "pumpjack"]
                       if any(L.group_done(h) for h in plan.makers.get("stock:" + it, []) + mall_groups(plan, it))}
    ctx.snap = control.measure(q, ctx.track) if not a.dry else {"made": {}, "used": {}, "stock": {}}
    ch.votes.clear()
    reschedule(ctx)
    # rule 2: everything may be hand-crafted while no mall runs: the bootstrap (the early mall not built yet) or a
    # dead grid (no steam: the mall makes nothing); otherwise only what the schedule decides to hand-craft (its
    # objective prices every such piece)
    dead = not a.dry and not state["handcraft"] and grid_dead(q)
    if dead != state.get("grid_dead", False):
        state["grid_dead"] = dead
        log("the grid makes no power: hand-crafting until steam is back" if dead else "steam is back")
    ctx.grid_dead = dead


def reschedule(ctx):
    """The CP-SAT schedule solved again (in a thread: the controller keeps working) whenever the groups done or
    the research changed; a dry run solves once, in line."""
    from auto import live as lv
    plan, L, sched, q = ctx.plan, ctx.L, ctx.sched, ctx.q
    done_now = frozenset(h for h in plan.prod if L.group_done(h))
    key = (done_now, frozenset(L.researched))
    if key != sched["key"] and not sched["busy"] and not ctx.args.dry:
        sched["key"], sched["busy"] = key, True
        try:      # the flow phase starts from what the builders can take, measured rates, coal left
            stat_ = {(round(x, 2), round(y, 2)): s_ for n_, x, y, s_ in
                     lv.status(q, plan.box(sorted(L.built), 2))} if L.built else {}
            sargs = (dict(ctx.gate.have()), made_rates(plan, L, ctx.snap, stat_), coal_left(q, plan))
        except Exception as e:
            log("schedule inputs:", str(e)[:120])
            sargs = (None, None, None)
        threading.Thread(target=resolve, args=(ctx, set(L.built), set(L.researched)) + sargs, daemon=True).start()
    if ctx.args.dry and sched["s"] is None:
        sched["busy"] = True
        resolve(ctx, set(L.built), set(L.researched))


def sec_per_piece(ctx):
    """The crew's measured pace (game seconds per placed piece), from this run's increments."""
    JL = core.JL
    rows = [json.loads(x) for x in open(JL, encoding="utf-8") if x.strip()] if JL.exists() else []
    r = sorted(x["pieces"] / x["minutes"] for x in rows if x.get("pieces") and (x.get("minutes") or 0) > 0.2)
    return 60 * ctx.args.speed / r[len(r) // 2] if r else 3.5


def resolve(ctx, built, researched, stock0=None, rates=None, coal=None):
    """CP-SAT schedule from what stands now (auto.schedule). stock0/rates/coal (what the builders can take,
    measured made rates, coal under the coal drills) feed its material flow phase."""
    from auto import schedule
    sched = ctx.sched
    try:
        r = schedule.solve(ctx.plan, built, researched, ctx.gd, ctx.dem, sec_per_piece=sec_per_piece(ctx),
                           labs=ctx.labs_n, seconds=60, retired=set(ctx.L.retired), early_mall=EARLY_MALL,
                           stock0=stock0, rates=rates, coal_left=coal)
        sched["s"] = r
        c_ = r.get("coal") or {}
        log(f"schedule {r['status']}: rocket at {(r.get('makespan') or 0) / 3600:.1f} game h; next "
            f"{r['order'][:5]}; research {r['research'][:4]}; hand {len(r['hand'])} groups; "
            f"flow {r.get('flow_status')}" + (f"; coal need {c_.get('need')} left {c_.get('left')}" if c_ else "")
            + (f"; DROPPED {r['conflict']}" if r.get("conflict") else ""))
    except Exception as e:
        log("schedule error:", str(e)[:200])
    finally:
        sched["busy"] = False


def pick(ctx):
    """(group, why) to build this round (choose), with the research it wants and the hand-crafting rule for it."""
    state, ch, gate = ctx.state, ctx.ch, ctx.gate
    s_ = ctx.sched["s"] or {}
    ctx.macros.HANDCRAFT = state["handcraft"] or ctx.grid_dead
    gate.hand = set(s_.get("hand", []))
    g, why = choose(Look(ctx.plan, ctx.L, ch, state, ctx.q, ctx.sched["s"], None if ctx.args.dry else gate,
                         ctx.want, ctx.bad_pairs, dry=ctx.args.dry))
    ch.want_techs = list(s_.get("research", [])) or [t for t, _ in ch.votes.most_common()]
    ctx.rstate["want"] = ch.want_techs + [t for t in ctx.dem["techs"] if t not in ch.want_techs]
    ctx.macros.HANDCRAFT = state["handcraft"] or ctx.grid_dead or (bool(s_) and g in s_.get("hand", []))
    return g, why


def idle(ctx, why):
    """Nothing to build: True when the run is over (a dry run, or the rocket went up), else a wait."""
    log(f"round {ctx.rnd}: nothing to build ({why}); waiting")
    if ctx.args.dry:
        return True
    if launched(ctx.q):               # the silo is the last group: its rocket goes up while nothing is left
        log("ROCKET LAUNCHED")
        return True
    time.sleep(30)
