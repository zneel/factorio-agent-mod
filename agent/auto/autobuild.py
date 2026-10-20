"""Staged builder: characters build a seed's plan from nothing, stage by stage, with research driven alongside.

    python -m auto.autobuild <seed> [--scratch] [--chars 3] [--speed 10] [--stages 0-5] [--boot-minutes 20]

Needs plan.json + stages.json (auto.plan2, auto.stage) and the burner plans (auto.boot). Progress is kept in
runtime/auto/<seed>/build-state.json (re-running continues), the log in build.log.

1. boot: every character runs bootstrap.py on its share of the burner plans (drills + furnaces/chests), then one
   keeps tending them (fuel, emptying) for the rest of the run.
2. stages: in stages.json order; a stage's placements are split among the builders by position, each runs
   supply.build_plan (fetch / craft / place) until its part stands. Between attempts the research driver queues
   the technologies the next stages need.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
sys.path.insert(0, str(AGENT))

lock = threading.Lock()
LOG = None


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    with lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def pick_depot(plan, boot, center=(0, 0), m=None):
    """A free 4x1 strip near center (the spawn, or where the building happens) for the depot chests: nothing
    planned within 2 tiles, dry land when the map is given."""
    from auto import geom
    blocked = geom.tiles(plan["complex"] + plan["world"] + boot)
    cx, cy = int(center[0]), int(center[1])
    for r in range(3, 120):
        for x in range(-r, r + 1):
            for y in (-r, r):
                strip = {(cx + x + i, cy + y) for i in range(4)}
                ring = {(a + dx, b + dy) for a, b in strip for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)}
                if not ring & blocked and (m is None or all(m.t(*t) == "." for t in ring)):
                    return cx + x + .5, cy + y + .5
    raise SystemExit("no room for the depot")


OBSTACLES_LUA = r"""
local S=game.surfaces[1] local o={} local seen={}
for _,a in pairs(helpers.json_to_table('%s')) do
  for _,e in pairs(S.find_entities_filtered{area={{a[1]+0.05,a[2]+0.05},{a[3]-0.05,a[4]-0.05}}}) do
    if (e.type=='tree' or e.type=='simple-entity' or e.name:find('crash%%-site')) and not seen[e.unit_number or (e.position.x..','..e.position.y)] then seen[e.unit_number or (e.position.x..','..e.position.y)]=1
      o[#o+1]={e.position.x,e.position.y} end end end
rcon.print(helpers.table_to_json(o))
"""


def clear(b, places, margin=0):
    """Mine every tree, rock and wreck overlapping the placements' footprints (rotation aware), grown by margin
    tiles, before building: the game reports some of them only as an "invalid spot", and a margin keeps a walkway
    so builders don't box themselves in between trees and their own pieces."""
    from auto import geom
    boxes = []
    for p in places:
        ts = geom.footprint(p)
        xs, ys = [t[0] for t in ts], [t[1] for t in ts]
        boxes.append([min(xs) - margin, min(ys) - margin, max(xs) + 1 + margin, max(ys) + 1 + margin])
    found = []
    for i in range(0, len(boxes), 300):
        r = b.raw("/silent-command " + (OBSTACLES_LUA % json.dumps(boxes[i:i + 300])).replace(chr(10), " "))
        found += json.loads(r or "[]") or []
    tour, left = [], sorted(set(map(tuple, found)))
    pos = b.call("state")["agent"]["position"] if left else None
    cur = (pos["x"], pos["y"]) if pos else (0, 0)
    while left:   # nearest next: one walk through the site, not back and forth
        k = min(range(len(left)), key=lambda i: (left[i][0] - cur[0]) ** 2 + (left[i][1] - cur[1]) ** 2)
        cur = left.pop(k)
        tour.append(cur)
    for x, y in tour:
        try:
            b.run("mine", x=x, y=y, timeout=120)
        except Exception:
            pass
    return len(found)


def split(places, n):
    """Cut a stage into n spatial chunks (sorted along its longer axis) so builders don't walk over each other."""
    if n <= 1 or len(places) < 2 * n:
        return [places]
    xs = [p["x"] for p in places]
    ys = [p["y"] for p in places]
    axis = "x" if max(xs) - min(xs) >= max(ys) - min(ys) else "y"
    s = sorted(places, key=lambda p: p[axis])
    k = math.ceil(len(s) / n)
    return [s[i:i + k] for i in range(0, len(s), k)]


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--scratch", action="store_true")
    ap.add_argument("--chars", type=int, default=3)
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--stages", default=None, help="a-b: only these stage numbers")
    ap.add_argument("--boot-minutes", type=float, default=20)
    a = ap.parse_args()
    from auto import mapdata
    out = mapdata.out_dir(a.seed)
    LOG = out / "build.log"
    plan = json.loads((out / "plan.json").read_text())
    stages = json.loads((out / "stages.json").read_text())
    boot_dir = out / "boot"
    boot_files = [boot_dir / f"boot-{k}.json" for k in ("iron", "copper", "coal", "stone")]
    boot = [e for f in boot_files for e in json.loads(f.read_text())["place"]]
    if a.scratch:
        os.environ["FA_TARGET"] = "scratch"
    os.environ["FA_BOOT_DIR"] = str(boot_dir)
    dx, dy = pick_depot(plan, boot)
    os.environ["FA_DEPOT"] = f"{dx},{dy}"
    from bridge import Bridge  # noqa: E402  (after the env: the bridge reads the target from it)
    import supply  # noqa: E402
    from auto import research  # noqa: E402
    sfile = out / "build-state.json"
    state = json.loads(sfile.read_text()) if sfile.exists() else {"boot": False, "done": []}
    if state["done"] and isinstance(state["done"][0], int):   # old state: stage numbers -> group names
        state["done"] = [r["group"] for r in stages if r["stage"] in state["done"]]

    def save():
        sfile.write_text(json.dumps(state))
    chars = [f"b{i + 1}" for i in range(a.chars)]
    ctl = Bridge(chars[0], autostart=False)

    def q(lua):
        return ctl.raw(lua)
    ctl.call("set_mode", mode="realtime")
    q(f"/silent-command game.speed={a.speed} game.tick_paused=false rcon.print(1)")
    log(f"seed {a.seed} on {'scratch' if a.scratch else 'live'}; chars {chars}; depot {dx},{dy}")
    env = dict(os.environ)
    py = sys.executable

    # ---- 1. boot
    if not state["boot"]:
        plans = ",".join(str(f) for f in boot_files)
        procs = [subprocess.Popen([py, str(AGENT / "bootstrap.py"), c, plans, "--share", f"{i}/{len(chars)}",
                                   "--minutes", str(a.boot_minutes)], env=env, cwd=str(AGENT),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for i, c in enumerate(chars)]
        outs = []
        for c, p in zip(chars, procs):
            o = p.communicate()[0]
            outs.append(o)
            log(f"boot {c}: {o.strip().splitlines()[-1] if o.strip() else '(no output)'}")
        state["boot"] = all("DONE" in o for o in outs)
        save()
    # the tender also fuels the steam plant's boilers by hand until the coal belt reaches them
    tend_files = [str(f) for f in boot_files] + [str(AGENT.parent / r["file"]) for r in stages if r["group"] == "power"]
    tender = subprocess.Popen([py, str(AGENT / "bootstrap.py"), chars[-1], ",".join(tend_files),
                               "--tend", "--minutes", "100000"], env=env, cwd=str(AGENT),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    builders = chars[:-1] if len(chars) > 1 else chars
    done = {r["stage"] for r in stages if r["group"] in state["done"]}
    hut = research.hut_site(plan, boot)
    log("research hut lab at", hut)
    stop = threading.Event()
    rthread = threading.Thread(target=research.serve, daemon=True,
                               args=(Bridge("r1", autostart=False), q, stages, done, hut, log, stop))
    rthread.start()

    # ---- 2. stages
    lo, hi = (int(v) for v in a.stages.split("-")) if a.stages else (0, len(stages) - 1)
    try:
        for row in stages:
            k = row["stage"]
            if k in done or not lo <= k <= hi:
                continue
            places = json.loads((AGENT.parent / row["file"]).read_text())["place"]
            log(f"stage {k} {row['group']}: {len(places)} entities, techs {len(row['techs'])}")
            t0 = time.time()
            while True:
                missing = [t for t in row["techs"] if t not in research.researched(q, row["techs"])]
                chunks = split(places, len(builders))
                results = [None] * len(chunks)

                def work(i, c, chunk):
                    try:
                        bb = Bridge(c, autostart=False)
                        clear(bb, chunk)
                        results[i] = supply.build_plan(bb, chunk, minutes=10)
                    except Exception as e:   # one builder's error must not stop the stage
                        results[i] = {"done": False, "error": str(e)[:200]}
                ts = [threading.Thread(target=work, args=(i, builders[i % len(builders)], ch))
                      for i, ch in enumerate(chunks)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                if all(r_ and r_.get("done") for r_ in results):
                    done.add(k)
                    state["done"] = sorted({r_["group"] for r_ in stages if r_["stage"] in done})
                    save()
                    log(f"stage {k} done in {(time.time() - t0) / 60:.1f} min")
                    break
                short = [r_.get("short") or r_.get("error") or (r_.get("result") or {}).get("failed")
                         for r_ in results if r_ and not r_.get("done")]
                log(f"stage {k} not done; missing techs {missing}; {str(short)[:300]}")
                time.sleep(20)
    finally:
        stop.set()
        tender.terminate()


if __name__ == "__main__":
    main()
