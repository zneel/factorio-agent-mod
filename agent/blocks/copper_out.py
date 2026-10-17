"""South copper outpost -> electric-furnace smelter at x 26-49, y 37-50. Posts jobs; prints only job ids."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
import plan  # noqa: E402

RT = os.path.join(AGENT, "..", "runtime", "blocks")
route = json.loads(open(os.path.join(RT, "copper-route.txt")).read().strip().splitlines()[-1])
smelter = json.load(open(os.path.join(RT, "copper-places.json")))
b = plan._bridge()

CHECK = ("/sc local s=game.surfaces[1] local P=helpers.json_to_table('%s') local o={} for i,p in pairs(P) do "
         "if s.can_place_entity{name=p[3],position={p[1],p[2]},force='player',build_check_type=defines.build_check_type.manual} "
         "then o[#o+1]=i end end rcon.print(table.concat(o,','))")


def placeable(cands, name):
    payload = json.dumps([[x, y, name] for x, y in cands])
    ok = b.raw(CHECK % payload).strip()
    return {int(i) for i in ok.split(",") if i}


# drills: both sides of the collector x=85.5, y 218.5..236.5
dc = [(x, y) for y in [218.5 + 3 * k for k in range(7)] for x in (83.5, 87.5)]
okd = placeable(dc, "electric-mining-drill")
drills = [{"item": "electric-mining-drill", "x": x, "y": y, "direction": "east" if x < 85.5 else "west"}
          for i, (x, y) in enumerate(dc, 1) if i in okd]
collector = [{"item": "transport-belt", "x": 85.5, "y": y, "direction": "north"} for y in [237.5 - k for k in range(21)]]
# small poles every 6 tiles along the route, on a free tile beside it (east first, then west)
taken = {(p["x"], p["y"]) for p in route + collector + drills}
poles, last = [], None
path = collector[::-1] + route
for i, p in enumerate(path):
    if i % 6:
        continue
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        c = (p["x"] + dx, p["y"] + dy)
        if c not in taken:
            poles.append(c)
            break
okp = placeable(poles, "small-electric-pole")
poles = [{"item": "small-electric-pole", "x": x, "y": y} for i, (x, y) in enumerate(poles, 1) if i in okp]
# drill-field poles between drill rows (x=85.5 is the belt; use x 81.5 and 89.5)
fp = [(x, y) for y in (220, 226, 232, 238) for x in (81.5, 89.5)]
okf = placeable([(x, y + 0.5) for x, y in fp], "small-electric-pole")
poles += [{"item": "small-electric-pole", "x": x, "y": y + 0.5} for i, (x, y) in enumerate(fp, 1) if i in okf]


def runs(ps):
    return " | ".join(plan.segments(ps))


def lst(ps):
    return "; ".join(f"{p['item'].replace('electric-mining-drill', 'drill').replace('small-electric-pole', 'pole')} "
                     f"{p['x']:g},{p['y']:g}" + (f" {p['direction']}" if p.get("direction") else "") for p in ps)


INTRO = ("COPPER OUTPOST (south patch x72-101 y217-239, 1.24M ore) -> new electric-furnace smelter at x26-49 y37-50 "
         "(about 360 copper plate/min for the processing-unit block). Routed and placed by plan.py; tiles checked on "
         "the live map. Belt runs 'a -> b' cover every tile between, same direction.")


def post(title, body, accept, place, who=None, after=None):
    args = [sys.executable, "cli.py", "t", "post_job", f"title={title}", f"brief={INTRO} {body}", f"accept={accept}",
            "priority=2", "place=" + json.dumps([{k: v for k, v in q.items() if k in ("item", "x", "y", "direction")}
                                                 for q in place][:10])]
    if who:
        args.append(f"for={who}")
    if after:
        args.append(f"after={after}")
    env = dict(os.environ, FACTORIO_AUTOSTART="0", FACTORIO_AGENT_NAME="planner")
    out = subprocess.run(args, cwd=AGENT, capture_output=True, text=True, env=env).stdout.strip().splitlines()
    head = out[0] if out else "?"
    print(head[:90])
    return head[1:head.index(" ")] if head.startswith("#") else None


half = len(route) // 2
J1 = post("Copper outpost (1/4): power line and drill-field poles",
          f"Place small-electric-pole at: {lst(poles)} ({len(poles)} poles; the first ones near 52.5,44 join the grid).",
          f"entity {poles[0]['x']:g},{poles[0]['y']:g} small-electric-pole; entity {poles[-1]['x']:g},{poles[-1]['y']:g} small-electric-pole",
          poles, who="runner1")
J2 = post("Copper outpost (2/4): 12 drills and the collector belt",
          f"Drills (craft what the store lacks: 3 circuits + 5 gears + 10 iron each): {lst(drills)}. Collector: {runs(collector)}.",
          f"entity {drills[0]['x']:g},{drills[0]['y']:g} electric-mining-drill; entity 85.5,226.5 transport-belt",
          drills + collector[:2], who="hauler")
J3 = post("Copper outpost (3/4): ore belt to the smelter",
          f"About {len(route)} belts (take from chests 80.5,15.5 / 44.5,32.5 or craft: 1 gear + 1 iron = 2 belts): {runs(route)}.",
          "entity 85.5,180.5 transport-belt; entity 52.5,80.5 transport-belt; entity 49.5,43.5 transport-belt",
          route[:3] + route[-3:], who="agent")
sm = [p for p in smelter if p["group"] == "machines"]
rest = [p for p in smelter if p["group"] != "machines"]
J4 = post("Copper outpost (4/4): smelter, 10 electric furnaces",
          "Craft 10 electric-furnace (10 steel + 5 advanced-circuit + 10 stone-brick each; advanced circuits are "
          "hand-craftable: 2 circuits + 2 plastic + 4 cable). Machines: " + lst(sm) + ". Belts: " +
          runs([p for p in rest if p["item"] == "transport-belt"]) + ". Inserters and poles: " +
          "; ".join(f"{p['item']} {p['x']:g},{p['y']:g}" + (f" {p['direction']}" if p.get('direction') else '')
                    for p in rest if p["item"] != "transport-belt") + ".",
          "entity 28.5,40.5 electric-furnace; entity 44.5,46.5 electric-furnace; belt 30.5,37.5 copper-plate",
          sm[:4] + rest[:4], who="runner2")
print("drills", len(drills), "poles", len(poles), "jobs", J1, J2, J3, J4)
