"""Scouting (auto.scout, auto.budget): radars while the known patches hold less than the run needs, then
outposts on the patches the mine plan picks."""
import json
import math

from auto.growth.core import log
from auto.growth.sensors import starving_fuel
from auto.growth.world import overlap


def scout_step(plan, L, q, state, sched, ch, dry=False):
    """(group, why) for a radar or an outpost, else None. Resource-aware (auto.budget): the whole run's raw budget
    (the phased LP + fuel, calibrated on a finished run) against the patches known so far (generated chunks within
    reach). Short of it: radars, nothing else (outposts wait). Once the known patches cover every resource, the radars
    switch off and a MILP picks the patches to mine (enough amount per resource, drill rate per phase, the least route
    to the base): an outpost goes on the next chosen patch whenever what is left in the mined patches falls short of
    what is left to mine. Crude is a rate: pumpjack outposts on the refineries' deficit (scout.deficits).
    dry: the radars are not switched (only the state says so)."""
    from auto import budget, scout
    charted, rows = scout.scan(q)
    ps = scout.patches(q, rows)
    try:
        crude = scout.crude_rates(q)     # crude is a rate: the refineries' need beyond what the wells give now
    except Exception as e:
        crude = None
        log("scout: crude rates:", str(e)[:120])
    x1, y1, x2, y2 = plan.box(range(len(plan.p)), 0)
    base = ((x1 + x2) / 2, (y1 + y2) / 2)
    if getattr(ch, "budget", None) is None:
        ch.budget = budget.need(ch.dem, budget.load_calib(ch.seed))
        log("budget to the rocket:", ch.budget["total"], f"(fuel {ch.budget['fuel']}, idle {ch.budget['idle']}; "
            f"crude {ch.budget['rate']['crude-oil']:.0f}/min)")
    done = budget.mined_so_far(q)
    bl = budget.left(ch.budget, done)
    kn = budget.known(ps, base, ch.budget["run_hours"])
    short = budget.short(bl, kn)
    used = scout.mined(plan, L, ps)
    line = "; ".join(f"{n} {kn.get(n, 0) / 1000:.0f}k/{v / 1000:.0f}k" for n, v in bl["total"].items()) + \
        f"; crude {kn.get('crude-oil', 0):.0f}/{ch.budget['rate']['crude-oil'] * budget.CRUDE_MARGIN:.0f} per min"
    log(f"scout pass: known/needed {line}; {len(charted)} chunks, {len(ps)} patches; "
        + (f"SHORT {sorted(short)}" if short else "budget met"))
    state["budget"] = {"left": bl["total"], "known": {k_: int(v_) for k_, v_ in kn.items()}, "short": short,
                       "done": done, "charted": len(charted), "patches": len(ps)}
    radars = state.get("radars", [])
    rc = [(math.floor(x / scout.CHUNK), math.floor(y / scout.CHUNK)) for x, y in radars]

    def radar(why, most=6):
        if len(radars) >= most or ch.skip.get("radar", 0) > 0:
            return None
        pend = state.get("radar_site")      # a radar planned and not standing yet (no radar item, not researched):
        if pend:                             # the same site again, not a new one beside it with poles of its own
            return f"radar:{pend[0]},{pend[1]}", f"{why}: the radar site picked before"
        site, k = scout.radar_site(plan, L, ch.m, charted, rc)
        if site and k >= 60:
            state["radar_site"] = list(site)
            L.radar = site
            return f"radar:{site[0]},{site[1]}", f"{why}: a radar there reveals {k} dark chunks"
        return None
    # fuel is local: smelters out of fuel while a big coal patch is charted -> one more coal outpost, into the
    # starving network (the target pick puts dry coal mines first); patches only our own outposts mine still count
    if sum(1 for o in state.get("outposts", []) if o["name"] == "coal") < 6 and ch.skip.get("outpost:coal", 0) <= 0 \
            and starving_fuel(plan, L, q):
        pc = scout.outpost_patch(ps, scout.mined(plan, L, ps, own=False), "coal", 100000, base)
        if pc:
            L.outpost = ("coal", pc, 12)
            return "outpost:coal", (f"smelters out of fuel: a coal outpost on the patch of {pc['amount']} at "
                                    f"({pc['center'][0]:.0f},{pc['center'][1]:.0f}) into the starving network")
    if short:
        # scout before any mine beyond the start patches: radars (up to 12 while short) until the map holds it all;
        # crude alone short (a rate, pumpjack outposts) holds no ore outpost
        if state.get("radars_off"):
            if not dry:
                budget.radars(q, True)
            state["radars_off"] = False
        r_ = radar(f"the known patches hold less than the run needs ({short})", most=12)
        if r_ or set(short) - {"crude-oil"}:
            return r_
    if not short and not state.get("radars_off") and radars:
        log(f"scout: budget met, {'(dry) no' if dry else budget.radars(q, False)} radars switched off (300 kW each)")
        state["radars_off"] = True
    drills = {}
    for k_ in used:
        cs = set(ps[k_]["chunks"])
        drills[k_] = sum(1 for p_ in plan.p if p_["item"] == "electric-mining-drill" and
                         (math.floor(p_["x"] / scout.CHUNK), math.floor(p_["y"] / scout.CHUNK)) in cs)
    failed = [tuple(b_) for b_ in state.get("outpost_failed", ())]      # patches no outpost could reach
    sel = budget.select(ps, bl, base, used, drills_have=drills,
                        skip={k_ for k_, p_ in enumerate(ps) if any(overlap(p_["box"], b_) for b_ in failed)})
    key_ = [[c_["name"], c_["center"], c_["drills"]] for c_ in sel.get("chosen", ())]
    if key_ != state.get("mines_key"):
        state["mines_key"] = key_
        log(f"mine plan ({sel['status']}): " + "; ".join(
            f"{c_['name']} {c_['amount'] // 1000}k at {tuple(c_['center'])} {'mined' if c_['mined'] else 'NEW'} "
            f"{c_['drills']} drills, {c_['dist']} tiles" for c_ in sel.get("chosen", ())))
        try:
            (ch.out / "mines.json").write_text(json.dumps({"base": base, "budget": bl["total"], **sel}))
        except Exception as e:
            log("mines.json:", str(e)[:120])
    d = scout.deficits(None, ch.dem, plan, L, ps, crude=crude)          # crude only (a rate)
    held_ = {}
    for k_ in used:
        held_[ps[k_]["name"]] = held_.get(ps[k_]["name"], 0) + budget.USABLE * ps[k_]["amount"]
    for n, v in bl["total"].items():
        if v > held_.get(n, 0):
            d[n] = int(v - held_.get(n, 0))
    state["scout"] = {"short": d, "charted": len(charted), "patches": len(ps)}
    boxes = [tuple(o["box"]) for o in state.get("outposts", [])]
    for n, sh in sorted(d.items(), key=lambda kv: (kv[0] != "coal", -kv[1])):   # coal first: everything burns it
        if sum(1 for o in state.get("outposts", []) if o["name"] == n) >= 6 or ch.skip.get("outpost:" + n, 0) > 0:
            continue
        if n == "crude-oil":   # a patch with free wells (the base's own patch first if it has some)
            pc = scout.crude_patch(lambda b_: scout.free_wells(q, b_), ps, sh, base)
            if pc is None:
                r_ = radar("crude-oil short of the refineries' rate")
                if r_:
                    return r_
                continue
            L.outpost = (n, pc, sh)
        else:          # the next patch the mine plan chose that nothing mines yet
            nxt = [c_ for c_ in sel.get("chosen", ()) if c_["name"] == n and not c_["mined"]
                   and not any(overlap(c_["box"], b_) for b_ in boxes)]
            if not nxt:
                continue
            pc = ps[nxt[0]["k"]]
            # the MILP's drills keep the run's average pace; once the start patch is dry the outpost carries the
            # whole peak rate (the LP's, 30/min a drill)
            nd = max(nxt[0]["drills"], math.ceil(ch.budget["rate"].get(n, 0) / budget.DRILL_PER_MIN))
            L.outpost = (n, pc, max(4, min(12, nd)))
        return "outpost:" + n, (f"{n}: {sh} more to mine than the mined patches hold; the mine plan's patch of "
                                f"{pc['amount']} at ({pc['center'][0]:.0f},{pc['center'][1]:.0f})")
    return None
