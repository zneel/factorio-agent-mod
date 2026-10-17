"""Repair block X (two flow bugs: G1 upstream of every cable maker, both LDS upstream of the steel furnaces).
Layout: plan.py layout blocks/rocketX.repair.json --out blocks/rocketX2.json (CP-SAT, built belts pinned).
This script: acid pipe to PU's new south port (plan_nx pipe router), compile, diff against the live map
(what to remove, what to place), B5/B6 extension to x=10.5 and the B5 feed's new start (A* router).
Writes runtime/blocks/rocketX-fix.json {remove: [...], place: {group: [...]}}.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
sys.path.insert(0, HERE)
import plan  # noqa: E402
import plan_block  # noqa: E402
import plan_nx  # noqa: E402
from rocket import T, tiles, RT  # noqa: E402

AREA = (-21, 50, 12, 62)  # rows A and B with their inserter rows (belts B3/B4 at 55.5/56.5 stay)
KINDS = ("assembling-machine", "furnace", "inserter", "electric-pole")


def live(b):
    lua = ("/sc local o={} for _,e in pairs(game.surfaces[1].find_entities_filtered{area={{%d,%d},{%d,%d}},type={%s}}) do "
           "local r='' if e.type=='assembling-machine' then local q=e.get_recipe() r=q and q.name or '' end "
           "o[#o+1]=e.name..','..e.position.x..','..e.position.y..','..e.direction..','..r end rcon.print(table.concat(o,';'))"
           ) % (AREA[0], AREA[1], AREA[2], AREA[3], ",".join(f"'{k}'" for k in KINDS))
    out = []
    for s in b.raw(lua).strip().split(";"):
        if s:
            n, x, y, d, r = s.split(",")
            out.append({"item": n, "x": float(x), "y": float(y), "dir": int(d), "recipe": r})
    return out


DIRN = {"north": 0, "east": 4, "south": 8, "west": 12}


def _live_dir(p):
    """The game's direction for a placement: an inserter faces where it picks up (opposite of its drop)."""
    d = DIRN[p["direction"]]
    return (d + 8) % 16 if p["item"].endswith("inserter") else d


def main():
    b = plan._bridge()
    rec = plan_block.recipes_from_game(b, plan.RECIPE_LUA)
    spec = json.load(open(os.path.join(HERE, "rocketX2.json"), encoding="utf-8"))
    pu = next(m for m in spec["machines"] if m["id"] == "PU")
    pu["direction"] = "south"  # acid enters from below
    have = live(b)
    # grid with the new block (old machines in rows A/B ignored: they are being moved)
    g = plan.Grid.load(b, -30, 44, 26, 72)
    rows = [list(r) for r in g.rows]
    for e in have:
        if e["item"] != "chemical-plant":
            n = 3 if e["item"] in ("assembling-machine-2", "electric-furnace") else 1
            for (x, y) in tiles({"item": "assembling-machine-2" if n == 3 else "x", "x": e["x"], "y": e["y"]}):
                if g.x1 <= x < g.x2 and g.y1 <= y < g.y2:
                    rows[y - g.y1][x - g.x1] = "."
    g.rows = ["".join(r) for r in rows]
    r0 = plan_block.compile_block(spec, rec)
    bl = set()
    for p in r0["places"]:
        bl |= tiles(p)
    acid = {T(-17.5, 53.5), T(-16.5, 53.5), T(-15.5, 53.5)}
    port = T(pu["x"], pu["y"] + 2)
    route = plan_nx.route_pipe(g, acid, port, blocked=bl - {port}, own=acid, goal_dir="north", tries=12)
    if not route:
        raise SystemExit("no acid route to PU")
    route = [p for p in route if not (p.get("source") and T(p["x"], p["y"]) in acid)]
    spec["pipes"] = spec.get("pipes", []) + route
    r = plan_block.compile_block(spec, rec)
    if r["errors"]:
        raise SystemExit("; ".join(r["errors"]))
    slow = {w.split()[1] for w in r["rates"] if w.startswith("inserter ")}
    for p in r["places"]:
        if p["item"] == "inserter" and f"{p['x']:g},{p['y']:g}" in slow:
            p["item"] = "fast-inserter"
    new = [p for p in r["places"] if p["group"] in ("machines", "inserters", "poles", "pipes")]
    key = lambda n, x, y: (n, round(x * 2), round(y * 2))  # noqa: E731
    want = {key(p["item"], p["x"], p["y"]): p for p in new if p["group"] != "pipes"}
    remove, keep = [], set()
    for e in have:
        k = key(e["item"], e["x"], e["y"])
        p = want.get(k)
        same = p and (p["item"] == "chemical-plant" or e["item"] == "electric-pole" or
                      ("direction" not in p or _live_dir(p) == e["dir"]) and
                      (not p.get("recipe") or p["recipe"] == e["recipe"]))
        if same:
            keep.add(k)
        else:
            remove.append(e)
    place = [p for p in new if p["group"] == "pipes" or key(p["item"], p["x"], p["y"]) not in keep]
    # poles stay (other blocks may hang on them, e.g. block Y's bridge pole) unless a new piece needs the tile,
    # and then the new pole plan already covers it; new poles on tiles of kept poles are dropped
    need = set().union(*[tiles(p) for p in place if p["item"] != "small-electric-pole"]) if place else set()
    remove = [e for e in remove if e["item"] != "small-electric-pole" or T(e["x"], e["y"]) in need]
    kept_poles = {T(e["x"], e["y"]) for e in have if e["item"] == "small-electric-pole"} - {T(e["x"], e["y"]) for e in remove}
    place = [p for p in place if not (p["item"] == "small-electric-pole" and T(p["x"], p["y"]) in kept_poles)]
    # B5/B6 extension east to x=10.5 and the B5 feed's new start (old start tiles are removed)
    ext = [{"item": "transport-belt", "x": x + 0.5, "y": y, "direction": "east", "group": "ext"}
           for y in (62.5, 63.5) for x in range(3, 11)]
    old_feed = [{"item": "transport-belt", "x": 4.5, "y": 62.5}, {"item": "underground-belt", "x": 4.5, "y": 63.5},
                {"item": "underground-belt", "x": 4.5, "y": 66.5}]
    remove += [dict(o, dir=None, recipe="") for o in old_feed]
    g2 = plan.Grid.load(b, -30, 44, 30, 80)
    rows = [list(r) for r in g2.rows]
    for o in old_feed:
        rows[int(o["y"] // 1) - g2.y1][int(o["x"] // 1) - g2.x1] = "."
    for p in ext + [{"item": "x", "x": 11.5, "y": 63.5}]:  # B6's new end must stay a dead end
        rows[int(p["y"] // 1) - g2.y1][int(p["x"] // 1) - g2.x1] = "#"
    g2.rows = ["".join(r) for r in rows]
    feed = plan.route(g2, (11, 62), (5, 67), "east", "west")
    if not feed:
        raise SystemExit("no route for the B5 feed")
    gone = set().union(*[tiles({"item": e["item"], "x": e["x"], "y": e["y"]}) for e in remove]) if remove else set()
    chk = plan_block.check_on_map(b, place + ext + feed)
    bad = [c for c in chk.split(";") if c.strip() and c.strip() != "OK"]
    real = []
    for c in bad:  # failures on tiles we are clearing are expected
        try:
            pos = c.split("@")[1].split(" ")[0]
            x, y = map(float, pos.split(","))
            if T(x, y) not in gone:
                real.append(c.strip())
        except (IndexError, ValueError):
            real.append(c.strip())
    print("map check (ignoring tiles being cleared):", "; ".join(real) or "OK")
    out = {"remove": remove, "place": {"block": place, "belts": ext + [dict(p, group="feed") for p in feed]}}
    json.dump(out, open(os.path.join(RT, "rocketX-fix.json"), "w"), indent=0)
    print(f"remove {len(remove)}, place {len(place)} block pieces + {len(ext)} belt ext + {len(feed)} feed tiles")
    print("feed:", " | ".join(plan.segments(feed)))
    print("acid:", "; ".join(f"{p['item']} {p['x']:g},{p['y']:g}" + (f" {p['direction']} {p['end']}" if p.get('end') else "")
                              for p in route))


if __name__ == "__main__":
    main()
