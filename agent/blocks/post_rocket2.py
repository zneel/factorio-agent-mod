"""Post rocket phase 2 (block Y laid out by CP-SAT) as jobs, straight from runtime/blocks/rocket2-places.json."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
from plan import segments  # noqa: E402

P = json.load(open(os.path.join(AGENT, "..", "runtime", "blocks", "rocket2-places.json")))
PY = sys.executable
KIT = "chest 44.5,32.5"
INTRO = ("ROCKET BLOCK phase 2 (layout found by CP-SAT, routes by A*, all checked tile by tile on the live map). "
         "Block Y: x -19.5..-3.5, y 65.5..86.5, south of the processing-unit block. Direction = where items go / "
         "where the machine faces. Underground and pipe-to-ground pairs: entrance first, then exit (belt_type=output "
         f"for belts). Parts: {KIT} (AM2 kit), depot 60.5,-15.5, chests 44.5,32.5 / 80.5,15.5; craft what is missing.")


def G(prefix, item=None):
    return [p for p in P if p["group"].startswith(prefix) and (item is None or p["item"] == item)]


def lst(ps):
    out = []
    for p in ps:
        s = f"{p['item']} {p['x']:g},{p['y']:g}"
        if p.get("direction"):
            s += f" {p['direction']}"
        if p.get("end"):
            s += f" ({p['end']})"
        if p.get("recipe"):
            s += f" recipe={p['recipe']}"
        out.append(s)
    return "; ".join(out)


def runs(ps):
    return " | ".join(segments(ps))


def post(title, brief, accept, place, after=None, who=None):
    args = [PY, "cli.py", "t", "post_job", f"title={title}", f"brief={INTRO} {brief} ACCEPT: {accept}",
            "priority=1", "place=" + json.dumps([{k: v for k, v in p.items() if k in ("item", "x", "y", "direction")}
                                                 for p in place][:12])]
    if after:
        args.append(f"after={after}")
    if who:
        args.append(f"for={who}")
    env = dict(os.environ, FACTORIO_AUTOSTART="0", FACTORIO_AGENT_NAME="planner")
    out = subprocess.run(args, cwd=AGENT, capture_output=True, text=True, env=env).stdout.strip().splitlines()
    head = out[0] if out else "?"
    print(head[:110])
    return head[1:head.index(" ")] if head.startswith("#") else None


ka = post("Rocket kit A: craft 6 assembling-machine-2 into " + KIT,
          "No AM2 is left anywhere. Craft 6 assembling-machine-2 (each: 1 assembling-machine-1, 3 electronic circuits, "
          f"5 gears, 2 steel) and put them in {KIT}. Iron/steel: store chests 27.5,-12.5 and 32.5,15.5.",
          "has 44.5,32.5 assembling-machine-2 >= 6", [], who="runner4")
kb = post("Rocket kit B: craft 6 assembling-machine-2 + 1 chemical plant into " + KIT,
          f"Craft 6 assembling-machine-2 and 1 chemical-plant (or take the spare from the chest near the oil plants) "
          f"and put them in {KIT}, next to kit A's 6.",
          "has 44.5,32.5 assembling-machine-2 >= 12; has 44.5,32.5 chemical-plant >= 1", [], after=ka, who="runner1")
post("Rocket 7/8 finish: row A cable assemblers and the 2nd steel furnace",
     "Job 7/8 (#370) stopped for parts. Using 4 AM2 from the kit and the electric furnaces in the kit chest, place: "
     "assembling-machine-2 -11.5,52.5 / -7.5,52.5 / -3.5,52.5 / 0.5,52.5 recipe=copper-cable; electric-furnace 4.5,52.5 and "
     "8.5,52.5 (if missing); their inserters as listed in #370 (check each with probe: input inserters on y=50.5 facing south, "
     "long-handed output inserters on y=54.5 facing south).",
     "recipe -11.5,52.5 copper-cable; recipe 0.5,52.5 copper-cable; entity 8.5,52.5 electric-furnace; "
     "drop -11.5,54.5 -11.5,56.5", [], after=ka, who="runner2")
yl = G("yellow")
post("Rocket Y-1: utility-pack belt from block Y to the labs (138 tiles)",
     "From the end of K5 (-3.5,80.5, utility packs on its north lane) to lab 58.5,-15.5: " + runs(yl) +
     ". The last piece is the inserter 58.5,-17.5 facing south into the lab.",
     f"entity {yl[0]['x']:g},{yl[0]['y']:g} transport-belt; entity 58.5,-18.5 transport-belt; drop 58.5,-17.5 58.5,-16.5",
     yl, who="hauler")
fd = G("feedB5") + G("feedB3") + G("feedB4") + G("loopB3")
post("Rocket Y-2: feeds into block Y (iron+copper, steel+LDS, processing units+cable) and the west loop",
     "B5 (copper/iron) from its end 2.5,62.5: " + runs(G("feedB5")) + ". B3 (LDS/steel) from 20.5,55.5: " +
     runs(G("feedB3")) + ". B4 (processing units/cable) from 20.5,56.5: " + runs(G("feedB4")) +
     ". West loop K1 -> K4: " + runs(G("loopB3")) + ".",
     "entity -20.5,73.5 transport-belt; entity -2.5,66.5 transport-belt; entity -20.5,86.5 transport-belt; "
     "entity -21.5,70.5 transport-belt", fd, who="runner3")
kb_ = G("Y:belt")
yb = post("Rocket Y-3: block Y belts K0..K6",
          "; ".join(f"{k}: " + runs(G('Y:belt ' + k)) for k in ("K0", "K1", "K2", "K3", "K4", "K5", "K6")) + ".",
          "entity -19.5,65.5 transport-belt; entity -3.5,73.5 transport-belt; entity -19.5,86.5 transport-belt", kb_, who="agent")
mi = G("Y:machines") + G("Y:inserters") + G("Y:poles") + G("power")
post("Rocket Y-4: block Y machines, inserters, poles",
     "Machines (from the kit chest): " + lst(G("Y:machines")) + ". The battery chemical plant faces EAST (acid enters on "
     "its east side); the electric-engine AM2 faces WEST (lubricant enters on its west side). Inserters (fast where "
     "marked): " + lst(G("Y:inserters")) + ". Poles: " + lst(G("Y:poles") + G("power")) + ".",
     "recipe -17.5,69.5 electric-engine-unit; recipe -9.5,76.5 flying-robot-frame; recipe -9.5,83.5 utility-science-pack; "
     "recipe -5.5,76.5 battery; entity -13.5,76.5 assembling-machine-2", mi, after=kb, who="runner4")
post("Rocket Y-5: lubricant and acid pipes to block Y",
     "Lubricant from the lubricant plant (-41.5,52.5) south port to the electric-engine AM2's west port -19.5,69.5: " +
     lst(G("lube")) + ". Acid from the acid pipes at -17.5..-15.5,53.5 to the battery plant's east port: " + lst(G("acid")) + ".",
     "entity -19.5,69.5 pipe-to-ground; entity -3.5,75.5 pipe", G("lube") + G("acid"), who="runner2")
