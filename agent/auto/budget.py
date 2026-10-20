"""Whole-run resource budget, and which patches cover it.

    b = budget.need(dem)                       {"total": {res: amount to the rocket}, "rate": {res: per min}, ...}
    k = budget.known(ps, base)                 {res: amount (crude: crude/min)} the known patches in reach hold
    s = budget.short(b, k)                     {res: still missing}: scout until empty
    sel = budget.select(ps, b, base, mined)    the patches to mine (scipy.optimize.milp, HiGHS)

    python -m auto.budget --seed N [--demand demand.json] [--calibrate]

The budget comes from the phased LP (auto.demand: plan_lp.solve_phases): per phase, the raw each recipe eats at the
LP's crafts/min times the phase's hours, plus fuel: coal for the stone furnaces (90 kW while they craft) and for the
boilers (every electric machine's MJ: assemblers, chem plants, refineries, the silo at their LP duty, the drills per
ore mined, the pumpjacks per crude, the labs per lab-second of research), 4 MJ per coal. Totals hardly depend on the
phase hours: a craft costs the same ore and MJ whenever it runs.
A real run uses more than the LP's goal (belts and chests full of items, rebuilds, remines, the bootstrap's burners),
and it burns coal while idle (radars, machines' drain): coal = CALIB x LP + idle coal/min x the run's game hours.
CALIB per ore, the idle burn and the run's hours are measured on a finished run (--calibrate: the live production
statistics, 50-min samples cut at the launch, kept in runtime/auto/<seed>/budget-calib.json); MARGIN on top.
Crude is a rate, not a total: a well never runs dry, it declines to 20% (auto.demand.WELL_FLOOR). Its budget is the
LP's peak crude/min against what the known wells still give at the end of the run.
"""
import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from auto import demand as demand_mod  # noqa: E402
from auto.mapdata import out_dir  # noqa: E402

ORES = ("coal", "iron-ore", "copper-ore", "stone")
COAL_KJ = 4000
FURNACE_KW = 90                     # stone furnace, speed 1
DRILL_KW, DRILL_PER_MIN = 90, 30    # electric mining drill
PUMPJACK_KW, LAB_KW = 90, 60
CAT_KW = {"crafting": 150, "advanced-crafting": 150, "crafting-with-fluid": 150, "chemistry": 210,
          "oil-processing": 420, "rocket-building": 250}
MARGIN = 1.2
# seed 1060's rocket run (scratch, 2026-10-20, from zero; 1.3 -> 63.8 game h, a 9 h fuel stall included), production
# statistics cut at the launch: coal 382k mined, iron ore 631k, copper ore 444k, stone 48k; idle 23.9 coal/min
# (after the launch: 2 radars + drain). calibrate() against demand.json (the plan that run built)
CALIB = {"calib": {"coal": 2.22, "iron-ore": 2.0, "copper-ore": 2.12, "stone": 2.14}, "idle": 23.9, "hours": 62.5}
SAMPLE_MIN = 50                     # the 250-hour statistics: 300 samples of 50 game minutes
DRILL_TILES = 15                    # patch tiles per drill a column layout gets (one-sided columns, 3 tiles a drill)
USABLE = 0.85                       # of a patch's amount the drills reach (edges, a column's 5x5 areas)
CRUDE_MARGIN = 1.1
REACH = 900                         # Manhattan tiles from the base a belt route still pays (seed 1060: coal outposts at ~700 ran)


def _phases(dem):
    """The LP's phases; an old single-phase demand file is one phase of dem["hours"]."""
    if dem.get("phases"):
        return dem["phases"]
    return [{"name": "run", "hours": dem["hours"], "crafts_per_min": dem["crafts_per_min"],
             "machines": {r: dem["machines"][r] for r in dem["crafts_per_min"]},
             "crude_per_pumpjack": demand_mod.well_yield(None, dem["hours"])}]


def phase_rates(dem):
    """Per phase: {"name", "hours", "raw": {res: per min eaten by recipes}, "fuel": coal/min burned (furnaces +
    boilers), "kw": electric kW, "furnace_kw": burner kW}."""
    rec = dem["recipes"]
    out = []
    for ph in _phases(dem):
        raw, kw, fkw = {}, 0.0, 0.0
        for r, x in ph["crafts_per_min"].items():
            R = rec[r]
            for it, n in R["ing"]:
                if it in ORES or it == "crude-oil":
                    raw[it] = raw.get(it, 0) + n * x
            for it, n in R["prod"]:
                if it in ORES or it == "crude-oil":
                    raw[it] = raw.get(it, 0) - n * x
            busy = x * R["energy"] / (60 * demand_mod.SPEED[R["cat"]])        # machines at work
            if R["cat"] == "smelting":
                fkw += busy * FURNACE_KW
            else:
                kw += busy * CAT_KW.get(R["cat"], 150)
        raw = {k: v for k, v in raw.items() if v > 1e-9}
        kw += sum(v for k, v in raw.items() if k in ORES) / DRILL_PER_MIN * DRILL_KW
        if raw.get("crude-oil"):
            kw += raw["crude-oil"] / ph["crude_per_pumpjack"] * PUMPJACK_KW
        out.append({"name": ph["name"], "hours": ph["hours"], "raw": raw, "kw": kw, "furnace_kw": fkw})
    # labs: every lab-second of research (the research phase, or the whole run)
    lab_kj = dem.get("research_labs", 0) * out[0]["hours"] * 3600 * LAB_KW
    out[0]["kw"] += lab_kj / (out[0]["hours"] * 3600)
    for p in out:
        p["fuel"] = (p["kw"] + p["furnace_kw"]) * 60 / COAL_KJ
    return out


def need(dem, calib=None, margin=MARGIN):
    """{"total": {ore: amount to the rocket}, "rate": {res: peak per min (crude: crude/min)}, "phases": per phase
    {res: amount}, "fuel": coal the LP's work burns, "lp": the totals before calibration and margin}.
    calib: {"calib": {ore: measured / LP}, "idle": coal/min, "hours": game hours of a run} ({}: the bare LP)."""
    calib = CALIB if calib is None else calib
    ph = phase_rates(dem)
    lp, per = {}, []
    for p in ph:
        m = 60 * p["hours"]
        t = {k: v * m for k, v in p["raw"].items() if k in ORES}
        t["coal"] = t.get("coal", 0) + p["fuel"] * m
        per.append(t)
        for k, v in t.items():
            lp[k] = lp.get(k, 0) + v
    fuel = sum(p["fuel"] * 60 * p["hours"] for p in ph)
    f = {k: (calib.get("calib") or {}).get(k, 1.0) for k in ORES}
    hrs = sum(p["hours"] for p in ph)
    idle = calib.get("idle", 0) * 60 * calib.get("hours", hrs)       # coal burned idle over a real run's hours
    phases = []
    for p, t in zip(ph, per):
        a = {k: v * f[k] for k, v in t.items()}
        a["coal"] = a.get("coal", 0) + idle * p["hours"] / hrs
        phases.append({"name": p["name"], "hours": p["hours"], **{k: int(v * margin) for k, v in a.items()}})
    rate = {k: max(p["raw"].get(k, 0) + (p["fuel"] if k == "coal" else 0) for p in ph) for k in ORES}
    rate["crude-oil"] = max(p["raw"].get("crude-oil", 0) for p in ph)
    return {"total": {k: sum(p.get(k, 0) for p in phases) for k in ORES}, "rate": rate, "phases": phases,
            "fuel": int(fuel), "idle": int(idle), "lp": {k: int(v) for k, v in lp.items()}, "hours": hrs,
            "run_hours": calib.get("hours", hrs)}


MINED_LUA = ("/silent-command local s=game.forces.player.get_item_production_statistics(1) local o={} "
             "for _,n in pairs({'coal','iron-ore','copper-ore','stone'}) do o[n]=s.get_input_count(n) end "
             "rcon.print(helpers.table_to_json(o))")
# radars burn 300 kW each: on while the map is short of the budget, off once it is met
RADARS_LUA = ("/silent-command local n=0 for _,r in pairs(game.surfaces[1].find_entities_filtered{type='radar',"
              "force='player'}) do if r.active~=%s then r.active=%s n=n+1 end end rcon.print(n)")


def mined_so_far(q):
    """{ore: mined since the map started} (the force's production statistics)."""
    return json.loads(q(MINED_LUA) or "{}")


def radars(q, on):
    """Switch every radar on or off; returns how many changed."""
    v = "true" if on else "false"
    return int(q(RADARS_LUA % (v, v)) or 0)


def well_rate(p, hours):
    """crude/min a crude patch's wells (p["tiles"] wells, p["amount"] in all) give after `hours` of pumping."""
    n = max(1, p["tiles"])
    return n * demand_mod.well_yield([p["amount"] / n], hours)


def dist(p, base):
    return abs(p["center"][0] - base[0]) + abs(p["center"][1] - base[1])


def known(ps, base, hours=8.0, reach=REACH):
    """{res: what the known patches within reach hold}: ores the amount the drills reach, crude its crude/min at the
    end of the run."""
    out = {}
    for p in ps:
        if dist(p, base) > reach:
            continue
        if p["name"] == "crude-oil":
            out["crude-oil"] = out.get("crude-oil", 0) + well_rate(p, hours)
        elif p["name"] in ORES:
            out[p["name"]] = out.get(p["name"], 0) + USABLE * p["amount"]
    return out


def left(b, done):
    """The budget still to mine once `done` ({ore: mined so far}, the production statistics) is out of the ground:
    totals and phase totals cut by what is done, oldest phase first."""
    out = dict(b, total={}, phases=[dict(p) for p in b["phases"]])
    for n, v in b["total"].items():
        d = done.get(n, 0)
        out["total"][n] = max(0, v - d)
        for p in out["phases"]:
            u = min(p.get(n, 0), d)
            p[n] = p.get(n, 0) - u
            d -= u
    return out


def short(b, k):
    """{res: missing}: ore totals (amount) and crude (crude/min) the known patches do not cover yet (b: need() or
    left())."""
    out = {n: int(v - k.get(n, 0)) for n, v in b["total"].items() if v > k.get(n, 0)}
    c = b["rate"].get("crude-oil", 0) * CRUDE_MARGIN
    if c > k.get("crude-oil", 0):
        out["crude-oil"] = int(c - k.get("crude-oil", 0))
    return out


def select(ps, b, base, mined=(), reach=REACH, drills_have=None, skip=()):
    """The patches to mine: a MILP (scipy.optimize.milp, HiGHS).
    y_k in {0,1} mine patch k (the patches the plan mines already: fixed 1), z_kp >= 0 ore taken from k in phase p,
    d_k >= 0 drills on k.
      amount   sum_p z_kp <= USABLE amount_k y_k
      need     sum_k z_kp >= the budget's phase total of k's resource
      rate     z_kp <= d_k * 30/min * 60 hours_p           d_k <= tiles_k / DRILL_TILES y_k (or the drills there)
    hours_p: the phase's share of a real run (b["run_hours"], the calibration's measured run), not the LP's hours: a
    run takes that long, so that is the pace the drills keep up with.
    min  sum_k route_k y_k (Manhattan tiles to the base; mined: 0) + 1e-3 sum d_k
    skip: patches left out (an outpost there could not be planned).
    Returns {"chosen": [{"k", "name", "amount", "center", "drills", "dist", "take": {phase: amount}}],
    "status"} or {"status": "infeasible", "short": {...}}. Crude is left to the pumpjack outposts (a rate)."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    mined = set(mined)
    pace = b.get("run_hours", 1) / b["hours"] if b.get("hours") else 1
    cand = [k for k, p in enumerate(ps) if p["name"] in ORES and (k in mined or (dist(p, base) <= reach and
                                                                                  k not in skip))]
    P = len(b["phases"])
    K = len(cand)
    if not K:
        return {"status": "infeasible", "short": dict(b["total"])}
    Y = lambda i: i                                  # noqa: E731
    D = lambda i: K + i                              # noqa: E731
    Z = lambda i, p: 2 * K + i * P + p               # noqa: E731
    nv = 2 * K + K * P
    c = np.zeros(nv)
    lo, hi = np.zeros(nv), np.full(nv, np.inf)
    integ = np.zeros(nv)
    for i, k in enumerate(cand):
        p = ps[k]
        c[Y(i)] = 0 if k in mined else dist(p, base) + 50     # + a fixed cost per outpost (poles, the route's ends)
        c[D(i)] = 1e-3
        integ[Y(i)] = 1
        hi[Y(i)] = 1
        if k in mined:
            lo[Y(i)] = 1
    rows, rlo, rhi = [], [], []

    def row(coefs, a, b_):
        r = np.zeros(nv)
        for j, v in coefs:
            r[j] += v
        rows.append(r)
        rlo.append(a)
        rhi.append(b_)
    for i, k in enumerate(cand):
        p = ps[k]
        row([(Z(i, q), 1) for q in range(P)] + [(Y(i), -USABLE * p["amount"])], -np.inf, 0)
        cap = max(1.0, p["tiles"] / DRILL_TILES)
        if drills_have and k in drills_have:
            cap = max(cap, drills_have[k])
        row([(D(i), 1), (Y(i), -cap)], -np.inf, 0)
        for q, ph in enumerate(b["phases"]):
            row([(Z(i, q), 1), (D(i), -DRILL_PER_MIN * 60 * ph["hours"] * pace)], -np.inf, 0)
    for q, ph in enumerate(b["phases"]):
        for n in ORES:
            idx = [i for i, k in enumerate(cand) if ps[k]["name"] == n]
            if ph.get(n, 0) > 0:
                row([(Z(i, q), 1) for i in idx], ph[n], np.inf)
    res = milp(c, constraints=LinearConstraint(np.array(rows), rlo, rhi), integrality=integ, bounds=Bounds(lo, hi),
               options={"time_limit": 30})
    if res.x is None:
        have = {}
        for k in cand:
            have[ps[k]["name"]] = have.get(ps[k]["name"], 0) + USABLE * ps[k]["amount"]
        return {"status": "infeasible", "short": {n: int(v - have.get(n, 0)) for n, v in b["total"].items()
                                                  if v > have.get(n, 0)}}
    v = res.x
    chosen = []
    for i, k in enumerate(cand):
        if v[Y(i)] > 0.5:
            p = ps[k]
            chosen.append({"k": k, "name": p["name"], "amount": p["amount"], "center": [round(t) for t in p["center"]],
                           "box": list(p["box"]), "mined": k in mined, "dist": round(dist(p, base)),
                           "drills": math.ceil(v[D(i)] - 1e-6),
                           "take": {ph["name"]: int(v[Z(i, q)]) for q, ph in enumerate(b["phases"])}})
    chosen.sort(key=lambda s: (s["name"], not s["mined"], s["dist"]))
    return {"status": "optimal" if res.status == 0 else res.message, "chosen": chosen,
            "route": int(sum(s["dist"] for s in chosen if not s["mined"]))}


STATS_LUA = ("/silent-command local s=game.forces.player.get_item_production_statistics(1) local o={tick=game.tick} "
             "for _,n in pairs({'coal','iron-ore','copper-ore','stone'}) do for _,c in pairs({'input','output'}) do "
             "local a={} for i=1,300 do a[i]=math.floor(s.get_flow_count{name=n,category=c,sample_index=i,count=true,"
             "precision_index=defines.flow_precision_index.two_hundred_fifty_hours}) end o[n..':'..c]=a end end "
             "rcon.print(helpers.table_to_json(o))")


def calibrate(series, dem, launch=None):
    """Calibration from a finished run's 250-hour production statistics (STATS_LUA: {"<ore>:input"|":output": 300
    samples of 50 min, newest first}). The run: from the oldest sample with coal mined to `launch` (a sample index;
    default the newest sample with iron ore mined: the factory stops eating ore at the launch). The idle burn: the
    median coal used per sample after the launch. Returns {"calib", "idle", "hours", "measured", "lp"}."""
    first = max(i for i, v in enumerate(series["coal:input"]) if v)
    if launch is None:
        launch = min(i for i, v in enumerate(series["iron-ore:input"]) if v)
    got = {n: sum(series[n + ":input"][launch:first + 1]) for n in ORES}
    after = sorted(series["coal:output"][:launch])
    idle = after[len(after) // 2] / SAMPLE_MIN if after else CALIB["idle"]
    hours = (first + 1 - launch) * SAMPLE_MIN / 60
    lp = need(dem, calib={}, margin=1.0)["lp"]
    cal = {n: round(got[n] / lp[n], 3) for n in ORES if lp.get(n) and got.get(n)}
    if "coal" in cal:
        cal["coal"] = round((got["coal"] - idle * 60 * hours) / lp["coal"], 3)
    return {"calib": cal, "idle": round(idle, 2), "hours": round(hours, 1), "measured": got, "lp": lp}


def load_calib(seed):
    f = out_dir(seed) / "budget-calib.json"
    return json.loads(f.read_text()) if f.exists() else CALIB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--demand", default="demand.json")
    ap.add_argument("--calibrate", action="store_true", help="measure a finished run (the scratch server)")
    a = ap.parse_args()
    dem = json.loads((out_dir(a.seed) / a.demand).read_text())
    if a.calibrate:
        import fa_scratch  # noqa: F401  (points the bridge at the scratch server)
        from bridge import Bridge
        c = calibrate(json.loads(Bridge("budget", autostart=False).raw(STATS_LUA)), dem)
        (out_dir(a.seed) / "budget-calib.json").write_text(json.dumps(c, indent=1))
        print("calibration:", c)
    b = need(dem, load_calib(a.seed))
    print("LP (no calib):", b["lp"], "fuel", b["fuel"], "| idle coal", b["idle"])
    print("budget:", b["total"])
    print("peak rate/min:", {k: round(v) for k, v in b["rate"].items()})
    for p in phase_rates(dem):
        print(f"  {p['name']}: {p['hours']} h, {p['kw'] / 1000:.1f} MW electric, {p['furnace_kw'] / 1000:.1f} MW "
              f"furnaces, fuel {p['fuel']:.0f} coal/min")


if __name__ == "__main__":
    main()
