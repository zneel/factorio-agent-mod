"""The growth controller's model: the space plan as groups (Plan), the live game (Live), what a plan piece is
placed as given the research (materialize), and the run's log."""
import collections
import json
import math
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
AGENT = HERE.parent
sys.path.insert(0, str(AGENT))


LOG = JL = None          # grow.log (one line per event) and grow.jsonl (one record per increment): set_log()
_lock = threading.Lock()


def set_log(out):
    global LOG, JL
    LOG, JL = out / "grow.log", out / "grow.jsonl"


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    with _lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def record(d):
    with _lock, open(JL, "a", encoding="utf-8") as f:
        f.write(json.dumps(d) + "\n")


SMELT_OUT = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "iron-plate": "steel-plate", "stone": "stone-brick"}
ELECTRIC = ("inserter", "assembling-machine", "electric-mining-drill", "lab", "electric-furnace", "chemical-plant",
            "oil-refinery", "pumpjack", "rocket-silo")
MACHINES = ("assembling-machine", "furnace", "mining-drill", "lab", "boiler", "steam-engine", "offshore-pump",
            "chemical-plant", "oil-refinery", "pumpjack", "rocket-silo")
ASM1_CATS = {"crafting", "basic-crafting", "advanced-crafting"}
POLE_ITEMS = ("small-electric-pole", "medium-electric-pole")
WORLD_TRANSPORT = re.compile(r"^trunk:")   # outputs are walked onto their trunk (to the bus head), nothing further
BAD = {"no_power", "low_power", "no_fuel", "disabled_by_control_behavior", "marked_for_deconstruction",
       "no_recipe", "fluid_ingredient_shortage", "item_ingredient_shortage", "no_ingredients",
       "missing_required_fluid", "no_input_fluid", "missing_science_packs", "waiting_for_source_items"}
OK = {"working", "normal", "full_output", "waiting_for_space_in_destination", "output_full",
      "no_minable_resources", "waiting_for_target_to_be_built", "waiting_to_launch_rocket", "preparing_rocket_for_launch",
      "launching_rocket", "waiting_for_more_items", "fully_charged"}


def is_machine(p):
    return any(k in p["item"] for k in MACHINES) or (p["item"] == "iron-chest" and p.get("mall"))


# --------------------------------------------------------------------------------------------- the space plan
class Plan:
    """plan.json as groups of placements with what each group makes and needs, its techs, and the flow graph."""

    def __init__(self, seed, gd, dem, path=None):
        from auto import flow, mapdata, ports
        out = mapdata.out_dir(seed)
        raw = json.loads(Path(path or out / "plan.json").read_text())
        places = [dict(p) for p in raw["complex"] + raw["world"] if not p.get("remove") and p.get("grp") != "poles"]
        self.gd, self.dem = gd, dem
        self.p = places
        self._retag(places)
        self.flow = flow.Flow(places, ports.ports)
        self.groups = collections.defaultdict(list)
        for i, p in enumerate(places):
            self.groups[p.get("grp", "?")].append(i)
        self._split_power()
        self.made, self.need, self.techs = {}, {}, {}
        for g, idx in self.groups.items():
            self.made[g], self.need[g] = self._io(g, [places[i] for i in idx])
            self.techs[g] = self._techs([places[i] for i in idx])
        self.prod = {g for g, idx in self.groups.items() if any(not self.flow.transport(i) for i in idx)}
        self.makers = collections.defaultdict(list)
        for g in self.prod:
            for it in self.made[g]:
                self.makers[it].append(g)
        # potential producers of each group (static: nothing built), for buildability and the upstream walk
        self.deps = {}
        for g in self.prod:
            _, prods = self.flow.upstream(set(self.groups[g]), set())
            self.deps[g] = {self.p[j].get("grp") for j in prods} - {g}
        self.tiles = {}
        from auto import geom
        for i, p in enumerate(places):
            for t in geom.footprint(p):
                self.tiles[t] = i
        self.key = {}
        for i, p in enumerate(places):
            self.key[(round(p["x"], 2), round(p["y"], 2))] = i

    def _retag(self, places):
        """A mall chest on the bus (a bus item's last stop) and the inserter filling it become group mall:<item>."""
        at = {(math.floor(p["x"]), math.floor(p["y"])): p for p in places}
        for p in places:
            if p["item"] == "iron-chest" and p.get("mall") and p.get("grp") in ("bus", None):
                g = "mall:" + "+".join(p["mall"])
                p["grp"] = g
                t = (math.floor(p["x"]), math.floor(p["y"]))
                for d, (dx, dy) in (("south", (0, 1)), ("north", (0, -1)), ("east", (1, 0)), ("west", (-1, 0))):
                    ins = at.get((t[0] - dx, t[1] - dy))     # the inserter dropping into the chest
                    if ins and "inserter" in ins["item"] and ins.get("direction") == d:
                        ins["grp"] = g

    def _split_power(self):
        """The steam plant as units: power (pump, pipes, belts, poles) + power:k (a boiler, the engines it feeds
        steam, its fuel inserter), numbered along the water chain from the pump; built as demand grows."""
        idx = self.groups.get("power")
        if not idx:
            return
        boilers = [i for i in idx if self.p[i]["item"] == "boiler"]
        pump = next((i for i in idx if self.p[i]["item"] == "offshore-pump"), None)
        # order boilers by fluid-graph distance from the pump
        dist = {pump: 0} if pump is not None else {}
        fr = [pump] if pump is not None else []
        while fr:
            nx_ = []
            for i in fr:
                for j in self.flow.fluid[i]:
                    if j not in dist and j in set(idx):
                        dist[j] = dist[i] + 1
                        nx_.append(j)
            fr = nx_
        boilers.sort(key=lambda i: (dist.get(i, 1e9), self.p[i]["x"], self.p[i]["y"]))
        unit = {}
        for k, bi in enumerate(boilers):
            unit[bi] = k
            # engines reached through steam (engine chains), not through another boiler
            fr, seen = [bi], {bi}
            while fr:
                nx_ = []
                for i in fr:
                    for j in self.flow.fluid[i]:
                        if j in seen or self.p[j]["item"] != "steam-engine":
                            continue
                        seen.add(j)
                        unit[j] = k
                        nx_.append(j)
                fr = nx_
            for j in self.flow.fed_by[bi]:
                if "inserter" in self.p[j]["item"]:
                    unit[j] = k
        # the fuel belt and water pipes along the boiler row: each piece goes with the nearest boiler, so a unit brings its own
        # stretch (and the stretches between it and the coal, as transport on its upstream path)
        bpos = [(self.p[bi]["x"], self.p[bi]["y"], unit[bi]) for bi in boilers]
        for i in idx:
            if i not in unit and self.p[i]["item"] in ("transport-belt", "underground-belt", "splitter", "pipe",
                                                       "pipe-to-ground") and bpos:
                x, y = self.p[i]["x"], self.p[i]["y"]
                unit[i] = min(bpos, key=lambda b: abs(b[0] - x) + abs(b[1] - y))[2]
        base = []
        for i in idx:
            if i in unit:
                self.p[i]["grp"] = f"power:{unit[i]}"
                self.groups[f"power:{unit[i]}"].append(i)
            else:
                base.append(i)
        self.groups["power"] = base

    def _io(self, g, ents):
        rec = self.gd["recipes"]
        made, need = set(), set()
        for p in ents:
            it = p["item"]
            if p.get("recipe") and p["recipe"] in rec:
                r = rec[p["recipe"]]
                made |= {x[0] for x in r["prod"]}
                need |= {x[0] for x in r["ing"]}
            elif it == "stone-furnace":
                m = re.match(r"^smelt[:-]([a-z-]+?)(?::\d+)?$", g)
                src = m.group(1) if m else ""
                if src in SMELT_OUT:
                    made.add(SMELT_OUT[src])
                    need |= {src, "coal"}
                elif src in SMELT_OUT.values():          # smelt-steel-plate, smelt-stone-brick: named by output
                    made.add(src)
                    need |= {k for k, v in SMELT_OUT.items() if v == src} | {"coal"}
            elif it == "electric-mining-drill":
                ore = g.split(":")[1] if ":" in g else ""
                made.add("iron-ore" if ore == "raw-iron-ore" else ore)
            elif it == "pumpjack":
                made.add("crude-oil")
            elif it == "offshore-pump":
                made.add("water")
            elif it == "boiler":
                made.add("steam")
                need |= {"coal", "water"}
            elif it == "steam-engine":
                made.add("electricity")
                need.add("steam")
            elif it == "lab":
                need |= set(self.dem.get("packs", {}))
                made.add("research")
            elif it == "rocket-silo":
                need |= {"processing-unit", "low-density-structure", "rocket-fuel"}
                made.add("rocket-part")
            elif it == "iron-chest" and p.get("mall"):
                need |= set(p["mall"])
                made |= {"stock:" + m for m in p["mall"]}
        return made, need - made     # a product also used inside (belts for undergrounds) still leaves

    def _techs(self, ents):
        """Techs to make the group's entities (its machines' recipes are gated one by one, see recipe_ok)."""
        rec = self.gd["recipes"]
        items = set()
        for p in ents:
            it = p["item"]
            if it == "assembling-machine-2" and rec.get(p.get("recipe") or "", {}).get("cat") in ASM1_CATS:
                it = "assembling-machine-1"      # built as an assembler 1 until automation-2 (upgraded later)
            if it not in POLE_ITEMS:
                items.add(it)
        return techs_of(items, self.gd)

    def recipes(self, g):
        return sorted({self.p[i]["recipe"] for i in self.groups[g] if self.p[i].get("recipe")})

    def box(self, idx, m=4):
        xs = [self.p[i]["x"] for i in idx]
        ys = [self.p[i]["y"] for i in idx]
        return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


# --------------------------------------------------------------------------------------------- the live game
class Live:
    def __init__(self, q, plan):
        self.q, self.plan = q, plan
        self.built = set()
        self.extra = {}          # (x, y) -> name: player entities that are not plan pieces (our poles, boot)
        self.standin = set()     # plan splitters standing as two belts until logistics
        self.retired = set()     # dry mine groups replaced by a remine (their drills were picked up)
        self.own = []            # pieces of our own outside the plan (remine columns and routes), from the state
        self.dead = set()        # plan drills moved off dry ground (auto.relocate): not to be put back
        self.researched = set()
        self.names = {}          # (x, y) -> name of every player entity standing (as of the last refresh)
        self.state_gone = []     # plan pieces that stood and are gone (the restore repair puts them back)
        self.gone_extra = []     # our own pieces that stood and are gone (restore)
        # what a repair rule found, for its handler (auto.repairs) to act on
        self.relocate = None     # (mine group, [dry drills])
        self.outpost = None      # (resource, patch, drills)
        self.radar = None        # the radar site picked
        self.reconnect = []      # own route pieces to lay again
        self.pairs = []          # own undergrounds to pick up
        self.trim = []           # belt tails to pick up
        self.spent = []          # a spent block's pieces to pick up
        self.unstick = []        # (inserter, held item, recipe)
        self.misfaced = []       # machines facing another way than planned

    def refresh(self, box=None):
        from auto import live
        if box is None:     # the plan's area, grown to every piece of our own outside it (remine columns, routes)
            x1, y1, x2, y2 = self.plan.box(range(len(self.plan.p)), 8)
            for p in self.own:
                x1, y1, x2, y2 = min(x1, p["x"] - 4), min(y1, p["y"] - 4), max(x2, p["x"] + 4), max(y2, p["y"] + 4)
            box = (x1, y1, x2, y2)
        ents = live.scan(self.q, box)
        x1, y1, x2, y2 = box
        self.built = {i for i in self.built if not (x1 <= self.plan.p[i]["x"] <= x2 and y1 <= self.plan.p[i]["y"] <= y2)}
        self.extra = {k: v for k, v in self.extra.items() if not (x1 <= k[0] <= x2 and y1 <= k[1] <= y2)}
        self.names = {k: v for k, v in self.names.items() if not (x1 <= k[0] <= x2 and y1 <= k[1] <= y2)}
        for n, x, y, d, bt in ents:
            self.names[(round(x, 2), round(y, 2))] = n
            i = self.plan.key.get((round(x, 2), round(y, 2)))
            if i is not None and self._same(self.plan.p[i], n):
                self.built.add(i)
            else:
                self.extra[(x, y)] = n
        # a planned splitter with straight belts on both its tiles stands in (before logistics): counts as built
        from auto import geom
        belts = {(math.floor(x), math.floor(y)) for (x, y), n in self.extra.items() if n == "transport-belt"}
        self.standin = {i for i in getattr(self, "standin", set()) if i in self.built and
                        not (x1 <= self.plan.p[i]["x"] <= x2 and y1 <= self.plan.p[i]["y"] <= y2)}
        for i, p in enumerate(self.plan.p):
            if p["item"] == "splitter" and i not in self.built and x1 <= p["x"] <= x2 and y1 <= p["y"] <= y2                     and geom.footprint(p) <= belts:
                self.built.add(i)
                self.standin.add(i)
        return len(ents)

    @staticmethod
    def _same(p, name):
        it = p["item"]
        if it == name:
            return True
        if it == "assembling-machine-2" and name == "assembling-machine-1":
            return True
        return it in POLE_ITEMS and name in POLE_ITEMS

    def group_done(self, g):
        if g in self.retired:
            return True
        return all(i in self.built or i in self.dead for i in self.plan.groups[g])

    def statuses(self, idx):
        """status counts for the machines among idx, plus the bad ones with positions."""
        from auto import live
        idx = [i for i in idx if is_machine(self.plan.p[i]) or "inserter" in self.plan.p[i]["item"]]
        if not idx:
            return collections.Counter(), []
        st = live.status(self.q, self.plan.box(idx, 2))
        at = {(round(x, 2), round(y, 2)): s for n, x, y, s in st}
        cnt, bad = collections.Counter(), []
        for i in idx:
            p = self.plan.p[i]
            s = at.get((round(p["x"], 2), round(p["y"], 2)), "missing")
            if p["item"] == "iron-chest":   # chests have no status: standing is all
                s = "working" if i in self.built else "missing"
            if "inserter" in p["item"] and s not in ("no_power", "low_power", "missing"):
                continue          # an idle inserter is fine; only power matters for it
            cnt[f"{p['item']}:{s}"] += 1
            if s in BAD or s == "missing":
                bad.append((p["item"], p["x"], p["y"], s, p.get("grp")))
        return cnt, bad



FUEL = {"coal"}


BOOT_ENTS = ("burner-mining-drill", "stone-furnace", "iron-chest", "wooden-chest", "lab")


# read deep inside materialize (auto.schedule calls it too): module state, set by the controller
MALL_MAKES = set()   # items whose mall maker and chest stand (set each round)
GD = None            # gamedata (set at start)
# a recipe that needs a tech the factory can't reach yet gets a stand-in with the same ports: refineries start on basic
# oil processing (petroleum from the same output) because advanced oil processing needs blue science, made from it
RECIPE_SUB = {"advanced-oil-processing": "basic-oil-processing"}
_TECHS = {}


def techs_of(names, gd):
    from auto import stage
    key = tuple(sorted(names))
    if key not in _TECHS:
        _TECHS[key] = stage.techs_for(set(names), gd)
    return _TECHS[key]


def recipe_ok(r, researched):
    return techs_of([r], GD) <= researched


def recipe_now(r, researched):
    """The recipe to set now: r, its stand-in, or None (placed idle, set when researched)."""
    if recipe_ok(r, researched):
        return r
    sub = RECIPE_SUB.get(r)
    if sub and recipe_ok(sub, researched):
        return sub
    return None


def materialize(p, researched, pole_item):
    """What actually gets placed for a plan piece, given the research: assembler 1 before automation-2, the
    researched pole type, and before logistics a splitter as two straight belts (the trunk keeps flowing, the
    branch waits; the splitter replaces them once it can be made). Returns a list."""
    q = {k: v for k, v in p.items() if k in ("item", "x", "y", "direction", "belt_type", "recipe")}
    if q["item"] == "assembling-machine-2" and ("automation-2" not in researched or
                                                "assembling-machine-2" not in MALL_MAKES):
        q["item"] = "assembling-machine-1"     # until the mall makes assembler 2s (its own block uses them too)
    if q["item"] in POLE_ITEMS:
        q["item"] = pole_item
    if q.get("recipe") and GD is not None:
        now = recipe_now(q["recipe"], researched)
        if now != q["recipe"]:
            q["final"] = q["recipe"]
            if now:
                q["recipe"] = now
            else:
                q.pop("recipe")
    if q["item"] == "splitter" and "logistics" not in researched:
        from auto import geom
        return [{"item": "transport-belt", "x": t[0] + .5, "y": t[1] + .5, "direction": q.get("direction", "north"),
                 "standin": True} for t in sorted(geom.footprint(q))]
    return [q]

