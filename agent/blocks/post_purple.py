"""Post the purple block as jobs, straight from plan.py's compiled placements (no hand-typed tiles)."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
from plan import segments  # noqa: E402

RT = os.path.join(AGENT, "..", "runtime", "blocks")
places = json.load(open(os.path.join(RT, "purple-places.json")))
routes = json.load(open(os.path.join(RT, "purple-routes.json")))
PY = sys.executable
INTRO = ("PURPLE SCIENCE BLOCK (designed and checked tile by tile by plan.py block blocks/purple.json; all tiles "
         "free on the live map). Site x -22..6, y -29..-10, west of the cell-2 columns. Belts flow WEST unless "
         "the line says otherwise; direction = where items go; underground exits are marked (place them with "
         "belt_type=output). Parts: depot 60.5,-15.5 (AM2, inserters, long-handed, undergrounds, splitters, "
         "belts) and chests 44.5,32.5 / 80.5,15.5 (belts); craft what is missing.")


def runs(ps):
    return " | ".join(segments(ps))


def pick(group_prefix):
    return [p for p in places if p["group"].startswith(group_prefix)]


def lst(ps, extra=""):
    out = []
    for p in ps:
        s = f"{p['item']} {p['x']:g},{p['y']:g}"
        if p.get("direction"):
            s += f" {p['direction']}"
        if p.get("recipe"):
            s += f" recipe={p['recipe']}"
        out.append(s)
    return "; ".join(out)


def post(title, brief, accept, place, after=None, who=None):
    args = [PY, "cli.py", "t", "post_job", f"title={title}", f"brief={INTRO} {brief}", f"accept={accept}",
            "priority=2", "place=" + json.dumps([{k: v for k, v in p.items() if k in ("item", "x", "y", "direction")}
                                                 for p in place][:12])]
    if after:
        args.append(f"after={after}")
    if who:
        args.append(f"for={who}")
    env = dict(os.environ, FACTORIO_AUTOSTART="0", FACTORIO_AGENT_NAME="planner")
    out = subprocess.run(args, cwd=AGENT, capture_output=True, text=True, env=env).stdout.strip().splitlines()
    print(out[0] if out else "?")
    head = out[0] if out else ""
    return head[1:head.index(" ")] if head.startswith("#") else None


# split the routes back into their four parts (they were saved in order R2, R3, R1, R4)
def route_part(x0, y0):
    for i, p in enumerate(routes):
        if abs(p["x"] - x0) < 0.01 and abs(p["y"] - y0) < 0.01:
            j = i
            while j + 1 < len(routes) and abs(routes[j + 1]["x"] - routes[j]["x"]) + abs(routes[j + 1]["y"] - routes[j]["y"]) <= 5.01:
                j += 1
            return routes[i:j + 1]
    raise SystemExit(f"route start {x0},{y0} not found")


r_steel, r_plastic, r_bus, r_stone = (route_part(18.5, -17.5), route_part(23.5, -16.5),
                                      route_part(22.5, -10.5), route_part(106.5, -12.5))
bus = pick("belt BUS") + pick("belt extra") + pick("belt U1")
A = post("Purple block (1/5): extend the iron+copper bus west, loop it into U1",
         "The bus y=-10.5 (iron south lane, copper north lane) ends at 25.5,-10.5. (1) Mine fast-inserter "
         "24.5,-10.5 and iron-chest 23.5,-10.5 (keep the copper plate inside: put it in depot 60.5,-15.5). "
         "Place transport-belt direction=west at 24.5,-10.5 and 23.5,-10.5. (2) Bus extension: " + runs(r_bus) +
         ". (3) Block bus, west loop and U1: " + runs(bus) + ". After two right turns the bus becomes U1 "
         "(y=-27.5) flowing EAST with iron on its north lane.",
         "belt 0.5,-10.5 iron-plate; belt -22.5,-18.5 iron-plate; belt -10.5,-27.5 iron-plate",
         r_bus + bus, who="agent")
T = pick("belt T1") + pick("belt T2")
B = post("Purple block (2/5): steel and plastic feeds, belts T1/T2",
         "(1) Belts T1 (y=-20.5) and T2 (y=-21.5): " + runs(T) + ". (2) Steel tap: splitter at x=19 y=-16.5 "
         "direction=north on the steel column x=19.5 (it takes over belt 19.5,-16.5 and adds 18.5,-16.5); "
         "route: " + runs(r_steel) + " (it side-loads steel onto T1's south lane at 2.5,-20.5). (3) Plastic: "
         "fast-inserter 22.5,-16.5 direction=east (from the plastic+sulfur column x=21.5 onto the new belt "
         "23.5,-16.5), configure filters='[\"plastic-bar\"]' filter_mode=whitelist; route: " + runs(r_plastic) +
         " (it side-loads plastic onto T2's south lane at 5.5,-21.5).",
         "belt -10.5,-20.5 steel-plate; belt -10.5,-21.5 plastic-bar; entity 19,-16.5 splitter",
         T + r_steel[:3] + r_plastic[:3], who="runner2")
M = pick("machines")
C = post("Purple block (3/5): 13 machines with recipes",
         "Place and set_recipe (electric-furnace entities need no recipe): " + lst(M) +
         ". Craft 1 electric-furnace item (10 steel + 5 advanced-circuit + 10 stone-brick) for the brick "
         "furnace at -2.5,-24.5.",
         "recipe -18.5,-13.5 copper-cable; recipe -12.5,-17.5 advanced-circuit; recipe -4.5,-17.5 productivity-module; "
         "recipe -10.5,-24.5 production-science-pack; entity -2.5,-24.5 electric-furnace",
         M, after=A)
I = pick("inserters") + pick("poles")
links = [{"item": "small-electric-pole", "x": -9.5, "y": -30.5, "group": "poles"},
         {"item": "small-electric-pole", "x": -7.5, "y": -36.5, "group": "poles"}]
D = post("Purple block (4/5): 33 inserters and 12 poles",
         "Every inserter, its direction and why (from the compiler): " + lst(I + links) +
         ". The last two poles link the block to the outpost pole -5.5,-37.5. base_status x=-9 y=-19 radius=12: "
         "nothing may show no_power.",
         "drop -15.5,-15.5 -15.5,-16.5; drop -17.5,-19.5 -17.5,-20.5; drop -10.5,-22.5 -10.5,-23.5; "
         "working -18.5,-13.5; working -12.5,-17.5",
         I + links, after=C)
U2 = pick("belt U2")
E = post("Purple block (5/5): stone line from the east, U2, purple buffer",
         "(1) Stone collector: transport-belt direction=north at 106.5,-5.5 to 106.5,-11.5 (the three stone "
         "drills at 108.5 drop onto x=106.5). (2) Stone route (113 tiles): " + runs(r_stone) + " (it side-loads "
         "stone onto U2's south lane at 5.5,-28.5). (3) U2: " + runs(U2) + ". (4) Purple buffer at U2's end: "
         "inserter -21.5,-28.5 direction=west and iron-chest -22.5,-28.5 (temporary until the lab feed exists).",
         "belt 50.5,-26.5 stone; belt -10.5,-28.5 stone; entity -22.5,-28.5 iron-chest",
         U2 + r_stone[:4] + [{"item": "iron-chest", "x": -22.5, "y": -28.5}])
print("posted", A, B, C, D, E)
