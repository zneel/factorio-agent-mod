"""Composite actions built from bridge primitives.

Each macro does in one tool call what would otherwise take the agent many round trips (and every
round trip re-sends the whole conversation), and returns a one-line summary.
"""

import time

from bridge import BridgeError

BURNER_TYPES = ("mining-drill", "furnace", "boiler", "inserter")
STORE_TYPES = ("furnace", "container", "assembling-machine")
OUTPUT_INV = {"furnace": "furnace_result", "container": "chest",
              "assembling-machine": "assembling_machine_output"}
# Lean flow classes. A machine is either adding value (working) or losing capacity, and the loss
# says where to look: blocked = its output is not being pulled (downstream is the problem, or it is
# overproducing); starved = its input is not arriving (upstream is the problem); down = fuel, power
# or setup. Inserters and chests only move/store items, so only "down" counts for them.
FLOW = {"working": "working", "normal": "working", None: "working",
        "full_output": "blocked", "waiting_for_space_in_destination": "blocked",
        "waiting_for_source_items": "starved", "no_ingredients": "starved", "no_input_fluid": "starved",
        "item_ingredient_shortage": "starved", "fluid_ingredient_shortage": "starved",
        "missing_required_fluid": "starved", "missing_science_packs": "starved",
        "no_minable_resources": "starved"}
LOSS_HINT = {"blocked": "output not pulled: link it to its consumer, add consumers, or stop feeding it",
             "starved": "input not arriving: trace upstream (belt_status/inspect) to the constraint",
             "down": "no fuel/power/recipe/research: restore it"}
MOVERS = ("inserter", "chest", "container")


def flow_class(e):
    st = e.get("status")
    if st in (None, "working", "normal"):
        return "working"
    if any(m in e.get("name", "") or m == e.get("type") for m in MOVERS):
        return "down" if st in ("no_fuel", "no_power", "low_power") else "working"
    return FLOW.get(st, "down")


OK_STATUS = {"working", "normal", None}  # kept for callers that only want "is it running"


def _own(b, types, radius, x=None, y=None):
    ents = []
    for t in types:
        try:
            for e in b.call("find", type=t, radius=radius, limit=200, x=x, y=y)["nearest"]:
                e["type"] = t
                ents.append(e)
        except BridgeError:
            pass
    return [e for e in ents if e.get("id")]  # own buildings have unit numbers; trees/ore do not


def _tour(b, ents):
    """Greedy nearest-neighbour visiting order from the agent's position."""
    pos = b.call("state")["agent"]["position"]
    left, order = list(ents), []
    while left:
        e = min(left, key=lambda e: (e["pos"]["x"] - pos["x"]) ** 2 + (e["pos"]["y"] - pos["y"]) ** 2)
        left.remove(e)
        order.append(e)
        pos = e["pos"]
    return order


def _ref(e):
    return {"x": e["pos"]["x"], "y": e["pos"]["y"], "name": e["name"]}


def _reach_call(b, e, cmd, **args):
    """Call cmd on entity e, walking into reach first if needed."""
    try:
        return b.call(cmd, **_ref(e), **args)
    except BridgeError as err:
        if "out of reach" not in str(err):
            raise
    t = b.run("move_to", x=e["pos"]["x"], y=e["pos"]["y"], radius=6, timeout=120)
    if t.get("state") != "done":
        raise BridgeError(f"could not reach {e['name']} at {e['pos']}: {t.get('error')}")
    return b.call(cmd, **_ref(e), **args)


def collect(b, item=None, radius=32, x=None, y=None):
    ents = _own(b, STORE_TYPES, radius, x, y)
    got, visited, skipped = {}, 0, 0
    for e in _tour(b, ents):
        try:
            # only output slots / chests, never fuel or ingredients
            inv = OUTPUT_INV.get(e["type"])
            if not inv:
                continue
            r = _reach_call(b, e, "take", item=item, inventory=inv)
        except BridgeError as err:
            if "nothing to take" in str(err) or "no " in str(err)[:5]:
                continue
            skipped += 1
            continue
        visited += 1
        for k, v in r["taken"].items():
            got[k] = got.get(k, 0) + v
    return {"collected": got or "nothing", "from": visited, "skipped": skipped}


def refuel(b, fuel="coal", amount=5, radius=32, x=None, y=None):
    ents = _own(b, BURNER_TYPES, radius, x, y)
    need = []
    for e in ents:
        d = b.call("inspect", **_ref(e))
        f = (d.get("inventories") or {}).get("fuel")
        if f is None:
            continue  # electric
        have = sum(f["items"].values()) if f["items"] else 0
        if have < amount:
            need.append((e, amount - have))
    filled, used, short = 0, 0, []
    order = _tour(b, [e for e, _ in need])
    want = {id(e): n for e, n in need}
    for e in order:
        try:
            r = _reach_call(b, e, "insert", item=fuel, count=want[id(e)], inventory="fuel")
            filled += 1
            used += r["inserted"]
        except BridgeError as err:
            short.append(f"{e['name']}@{e['pos']['x']},{e['pos']['y']}: {str(err)[:60]}")
            if "no " + fuel in str(err):
                break
    return {"refueled": filled, "of": len(need), "fuel_used": used, "checked": len(ents),
            "problems": short or None}


def base_status(b, radius=48, x=None, y=None):
    types = ("mining-drill", "furnace", "boiler", "generator", "offshore-pump", "assembling-machine",
             "lab", "inserter", "container")
    groups, losses, count = {}, {"blocked": [], "starved": [], "down": []}, {"working": 0, "machines": 0}
    for e in _own(b, types, radius, x, y):
        if e["name"].startswith("crash-site"):
            continue
        g = groups.setdefault(e["name"], {})
        st = e.get("status") or "-"
        g[st] = g.get(st, 0) + 1
        c = flow_class(e)
        if not any(m in e["name"] or m == e.get("type") for m in MOVERS):
            count["machines"] += 1
            count["working"] += c == "working"
        if c != "working":
            losses[c].append(f"{e['name']}@{e['pos']['x']},{e['pos']['y']}")
    if not groups:
        return "no buildings in range"
    m = count["machines"]
    head = (f"flow: {round(100 * count['working'] / m) if m else 0}% of {m} machines working | "
            + " | ".join(f"{k} {len(v)}" for k, v in losses.items()))
    lines = [head] + [f"{n}: " + ",".join(f"{s}:{c}" for s, c in g.items()) for n, g in sorted(groups.items())]
    for k, v in losses.items():
        if v:
            more = f" (+{len(v) - 15})" if len(v) > 15 else ""
            lines.append(f"{k} ({LOSS_HINT[k]}): " + " ".join(v[:15]) + more)
    return "\n".join(lines)


def _near(b, pos, target, reach=8.0):
    """Walk until target is within reach; returns the (possibly new) agent position."""
    if (pos["x"] - target[0]) ** 2 + (pos["y"] - target[1]) ** 2 <= reach * reach:
        return pos
    t = b.run("move_to", x=target[0], y=target[1], radius=3, timeout=180)
    if t.get("state") != "done":
        # the target sits inside a dense build: any reachable spot within reach will do (approach side first)
        err = t.get("error")
        dx, dy = pos["x"] - target[0], pos["y"] - target[1]
        n = max(1e-6, (dx * dx + dy * dy) ** 0.5)
        spots = [(target[0] + dx / n * r, target[1] + dy / n * r) for r in (5, 7)]
        spots += [(target[0] + ox, target[1] + oy) for ox, oy in ((6, 0), (-6, 0), (0, 6), (0, -6), (5, 5), (-5, 5),
                                                                  (5, -5), (-5, -5))]
        for sx, sy in spots:
            t = b.run("move_to", x=sx, y=sy, radius=2, timeout=120)
            if t.get("state") == "done":
                return t["result"]["position"]
        # walled in by its own building: pick up what stands next to it, toward the target, and walk out (the
        # build loop puts it back later, it is still in the plan)
        me = b.call("state")["agent"]["position"]
        sx = 1 if target[0] > me["x"] else -1
        sy = 1 if target[1] > me["y"] else -1
        for ox, oy in ((sx * 1.5, 0), (0, sy * 1.5), (-sx * 1.5, 0), (0, -sy * 1.5)):
            try:
                b.run("mine", x=me["x"] + ox, y=me["y"] + oy, timeout=60)
            except BridgeError:
                continue
            t = b.run("move_to", x=target[0], y=target[1], radius=3, timeout=180)
            if t.get("state") == "done":
                return t["result"]["position"]
        raise BridgeError(f"can't walk to {target}: {err}")
    return t["result"]["position"]


def _try_place(b, item, x, y, direction, nudge=0, **extra):
    """Place at x,y; if nudge>0 also try spots up to nudge tiles away (for poles)."""
    spots = [(0, 0)] + [(dx, dy) for r in range(1, nudge + 1)
                        for dx in range(-r, r + 1) for dy in range(-r, r + 1) if max(abs(dx), abs(dy)) == r]
    last = None
    for dx, dy in spots:
        try:
            return b.call("place", item=item, x=x + dx, y=y + dy, direction=direction, **extra)
        except BridgeError as e:
            last = e
            if "no " + item in str(e) or "unknown item" in str(e):
                raise
            if "blocked by character" in str(e) and (dx, dy) == (0, 0):   # standing on the spot: step off, retry
                try:
                    b.run("move_to", x=x + 4, y=y + 4, radius=2, timeout=60)
                    return b.call("place", item=item, x=x, y=y, direction=direction, **extra)
                except BridgeError as e2:
                    last = e2
    raise last


def _dir(dx, dy):
    return "east" if dx > 0 else "west" if dx < 0 else "south" if dy > 0 else "north"


def belt_line(b, points, item="transport-belt"):
    """Lay belts along orthogonal waypoints [[x,y],...] (tile centers, e.g. 10.5). Each belt faces the
    next tile; the last keeps the final direction."""
    tiles = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 != x2 and y1 != y2:
            raise BridgeError(f"segment {x1},{y1} -> {x2},{y2} is diagonal; add a corner waypoint")
        dx, dy = (x2 > x1) - (x2 < x1), (y2 > y1) - (y2 < y1)
        n = int(round(abs(x2 - x1) + abs(y2 - y1)))
        for i in range(n):
            tiles.append((x1 + dx * i, y1 + dy * i, _dir(dx, dy)))
    if points:
        last_dir = tiles[-1][2] if tiles else "north"
        tiles.append((points[-1][0], points[-1][1], last_dir))
    pos = b.call("state")["agent"]["position"]
    placed, failed = 0, []
    for x, y, d in tiles:
        try:
            pos = _near(b, pos, (x, y))
            _try_place(b, item, x, y, d)
            placed += 1
        except BridgeError as e:
            failed.append(f"{x},{y}: {str(e)[:70]}")
            if "no " + item in str(e):
                break
    return {"placed": placed, "of": len(tiles), "failed": failed or None}


def place_row(b, item, x, y, count, dx=0, dy=0, direction="north", recipe=None):
    """Place `count` copies starting at x,y stepping by dx,dy; optionally set an assembler recipe."""
    pos = b.call("state")["agent"]["position"]
    placed, failed = [], []
    for i in range(count):
        px, py = x + dx * i, y + dy * i
        try:
            pos = _near(b, pos, (px, py))
            e = _try_place(b, item, px, py, direction)
            if recipe:
                b.call("set_recipe", x=px, y=py, recipe=recipe)
            placed.append(f"{e['pos']['x']},{e['pos']['y']}")
        except BridgeError as err:
            failed.append(f"{px},{py}: {str(err)[:70]}")
            if "no " + item in str(err):
                break
    return {"placed": len(placed), "at": " ".join(placed), "failed": failed or None}


def pole_line(b, x1, y1, x2, y2, item="small-electric-pole", spacing=6):
    """Poles from (x1,y1) to (x2,y2) along an L (x first, then y), every `spacing` tiles, nudged up
    to 2 tiles around obstacles. Small poles connect up to 7.5 tiles apart."""
    pts = []
    for (ax, ay), (bx, by) in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2))):
        n = int(abs(bx - ax) + abs(by - ay))
        steps = max(1, -(-n // spacing)) if n else 0
        for i in range(1, steps + 1):
            t = i / steps
            pts.append((round(ax + (bx - ax) * t) + 0.5, round(ay + (by - ay) * t) + 0.5))
    pos = b.call("state")["agent"]["position"]
    placed, failed = [], []
    for px, py in pts:
        try:
            pos = _near(b, pos, (px, py))
            e = _try_place(b, item, px, py, "north", nudge=1)
            placed.append(f"{e['pos']['x']},{e['pos']['y']}")
        except BridgeError as err:
            failed.append(f"{px},{py}: {str(err)[:70]}")
            break
    return {"placed": len(placed), "of": len(pts), "last": placed[-1] if placed else None,
            "failed": failed or None}


def compact_state(s):
    a = s.get("agent", {})
    for k in ("reach", "resource_reach", "weapons", "ammo", "name", "alive"):
        a.pop(k, None)
    if a.get("health") == 250:
        a.pop("health")
    if s.get("speed") == 1:
        s.pop("speed")
    s.pop("game_seconds", None)
    if not s.get("unread_chat"):
        s.pop("unread_chat", None)
    return s


def _plan_list(plan):
    """A placement plan: a JSON file path (list, or {"place": [...]}) or the list itself."""
    import json
    if isinstance(plan, str):
        from pathlib import Path
        p = Path(plan)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / plan
        plan = json.loads(p.read_text())
    if isinstance(plan, dict):
        plan = plan.get("place", [])
    return plan


OPP = {"north": "south", "south": "north", "east": "west", "west": "east"}
# hand-crafting is for the burner bootstrap only (GROWTH_PROMPT rule 2); the growth controller turns it off once
# the mall makes the building materials. Small poles need wood, which only characters get: always hand-made.
HANDCRAFT = True
ALWAYS_HAND = {"small-electric-pole"}
SYMMETRIC = {"steam-engine", "pipe", "small-electric-pole", "medium-electric-pole", "lab", "storage-tank"}


def game_dir(p):
    """Plans say direction = where items go; an inserter's engine direction names its pickup side."""
    d = p.get("direction", "north")
    return OPP[d] if "inserter" in p["item"] else d


def build(b, plan, dry=False, mine_obstacles=True):
    """Build every entry of a placement plan [{item,x,y,direction,belt_type?,recipe?}] in file order,
    walking as needed. Idempotent: an entry whose entity already stands there (same name and
    direction) is skipped, so a worker can rerun it after fetching more items. Trees and rocks on a
    tile are mined first. Missing items are crafted when the character has the ingredients;
    otherwise the build stops and reports what is still needed in total."""
    items = _plan_list(plan)
    st = b.call("state")["agent"]
    inv = dict(st.get("inventory") or {})
    pos = st["position"]
    todo, skipped, conflicts = [], 0, []
    pending_removals = 0
    for p in [q for q in items if q.get("remove")]:  # removals first: {"remove": true, "item", "x", "y"}
        ents = b.call("probe", x=p["x"], y=p["y"]).get("entities") or []
        present = any(e["name"] == p["item"] for e in ents)
        pending_removals += present and dry
        if present and not dry:
            try:
                b.run("mine", x=p["x"], y=p["y"], timeout=120)
            except BridgeError as e:
                conflicts.append(f"remove {p['item']} {p['x']},{p['y']}: {str(e)[:60]}")
    items = [q for q in items if not q.get("remove")]
    if not dry:
        pos = b.call("state")["agent"]["position"]  # removals walk the character
    for p in items:
        ents = b.call("probe", x=p["x"], y=p["y"]).get("entities") or []
        same = [e for e in ents if e["name"] == p["item"] and abs(e["pos"]["x"] - p["x"]) < 0.01
                and abs(e["pos"]["y"] - p["y"]) < 0.01 and (e.get("dir") in (None, game_dir(p))
                                                            or p["item"] in ("stone-furnace", "steel-furnace")
                                                            # symmetric: the game stores west as east, south as north
                                                            or (p["item"] in SYMMETRIC and e.get("dir") == OPP[game_dir(p)]))]
        if same:
            skipped += 1
            continue
        todo.append(p)
    need = collections_counter(p["item"] for p in todo)
    short = {k: v - inv.get(k, 0) for k, v in need.items() if v > inv.get(k, 0)}
    if dry:
        return {"to_place": len(todo) + pending_removals, "already": skipped, "need": dict(need) or None,
                "short": short or None, "removals": pending_removals or None}
    placed, missing = 0, {}
    for i, p in enumerate(todo):
        it = p["item"]
        if it in missing:
            missing[it] += 1
            continue
        if inv.get(it, 0) < 1 and not (HANDCRAFT or it in ALWAYS_HAND):
            missing[it] = missing.get(it, 0) + 1
            continue
        if inv.get(it, 0) < 1:
            rest = sum(1 for q in todo[i:] if q["item"] == it)
            try:
                b.run("craft", recipe=it, count=rest, wait=True, timeout=600)
            except BridgeError:
                pass
            for _ in range(120):  # the queue may still be crafting (intermediates first)
                a = b.call("state")["agent"]
                inv = dict(a.get("inventory") or {})
                if inv.get(it, 0) >= 1 or not a.get("crafting_queue"):
                    break
                time.sleep(0.5)
            if inv.get(it, 0) < 1:
                missing[it] = missing.get(it, 0) + 1
                continue
        try:
            pos = _near(b, pos, (p["x"], p["y"]))
            extra = {"belt_type": p["belt_type"]} if p.get("belt_type") else {}
            try:
                try:
                    b.call("place", item=it, x=p["x"], y=p["y"], direction=game_dir(p), **extra)
                except BridgeError as e:
                    if "move closer" not in str(e):
                        raise
                    pos = _near(b, {"x": 1e9, "y": 1e9}, (p["x"], p["y"]))  # position was stale: walk, retry
                    b.call("place", item=it, x=p["x"], y=p["y"], direction=game_dir(p), **extra)
            except BridgeError as e:
                if not (mine_obstacles and any(w in str(e) for w in ("tree", "rock", "wreck", "boulder", "crash-site"))):
                    raise
                for blk in str(e).split("blocked by ", 1)[1].split(";")[0].split(", "):
                    nm, xy = blk.split("@")
                    if any(w in nm for w in ("tree", "rock", "boulder", "wreck", "crash-site")):
                        bx, by = (float(v) for v in xy.split(","))
                        b.run("mine", x=bx, y=by, timeout=300)
                b.call("place", item=it, x=p["x"], y=p["y"], direction=game_dir(p), **extra)
            inv[it] = inv.get(it, 0) - 1
            if p.get("recipe"):
                b.call("set_recipe", x=p["x"], y=p["y"], recipe=p["recipe"])
            placed += 1
        except BridgeError as e:
            conflicts.append(f"{it} {p['x']},{p['y']}: {str(e)[:90]}")
            if len(conflicts) >= 5:
                break
    return {"placed": placed, "already": skipped, "of": len(items), "failed": conflicts or None,
            "short": missing or None}


def collections_counter(it):
    import collections
    return collections.Counter(it)
