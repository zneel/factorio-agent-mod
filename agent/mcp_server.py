"""MCP server (stdio, JSON-RPC 2.0) that lets an AI agent play Factorio through the agent-bridge mod.

Stdlib only. Run:  python mcp_server.py
Env:  FACTORIO_AGENT_NAME  which character this client controls (default "agent"; use different
                           names to run several agents on one map)
      FACTORIO_AUTOSTART   "0" to never launch the headless server automatically
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge, BridgeError  # noqa: E402
import macros  # noqa: E402
import terse  # noqa: E402

SERVER_NAME = "factorio-agent"
VERSION = "0.1.0"
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """\
You are playing Factorio (2.0, base game) as a character on a dedicated map. Goal unless told
otherwise: build up a factory and eventually launch a rocket.

How the world works through these tools:
- Coordinates are map tiles; x grows east, y grows south. Entities sit at tile centers (e.g. 10.5, -3.5)
  for 1x1 and on tile corners for 2x2 (e.g. 10, -3).
- You follow normal player rules: reach 10 tiles for buildings, 2.7 tiles for hand-mining ore,
  build range 10, real walking speed, real mining and crafting times, finite inventory.
- Time: in the default "auto" mode the map is paused whenever none of your actions is running and
  no human is connected, so thinking costs no game time. Machines only run while you act or `wait`.
- Long actions (move_to, mine, craft, wait) block until done and report the inventory change.
  mine walks into reach by itself. Placing, inserting and taking are instant but need reach.
- Edit what is built instead of tearing it down: upgrade places a better tier over a building and keeps
  its contents and recipe; configure sets splitter priority/filter and inserter filters; rotate,
  set_recipe; mine picks a building up (with its contents) to move it.
- Iron/copper plates come from furnaces (smelting can't be hand-crafted). Fuel: wood, coal.
- Early game: mine stone -> craft stone furnaces; burner drills on ore drop straight into a furnace
  at their drop position (a direct pull link); hand-craft gears, then more drills.
- 2.0 starts with trigger technologies (e.g. "craft 50 iron plates") that complete by themselves;
  later tech needs labs fed with science packs.
- You share the map with teammates (other characters). read_chat shows messages from humans and
  teammates; say talks back (to=<name> for one teammate); team shows where everyone is, what they do
  and carry; set_intent posts your current job so nobody builds on the same tiles.

Run the factory like a lean plant (Toyota Production System + theory of constraints):
- The customer is the rocket, reached through research. A machine is only productive while its
  output is being pulled toward that customer. Capacity is lost three ways, and base_status
  reports each with positions:
    blocked  output full: nothing downstream pulls it (missing link or consumer, or overproduction)
    starved  waiting for input: the problem is upstream
    down     no fuel, power, recipe or research
- Jidoka: when base_status shows losses, fix them before building anything new. Find the root
  cause with data (inspect, belt_status; ask "why" until you hit the physical cause), not guesses.
- Pull, don't push: connect each output straight to its consumer (inserter into the next machine,
  belt to the next step). Emptying full furnaces into your inventory is muda unless you are about
  to use those items yourself; the fix for "blocked" is a link or a consumer, or stop feeding it.
- Find the constraint (the step whose machines all work while the steps after it starve) and add
  capacity only there. Capacity anywhere else just piles up inventory and shows up as "blocked".
- Balance to the demand rate with recipe_info. Rates at speed 1: burner drill 0.25 ore/s,
  electric drill 0.5/s, stone furnace 0.3125 plates/s (so 5 burner drills feed 4 furnaces),
  assembler 1 0.5x and assembler 2 0.75x the recipe rate, yellow belt 15 items/s (7.5 per lane),
  1 boiler feeds 2 steam engines. Machines needed = demand/s x craft time / (speed x output).
- Small, capped buffers (kanban): a few minutes of the consumer's use in a chest or belt is
  enough. A belt that belt_status shows backed up to a dead end is a blocked line.
- Muda to cut: walking, hand-hauling, waiting, hand-crafting what an assembler could make,
  hand-mining what drills already produce (take it from their output, a chest or a full belt end),
  overproducing items nothing consumes, hoarding inventory.
- Kaizen loop: base_status (measure) -> fix the single biggest loss -> base_status again. Quote the
  flow % when reporting progress.

Use view_map for spatial layout (one char per tile), look_around for a summary, inspect for one
entity, belt_status for a belt line. Prefer data over guessing.

Replies are terse key=value text: positions "x,y", item counts "name:n,name:n", inv+= is the
inventory change. Save tokens and round trips:
- chain known steps with batch; use collect / refuel / base_status instead of visiting buildings
  one by one;
- keep view_map and look_around radii as small as the question needs.
"""

ENTITY_REF = {
    "x": {"type": "number", "description": "x of the entity (any tile it covers)"},
    "y": {"type": "number", "description": "y of the entity (any tile it covers)"},
    "id": {"type": "integer", "description": "entity id (unit number) instead of x/y"},
    "name": {"type": "string", "description": "optional entity name to disambiguate"},
}


def obj(props=None, required=None):
    s = {"type": "object", "properties": props or {}}
    if required:
        s["required"] = required
    return s


def fmt_map(res):
    x0, y0 = res["origin"]["x"], res["origin"]["y"]
    w = len(res["rows"][0]) if res["rows"] else 0
    ruler = "".join(str((x0 + i) % 10) if (x0 + i) % 5 == 0 else " " for i in range(w))
    lines = [f"x0={x0} (col i = x0+i), rows labeled by y", "        " + ruler]
    for i, row in enumerate(res["rows"]):
        lines.append(f"{y0 + i:>7} {row}")
    legend = {"@": "you", ".": "empty ground", "~": "water", "t": "tree", "r": "rock", "#": "cliff",
              "*": "item on ground", "i": "iron-ore", "c": "copper-ore", "k": "coal", "s": "stone",
              "u": "uranium-ore", "o": "crude-oil", "^>v<": "belt direction", "P": "other character",
              "!": "enemy"}
    legend.update(res.get("legend") or {})
    seen = set("".join(res["rows"])) - {"."}
    legend = {k: v for k, v in legend.items() if any(c in seen for c in k)}
    lines.append(" ".join(f"{k}={v}" for k, v in legend.items()))
    return "\n".join(lines)


def compact_nearby(n):
    groups, odd = {}, []
    for e in n.pop("buildings", []):
        g = groups.setdefault(e["name"], {})
        st = e.get("status") or "-"
        g[st] = g.get(st, 0) + 1
        if macros.flow_class(e) != "working":
            odd.append(f"{e['name']}@{terse.val(e['pos'])}={e['status']}")
    n["buildings"] = {k: ",".join(f"{s}:{c}" for s, c in g.items()) for k, g in groups.items()}
    if odd:
        n["problems"] = " ".join(odd[:15])
    for r in n.get("resources", {}).values():
        r.pop("dist", None)
    n.pop("center", None)
    return n


PACKS = {"automation-science-pack": "red", "logistic-science-pack": "green", "military-science-pack": "black",
         "chemical-science-pack": "blue", "production-science-pack": "purple",
         "utility-science-pack": "yellow", "space-science-pack": "white"}


def compact_research(r, limit=12):
    def one(t):
        if t.get("trigger"):
            cost = "trigger: " + t["trigger"]
        else:
            cost = f"{t.get('units')}x" + "+".join(PACKS.get(k, k) for k in (t.get("unit_cost") or {}))
        return f"{t['name']} [{cost}] -> {','.join(t.get('unlocks') or []) or '-'}"
    av = r.get("available", [])
    lines = [f"current={r.get('current') or '-'} progress={r.get('progress', 0)} queue={','.join(r.get('queue') or []) or '-'} researched={r.get('researched_count')} pending={','.join(r.get('pending') or []) or '-'}",
             "available (cheapest first):"] + [one(t) for t in av[:limit]]
    if len(av) > limit:
        lines.append(f"+{len(av) - limit} more")
    return "\n".join(lines)


OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}


def flow_dir(a):
    """Engine inserter direction names the pickup side; tools promise 'direction = where items go'."""
    d = a.get("direction") or "north"
    return OPPOSITE[d] if "inserter" in a["item"] else d


def ref_args(a):
    return {k: a.get(k) for k in ("x", "y", "id", "name")}


TOOLS = []


def tool(name, description, schema, handler):
    TOOLS.append({"name": name, "description": description, "inputSchema": schema, "handler": handler})


# ---- observation
tool("get_state", "Your character (position, health, inventory, crafting queue, current task), game tick, "
     "pause/time mode, current research and unread chat count.",
     obj(), lambda b, a: macros.compact_state(b.call("state")))
tool("look_around", "Summary of everything within a radius: your buildings (with status/recipe), resource "
     "tiles by type, nearest tree/rock, water, other entities.",
     obj({"radius": {"type": "number", "default": 16, "description": "1-64"},
          "x": {"type": "number"}, "y": {"type": "number"}}),
     lambda b, a: compact_nearby(b.call("nearby", radius=a.get("radius"), x=a.get("x"), y=a.get("y"))))
tool("view_map", "ASCII top-down map around you (or x,y), one character per tile, with a legend. Best way "
     "to plan building layouts.",
     obj({"radius": {"type": "integer", "default": 12, "description": "3-40"},
          "x": {"type": "number"}, "y": {"type": "number"}}),
     lambda b, a: fmt_map(b.call("map", radius=a.get("radius"), x=a.get("x"), y=a.get("y"))))
tool("find_resources", "Find ore/stone/coal/oil patches: tile count, total amount, center, bounding box and "
     "the nearest tile to you. Also reports the nearest water.",
     obj({"radius": {"type": "number", "default": 80, "description": "8-300"},
          "name": {"type": "string", "description": "only this resource, e.g. iron-ore"}}),
     lambda b, a: b.call("resources", radius=a.get("radius"), name=a.get("name")))
tool("find_entities", "Nearest entities by name (e.g. stone-furnace, 'tree', 'rock') or by type "
     "(e.g. assembling-machine).",
     obj({"name": {"type": "string"}, "type": {"type": "string"},
          "radius": {"type": "number", "default": 64}, "limit": {"type": "integer", "default": 10}}),
     lambda b, a: b.call("find", name=a.get("name"), type=a.get("type"), radius=a.get("radius"),
                         limit=a.get("limit")))
tool("inspect", "Full details of one entity: status, recipe, inventories, fuel, fluids, drop/pickup positions; "
     "for belts the items and fill % of each lane.",
     obj(ENTITY_REF), lambda b, a: b.call("inspect", **ref_args(a)))


def _items(v):
    return terse.val(v) if isinstance(v, dict) and v else "none"


def _list(v):
    return v if isinstance(v, list) else []  # Factorio encodes an empty Lua table as {}


def fmt_belt(r):
    lines = [f"{terse.val(r['start'])} -> {terse.val(r['end'])} belts={r['tiles']} end={r['end_reason']} "
             f"fill={r['fill']} (L {r['left']}, R {r['right']}) max={r['max_rate']}",
             f"items={_items(r.get('items'))}",
             f"L |{r['profile_left']}|", f"R |{r['profile_right']}|"]
    if len(_list(r.get("path"))) > 2:
        lines.append("path=" + terse.val(r["path"]))
    backed = r.get("backed_up") or {}
    if backed:
        lines.append("backed_up: " + "; ".join(f"{'L' if k == 'left' else 'R'} {v}" for k, v in backed.items()))
    for key in ("empty", "splits", "sideloads"):
        v = _list(r.get(key))
        if v:
            extra = f" +{len(v) - 6} more" if len(v) > 6 else ""
            lines.append(f"{key}: " + "; ".join(v[:6]) + extra)
    return "\n".join(lines)


tool("belt_status", "Follow a belt line downstream from x,y (through undergrounds, splitters and side-loads) "
     "and summarise it: items carried, fill % overall and per lane (L/R as seen along the flow), where it "
     "ends, where it is backed up (full up to a dead end) and empty stretches. Profiles show one char per belt "
     "along the path: . empty, 1-8 tenths full, # 90%+ full.",
     obj({"x": {"type": "number"}, "y": {"type": "number"}, "id": {"type": "integer"},
          "max_tiles": {"type": "integer", "default": 200, "description": "belts to follow, 1-1000"}}),
     lambda b, a: fmt_belt(b.call("belt_status", x=a.get("x"), y=a.get("y"), id=a.get("id"),
                                  max_tiles=a.get("max_tiles"))))

# ---- knowledge
tool("recipe_info", "Ingredients, products and crafting time of a recipe, whether it is hand-craftable and how "
     "many you can craft now. Also lists other recipes that produce the item.",
     obj({"name": {"type": "string", "description": "recipe or item name"}}, ["name"]),
     lambda b, a: b.call("recipe", name=a["name"]))
tool("list_recipes", "Unlocked recipes. Hand-craftable ones show how many you could craft now.",
     obj({"craftable_now": {"type": "boolean", "default": False},
          "include_machine_recipes": {"type": "boolean", "default": False}}),
     lambda b, a: b.call("recipes", craftable_now=a.get("craftable_now"),
                         handcraft_only=not a.get("include_machine_recipes", False)))
tool("research_status", "Current research and progress, queue, and technologies available to research next "
     "(with cost, unlocks, or the trigger that completes them).",
     obj(), lambda b, a: compact_research(b.call("research_status")))
tool("tech_info", "Details for one technology including prerequisites.",
     obj({"name": {"type": "string"}}, ["name"]), lambda b, a: b.call("tech", name=a["name"]))

# ---- actions
tool("fluid_ports", "Before placing a fluid entity (pumpjack, refinery, chemical plant, boiler, storage tank, "
     "offshore pump...), get the exact tiles where pipes must go for a given position and direction. With "
     "recipe, input/output boxes get a GUESSED fluid label (verify with inspect after set_recipe: placed entities "
     "show real filters in fluid_ports, linked=yes when connected).",
     obj({"name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
          "direction": {"type": "string", "enum": ["north", "east", "south", "west"], "default": "north"},
          "recipe": {"type": "string"}}, ["name", "x", "y"]),
     lambda b, a: b.call("fluid_ports", name=a["name"], x=a["x"], y=a["y"], direction=a.get("direction"),
                         recipe=a.get("recipe")))
tool("move_to", "Walk to a position (pathfinding around obstacles). Blocks until arrived.",
     obj({"x": {"type": "number"}, "y": {"type": "number"},
          "radius": {"type": "number", "default": 1, "description": "how close is close enough"}}, ["x", "y"]),
     lambda b, a: b.run("move_to", x=a["x"], y=a["y"], radius=a.get("radius")))
tool("mine", "Mine by hand: ore/stone/coal tiles, trees, rocks, or pick up one of your buildings (returns it "
     "and its contents). Give x,y of the target, or just a name ('iron-ore', 'tree', 'rock', 'coal') for the "
     "nearest one. Walks into reach automatically. count = units of ore, or number of trees/rocks.",
     obj({"x": {"type": "number"}, "y": {"type": "number"}, "name": {"type": "string"},
          "count": {"type": "integer", "default": 1}}),
     lambda b, a: b.run("mine", x=a.get("x"), y=a.get("y"), name=a.get("name"), count=a.get("count"),
                        timeout=600))
tool("craft", "Hand-craft a recipe (intermediates such as gears are crafted automatically if you have the raw "
     "materials). wait=true blocks until the crafting queue is empty.",
     obj({"recipe": {"type": "string"}, "count": {"type": "integer", "default": 1},
          "wait": {"type": "boolean", "default": True}}, ["recipe"]),
     lambda b, a: b.run("craft", recipe=a["recipe"], count=a.get("count"), wait=a.get("wait"), timeout=600))
tool("place", "Place a building from your inventory at x,y (within build range 10). direction: north/east/"
     "south/west (drills and inserters output toward it; belts move toward it). Reports ore under drills "
     "and what a drill drops into.",
     obj({"item": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
          "direction": {"type": "string", "enum": ["north", "east", "south", "west"], "default": "north"},
          "belt_type": {"type": "string", "enum": ["input", "output"],
                        "description": "underground belts only"}}, ["item", "x", "y"]),
     lambda b, a: b.call("place", item=a["item"], x=a["x"], y=a["y"], direction=flow_dir(a),
                         belt_type=a.get("belt_type")))
tool("insert_items", "Put items from your inventory into an entity in reach (fuel goes to the fuel slot, ore "
     "into furnaces, etc.). Omit count to insert everything you have.",
     obj(dict(ENTITY_REF, item={"type": "string"}, count={"type": "integer"},
              inventory={"type": "string", "description": "optional defines.inventory name, e.g. fuel, "
                                                          "furnace_source, assembling_machine_input, chest"}),
         ["item"]),
     lambda b, a: b.call("insert", item=a["item"], count=a.get("count"), inventory=a.get("inventory"),
                         **ref_args(a)))
tool("take_items", "Take items out of an entity in reach (furnace output, chest, drill...). Omit item to take "
     "everything, omit count to take all of it.",
     obj(dict(ENTITY_REF, item={"type": "string"}, count={"type": "integer"},
              inventory={"type": "string"})),
     lambda b, a: b.call("take", item=a.get("item"), count=a.get("count"), inventory=a.get("inventory"),
                         **ref_args(a)))
tool("set_recipe", "Set the recipe of an assembling machine in reach.",
     obj(dict(ENTITY_REF, recipe={"type": "string"}), ["recipe"]),
     lambda b, a: b.call("set_recipe", recipe=a["recipe"], **ref_args(a)))
tool("rotate", "Rotate an entity in reach (clockwise, or counter-clockwise with reverse).",
     obj(dict(ENTITY_REF, reverse={"type": "boolean", "default": False})),
     lambda b, a: b.call("rotate", reverse=a.get("reverse"), **ref_args(a)))
tool("upgrade", "Upgrade a building in place by placing a better one over it (stone -> steel furnace, yellow -> "
     "red belt, inserter or assembler tiers, ...). Contents, recipe and settings are kept and the old "
     "building returns to your inventory. Needs the new item and build range 10. direction is optional "
     "(default: keep).",
     obj({"item": {"type": "string", "description": "the new building's item"},
          "x": {"type": "number"}, "y": {"type": "number"}, "id": {"type": "integer"},
          "direction": {"type": "string", "enum": ["north", "east", "south", "west"]}}, ["item"]),
     lambda b, a: b.call("upgrade", item=a["item"], x=a.get("x"), y=a.get("y"), id=a.get("id"),
                         direction=flow_dir(a) if a.get("direction") else None))
tool("configure", "Change GUI settings of an entity in reach. Splitter: input_priority / output_priority "
     "(left, right, none) and filter (item name, or 'none' to clear; filtered items go to the output "
     "priority side, the rest to the other). Inserter: filters (list of up to 5 items, [] to clear) and "
     "filter_mode (whitelist or blacklist). Omitted settings stay; inspect shows current settings.",
     obj(dict(ENTITY_REF,
              input_priority={"type": "string", "enum": ["left", "right", "none"]},
              output_priority={"type": "string", "enum": ["left", "right", "none"]},
              filter={"type": "string"},
              filters={"type": "array", "items": {"type": "string"}},
              filter_mode={"type": "string", "enum": ["whitelist", "blacklist"]})),
     lambda b, a: b.call("configure", input_priority=a.get("input_priority"),
                         output_priority=a.get("output_priority"), filter=a.get("filter"),
                         filters=a.get("filters"), filter_mode=a.get("filter_mode"), **ref_args(a)))
tool("start_research", "Queue a technology for research (labs must be supplied with science packs).",
     obj({"name": {"type": "string"}}, ["name"]), lambda b, a: b.call("research", name=a["name"]))
tool("wait", "Let game time pass (machines run, furnaces smelt) without doing anything.",
     obj({"seconds": {"type": "number", "description": "game seconds, max 300"}}, ["seconds"]),
     lambda b, a: b.run("wait", seconds=min(float(a["seconds"]), 300), timeout=900))
tool("wait_for_task", "Keep waiting for a task that was still running when a call returned.",
     obj({"task_id": {"type": "integer"}}, ["task_id"]), lambda b, a: wait_task(b, a["task_id"]))
tool("cancel_task", "Stop the current action (walking, mining, waiting).",
     obj(), lambda b, a: b.call("cancel"))
tool("cancel_crafting", "Empty the hand-crafting queue (ingredients are returned).",
     obj(), lambda b, a: b.call("cancel_crafting"))

# ---- macros: one call instead of many round trips
tool("batch", "Run several tool calls in order in ONE call (saves round trips). Stops at the first error "
     "unless continue_on_error. Example steps: [{\"tool\":\"craft\",\"args\":{\"recipe\":\"pipe\",\"count\":5}},"
     "{\"tool\":\"place\",\"args\":{\"item\":\"boiler\",\"x\":3,\"y\":4,\"direction\":\"east\"}}]",
     obj({"steps": {"type": "array", "items": obj({"tool": {"type": "string"}, "args": {"type": "object"}},
                                                 ["tool"])},
          "continue_on_error": {"type": "boolean", "default": False}}, ["steps"]),
     lambda b, a: run_batch(b, a))
tool("collect", "Walk around your buildings within radius and take their output (furnace plates, chest "
     "contents). Give item to take only that item from any inventory. Hand-hauling is a stopgap: "
     "use it when you need the items yourself, not to unblock machines (link them to a consumer).",
     obj({"item": {"type": "string"}, "radius": {"type": "number", "default": 32},
          "x": {"type": "number"}, "y": {"type": "number"}}),
     lambda b, a: macros.collect(b, a.get("item"), a.get("radius", 32), a.get("x"), a.get("y")))
tool("refuel", "Top up every burner building (drills, furnaces, boilers, burner inserters) within radius to "
     "`amount` fuel, walking as needed.",
     obj({"fuel": {"type": "string", "default": "coal"}, "amount": {"type": "integer", "default": 5},
          "radius": {"type": "number", "default": 32}, "x": {"type": "number"}, "y": {"type": "number"}}),
     lambda b, a: macros.refuel(b, a.get("fuel", "coal"), a.get("amount", 5), a.get("radius", 32),
                                a.get("x"), a.get("y")))
tool("base_status", "Factory flow check: % of machines working, then every capacity loss with positions: "
     "blocked (output full = not pulled downstream / overproducing), starved (input missing upstream), "
     "down (fuel/power/recipe). Run it before and after every change.",
     obj({"radius": {"type": "number", "default": 48}, "x": {"type": "number"}, "y": {"type": "number"}}),
     lambda b, a: macros.base_status(b, a.get("radius", 48), a.get("x"), a.get("y")))


tool("belt_line", "Lay transport belts along orthogonal waypoints (tile centers like 10.5,3.5); each belt "
     "points to the next tile. Walks as needed. Craft enough belts first.",
     obj({"points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                     "description": "[[x,y],[x,y],...] corners of the route"},
          "item": {"type": "string", "default": "transport-belt"}}, ["points"]),
     lambda b, a: macros.belt_line(b, a["points"], a.get("item", "transport-belt")))
tool("place_row", "Place `count` copies of an item from x,y stepping dx,dy (e.g. a row of drills or "
     "furnaces), walking as needed; optional recipe for assemblers.",
     obj({"item": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
          "count": {"type": "integer"}, "dx": {"type": "number", "default": 0},
          "dy": {"type": "number", "default": 0},
          "direction": {"type": "string", "enum": ["north", "east", "south", "west"], "default": "north"},
          "recipe": {"type": "string"}}, ["item", "x", "y", "count"]),
     lambda b, a: macros.place_row(b, a["item"], a["x"], a["y"], a["count"], a.get("dx", 0), a.get("dy", 0),
                                   flow_dir(a), a.get("recipe")))
tool("pole_line", "Run electric poles from x1,y1 to x2,y2 (x first, then y), every 6 tiles, nudging around "
     "obstacles. Start next to an existing powered pole.",
     obj({"x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"},
          "y2": {"type": "number"}, "item": {"type": "string", "default": "small-electric-pole"}},
         ["x1", "y1", "x2", "y2"]),
     lambda b, a: macros.pole_line(b, a["x1"], a["y1"], a["x2"], a["y2"], a.get("item", "small-electric-pole")))


def run_batch(b, a):
    lines = []
    for i, step in enumerate(a.get("steps") or [], 1):
        name = step.get("tool")
        h = HANDLERS.get(name)
        if not h or name == "batch":
            lines.append(f"{i} {name}: unknown tool")
            break
        try:
            res = h(b, step.get("args") or {})
            failed = isinstance(res, dict) and res.get("state") == "failed"
            if isinstance(res, dict) and res.get("state") in ("done", "failed"):
                res.pop("id", None)
            lines.append(f"{i} {name}: {terse.render(res)}")
        except BridgeError as e:
            failed = True
            lines.append(f"{i} {name}: error: {e}")
        if failed and not a.get("continue_on_error"):
            lines.append(f"stopped; {len(a['steps']) - i} step(s) not run")
            break
    return "\n".join(lines)


# ---- social / meta
def fmt_chat(r):
    msgs = r.get("messages") if isinstance(r, dict) else None
    if not isinstance(msgs, list) or not msgs:
        return "no new messages"
    return "\n".join(f"{m['from']}{' > ' + m['to'] if m.get('to') else ''}: {m['message']}" for m in msgs)


def fmt_team(r):
    lines = []
    members = r.get("members") if isinstance(r.get("members"), dict) else {}
    for name, m in members.items():
        if m.get("dead"):
            lines.append(f"{name}: dead")
            continue
        doing = m.get("task") or (f"idle {m['idle_s']}s" if "idle_s" in m else "idle")
        parts = [f"{name}{' (you)' if m.get('you') else ''} @{terse.val(m.get('pos'))} {doing}"]
        if m.get("intent"):
            parts.append(f"intent: {m['intent']}")
        if isinstance(m.get("carries"), dict) and m["carries"]:
            parts.append(f"carries {terse.val(m['carries'])} free={m.get('free_slots')}")
        lines.append(" | ".join(parts))
    recent = r.get("recent_chat")
    if isinstance(recent, list) and recent:
        lines.append("recent chat:")
        lines += [f"  {m['from']}{' > ' + m['to'] if m.get('to') else ''}: {m['message']}" for m in recent]
    return "\n".join(lines) or "no characters"


tool("say", "Talk in game chat and above your character. Teammates (the other characters) read it with "
     "read_chat, so use it to coordinate: hand-offs, requests, warnings. to=<name> addresses one teammate "
     "(others and humans still see it in game).",
     obj({"message": {"type": "string"}, "to": {"type": "string", "description": "teammate name; omit for all"}},
         ["message"]),
     lambda b, a: b.call("say", message=a["message"], to=a.get("to")))
tool("read_chat", "New messages since your last call, from humans and from teammates (to everyone or to you).",
     obj(), lambda b, a: fmt_chat(b.call("chat")))
tool("team", "Every character: position, current task or idle time, posted intent and what it carries, "
     "plus the last chat lines. Check it before claiming work or asking a teammate for items.",
     obj(), lambda b, a: fmt_team(b.call("team")))
tool("set_intent", "Post what you are working on and where (e.g. 'building 3 labs at 64,-6'), so teammates "
     "don't collide. Shown in team and above your character. Empty text clears it.",
     obj({"text": {"type": "string"}}, ["text"]), lambda b, a: b.call("set_intent", text=a["text"]))


def fmt_job(j):
    if not isinstance(j, dict):
        return "no job"
    head = f"#{j['id']} [{j.get('state')}] p{j.get('priority')} {j.get('title')}"
    extra = []
    if j.get("for"):
        extra.append(f"for={j['for']}")
    if j.get("by"):
        extra.append(f"by={j['by']}")
    if j.get("after"):
        extra.append(f"after=#{j['after']}")
    if j.get("age_s") is not None:
        extra.append(f"{j['age_s']}s ago")
    if j.get("note"):
        extra.append(f"note: {j['note']}")
    s = head + (" (" + ", ".join(extra) + ")" if extra else "")
    if j.get("brief") and j.get("state") in ("open", "claimed"):
        s += "\n    " + j["brief"]
    return s


def fmt_jobs(r):
    jobs = r.get("jobs") if isinstance(r, dict) else None
    if not isinstance(jobs, list) or not jobs:
        return "job board empty"
    return "\n".join(fmt_job(j) for j in jobs)


def fmt_claim(r):
    if isinstance(r, dict) and isinstance(r.get("job"), dict):
        return ("resumed " if r.get("resumed") else "claimed ") + fmt_job(r["job"])
    n = r.get("open_for_others", 0) if isinstance(r, dict) else 0
    return f"no job for you ({n} open for others)"


def _post_job(b, a):
    import crew  # lazy: crew imports this module
    brief = a.get("brief") or ""
    if a.get("accept"):
        brief = brief.rstrip() + "\nACCEPT: " + a["accept"]
    err = crew.vet_job(b, brief, a.get("place"))
    if err:
        return "not posted: " + err
    return fmt_job(b.call("post_job", title=a["title"], brief=brief, priority=a.get("priority"),
                          after=a.get("after"), **({"for": a["for"]} if a.get("for") else {}),
                          **{"from": b.agent}))  # stamp the poster, so the result is reported back to them


def _finish_job(b, a):
    import crew
    return crew.finish_checked(b, a["id"], a.get("state") or "done", a.get("note"))


tool("post_job", "Put a job on the board for the crew. title = one line; brief = the full worker brief "
     "(positions, counts, source of materials). accept = machine-checkable checks, ';'-separated, required: "
     "working X,Y | status X,Y S | entity X,Y NAME | recipe X,Y R | has X,Y ITEM >= N | belt X,Y ITEM | "
     "drop X,Y DX,DY | count ITEM >= N (or end the brief with 'ACCEPT: ...'). place = optional list of "
     "{item,x,y,direction} the job will build; each is checked with can_place and the job is refused if a "
     "tile is blocked. for = one character (default anyone), priority 1 urgent .. 5 low (default 3), "
     "after = id of a job that must be done first.",
     obj({"title": {"type": "string"}, "brief": {"type": "string"}, "accept": {"type": "string"},
          "place": {"type": "array", "items": {"type": "object"}}, "for": {"type": "string"},
          "priority": {"type": "integer"}, "after": {"type": "integer"}}, ["title"]),
     _post_job)
tool("can_place", "Can ITEM be built at x,y facing direction? Returns ok, or the blockers on those tiles "
     "(and whether an in-place upgrade is possible). Read-only; spawns no character.",
     obj({"item": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
          "direction": {"type": "string"}}, ["item", "x", "y"]),
     lambda b, a: b.call("can_place", item=a["item"], x=a["x"], y=a["y"], direction=a.get("direction")))
tool("probe", "Everything on one tile: name, status, direction, recipe, inserter pickup/drop, items in all "
     "inventories and belt lanes. Read-only; spawns no character.",
     obj({"x": {"type": "number"}, "y": {"type": "number"}}, ["x", "y"]),
     lambda b, a: b.call("probe", x=a["x"], y=a["y"]))
def _plant_status(b, a):
    import crew  # crew imports this module's formatters; import lazily to avoid a cycle
    return crew.overview(b)


def _production(b, a):
    p = b.call("production", window=a.get("window"), min_rate=a.get("min_rate"))
    rows = []
    for kind in ("items", "fluids"):
        d = p.get(kind) if isinstance(p.get(kind), dict) else {}
        for n, v in sorted(d.items(), key=lambda kv: -max(kv[1]["made"], kv[1]["used"])):
            if a.get("item") and a["item"] not in n:
                continue
            rows.append(f"{n} {v['made']:g}/{v['used']:g}")
    return f"per minute made/used over {p.get('window')}:\n" + "\n".join(rows[:40] or ["nothing"])


tool("production", "Real throughput from the game's production statistics: per-minute made/used for every "
     "item and fluid over a window (five_seconds, one_minute, ten_minutes, one_hour). Used > made means a "
     "shortfall. Read-only; spawns no character.",
     obj({"window": {"type": "string", "default": "ten_minutes"},
          "item": {"type": "string", "description": "only names containing this"},
          "min_rate": {"type": "number", "default": 0.5}}),
     _production)
tool("plant_status", "Planner's game state in one read-only call: alerts first (research stopped or short "
     "queue, idle characters without a job, board running low, blocked jobs awaiting a decision, power, "
     "machines without recipe), then flow %, losses ranked by machines x closeness to labs, research, "
     "crew and job board. Use it to prioritise and post jobs. Spawns no character.",
     obj(), _plant_status)
tool("jobs", "The job board: open, claimed and blocked jobs plus the last few finished (all=true for "
     "everything).",
     obj({"all": {"type": "boolean"}, "recent": {"type": "integer", "default": 5}}),
     lambda b, a: fmt_jobs(b.call("jobs", all=a.get("all"), recent=a.get("recent"))))
tool("claim_job", "Take the next job for your character (yours first, then by priority and age). If you "
     "already hold one, it is returned again so an interrupted job resumes.",
     obj(), lambda b, a: fmt_claim(b.call("claim_job")))
tool("finish_job", "Close the job you hold: state=done, or blocked with a note saying what is missing. "
     "state=done runs the job's ACCEPT checks first; if one fails the job is not closed and you get the "
     "measured values: fix and finish again, or finish blocked. Posts the result to chat.",
     obj({"id": {"type": "integer"}, "state": {"type": "string", "enum": ["done", "blocked"], "default": "done"},
          "note": {"type": "string"}}, ["id"]),
     _finish_job)
tool("update_job", "Planner edit of a job: reopen (state=open), cancel (state=cancelled), or change "
     "priority, brief, title, owner (for=name or any).",
     obj({"id": {"type": "integer"}, "state": {"type": "string",
                                                "enum": ["open", "claimed", "done", "blocked", "cancelled"]},
          "priority": {"type": "integer"}, "brief": {"type": "string"}, "title": {"type": "string"},
          "for": {"type": "string"}, "note": {"type": "string"}}, ["id"]),
     lambda b, a: fmt_job(b.call("update_job", id=a["id"], state=a.get("state"), priority=a.get("priority"),
                                 brief=a.get("brief"), title=a.get("title"), note=a.get("note"),
                                 **({"for": a["for"]} if a.get("for") else {}))))
tool("respawn", "Respawn after your character died.", obj(), lambda b, a: b.call("respawn"))
tool("set_time_mode", "auto: paused while you think and no human is online (default); turn: always paused "
     "while idle; realtime: never paused.",
     obj({"mode": {"type": "string", "enum": ["auto", "turn", "realtime"]}}, ["mode"]),
     lambda b, a: b.call("set_mode", mode=a["mode"]))
tool("set_game_speed", "Game speed multiplier (1 = normal, up to 20). Higher speeds make long actions finish "
     "sooner in wall-clock time.",
     obj({"speed": {"type": "number"}}, ["speed"]), lambda b, a: b.call("set_speed", speed=a["speed"]))


def wait_task(b, tid):
    import time
    deadline = time.time() + 300
    while time.time() < deadline:
        t = b.call("task", id=tid)
        if t.get("state") != "running":
            return t
        time.sleep(0.2)
    return b.call("task", id=tid)


HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Server:
    def __init__(self):
        self.bridge = Bridge(os.environ.get("FACTORIO_AGENT_NAME", "agent"),
                             autostart=os.environ.get("FACTORIO_AUTOSTART", "1") != "0")

    def handle(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            want = params.get("protocolVersion")
            return {
                "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            h = HANDLERS.get(name)
            if not h:
                return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
            try:
                res = h(self.bridge, args)
                failed = isinstance(res, dict) and res.get("state") == "failed"
                if isinstance(res, dict) and res.get("state") in ("done", "failed"):
                    res.pop("id", None)
                text = terse.render(res)
                return {"content": [{"type": "text", "text": text}], "isError": failed}
            except BridgeError as e:
                return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            except Exception as e:  # keep the server alive whatever happens
                log(traceback.format_exc())
                return {"content": [{"type": "text", "text": f"internal error: {e}"}], "isError": True}
        raise KeyError(method)

    def serve(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if "id" not in msg:
                continue  # notification (initialized, cancelled, ...)
            try:
                result = self.handle(msg)
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except KeyError:
                self.send({"jsonrpc": "2.0", "id": msg["id"],
                           "error": {"code": -32601, "message": f"method not found: {msg.get('method')}"}})

    @staticmethod
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdin.reconfigure(encoding="utf-8")
    Server().serve()
