"""Always-on crew helpers around the job board.

    python crew.py next <character> [--wait 100]    worker: block until a job is claimed for <character>
    python crew.py watch [--wait 100]               planner: block until the board needs attention
    python crew.py status                           planner: flow, losses, crew and board in one read

`next` claims the next job for the character and prints it. While the board has nothing for it, the
character does standing work instead of standing still (lean: no idle crew):
  - keeps a kanban of building parts in its inventory by hand-crafting in the background;
  - tops up burner fuel around it every couple of minutes;
  - when the whole board is empty, posts an andon job for the biggest capacity loss in the base
    (blocked / starved / down machines), so the line never waits for the planner.
After --wait seconds without a job it prints "no job yet" (shell calls time out after ~2 minutes).

`watch` returns as soon as a job finishes or gets blocked, or fewer than 2 jobs are open, and prints
the board plus the flow line, so a planner only wakes up when there is something to decide.
"""

import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge, BridgeError  # noqa: E402
import macros  # noqa: E402
import server  # noqa: E402
from mcp_server import fmt_claim, fmt_jobs, fmt_team  # noqa: E402

REFUEL_EVERY = 120   # wall seconds between refuel rounds per character
ANDON_REPEAT = 1200  # wall seconds before the same loss may be posted again
# kanban: keep at least this many of each part on the character, crafted while it waits
KANBAN = {"transport-belt": 100, "inserter": 20, "small-electric-pole": 20, "pipe": 30,
          "iron-gear-wheel": 40, "electronic-circuit": 20, "underground-belt": 10}
CRAFT_BATCH = 40
MACHINES = ("mining-drill", "furnace", "assembling-machine", "lab", "boiler", "generator", "offshore-pump",
            "oil-refinery", "chemical-plant", "rocket-silo")


def _load(name):
    try:
        return json.loads((server.RUNTIME / name).read_text())
    except (OSError, ValueError):
        return {}


def _save(name, st):
    (server.RUNTIME / name).write_text(json.dumps(st))


# ---- base measurement (read-only, from snapshot)

def losses(b):
    s = b.call("snapshot")
    count, groups = collections.Counter(), collections.defaultdict(list)
    for e in s["entities"]:
        if e["t"] not in MACHINES:
            continue
        c = macros.flow_class({"name": e["n"], "type": e["t"], "status": e.get("s")})
        count[c] += 1
        if c != "working":
            label = e["n"] + (f"[{e['r']}]" if e.get("r") else "")
            groups[(c, label, e.get("s"))].append((e["x"], e["y"]))
    return s, count, groups


def flow_line(count):
    n = sum(count.values()) or 1
    return (f"flow {round(100 * count['working'] / n)}% of {sum(count.values())} machines | "
            f"blocked {count['blocked']} | starved {count['starved']} | down {count['down']}")


# ---- standing work

def _refuel(b, char, st):
    if time.time() - st.get("last_refuel", 0) < REFUEL_EVERY:
        return None
    st["last_refuel"] = time.time()
    inv = (b.call("state").get("agent") or {}).get("inventory") or {}
    fuel = "coal" if inv.get("coal", 0) >= 10 else "wood" if inv.get("wood", 0) >= 10 else None
    if not fuel:
        return None
    r = macros.refuel(b, fuel, 5, 32)
    return f"refuelled {r['refueled']}/{r['of']} burners" if r["of"] else None


def _restock(b):
    """Queue hand-crafting for the first kanban part below its minimum (runs in the background)."""
    a = b.call("state").get("agent") or {}
    if a.get("crafting_queue"):
        return None  # already crafting
    inv = a.get("inventory") or {}
    for item, want in KANBAN.items():
        have = inv.get(item, 0)
        if have >= want:
            continue
        try:
            r = b.call("craft", recipe=item, count=min(CRAFT_BATCH, want - have), wait=False)
            return f"crafting {r.get('started')} {item} (kanban {have}/{want})"
        except BridgeError:
            continue  # missing ingredients or not unlocked; try the next part
    return None


def _andon(b, char):
    """Nothing on the board this character may take: post a job for the biggest loss not posted
    recently. (Open jobs pinned to someone else don't count; they can't feed this character.)"""
    jobs = b.call("jobs", recent=0)["jobs"]
    jobs = jobs if isinstance(jobs, list) else []
    if any(j.get("state") == "open" and j.get("for") in (None, char) for j in jobs if isinstance(j, dict)):
        return None
    st = _load("crew-andon.json")
    now = time.time()
    _, count, groups = losses(b)
    # down and starved first: "blocked" is usually a symptom of a missing consumer further down,
    # which the starved/down job of that consumer fixes; diagnosing it separately is duplicated work
    order = {"down": 0, "starved": 1, "blocked": 2}
    for (cls, label, status), spots in sorted(groups.items(),
                                              key=lambda kv: (order[kv[0][0]], -len(kv[1]) * _weight(kv[0][1]))):
        sig = f"{cls}|{label}|{status}"
        if now - st.get(sig, 0) < ANDON_REPEAT:
            continue
        st[sig] = now
        _save("crew-andon.json", st)
        where = " ".join(f"{x},{y}" for x, y in spots[:8]) + (f" (+{len(spots) - 8})" if len(spots) > 8 else "")
        brief = (f"Andon (auto-posted because the board was empty): {len(spots)} x {label} {cls} ({status}) at "
                 f"{where}. {macros.LOSS_HINT[cls]}. Find the root cause with inspect/belt_status and fix it "
                 f"for the whole group. If it is not worth fixing (e.g. depleted ore, spare machines), pick "
                 f"them up and say where they could be reused. Base now: {flow_line(count)}. Note flow "
                 f"before -> after.")
        j = b.call("post_job", title=f"Andon: {len(spots)} x {label} {status}"[:120], brief=brief,
                   priority=3, **{"from": "andon"})
        return f"posted andon job #{j['id']} ({len(spots)} x {label} {status})"
    return None


JOBS_PER_WORKER = 3  # a worker's context grows with every step; after this many jobs it must stop and be replaced


def next_job(char, wait, fresh=False):
    b = Bridge(char, autostart=False)
    # a typo in the name would spawn a new character on the first call; refuse unknown names
    known = b.call("agents") or {}
    if char not in known and os.environ.get("FACTORIO_SPAWN") != "1":
        print(f"error: no character named '{char}' (known: {', '.join(sorted(known))}). "
              "Check the name; set FACTORIO_SPAWN=1 to create a new character on purpose.")
        return
    deadline = time.time() + wait
    notes, open_others = [], 0
    st_name = f"crew-{char}.json"
    if fresh:  # a new worker starts counting from zero
        st = _load(st_name)
        st["claims"] = 0
        _save(st_name, st)
    while True:
        try:
            st = _load(st_name)
            held = False
            if st.get("claims", 0) >= JOBS_PER_WORKER:
                # only an unfinished job may still be handed out (it resumes); no new claims
                held = any(isinstance(j, dict) and j.get("state") == "claimed" and j.get("by") == char
                           for j in _board(b))
                if not held:
                    print(f"STOP: you have done {st['claims']} jobs. Your context is full. Give your final "
                          "report now and end; a fresh worker continues.")
                    return
            r = b.call("claim_job")
            if isinstance(r.get("job"), dict):
                if not r.get("resumed"):
                    st = _load(st_name)
                    st["claims"] = st.get("claims", 0) + 1
                    _save(st_name, st)
                print(fmt_claim(r))
                if notes:
                    print("while waiting: " + "; ".join(notes[-4:]))
                return
            open_others = r.get("open_for_others", 0)
            st = _load(st_name)
            for chore in (lambda: _restock(b), lambda: _refuel(b, char, st), lambda: _andon(b, char)):
                note = chore()
                if note:
                    notes.append(note)
            _save(st_name, st)
        except BridgeError as e:
            notes.append(f"server: {e}")
            time.sleep(5)
        if time.time() > deadline:
            break
        time.sleep(3)
    print(f"no job yet ({open_others} open for others); run crew.py next again"
          + (". while waiting: " + "; ".join(notes[-4:]) if notes else ""))


# ---- planner side

def _board(b):
    jobs = b.call("jobs", recent=8)["jobs"]
    return jobs if isinstance(jobs, list) else []


def _weight(label):
    """Losses close to the customer (labs, then science packs) cost the most throughput."""
    if label.startswith("lab"):
        return 3
    if "science-pack" in label or "rocket" in label:
        return 2
    return 1


PACK_SHORT = {"automation-science-pack": "red", "logistic-science-pack": "green", "military-science-pack": "black",
              "chemical-science-pack": "blue", "production-science-pack": "purple",
              "utility-science-pack": "yellow", "space-science-pack": "white"}


def throughput(b, window="ten_minutes"):
    """Science packs made/used per minute and the biggest shortfalls (used > made), from the game's
    production statistics. Returns [] if the mod is older than 0.1.17."""
    try:
        p = b.call("production", window=window)
    except BridgeError:
        return []
    items = p.get("items") if isinstance(p.get("items"), dict) else {}
    fluids = p.get("fluids") if isinstance(p.get("fluids"), dict) else {}
    sci = [f"{PACK_SHORT[n]} {v['made']:g}/{v['used']:g}" for n, v in items.items() if n in PACK_SHORT]
    out = ["science/min made/used: " + (", ".join(sci) or "none")]
    short = []
    for kind, d in (("", items), ("fluid ", fluids)):
        for n, v in d.items():
            gap = v["used"] - v["made"]
            if n not in PACK_SHORT and v["used"] >= 2 and gap > 0.05 * v["used"]:
                short.append((gap, f"{kind}{n} -{gap:g}/min ({v['made']:g} made, {v['used']:g} used)"))
    if short:
        out.append("short: " + "; ".join(s for _, s in sorted(short, reverse=True)[:4]))
    return out


def compact(b, changed=()):
    """Token-lean planner view for the watch loop: what changed and what needs a decision, nothing else."""
    s, count, groups = losses(b)
    team = b.call("team", chat=0)
    members = team.get("members") if isinstance(team.get("members"), dict) else {}
    jobs = _board(b)
    r = s.get("research") or {}
    queue = r.get("queue") if isinstance(r.get("queue"), list) else []
    out = [f"{flow_line(count)} | research {r.get('current') or 'STOPPED'} "
           f"{round(100 * (r.get('progress') or 0))}% +{max(len(queue) - 1, 0)} queued"]
    held = {j.get("by"): j["id"] for j in jobs if j["state"] == "claimed"}
    crew = []
    for name, m in members.items():
        if m.get("dead"):
            crew.append(f"{name}:DEAD")
        elif name in held:
            crew.append(f"{name}:#{held[name]}")
        else:
            crew.append(f"{name}:idle{m.get('idle_s', 0)}s")
    out.append("crew " + " ".join(crew))
    out += throughput(b)
    if not r.get("current"):
        out.append("! research stopped")
    elif len(queue) < 2:
        out.append("! research queue short")
    open_jobs = [j for j in jobs if j["state"] == "open"]
    if len(open_jobs) < len(members):
        out.append(f"! board low: {len(open_jobs)} open for {len(members)} characters")
    for j in jobs:
        if j["state"] == "blocked":
            out.append(f"! blocked #{j['id']} {j['title'][:50]}: {(j.get('note') or '')[:160]}")
    power = sum(len(sp) for (c, lb, st), sp in groups.items() if st in ("no_power", "low_power"))
    if power:
        steam = sum(len(sp) for (c, lb, st), sp in groups.items() if lb.startswith(("steam-engine", "boiler")))
        out.append(f"! power: {power} machines low/no power" + (f"; {steam} boilers/engines idle (fuel/water?)" if steam else ""))
    for (cls, label, st), spots in groups.items():
        if st == "no_recipe":
            out.append(f"! {len(spots)} x {label} no_recipe")
    for j in jobs:
        if j["id"] in changed and j["state"] == "done":
            out.append(f"done #{j['id']} {j['title'][:50]}: {(j.get('note') or '')[:240]}")
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]) * _weight(kv[0][1]))
    out.append("top losses: " + "; ".join(f"{len(sp)}x {lb} {st} @{sp[0][0]},{sp[0][1]}"
                                         for (c, lb, st), sp in ranked[:5]))
    out.append("open: " + ("; ".join(f"#{j['id']} p{j['priority']} {j['title'][:40]}"
                                     + (f" ({j['for']})" if j.get("for") else "") for j in open_jobs) or "none"))
    return "\n".join(out)


def overview(b=None):
    """The planner's game state in one read: alerts first, then ranked losses, research, crew, board."""
    b = b or Bridge("planner", autostart=False)
    s, count, groups = losses(b)
    team = b.call("team", chat=6)
    members = team.get("members") if isinstance(team.get("members"), dict) else {}
    jobs = _board(b)
    r = s.get("research") or {}
    queue = r.get("queue") if isinstance(r.get("queue"), list) else []
    alerts = []
    if not r.get("current"):
        alerts.append("CRITICAL research stopped: queue is empty, labs idle -> start_research now")
    elif len(queue) < 2:
        alerts.append(f"research queue short ({len(queue)}): queue the next techs toward the rocket")
    open_jobs = [j for j in jobs if j["state"] == "open"]
    claimed = {j.get("by") for j in jobs if j["state"] == "claimed"}
    for name, m in members.items():
        if m.get("dead"):
            alerts.append(f"{name} is dead -> respawn")
        elif name not in claimed and not m.get("task") and m.get("idle_s", 0) > 60:
            alerts.append(f"{name} idle {m['idle_s']}s with no job (is its always-on worker running?)")
    if len(open_jobs) < len(members):
        alerts.append(f"board low: {len(open_jobs)} open job(s) for {len(members)} characters -> post more")
    blocked = [j for j in jobs if j["state"] == "blocked"]
    if blocked:
        alerts.append("blocked jobs need a decision: " + ", ".join(f"#{j['id']} {j['title'][:40]}" for j in blocked))
    for (cls, label, st), spots in groups.items():
        if st in ("no_power", "low_power"):
            alerts.append(f"power: {len(spots)} x {label} {st}")
        if st == "no_recipe":
            alerts.append(f"setup: {len(spots)} x {label} have no recipe at " + " ".join(f"{x},{y}" for x, y in spots[:4]))
    out = [f"tick {s['tick']} {flow_line(count)}",
           f"research {r.get('current') or '-'} {round(100 * (r.get('progress') or 0))}% "
           f"queue={','.join(queue) or '-'} done={r.get('researched_count')}"]
    out += throughput(b)
    out += ["ALERTS:"] + [f"  ! {a}" for a in alerts] if alerts else ["no alerts"]
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]) * _weight(kv[0][1]))
    out.append("losses (ranked by machines x closeness to labs):")
    for (cls, label, st), spots in ranked[:10]:
        out.append(f"  {cls} {len(spots)} x {label} {st}: " + " ".join(f"{x},{y}" for x, y in spots[:6]))
    out.append(fmt_team(team))
    out.append(fmt_jobs({"jobs": [dict(j, brief=None) for j in jobs]}))
    return "\n".join(out)


def status(b=None):
    print(overview(b))


def _needs(b, jobs):
    """Things only the planner can fix: characters with nothing they may take, a short research queue."""
    team = b.call("team", chat=0)
    members = team.get("members") if isinstance(team.get("members"), dict) else {}
    held = {j.get("by") for j in jobs if j["state"] == "claimed"}
    starving = [n for n in members if n not in held
                and not any(j["state"] == "open" and j.get("for") in (None, n) for j in jobs)]
    r = b.call("snapshot", chat=0).get("research") or {}  # read-only; research_status would need a character
    queue = r.get("queue") if isinstance(r.get("queue"), list) else []
    out = []
    if starving:
        out.append("no job for " + ",".join(sorted(starving)))
    if not r.get("current"):
        out.append("research stopped")
    elif len(queue) < 2:
        out.append("research queue short")
    return out


def watch(wait):
    b = Bridge("planner", autostart=False)
    first = _board(b)
    start = {j["id"]: j["state"] for j in first}
    st = _load("crew-watch.json")
    deadline = time.time() + wait
    why = "timeout"
    changed = []
    while time.time() < deadline:
        jobs = _board(b)
        changed = [j for j in jobs if j["state"] in ("done", "blocked") and start.get(j["id"]) != j["state"]]
        if changed:
            why = "finished: " + ", ".join(f"#{j['id']} {j['state']}" for j in changed)
            break
        needs = _needs(b, jobs)
        # wake at once for something new; the same unchanged need re-wakes at most once a minute,
        # so a planner that can't act on it doesn't spin and spend tokens
        if needs and (needs != st.get("needs") or time.time() - st.get("t", 0) > 60):
            st = {"needs": needs, "t": time.time()}
            _save("crew-watch.json", st)
            why = "; ".join(needs)
            break
        time.sleep(5)
    print(f"[{why}]")
    print(compact(b, {j["id"] for j in changed}))


def main():
    a = sys.argv[1:]
    wait = float(a[a.index("--wait") + 1]) if "--wait" in a else 100.0
    if len(a) >= 2 and a[0] == "next":
        next_job(a[1], wait, fresh="--fresh" in a)
    elif a and a[0] == "watch":
        watch(wait)
    elif a and a[0] == "status":
        status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
