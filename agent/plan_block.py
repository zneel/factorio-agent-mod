"""Block compiler: from machines + belt lanes to every inserter, pole and belt, checked on the live map.

Spec (JSON):
{
  "name": "purple",
  "machines": [{"id": "C1", "recipe": "copper-cable", "x": -18.5, "y": -13.5, "entity": "assembling-machine-2"}, ...],
  "belts": [{"id": "BUS", "y": -10.5, "x1": 2.5, "x2": -20.5, "dir": "west",
             "lanes": {"north": "copper-plate", "south": "iron-plate"}, "build": false}, ...],
  "outputs": {"M": "T2"},         # optional: machine id -> belt id (or a list) its product goes onto (else direct only)
  "reads": {"M": {"iron-plate": "BUS"}},   # optional: the belt each ingredient is read from (else the first in reach)
  "external": ["iron-plate"]      # optional: items brought in from outside (their lanes need no producer here)
}
Machines are 3x3 (centre on .5). Belts are horizontal rows from x1 to x2 (flow from x1 towards x2).
Rules the compiler applies:
- An ingredient comes from a neighbouring machine that makes it (one free tile between them, the
  inserter sits on that tile where the two footprints overlap), else from a belt lane carrying it one
  tile (inserter) or two tiles (long-handed) beyond the machine's top or bottom edge.
- One inserter per (machine, belt) pair serves every lane it needs from that belt.
- An output onto a belt lands on the belt's FAR lane (as the game does); the compiler checks that the
  far lane is the lane the spec assigns to that product, and refuses the spec otherwise.
- Every lane must hold one item: two producers or an input and an output on one lane are refused.
- Flow: an output inserter takes the slot furthest upstream on its belt, an input inserter the one furthest
  downstream; every reader must pick up at or after some producer's drop tile, and a lane of an item made in the
  block that is read must be fed by a machine of the block.
- Poles: greedy set cover with small poles on free tiles until every inserter and machine is powered,
  then a connectivity check (wire reach 7.5) to the nearest existing pole.
"""
import json

DIRV = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
FLUIDS = {"water", "steam", "crude-oil", "heavy-oil", "light-oil", "petroleum-gas", "lubricant", "sulfuric-acid"}


class SpecError(Exception):
    pass


def _tiles3(cx, cy):
    return {(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}


def compile_block(spec, recipes):
    """recipes: name -> (ingredients [(item, n)], products [(item, n)])."""
    machines = {m["id"]: dict(m, entity=m.get("entity", "assembling-machine-2")) for m in spec["machines"]}
    belts = {b["id"]: b for b in spec["belts"]}
    occupied = {}  # (x, y) -> what
    places, notes, errors = [], [], []

    def claim(t, what):
        if t in occupied:
            errors.append(f"tile {t[0]:g},{t[1]:g} wanted by {what} but used by {occupied[t]}")
            return False
        occupied[t] = what
        return True

    for p in spec.get("pipes", []) + spec.get("extra", []):  # pipes at fluid ports, hand-placed poles: claimed first
        claim((p["x"], p["y"]), p["item"])
    for m in machines.values():
        for t in _tiles3(m["x"], m["y"]):
            claim(t, m["id"])
        prod = recipes.get(m["recipe"])
        if not prod:
            errors.append(f"{m['id']}: unknown recipe {m['recipe']}")
            continue
        m["ing"] = [i for i, _ in prod[0] if i not in FLUIDS]  # fluids come by pipe (spec "pipes")
        m["out"] = prod[1][0][0]
    for b in belts.values():
        step = 1 if b["x2"] >= b["x1"] else -1
        b["tiles"] = [(b["x1"] + k * step, b["y"]) for k in range(int(abs(b["x2"] - b["x1"])) + 1)]
        for t in b["tiles"]:
            claim(t, b["id"])
        lane_items = [v for v in b["lanes"].values() if v]
        if len(lane_items) != len(set(lane_items)):
            notes.append(f"{b['id']}: both lanes carry the same item")

    lane_users = {}  # (belt, lane) -> {"in": set(machines), "out": set(machines)}

    def use_lane(bid, lane, role, mid):
        lane_users.setdefault((bid, lane), {"in": set(), "out": set()})[role].add(mid)

    inserters = []  # dicts: tile, kind, direction, src, dst, items

    def free_slot(cands):
        for t in cands:
            if t not in occupied:
                return t
        return None

    # 1) direct insertion between neighbouring machines
    for m in machines.values():
        if "ing" not in m:
            continue
        m["sources"] = {}
        for n in machines.values():
            if n is m or n.get("out") not in m.get("ing", []):
                continue
            dx, dy = n["x"] - m["x"], n["y"] - m["y"]
            cands, direction = [], None
            if abs(dy) <= 2 and abs(dx) == 4:  # side by side, one gap column
                gx = (m["x"] + n["x"]) / 2
                ys = sorted({m["y"] + k for k in (-1, 0, 1)} & {n["y"] + k for k in (-1, 0, 1)})
                cands = [(gx, y) for y in ys]
                direction = "east" if dx < 0 else "west"  # items move from n to m
            elif abs(dx) <= 2 and abs(dy) == 4:  # stacked, one gap row
                gy = (m["y"] + n["y"]) / 2
                xs = sorted({m["x"] + k for k in (-1, 0, 1)} & {n["x"] + k for k in (-1, 0, 1)})
                cands = [(x, gy) for x in xs]
                direction = "south" if dy < 0 else "north"
            if not cands:
                continue
            t = free_slot(cands)
            if not t:
                errors.append(f"{n['id']} -> {m['id']}: no free tile for the inserter ({cands})")
                continue
            claim(t, f"ins {n['id']}->{m['id']}")
            inserters.append({"tile": t, "kind": "inserter", "direction": direction, "src": n["id"], "dst": m["id"],
                              "items": [n["out"]]})
            m["sources"][n["out"]] = n["id"]

    # 2) belt inputs and outputs
    outputs = spec.get("outputs", {})
    reads = spec.get("reads", {})   # machine id -> {item: belt id} (the layout solver's choice), else the first in reach
    at = {}   # (machine, belt, "in"/"out") -> x of the inserter's pickup / drop tile on that belt

    def reach(b, m):
        dist = abs(b["y"] - m["y"]) - 1.5  # tiles between the machine edge and the belt centre
        ok = dist in (1.5, 2.5) and min(b["x1"], b["x2"]) <= m["x"] - 1 and max(b["x1"], b["x2"]) >= m["x"] + 1
        return dist if ok else None
    for m in machines.values():
        if "ing" not in m:
            continue
        need = [i for i in m["ing"] if i not in m["sources"]]
        per_belt = {}
        for it in need:
            found = None
            cands = ([belts[reads[m["id"]][it]]] if reads.get(m["id"], {}).get(it) in belts else []) + list(belts.values())
            for b in cands:
                for lane, item in b["lanes"].items():
                    if item == it and reach(b, m) is not None:
                        found = (b, lane, reach(b, m))
                        break
                if found:
                    break
            if not found:
                errors.append(f"{m['id']} ({m['recipe']}): no source for {it}")
                continue
            b, lane, dist = found
            per_belt.setdefault(b["id"], {"b": b, "dist": dist, "items": []})["items"].append(it)
            use_lane(b["id"], lane, "in", m["id"])
        outs = outputs.get(m["id"], [])
        for bid in ([outs] if isinstance(outs, str) else outs):
            b = belts[bid]
            dist = abs(b["y"] - m["y"]) - 1.5
            if dist not in (1.5, 2.5):
                errors.append(f"{m['id']}: output belt {b['id']} is out of reach")
            else:
                far = "north" if b["y"] < m["y"] else "south"
                if b["lanes"].get(far) != m["out"]:
                    errors.append(f"{m['id']}: output {m['out']} would land on {b['id']}'s {far} lane, which the spec "
                                  f"gives to {b['lanes'].get(far)}")
                use_lane(b["id"], far, "out", m["id"])
                per_belt.setdefault(("out", b["id"]), {"b": b, "dist": dist, "items": [m["out"]], "out": True})
        # outputs first, on the slot furthest upstream of their belt; inputs on the slot furthest downstream: then a
        # producer in the same column as a reader (another row) still drops before the reader picks up
        for key, pb in sorted(per_belt.items(), key=lambda kv: not kv[1].get("out")):
            b = pb["b"]
            row = m["y"] + (-2 if b["y"] < m["y"] else 2)
            kind = "inserter" if pb["dist"] == 1.5 else "long-handed-inserter"
            east = b["x2"] >= b["x1"]
            order = (-1, 0, 1) if east == bool(pb.get("out")) else (1, 0, -1)
            t = free_slot([(m["x"] + k, row) for k in order])
            if not t:
                errors.append(f"{m['id']}: no free tile on row {row:g} for the {b['id']} inserter")
                continue
            claim(t, f"ins {m['id']}<{b['id']}")
            at[m["id"], b["id"], "out" if pb.get("out") else "in"] = t[0]
            if pb.get("out"):
                direction = "north" if b["y"] < m["y"] else "south"
            else:
                direction = "south" if b["y"] < m["y"] else "north"
            inserters.append({"tile": t, "kind": kind, "direction": direction,
                              "src": m["id"] if pb.get("out") else b["id"], "dst": b["id"] if pb.get("out") else m["id"],
                              "items": pb["items"]})

    made = {m["out"] for m in machines.values() if m.get("out")}
    external = set(spec.get("external", []))
    for (bid, lane), u in lane_users.items():
        # flow: a machine reading a lane that this block feeds must pick up downstream of at least one producer's
        # drop (inserter tiles, not machine centres); a lane of an item made here that nothing here feeds is an error
        item = belts[bid]["lanes"][lane]
        if not u["out"] and u["in"] and item in made and item not in external and belts[bid].get("build", True):
            errors.append(f"{bid} {lane} lane ({item}): read by {sorted(u['in'])} but no machine here drops onto it")
        if u["out"]:
            east = belts[bid]["x2"] >= belts[bid]["x1"]
            for c in u["in"]:
                cx = at.get((c, bid, "in"), machines[c]["x"])
                drops = [at.get((p, bid, "out"), machines[p]["x"]) for p in u["out"]]
                if not any((x <= cx) if east else (x >= cx) for x in drops):
                    errors.append(f"{c} reads {bid} {lane} lane upstream of every producer {sorted(u['out'])}: "
                                  f"it never gets {item}")
        if len(u["out"]) > 1:
            notes.append(f"{bid} {lane} lane: several producers {sorted(u['out'])} (fine if they make the same item)")
    for b in belts.values():
        for lane, item in b["lanes"].items():
            if item and (b["id"], lane) not in lane_users:
                notes.append(f"{b['id']} {lane} lane ({item}) is not used here (it only passes through)")

    # 3) poles: greedy set cover (small pole supply 5x5 = +-2 tiles), candidates = free tiles in the bbox
    need_power = [("ins", i["tile"]) for i in inserters] + [("m", (m["x"], m["y"])) for m in machines.values()]
    xs = [t[0] for t in occupied] or [0]
    ys = [t[1] for t in occupied] or [0]
    cands = [(x + 0.0, y + 0.0) for x in _frange(min(xs) - 2, max(xs) + 2) for y in _frange(min(ys) - 2, max(ys) + 2)
             if (x, y) not in occupied]

    def covers(p, target):
        kind, (tx, ty) = target
        r = 2 if kind == "ins" else 3.5  # a machine counts if any of its tiles is inside the 5x5 area
        return abs(p[0] - tx) <= r and abs(p[1] - ty) <= r

    uncovered = set(range(len(need_power)))
    poles = []
    while uncovered:
        best, gain = None, set()
        for c in cands:
            if c in occupied:
                continue
            g = {i for i in uncovered if covers(c, need_power[i])}
            if len(g) > len(gain):
                best, gain = c, g
        if not best:
            errors.append("poles: some entities cannot be powered")
            break
        poles.append(best)
        occupied[best] = "pole"
        uncovered -= gain
    # connectivity among the new poles (wire reach 7.5): report components
    comps = _components(poles + [(q["x"], q["y"]) for q in spec.get("extra", []) if q["item"] == "small-electric-pole"], 7.5)

    for b in belts.values():
        if b.get("build", True):
            for (x, y) in b["tiles"]:
                places.append({"item": "transport-belt", "x": x, "y": y, "direction": b["dir"], "group": f"belt {b['id']}"})
    for e in spec.get("extra_belts", []):
        if claim((e["x"], e["y"]), "extra belt"):
            places.append({"item": "transport-belt", "x": e["x"], "y": e["y"], "direction": e["dir"], "group": "belt extra"})
    for m in machines.values():
        p = {"item": m["entity"], "x": m["x"], "y": m["y"], "group": "machines"}
        if m["entity"] != "electric-furnace":
            p["recipe"] = m["recipe"]
        if m.get("direction"):
            p["direction"] = m["direction"]
        places.append(p)
    for q in spec.get("pipes", []):
        places.append(dict(q, group="pipes"))
    for q in spec.get("extra", []):
        places.append(dict(q, group="extra"))
    for i in inserters:
        places.append({"item": i["kind"], "x": i["tile"][0], "y": i["tile"][1], "direction": i["direction"],
                       "group": "inserters", "why": f"{i['src']}->{i['dst']} {'+'.join(i['items'])}"})
    for p in poles:
        places.append({"item": "small-electric-pole", "x": p[0], "y": p[1], "group": "poles"})
    import plan_rules
    rate_warnings = plan_rules.check_rates(places, machines, recipes, spec.get("target"))
    return {"places": places, "errors": errors, "notes": notes, "pole_components": comps, "rates": rate_warnings}


def _frange(a, b):
    a = int(a // 1) + 0.5
    while a <= b:
        yield a
        a += 1


def _components(pts, reach):
    parent = list(range(len(pts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2) ** 0.5 <= reach:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(pts)):
        groups.setdefault(find(i), []).append(pts[i])
    return list(groups.values())


CHECK_LUA = r"""
local s=game.surfaces[1] local P=helpers.json_to_table('%s') local bad={}
local D={north=defines.direction.north,east=defines.direction.east,south=defines.direction.south,west=defines.direction.west}
for i,p in pairs(P) do
  local proto=prototypes.item[p.item] local name=proto and proto.place_result and proto.place_result.name or p.item
  local ok=s.can_place_entity{name=name,position={p.x,p.y},direction=D[p.direction or 'north'],force='player',build_check_type=defines.build_check_type.manual}
  if not ok then
    local bl={} for _,e in pairs(s.find_entities_filtered{area={{p.x-0.4,p.y-0.4},{p.x+0.4,p.y+0.4}}}) do if e.type~='resource' and e.type~='tree' and e.type~='simple-entity' and e.type~='character' then bl[#bl+1]=e.name end end
    if #bl>0 then bad[#bad+1]=p.item..'@'..p.x..','..p.y..' blocked by '..table.concat(bl,',') end
  end
end
rcon.print(#bad==0 and 'OK' or table.concat(bad,'\n'))
"""


def check_on_map(b, places):
    payload = json.dumps([{k: p[k] for k in ("item", "x", "y", "direction") if k in p} for p in places],
                         separators=(",", ":")).replace("'", "")
    return b.raw("/sc " + CHECK_LUA % payload)


def recipes_from_game(b, lua):
    data = json.loads(b.raw("/sc " + lua))
    return {name: (ing, prod, energy) for name, cat, energy, ing, prod, enabled in data}
