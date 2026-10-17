"""Post rocket phase 1 as jobs, straight from runtime/blocks/rocket-places.json (no hand-typed tiles)."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
from plan import segments  # noqa: E402

P = json.load(open(os.path.join(AGENT, "..", "runtime", "blocks", "rocket-places.json")))
PY = sys.executable
INTRO = ("ROCKET BLOCK phase 1 (designed and checked tile by tile by blocks/rocket.py; every tile is free on the live map). "
         "Site x -24..48, y 43..64, south-east of the oil plants. Direction = where items go / where the machine faces. "
         "Underground and pipe-to-ground pairs: place the entrance first, then the exit (belt_type=output for belts). "
         "Parts: AM2 in chest 60.5,-17.5; electric furnaces from runner2; inserters/belts/poles from depot 60.5,-15.5 "
         "and chests 44.5,32.5 / 80.5,15.5; craft what is missing.")


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


pw = G("power")
post("Rocket 1/8: power line to the south copper drills (21 small poles)",
     "The 7 copper drills at 83.5,218.5.. have no power. Place small-electric-pole at: " + lst(pw) +
     ". The first 8 link the new smelter poles (built by job 3/8) to the isolated poles 62.5,111.5..65.5,126.5; "
     "the other 13 link those to the drill-field poles 86.5,217.5... Wire reach 7.5: place them in this order.",
     f"entity {pw[0]['x']:g},{pw[0]['y']:g} small-electric-pole; entity {pw[7]['x']:g},{pw[7]['y']:g} small-electric-pole; "
     f"entity {pw[-1]['x']:g},{pw[-1]['y']:g} small-electric-pole", pw, who="runner1")

sb = G("rocketS:belt")
j2 = post("Rocket 2/8: smelter belts (copper ore, plate bus P, iron ore)",
          "Copper ore: the south ore belt ends at 49.5,43.5 going west; extend it: " + runs(G("rocketS:belt OC")) +
          ". Plate bus P (iron lane north, copper lane south once furnaces run): " + runs(G("rocketS:belt P")) +
          ". Iron ore row: " + runs(G("rocketS:belt OI")) + ".",
          "entity 24.5,43.5 transport-belt; entity 23.5,49.5 transport-belt; entity 36.5,55.5 transport-belt", sb, who="runner4")
sm = G("rocketS:machines") + G("rocketS:inserters") + G("rocketS:poles") + G("rocketS:extra")
post("Rocket 3/8: smelter, 6 copper + 4 iron electric furnaces",
     "Electric furnaces (no recipe needed): " + lst(G("rocketS:machines")) + ". Inserters: " + lst(G("rocketS:inserters")) +
     ". Poles: " + lst(G("rocketS:poles") + G("rocketS:extra")) + ".",
     "entity 25.5,46.5 electric-furnace; entity 40.5,46.5 electric-furnace; entity 34.5,52.5 electric-furnace; "
     "drop 25.5,48.5 25.5,49.5; drop 34.5,50.5 34.5,49.5; entity 20.5,52.5 small-electric-pole", sm, who="runner2")
io = G("ironore")
post("Rocket 4/8: iron ore feed from the west ore line to the smelter",
     "The west iron ore line ends full at 14.5,16.5 (belt going south). Extend it: " + runs(io) +
     ". It passes under the y=25 belts, the plastic line y=43.5 and the new bus y=48.5/49.5.",
     "entity 14.5,17.5 transport-belt; entity 14.5,52.5 underground-belt; entity 23.5,55.5 transport-belt", io, who="agent")
bus = G("rocketX:belt B1") + G("rocketX:belt B2") + G("loop1") + G("loop2") + G("plastic")
j5 = post("Rocket 5/8: bus B1/B2, the west loops, plastic+sulfur branch",
          "(1) Plastic/sulfur branch: mine the transport-belt at 10.5,43.5 (keep its items), place splitter 10.5,44 "
          "direction east (covers 10.5,43.5 and 10.5,44.5), then " + runs(G("plastic", "transport-belt")) +
          ". (2) B1 (sulfur north, plastic south) " + runs(G("rocketX:belt B1")) + ". (3) B2 (iron north, copper south) " +
          runs(G("rocketX:belt B2")) + ". (4) West loops: B2 -> " + runs(G("loop2")) + "; B1 -> " + runs(G("loop1")) + ".",
          "entity 10.5,44 splitter; entity 21.5,48.5 transport-belt; entity -21.5,49.5 transport-belt; "
          "entity -22.5,62.5 transport-belt; entity -22.5,63.5 transport-belt", bus, who="hauler")
low = G("rocketX:belt B3") + G("rocketX:belt B4") + G("rocketX:belt B5") + G("rocketX:belt B6")
j6 = post("Rocket 6/8: block belts B3..B6",
          "B3 (LDS north, steel south) " + runs(G("rocketX:belt B3")) + ". B4 (processing units north, cable south) " +
          runs(G("rocketX:belt B4")) + ". B5 (copper north, iron south) " + runs(G("rocketX:belt B5")) +
          ". B6 (plastic north, sulfur south) " + runs(G("rocketX:belt B6")) + ".",
          "entity -21.5,55.5 transport-belt; entity 20.5,56.5 transport-belt; entity 2.5,62.5 transport-belt; "
          "entity 2.5,63.5 transport-belt", low, who="runner3")
A = 52.5
rowA = [p for p in G("rocketX:machines") if p["y"] == A]
insA = [p for p in G("rocketX:inserters") if p["y"] < 55]
pipes = G("rocketX:pipes") + G("water")
post("Rocket 7/8: row A - acid plant, 4 cable AM2, 2 steel furnaces, water and acid pipes",
     "Machines: " + lst(rowA) + " (the chemical plant faces WEST: inputs on its west side, acid out on the east). "
     "Inserters: " + lst(insA) + ". Acid pipes to the processing-unit AM2: " + lst(G("rocketX:pipes")) +
     ". Water from the water column at -23.5,45.5: " + lst(G("water")) + ". Poles: " +
     lst([p for p in G("rocketX:poles") + G("rocketX:extra") if p["y"] < 55]) + ".",
     "recipe -19.5,52.5 sulfuric-acid; recipe -11.5,52.5 copper-cable; recipe 0.5,52.5 copper-cable; "
     "entity 8.5,52.5 electric-furnace; entity -21.5,47.5 pipe-to-ground; entity -15.5,57.5 pipe-to-ground; "
     "drop -11.5,54.5 -11.5,56.5", rowA + insA + pipes, after=j5, who="runner2")
rowB = [p for p in G("rocketX:machines") if p["y"] != A]
insB = [p for p in G("rocketX:inserters") if p["y"] > 55]
post("Rocket 8/8: row B - 2 circuit, red circuit, processing unit, 2 LDS assemblers",
     "Machines (AM2, facing north): " + lst(rowB) + ". The processing-unit AM2 takes acid through its NORTH port from "
     "the pipe-to-ground exit at -15.5,57.5 (job 7/8). Inserters (fast where marked): " + lst(insB) + ". Poles: " +
     lst([p for p in G("rocketX:poles") if p["y"] > 55]) + ".",
     "recipe -19.5,59.5 electronic-circuit; recipe -15.5,59.5 processing-unit; recipe -11.5,59.5 advanced-circuit; "
     "recipe -3.5,59.5 low-density-structure; recipe 0.5,59.5 low-density-structure; entity -17.5,58.5 fast-inserter",
     rowB + insB, after=j6, who="runner3")
