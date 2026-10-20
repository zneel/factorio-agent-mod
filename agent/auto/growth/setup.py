"""The controller's start: arguments, the plan and the live game into a Context, the threads that keep the game
speed and the mall limits, the bootstrap, the burner tender and the research runner."""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

from auto.growth import core
from auto.growth.chooser import Chooser
from auto.growth.context import Context
from auto.growth.core import AGENT, Live, Plan, log
from auto.growth.gate import Gate
from auto.growth.upkeep import research_loop, start_tender
from auto.growth.world import hut_spot


def args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--scratch", action="store_true")
    ap.add_argument("--chars", type=int, default=4)
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--rounds", type=int, default=100000)
    ap.add_argument("--dry", action="store_true", help="choose and plan increments, build nothing")
    ap.add_argument("--boot-minutes", type=float, default=20)
    return ap.parse_args(argv)


def context(a):
    """Everything main() needs, read in the order the old main() read it (the plan, the live game, the state)."""
    if a.scratch:
        os.environ["FA_TARGET"] = "scratch"
    from auto import gamedata, mapdata
    out = mapdata.out_dir(a.seed)
    core.set_log(out)
    gd = gamedata.load()
    core.GD = gd
    dem = json.loads((out / "demand.json").read_text())
    m = mapdata.Map.load(a.seed)
    from auto import scout
    scout.load(m, out)              # scouted ground beyond the seed's map (radars, auto.scout)
    log(f"=== grow seed {a.seed} on {'scratch' if a.scratch else 'live'}")
    plan = Plan(a.seed, gd, dem)
    log(f"plan: {len(plan.p)} pieces, {len(plan.groups)} groups ({len(plan.prod)} producing)")
    boot_dir = out / "boot"
    boot_files = [boot_dir / f"boot-{k}.json" for k in ("iron", "copper", "coal", "stone")]
    boot = [e for f in boot_files for e in json.loads(f.read_text())["place"]]
    os.environ["FA_BOOT_DIR"] = str(boot_dir)
    from auto.autobuild import pick_depot, clear, split
    dx, dy = pick_depot({"complex": plan.p, "world": []}, boot)
    # once the mall runs, the builders work and fetch around it: their depot moves next to the mall chests
    mall_ch = [p for p in plan.p if p["item"] == "iron-chest" and p.get("mall")]
    cen = (sum(p["x"] for p in mall_ch) / len(mall_ch), sum(p["y"] for p in mall_ch) / len(mall_ch)) if mall_ch else (0, 0)
    mall_depot = pick_depot({"complex": plan.p, "world": []}, boot, cen, m)
    os.environ["FA_DEPOT"] = f"{dx},{dy}"
    from bridge import Bridge   # noqa: E402  (reads FA_TARGET)
    import macros
    import supply
    from auto import control, live as lv
    chars = [f"b{i + 1}" for i in range(a.chars)]
    ctl = Bridge(chars[0], autostart=False)
    q = ctl.raw
    L = Live(q, plan)
    sfile = out / "grow-state.json"
    state = json.loads(sfile.read_text()) if sfile.exists() else {"boot": False, "handcraft": True, "n": 0}
    macros.HANDCRAFT = state["handcraft"]
    ch = Chooser(plan, L, dem, state["handcraft"])
    ch.m = m
    ch.seed, ch.out, ch.budget = a.seed, out, None
    ctx = Context(args=a, out=out, gd=gd, dem=dem, m=m, plan=plan, L=L, state=state, ch=ch, q=q, Bridge=Bridge,
                  supply=supply, macros=macros, clear=clear, split=split, chars=chars, boot_dir=boot_dir,
                  boot_files=boot_files, boot=boot, mall_depot=mall_depot)
    if not a.dry:
        ctl.call("set_mode", mode="realtime")
        q(f"/silent-command game.speed={a.speed} game.tick_paused=false rcon.print(1)")
        threading.Thread(target=keep_speed, args=(Bridge, a.speed), daemon=True).start()
        threading.Thread(target=keep_limits, args=(Bridge, ctx.limits), daemon=True).start()
    L.retired = set(state.get("retired", []))
    L.own = state.get("extra_places", [])
    L.dead = set(state.get("dead", []))
    n = L.refresh()
    L.researched = lv.researched(q)
    log(f"live: {n} entities, {len(L.built)} plan pieces built, {len(L.researched)} techs researched")
    # groups mostly standing were attempted (an earlier run): a hole in them is repaired like any failed build
    state.setdefault("tried", [])
    for g in plan.prod:
        mach = [i for i in plan.groups[g] if not plan.flow.transport(i)]
        nb = sum(1 for i in mach if i in L.built)
        if mach and nb >= 0.5 * len(mach) and g not in state["tried"]:
            state["tried"].append(g)
    ctx.env, ctx.py = dict(os.environ), sys.executable
    if not a.dry:
        bootstrap(ctx)
    ctx.builders = chars[:-1] if len(chars) > 1 and ctx.tender else chars
    ctx.gate = Gate(plan, L, ch, q, ctx.builders, Bridge)      # every build path asks it first: build, alt or skip
    ctx.gate.want = ctx.want
    ch.have = ctx.gate.have
    ctx.hut = hut_spot(plan, boot, m)
    log("hut lab (bootstrap research) at", ctx.hut)
    ctx.stop = threading.Event()
    if not a.dry:
        threading.Thread(target=research_loop, daemon=True,
                         args=(Bridge("r1", autostart=False), q, ctx.rstate, ctx.hut, state, ctx.stop,
                               ctx.gate)).start()
    ctx.targets = control.lp_targets(dem)
    ctx.track = sorted(set(ctx.targets) | {"iron-plate", "copper-plate", "coal", "stone", "iron-ore", "copper-ore"})
    ctx.labs_n = sum(1 for i in plan.groups.get("labs", []) if plan.p[i]["item"] == "lab") or 1
    return ctx


def bootstrap(ctx):
    """The burner bootstrap (once per seed: every character takes its share), then the tender that hand-feeds the
    boilers on a character of its own while hand-crafting lasts; after it the coal belt does (power links first)
    and one builder does all the work: no two characters race for one chest."""
    state, chars, a = ctx.state, ctx.chars, ctx.args
    if not state["boot"]:
        plans = ",".join(str(f) for f in ctx.boot_files)
        procs = [subprocess.Popen([ctx.py, str(AGENT / "bootstrap.py"), c, plans, "--share", f"{i}/{len(chars)}",
                                   "--minutes", str(a.boot_minutes)], env=ctx.env, cwd=str(AGENT),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for i, c in enumerate(chars)]
        outs = [p.communicate()[0] for p in procs]
        for c, o in zip(chars, outs):
            log(f"boot {c}: {o.strip().splitlines()[-1] if o.strip() else '(no output)'}")
        state["boot"] = True   # partial shares are fine: a burner spot can be blocked (wreck, character)
        ctx.save()
    plan = ctx.plan
    power_file = ctx.out / "grow-power.json"
    power_file.write_text(json.dumps({"place": [plan.p[i] for i in plan.groups.get("power", [])] +
                                      [plan.p[i] for g in plan.groups if g.startswith("power:") for i in plan.groups[g]]}))
    ctx.tender = start_tender(ctx.py, chars[-1], ctx.boot_dir, power_file, ctx.env) \
        if state["handcraft"] and len(chars) > 1 else None


def keep_speed(Bridge, speed):
    """From the start, bootstrap included: a player joining resets the speed to 1."""
    sb = Bridge("r2", autostart=False)
    while True:
        try:
            r0 = sb.raw(f"/silent-command if game.speed ~= {speed} or game.tick_paused then "
                        f"game.speed={speed} game.tick_paused=false rcon.print('reset') end")
            if r0 and "reset" in r0:
                log(f"game speed was off (a player joined?): set back to {speed}x")
        except Exception:
            pass
        time.sleep(20)


def keep_limits(Bridge, limits):
    """The round's mall caps sent again every 10 s: a whitelist set once a round (at 20x a round can be an hour of
    game time) lets one item refill the bar."""
    sb = Bridge("r3", autostart=False)
    while True:
        time.sleep(10)
        try:
            if limits["cmd"]:
                sb.raw(limits["cmd"])
            if limits["makers"]:
                sb.raw(limits["makers"])
        except Exception:
            pass
