"""Always-on crew helpers around the job board.

    python crew.py next <character> [--wait 100]    worker: block until a job is claimed for <character>
    python crew.py watch [--wait 100]               planner: block until the board needs attention
    python crew.py status                           planner: flow, losses, crew and board in one read
    python crew.py roadmap                          current phase, KPI gaps, what the silo path still costs
    python crew.py design "<title>" "<evidence>"    planner: ask the architect for a layout design
    python crew.py designs | design-done <id> "<note>"   supervisor: read / close design requests
    python crew.py note "<fact>" | notes [n]        shared facts about the base (positions, belt contents)
    python crew.py offline                          supervisor: workers whose heartbeat is stale
    python crew.py order <char> "<text>" | orders   overseer: direct order, printed on every command until acked
    python crew.py ack <char> <id> "<what you did>" worker: acknowledge an order
    python crew.py audit | where <item>             stock, ore, power, capacity vs output | which chests hold an item

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
        if cls == "blocked":
            continue  # a blocked machine is a symptom of a missing consumer elsewhere; the planner posts those
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


def _crew_context(b, char):
    """What a worker needs to collaborate, handed over with every job so nobody has to remember to look:
    what each teammate is doing right now, and the messages addressed to this character."""
    lines = []
    try:
        team = _dict_of(b.call("team", chat=0))
        jobs = _board(b)
        held = {j.get("by"): j for j in jobs if j.get("state") == "claimed"}
        mates = []
        for name, m in sorted(_dict_of(team.get("members")).items()):
            if name == char:
                continue
            j = held.get(name)
            what = f"#{j['id']} {j['title'][:60]}" if j else (m.get("task") or "idle")
            where = m.get("pos") or {}
            mates.append(f"{name}: {what} @{round(where.get('x', 0))},{round(where.get('y', 0))}")
        if mates:
            lines.append("crew now: " + " | ".join(mates))
        msgs = [m for m in _list_of(b.call("chat").get("messages")) if isinstance(m, dict)]
        if msgs:
            lines.append("messages for you: " + " || ".join(
                f"{m.get('from')}{' > ' + m['to'] if m.get('to') else ''}: {str(m.get('message'))[:140]}" for m in msgs[-4:]))
    except BridgeError:
        pass
    lines.append("NO HAND-FEEDING: never carry items into machines or feed chests. Build the automatic feed "
                 "(inserter from a belt/neighbour; an inserter only takes what its machine needs, so mixed "
                 "belts are fine). No route? view_map + belt_status, or post a design job.")
    lines.append("COLLABORATE: you are a crew, don't work alone. (1) Need parts, a feed, a machine moved or built "
                 "by someone else? DELEGATE: cli.py t post_job title=\"...\" brief=\"exact tiles + accept check\" "
                 "for=<teammate> priority=2 (for=<name> picks who; omit it for any free hand), keep working on what "
                 "you can, and the result comes back to you as a chat message. (2) A teammate works nearby or on a "
                 "related step: say to=<name> what you will change. (3) When you finish something they need: say "
                 "to=<name> what/where (item, chest, belt). (4) Blocked on something a teammate can do? Post it "
                 "as their job instead of just ending blocked.")
    return "\n".join(lines)


def _dict_of(v):
    return v if isinstance(v, dict) else {}


def _list_of(v):
    return v if isinstance(v, list) else []


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
            st["seen"] = time.time()   # heartbeat: tells the supervisor this worker is alive
            _save(st_name, st)
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
                print(_crew_context(b, char))
                if notes:
                    print("while waiting: " + "; ".join(notes[-4:]))
                return
            open_others = r.get("open_for_others", 0)
            st = _load(st_name)
            # workers only build: no auto-andon survey jobs (steering lever 4); the planner reads the audit
            for chore in (lambda: _restock(b), lambda: _refuel(b, char, st)):
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


# ---- roadmap: the goal is the rocket (ROADMAP.md); gates are measured, not guessed

ROADMAP_PHASES = [
    ("P1 blue online", [("chemical-science-pack", 6)]),
    ("P2 industry", [("iron-plate", 700), ("copper-plate", 500), ("steel-plate", 80)]),
    ("P3 purple science", [("production-science-pack", 4)]),
    ("P4 yellow science + oil", [("utility-science-pack", 3)]),
]
_PATH = {"t": 0.0, "data": None}


def silo_path():
    """Techs still gating the rocket silo and the research packs they cost (cached for 60 s)."""
    if _PATH["data"] and time.time() - _PATH["t"] < 60:
        return _PATH["data"]
    reader = Bridge("agent", autostart=False)  # `tech` is a character command; a read-only lookup
    seen = {}

    def walk(n):
        if n in seen:
            return
        t = _dict_of(reader.call("tech", name=n))
        seen[n] = t
        for p, done in _dict_of(t.get("prerequisites")).items():
            if not done:
                walk(p)

    walk("rocket-silo")
    todo = {n: t for n, t in seen.items() if not t.get("researched")}
    packs = {}
    for t in todo.values():
        for k, v in _dict_of(t.get("unit_cost")).items():
            packs[PACK_SHORT.get(k, k)] = packs.get(PACK_SHORT.get(k, k), 0) + int((t.get("units") or 0) * v)
    data = {"todo": sorted(todo), "packs": packs, "silo_done": bool(seen.get("rocket-silo", {}).get("researched"))}
    _PATH.update(t=time.time(), data=data)
    return data


FURNACE_RATE = {"iron-plate": 18.75, "copper-plate": 18.75, "steel-plate": 3.75}   # per minute, speed-1 furnace
FURNACE_SPEED = {"stone-furnace": 1, "steel-furnace": 2, "electric-furnace": 2}


def furnace_capacity(b):
    """Plates per minute the furnaces could make if fed (production stats only show what was consumed)."""
    cap = {}
    for e in b.call("snapshot").get("entities", []):
        if e.get("t") == "furnace" and e.get("r") in FURNACE_RATE:
            cap[e["r"]] = cap.get(e["r"], 0) + FURNACE_RATE[e["r"]] * FURNACE_SPEED.get(e["n"], 1)
    return cap


def roadmap(b):
    """Current phase, per-gate KPI gaps and what the silo path still costs. Returns (phase, lines)."""
    items = _dict_of(_dict_of(b.call("production", window="ten_minutes")).get("items"))
    made = lambda n: _dict_of(items.get(n)).get("made", 0)
    cap = furnace_capacity(b)
    # P2 counts smelting capacity: plates *made* can't exceed what the factory consumes
    amount = lambda n: cap.get(n, 0) if n in FURNACE_RATE else made(n)
    path = silo_path()
    phase, lines = None, []
    for name, kpis in ROADMAP_PHASES:
        gaps = [f"{i.replace('-science-pack', '').replace('-plate', '')} {round(amount(i))}/{t}" for i, t in kpis if amount(i) < t]
        lines.append(f"{name}: " + ("ok" if not gaps else "GAP " + ", ".join(gaps)))
        if gaps and phase is None:
            phase = name
    if phase is None:
        phase = "P6 silo + parts" if path["silo_done"] else "P5 silo research"
    left = " ".join(f"{k}{v}" for k, v in sorted(path["packs"].items()))
    head = f"ROADMAP phase {phase} | silo path: {len(path['todo'])} techs left, packs {left or 'none'}"
    return phase, [head] + lines


# ---- shared state beyond the job board: design requests, base notes, worker heartbeats

def _worker_age(char):
    seen = _load(f"crew-{char}.json").get("seen")
    return time.time() - seen if seen else 1e9


def offline_workers(b, chars=None):
    """Characters whose worker has not polled recently (4 min; 10 min while it holds a job)."""
    team = _dict_of(_dict_of(b.call("team", chat=0)).get("members"))
    held = {j.get("by") for j in _board(b) if j.get("state") == "claimed"}
    return [n for n in (chars or team) if _worker_age(n) > (600 if n in held else 240)]


def _read_designs():
    try:
        return json.loads((server.RUNTIME / "design-queue.json").read_text())
    except (OSError, ValueError):
        return []


def design_request(title, evidence):
    q = _read_designs()
    n = max([d["id"] for d in q] + [0]) + 1
    q.append({"id": n, "title": title[:120], "evidence": evidence[:1500], "state": "open", "ts": int(time.time())})
    (server.RUNTIME / "design-queue.json").write_text(json.dumps(q[-40:]))
    return n


def design_resolve(n, note):
    q = _read_designs()
    for d in q:
        if d["id"] == n:
            d["state"], d["note"] = "done", note[:300]
    (server.RUNTIME / "design-queue.json").write_text(json.dumps(q))


# ---- resource audit (agent-bridge 0.1.18): what we own, ore left, power, capacity vs actual output

KEY_STOCK = ["iron-plate", "copper-plate", "steel-plate", "stone-brick", "stone", "coal", "plastic-bar", "sulfur",
             "iron-gear-wheel", "electronic-circuit", "advanced-circuit", "engine-unit", "copper-cable", "pipe",
             "transport-belt", "inserter", "electric-mining-drill", "assembling-machine-2", "steel-furnace",
             "electric-furnace", "small-electric-pole", "medium-electric-pole"]
BLOCKED = ("full_output", "waiting_for_space_in_destination")


def audit(b):
    out = []
    try:
        items = _dict_of(_dict_of(b.call("stock", min=1, limit=300)).get("items"))
    except BridgeError as e:
        return f"audit needs agent-bridge 0.1.18 ({e})"
    parts = []
    for n in KEY_STOCK:
        v = _dict_of(items.get(n))
        if v.get("total"):
            where = "/".join(f"{'h' if k == 'carried' else k[0]}{v[k]}" for k in ("chest", "belt", "machine", "carried") if v.get(k))
            parts.append(f"{n} {v['total']} ({where})")
    out.append("STOCK total (c=chests b=belts m=machines h=carried by hand): " + "; ".join(parts))
    ore = _dict_of(_dict_of(b.call("ore")).get("ore"))
    out.append("ORE: " + "; ".join(f"{n.replace('-ore', '')} {v.get('per_min', 0):g}/min from {v.get('drills', 0)} drills, "
                                   f"{v.get('hours_left', '-')}h left of {v.get('amount', 0) // 1000}k" for n, v in ore.items()))
    p = _dict_of(b.call("power"))
    out.append(f"POWER: {p.get('producing_mw')} of {p.get('capacity_mw')} MW ({p.get('load_pct')}%), "
               f"{p.get('engines')} engines, boilers {p.get('boilers')}, low/no power machines "
               f"{p.get('low_power_machines')}/{p.get('no_power_machines')}")
    cap = _dict_of(_dict_of(b.call("capacity")).get("recipes"))
    prod = _dict_of(b.call("production", window="ten_minutes"))
    pitems, pfluids = _dict_of(prod.get("items")), _dict_of(prod.get("fluids"))
    rows = []
    for rname, r in cap.items():
        st = _dict_of(r.get("st"))
        pv = _dict_of(pitems.get(r["product"]) or pfluids.get(r["product"]))
        made, used, c = pv.get("made", 0), pv.get("used", 0), r.get("per_min") or 0
        util = made / c if c else 0
        blocked = sum(v for k, v in st.items() if k in BLOCKED)
        starved = sum(v for k, v in st.items() if "shortage" in k or k in ("no_ingredients", "waiting_for_source_items", "no_input_fluid"))
        flag = ""
        if util >= 0.85 and used >= 0.9 * made:
            flag = "BOTTLENECK (all output consumed; add machines)"
        elif util < 0.5 and blocked >= max(1, r["machines"] // 2):
            flag = "IDLE: blocked (no consumer)"
        elif util < 0.5 and starved >= max(1, r["machines"] // 2):
            flag = "IDLE: starved (input missing)"
        rows.append((0 if flag.startswith("BOTTLENECK") else 1 if flag else 2, rname, r["machines"], c, made, used, util, flag))
    rows.sort(key=lambda x: (x[0], -x[3]))
    out.append("CAPACITY per min (recipe: machines, could make, made, used, utilisation):")
    for _, rname, m, c, made, used, util, flag in rows[:18]:
        out.append(f"  {rname}: {m} machines, cap {c:g}, made {made:g}, used {used:g}, {round(util * 100)}%  {flag}")
    return "\n".join(out)


# ---- machine-checkable acceptance (steering lever 3; agent-bridge 0.1.19)
#
# A job's brief ends with "ACCEPT: <clause>; <clause>; ...". Clauses:
#   working X,Y                 the machine at X,Y reports working (or normal)
#   status X,Y <status>         e.g. status 28.5,-3.5 full_output
#   entity X,Y <name>           e.g. entity 70,-10 steel-furnace
#   has X,Y <item> <op> N       items inside the entity (chest, machine, belt) e.g. has 26.5,0.5 steel-plate < 900
#   belt X,Y <item>             the belt at X,Y carries the item
#   drop X,Y DX,DY              the inserter at X,Y drops at DX,DY (within half a tile)
#   recipe X,Y <recipe>         the assembler at X,Y runs that recipe
#   count <item> <op> N         factory-wide stock (chests+belts+machines+carried)
# ops: >= <= > < ==

import re

ACCEPT_KINDS = {"working", "status", "entity", "has", "belt", "drop", "recipe", "count"}
_NOT_MAIN = ("transport-belt", "underground-belt", "splitter", "electric-pole")


def accept_of(brief):
    """The ACCEPT clauses at the end of a brief (empty list if none)."""
    m = re.search(r"ACCEPT:\s*(.+)$", brief or "", re.S)
    if not m:
        return []
    return [c.strip() for c in m.group(1).replace("\n", " ").split(";") if c.strip()]


def validate_accept(clauses):
    """Syntax errors in ACCEPT clauses (empty list when all are fine)."""
    errs = []
    for c in clauses:
        t = c.split()
        if not t or t[0].lower() not in ACCEPT_KINDS:
            errs.append(f"'{c}': unknown check (use {', '.join(sorted(ACCEPT_KINDS))})")
            continue
        try:
            _parse_clause(c)
        except (ValueError, IndexError):
            errs.append(f"'{c}': bad syntax")
    return errs


def _xy(s):
    x, y = s.split(",")
    return float(x), float(y)


def _cmp(a, op, b):
    return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b}[op]


def _parse_clause(c):
    t = c.split()
    k = t[0].lower()
    if k == "count":
        return k, None, t[1:]
    x, y = _xy(t[1])
    rest = t[2:]
    if k in ("has",) and (len(rest) < 3 or rest[1] not in (">=", "<=", ">", "<", "==")):
        raise ValueError(c)
    if k in ("status", "entity", "belt", "recipe", "drop") and not rest:
        raise ValueError(c)
    if k == "drop":
        _xy(rest[0])
    return k, (x, y), rest


def eval_clause(b, c):
    """(ok, what was measured) for one clause."""
    k, xy, rest = _parse_clause(c)
    if k == "count":
        item, op, n = rest[0], rest[1], float(rest[2])
        v = _dict_of(_dict_of(b.call("stock", item=item).get("items")).get(item)).get("total", 0)
        return _cmp(v, op, n), f"{item} total {v}"
    ents = [e for e in _list_of(_dict_of(b.call("probe", x=xy[0], y=xy[1])).get("entities"))
            if e.get("type") != "item-entity"]  # items lying on the ground are never what a check means
    if not ents:
        return False, "nothing there"
    main = next((e for e in ents if e.get("type") not in _NOT_MAIN), ents[0])
    if k == "working":
        return main.get("status") in ("working", "normal"), f"{main['name']} {main.get('status')}"
    if k == "status":
        return main.get("status") == rest[0], f"{main['name']} {main.get('status')}"
    if k == "entity":
        return any(e["name"] == rest[0] for e in ents), "found " + ",".join(e["name"] for e in ents)
    if k == "recipe":
        return main.get("recipe") == rest[0], f"{main['name']} recipe {main.get('recipe')}"
    if k == "has":
        item, op, n = rest[0], rest[1], float(rest[2])
        v = sum(_dict_of(e.get("items")).get(item, 0) for e in ents)
        return _cmp(v, op, n), f"{item} {v}"
    if k == "belt":
        belts = [e for e in ents if e.get("type") in ("transport-belt", "underground-belt", "splitter")]
        v = sum(_dict_of(e.get("items")).get(rest[0], 0) for e in belts)
        return bool(belts) and v > 0, f"belt {rest[0]} {v}" if belts else "no belt there"
    if k == "drop":
        ins = next((e for e in ents if e.get("type") == "inserter"), None)
        if not ins:
            return False, "no inserter there"
        dx, dy = _xy(rest[0])
        d = ins.get("drop") or {}
        return abs(d.get("x", 1e9) - dx) <= 0.6 and abs(d.get("y", 1e9) - dy) <= 0.6, f"drops at {d.get('x')},{d.get('y')}"
    return False, "unknown"


def check_accept(b, clauses):
    """[(clause, ok, measured)] for every clause; a clause that errors counts as failed."""
    out = []
    for c in clauses:
        try:
            ok, got = eval_clause(b, c)
        except (BridgeError, ValueError, IndexError, KeyError) as e:
            ok, got = False, f"error: {e}"
        out.append((c, ok, got))
    return out


ACCEPT_HELP = ("end the brief with 'ACCEPT: <check>; <check>' (or pass accept=). Checks: working X,Y | "
               "status X,Y S | entity X,Y NAME | recipe X,Y R | has X,Y ITEM >= N | belt X,Y ITEM | "
               "drop X,Y DX,DY | count ITEM >= N")


def vet_job(b, brief, place=None):
    """Why a job may not be posted (None when it is fine): ACCEPT checks present and well-formed, and every
    tile in `place` free (or upgradable)."""
    clauses = accept_of(brief)
    if not clauses:
        return "a job needs machine-checkable ACCEPT checks: " + ACCEPT_HELP
    errs = validate_accept(clauses)
    if errs:
        return "; ".join(errs) + ". " + ACCEPT_HELP
    bad = []
    for p in _list_of(place):
        if not isinstance(p, dict) or "x" not in p or "y" not in p:
            bad.append(f"{p}: needs item,x,y")
            continue
        try:
            r = b.call("can_place", item=p.get("item"), name=p.get("name"), x=p["x"], y=p["y"],
                       direction=p.get("direction"))
        except BridgeError as e:
            bad.append(f"{p.get('item')} {p['x']},{p['y']}: {e}")
            continue
        if not r.get("ok") and not r.get("upgrade_possible"):
            bad.append(f"{p.get('item')} {p['x']},{p['y']} blocked by {','.join(_list_of(r.get('blockers'))) or 'terrain/water'}")
    if bad:
        return "tiles not placeable: " + "; ".join(bad[:6]) + ". Pick free tiles (view_map) and post again."
    return None


def finish_checked(b, job_id, state, note):
    """finish_job with the ACCEPT checks run first. A 'done' whose checks fail stays open the first time
    (the worker gets the measured values to fix); the second time it is closed blocked with them."""
    from mcp_server import fmt_job
    job = next((j for j in _list_of(b.call("jobs", all=True).get("jobs")) if j.get("id") == int(job_id)), None)
    if not job:
        return f"error: no job #{job_id}"
    clauses = accept_of(job.get("brief"))
    if validate_accept(clauses):
        clauses = []  # a job posted before checks were enforced (prose ACCEPT): close unverified
    checks = check_accept(b, clauses) if state == "done" else []
    failed = [(c, got) for c, ok, got in checks if not ok]
    if failed:
        st = _load("crew-checks.json")
        tries = st.get(str(job_id), 0) + 1
        st[str(job_id)] = tries
        _save("crew-checks.json", st)
        measured = "; ".join(f"{c} -> {got}" for c, got in failed)
        if tries < 2:
            return (f"NOT DONE: #{job_id} ACCEPT checks fail: {measured}. Fix it and call finish_job again "
                    "(a second failing finish closes it blocked with these values).")
        state, note = "blocked", f"ACCEPT failed: {measured}. Worker said: {note or ''}"[:400]
        log_outcome(job, "failed_checks", checks, note)
    else:
        log_outcome(job, state, checks, note)
    if checks and not failed:
        note = f"{note or ''} [verified {len(checks)}/{len(checks)}]"
    return fmt_job(b.call("finish_job", id=int(job_id), state=state, note=note))


# ---- outcome log and scorecard (steering lever 5)

def log_outcome(job, state, checks, worker_note):
    rec = {"ts": int(time.time()), "id": job.get("id"), "by": job.get("by"), "title": job.get("title"),
           "kind": (job.get("title") or "").split(":")[0][:24], "state": state,
           "checks": len(checks), "passed": sum(1 for _, ok, _ in checks if ok), "secs": job.get("age_s"),
           "note": (worker_note or "")[:160]}
    with open(server.RUNTIME / "outcomes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def outcomes():
    path = server.RUNTIME / "outcomes.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def scorecard_data():
    """Per worker: verified (done and all checks passed), unverified done, blocked, check failures, avg minutes."""
    per = {}
    for r in outcomes():
        s = per.setdefault(r.get("by") or "?", {"jobs": 0, "verified": 0, "unverified": 0, "blocked": 0, "failed": 0, "secs": 0})
        s["jobs"] += 1
        s["secs"] += r.get("secs") or 0
        if r["state"] == "done":
            s["verified" if r["checks"] and r["passed"] == r["checks"] else "unverified"] += 1
        elif r["state"] == "failed_checks":
            s["failed"] += 1
        else:
            s["blocked"] += 1
    return per


def scorecard():
    per = scorecard_data()
    if not per:
        return "no finished jobs logged yet"
    lines = ["worker   jobs verified unverified blocked failed_checks avg_min  verified%"]
    tot = {"jobs": 0, "verified": 0}
    for w, s in sorted(per.items()):
        tot["jobs"] += s["jobs"]
        tot["verified"] += s["verified"]
        lines.append(f"{w:8s} {s['jobs']:4d} {s['verified']:8d} {s['unverified']:10d} {s['blocked']:7d} {s['failed']:13d} "
                     f"{s['secs'] / max(s['jobs'], 1) / 60:7.1f}  {round(100 * s['verified'] / max(s['jobs'], 1))}%")
    lines.append(f"crew: {round(100 * tot['verified'] / max(tot['jobs'], 1))}% of {tot['jobs']} jobs verified (target 70%)")
    kinds = {}
    for r in outcomes():
        k = kinds.setdefault(r.get("kind") or "?", [0, 0])
        k[0] += 1
        k[1] += 1 if r["state"] == "done" and r["checks"] and r["passed"] == r["checks"] else 0
    worst = sorted(kinds.items(), key=lambda kv: kv[1][1] / kv[1][0])[:5]
    lines.append("weakest job kinds: " + "; ".join(f"{k} {v[1]}/{v[0]}" for k, v in worst))
    return "\n".join(lines)


def _orders():
    try:
        return json.loads((server.RUNTIME / "orders.json").read_text())
    except (OSError, ValueError):
        return []


def give_order(char, text):
    """Direct order from the overseer to one character; shown on top of every command it runs until acked."""
    orders = _orders()
    n = max([o["id"] for o in orders] + [0]) + 1
    orders.append({"id": n, "to": char, "text": text[:600], "ts": int(time.time()), "acked": False})
    (server.RUNTIME / "orders.json").write_text(json.dumps(orders[-60:]))
    return n


def ack_order(char, n, note):
    orders = _orders()
    for o in orders:
        if o["id"] == n and o["to"] == char:
            o["acked"], o["ack_note"], o["ack_ts"] = True, note[:300], int(time.time())
    (server.RUNTIME / "orders.json").write_text(json.dumps(orders))


def add_note(text):
    path = server.RUNTIME / "plant-notes.md"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines.append(f"- {time.strftime('%H:%M')} {text.strip()[:300]}")
    path.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")


def read_notes(n=25):
    path = server.RUNTIME / "plant-notes.md"
    return "\n".join(path.read_text(encoding="utf-8").splitlines()[-n:]) if path.exists() else "(no notes yet)"


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
    off = [n for n in members if _worker_age(n) > (600 if n in held else 240)]
    if off:
        out.append("! worker offline (supervisor restarts): " + ", ".join(f"{n} {int(min(_worker_age(n), 9999))}s" for n in off))
    try:
        out.append(roadmap(b)[1][0])
    except (BridgeError, KeyError, TypeError):
        pass
    open_designs = [d for d in _read_designs() if d.get("state") == "open"]
    if open_designs:
        out.append(f"design requests waiting for the architect: {len(open_designs)}")
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
    elif a and a[0] == "roadmap":
        print("\n".join(roadmap(Bridge("planner", autostart=False))[1]))
    elif a and a[0] == "offline":
        b = Bridge("planner", autostart=False)
        names = offline_workers(b)
        print("offline: " + (", ".join(f"{n} ({int(min(_worker_age(n), 9999))}s)" for n in names) or "none"))
    elif len(a) >= 3 and a[0] == "design":
        print(f"design request #{design_request(a[1], a[2])} filed for the architect")
    elif a and a[0] == "designs":
        open_ = [d for d in _read_designs() if d.get("state") == "open"]
        print("\n".join(f"#{d['id']} {d['title']}\n   {d['evidence']}" for d in open_) or "no open design requests")
    elif len(a) >= 3 and a[0] == "design-done":
        design_resolve(int(a[1]), a[2])
        print("closed")
    elif len(a) >= 2 and a[0] == "check":
        b = Bridge("planner", autostart=False)
        job = next((j for j in _list_of(b.call("jobs", all=True).get("jobs")) if j.get("id") == int(a[1])), None)
        if not job:
            print(f"no job #{a[1]}")
        else:
            res = check_accept(b, accept_of(job.get("brief")))
            print("\n".join(f"{'PASS' if ok else 'FAIL'}  {c}  -> {got}" for c, ok, got in res) or "this job has no ACCEPT clauses")
    elif a and a[0] == "scorecard":
        print(scorecard())
    elif a and a[0] == "audit":
        print(audit(Bridge("planner", autostart=False)))
    elif len(a) >= 2 and a[0] == "where":
        st = Bridge("planner", autostart=False).call("stock", item=a[1])
        v = _dict_of(_dict_of(st.get("items")).get(a[1]))
        print(f"{a[1]}: {v.get('total', 0)} total " + str({k: n for k, n in v.items() if k != 'total'}))
        for c in _list_of(st.get("chests")):
            print(f"  chest {c['pos']['x']},{c['pos']['y']}: {c['total']}")
    elif len(a) >= 3 and a[0] == "order":
        print(f"order #{give_order(a[1], ' '.join(a[2:]))} for {a[1]} (shown on every command it runs until acked)")
    elif len(a) >= 3 and a[0] == "ack":
        ack_order(a[1], int(a[2]), " ".join(a[3:]) if len(a) > 3 else "done")
        print("acknowledged")
    elif a and a[0] == "orders":
        for o in _orders()[-15:]:
            print(f"#{o['id']} -> {o['to']} [{'acked: ' + o.get('ack_note', '') if o.get('acked') else 'OPEN'}] {o['text'][:120]}")
    elif len(a) >= 2 and a[0] == "note":
        add_note(" ".join(a[1:]))
        print("noted")
    elif a and a[0] == "notes":
        print(read_notes(int(a[1]) if len(a) > 1 else 25))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
