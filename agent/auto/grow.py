"""Closed-loop growth controller: plays a seed from an empty map to a rocket launch (see GROWTH_PROMPT.md).

    python -m auto.grow <seed> [--scratch] [--chars 4] [--speed 10] [--rounds N] [--dry]

The seed's plan (auto.plan2: where every module, belt and pipe goes, solved from the map) is the space plan. This
program decides, round after round, what to build next and checks that it works:

  measure   production statistics (made / used per minute), machine statuses, chest stock, research
  target    the LP's rates (demand.json) for the items the researched technologies make possible
  choose    power first; then broken machines; then, during the bootstrap, the mall; then the worst deficit, walking
            upstream to the first group that can be built (its inputs exist, its techs are researched)
  plan      one increment: that group, the transport paths from its inputs back to what is built and from its
            outputs on to the world's trunks (auto.flow), and poles chained to the live grid (auto.poles); checked
            against the live map (nothing in the way but trees) and by draftsman
  build     the characters (supply.build_plan; hand-crafting only until the mall runs)
  verify    every machine of the increment powered and not broken; rates before and after go to the log
  research  labs kept busy with what the next increments need; trigger techs done by crafting

Log: runtime/auto/<seed>/grow.log (one line per event) and grow.jsonl (one record per increment).
"""
import argparse
import collections
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
sys.path.insert(0, str(AGENT))

LOG = JL = None
_lock = threading.Lock()


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


# --------------------------------------------------------------------------------------------- choosing
class Chooser:
    def __init__(self, plan, live, dem, handcraft):
        self.plan, self.live, self.dem = plan, live, dem
        self.handcraft = handcraft
        self.want_techs = []
        self.votes = collections.Counter()   # tech -> how many blocked picks this round wait for it
        self.skip = {}           # group -> rounds to skip (it failed to build or verify)
        self.pending = []        # (group, why): an alt a build path's gate named, built first next round
        self.have = None         # () -> reachable stock + the builders' inventories (set in main)
        self.held_line = None    # the last "short" line choose() logged (logged again only when it changes)

    def techs_ok(self, g):
        R = self.live.researched
        if not self.plan.techs[g] <= R:
            return False
        rs = self.plan.recipes(g)
        return not rs or any(recipe_now(r, R) for r in rs)

    def missing_techs(self, g):
        R = self.live.researched
        out = set(self.plan.techs[g] - R)
        for r in self.plan.recipes(g):
            if not recipe_now(r, R):
                out |= techs_of([r], GD) - R
        return out

    def missing_inputs(self, g):
        """Inputs of g that no built producer upstream of it makes (fuel coal included)."""
        out = set()
        if g == "labs":     # a lab runs on any pack: one made is enough
            packs = sorted(self.plan.need[g])
            return set() if any(self.live.group_done(h) for it in packs for h in self.plan.makers.get(it, []))                 else set(packs[:1])
        for it in self.plan.need[g]:
            makers = [h for h in self.plan.deps.get(g, ()) if it in self.plan.made.get(h, ())]
            if not makers:
                continue                  # not delivered by the plan's transport (e.g. by hand): not a dependency
            if not any(self.live.group_done(h) for h in makers):
                out.add(it)
        return out

    def buildable(self, g):
        if self.skip.get(g, 0) > 0:
            return False, "skipped"
        if not self.techs_ok(g):
            return False, "techs " + ",".join(sorted(self.missing_techs(g))[:4])
        if g != "power" and not g.startswith("power:") and self._electric(g) and not self.live.group_done("power"):
            return False, "no power"
        miss = self.missing_inputs(g)
        if g.startswith("power"):
            miss.discard("coal")      # the tender hand-feeds the boilers until a coal belt reaches them
        if miss:
            return False, "inputs " + ",".join(sorted(miss))
        return True, ""

    def _electric(self, g):
        return any((any(k in self.plan.p[i]["item"] for k in ELECTRIC) or self.plan.p[i]["item"] == "pump")
                   and "burner" not in self.plan.p[i]["item"] for i in self.plan.groups[g])

    def pick(self, item, seen=None, depth=0):
        """The first unbuilt group that can be built on the way to making `item`: one of its makers, or what a
        maker is waiting for (upstream). Returns (group, reason) or (None, reason)."""
        seen = seen if seen is not None else set()
        makers = [g for g in self.plan.makers.get(item, []) if not self.live.group_done(g)]
        if not makers:
            return None, f"{item}: all makers built"
        makers.sort(key=lambda g: len(self.plan.groups[g]))
        why = []
        for g in makers:
            if g in seen:
                continue
            seen.add(g)
            ok, w = self.buildable(g)
            if ok:
                return g, f"makes {item}"
            why.append(f"{g}: {w}")
            if w.startswith("techs"):
                for t in sorted(self.missing_techs(g)):
                    self.votes[t] += 1
            if w.startswith("inputs") and depth < 12:
                for it in sorted(self.missing_inputs(g)):
                    h, w2 = self.pick(it, seen, depth + 1)
                    if h:
                        return h, f"{it} for {g} ({item})"
                    why.append(w2)
            if w == "no power":
                return ("power" if not self.live.group_done("power") else None), f"power for {g}"
        return None, "; ".join(why)[:300]


# --------------------------------------------------------------------------------------------- increments
DEAD = set()   # groups whose drills found no ore left (dry or retired): standing, but supplying nothing


def supplied(plan, built):
    """Built pieces that something built actually feeds: built producers and the built transport downstream of
    them (items forward, fluids either way). A built belt stub that nothing feeds is no connection, and neither is
    a mine that has run dry."""
    f = plan.flow
    out = {i for i in built if not f.transport(i) and plan.p[i].get("grp") not in DEAD}
    fr = list(out)
    while fr:
        nx_ = []
        for i in fr:
            for j in list(f.feeds[i]) + list(f.fluid[i]):
                if j in built and j not in out:
                    out.add(j)
                    if f.transport(j):
                        nx_.append(j)
        fr = nx_
    return out


def link_paths(plan, built, g):
    """Unbuilt transport on the cheapest flow path from a built supplier of each item g needs to g (networkx
    Dijkstra over the plan's item and fluid links: entering a built piece costs ~0, an unbuilt one 1). The upstream
    walk stops at the first fed belt, so a branch side-loading into a working trunk (blue packs into the red/green
    lab belt) stays invisible to it; this walk starts at the suppliers."""
    import networkx as nx
    f = plan.flow
    G = getattr(plan, "_fg", None)
    if G is None:
        G = nx.DiGraph()
        for i in range(len(plan.p)):
            for j in list(f.feeds[i]) + list(f.fluid[i]):
                G.add_edge(i, j)
        plan._fg = G
    own = set(plan.groups[g])
    out = set()
    for it in plan.need.get(g, ()):
        rec = plan.gd["recipes"]

        def makes(i, h):                      # this machine (not just its group) makes it
            r = plan.p[i].get("recipe")
            if r in rec:
                return it in {x[0] for x in rec[r]["prod"]}
            return not r and it in plan.made.get(h, ())   # furnaces, drills, pumpjacks: the group's product
        src = {i for h in plan.deps.get(g, ()) if it in plan.made.get(h, ()) and h not in DEAD
               and all(i in built for i in plan.groups[h]) for i in plan.groups[h]
               if not f.transport(i) and makes(i, h)}
        if not src:
            continue

        def w(u, v, _d):
            if v == "T" or v in own or v in src:
                return 0.001
            if not f.transport(v):
                return None                   # paths run through transport only, not through other machines
            return 0.001 if v in built else 1.0
        # shortest path to any piece of g: a virtual sink "T" joined from g's pieces
        G.add_edges_from((i, "T") for i in own)
        try:
            ln, pth = nx.multi_source_dijkstra(G, src, target="T", weight=w)
        except nx.NetworkXNoPath:
            continue
        finally:
            G.remove_node("T")
        if ln >= 1:
            out |= {i for i in pth[1:-1] if i not in built and f.transport(i)}
    return out


def rear_belts(plan, idx, built):
    """idx plus the belt straight behind every belt of idx (or built) that takes a side-load: without its rear
    belt a side-loaded belt turns into a curve and the side input lands on both lanes (coal on the ore lane)."""
    f = plan.flow
    out = set(idx)
    for i in list(idx) + [j for j in built if j in idx or any(k in idx for k in f.fed_by[j])]:
        p = plan.p[i]
        if p["item"] != "transport-belt":
            continue
        d = p.get("direction", "north")
        behind = None
        sides = []
        for j in f.fed_by[i]:
            q = plan.p[j]
            if q["item"] in ("transport-belt", "underground-belt") and q.get("direction") == d:
                behind = j
            elif q["item"] in ("transport-belt", "underground-belt", "splitter"):
                sides.append(j)
        if sides and behind is not None and behind not in built:
            out.add(behind)
        # a T merge (two side inputs, one item per lane, nothing behind): with only one side standing the belt is a
        # curve and that side's item takes both lanes (pipes over the circuit lane into B5); the other side's last
        # belt keeps it a side-load
        if behind is None and len(sides) == 2 and any(j in out or j in built for j in sides):
            out |= {j for j in sides if j not in built}
    return out


def gaps(plan, built, limit=80):
    """Runs of unbuilt plan transport that a working producer's output runs into and that end at a built piece:
    a missing piece between two built stretches (an underground entrance a builder picked up, a failed placement).
    Returns the first gap's pieces."""
    f = plan.flow
    start = [i for i in built if not f.transport(i) and plan.p[i].get("grp") not in DEAD]
    seen, fr = set(start), list(start)
    while fr:
        i = fr.pop()
        for j in f.feeds[i]:
            if j in seen:
                continue
            seen.add(j)
            if j in built:
                if f.transport(j):
                    fr.append(j)
                continue
            if not f.transport(j):
                continue
            run, cur, ok = [j], j, False   # follow the unbuilt run forward until something built
            for _ in range(limit):
                nxt = [k for k in f.feeds[cur] if k not in run]
                if not nxt:
                    break
                k = nxt[0]
                if k in built:
                    ok = True
                    break
                if not f.transport(k):
                    break
                run.append(k)
                cur = k
            if ok:
                return run
    return []


def increment(plan, live, g, researched, pole_item):
    """Placements for group g plus the transport it needs (see module doc). Returns (places, groups covered)."""
    built = supplied(plan, live.built)
    if g == "restore":
        idx = [i for i in live.state_gone if i not in live.built]
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)] +             [dict(p) for p in getattr(live, "gone_extra", [])], [g], idx
    if g.startswith("repower:extra@"):   # a machine of our own: only its poles
        _, n_, x_, y_ = g[8:].split("@")
        return [{"item": n_, "x": float(x_), "y": float(y_), "upgrade": True}], [g], []
    if g.startswith("repower:"):     # the group's own machines, already standing: only poles get placed
        idx = [i for i in plan.groups[g[8:]] if any(k in plan.p[i]["item"] for k in ELECTRIC)]
        return [dict(q, upgrade=True) for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g == "reconnect":
        return [dict(p, upgrade=True) for p in live.reconnect], [g], []
    if g == "gap":
        idx = gaps(plan, live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g == "rears":
        bb = {i for i in live.built if plan.p[i]["item"] == "transport-belt"}
        idx = sorted(rear_belts(plan, bb, live.built) - live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g.startswith("upgrade:"):   # stand-ins replaced by the real thing now that it can be made
        idx = sorted(live.standin)
        return [dict(q, upgrade=True) for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g.startswith("finish:"):  # a half-built transport group whose machines all stand: the rest of it
        h = g[7:]
        idx = sorted(rear_belts(plan, {i for i in plan.groups[h] if i not in live.built and i not in live.dead},
                                live.built) - live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    if g.startswith("link:"):    # a built group's missing connection to suppliers that now stand
        h = g[5:]
        own = set(plan.groups[h])
        idx = sorted(rear_belts(plan, plan.flow.upstream(own, built - own)[0] | link_paths(plan, live.built, h),
                                live.built) - live.built)
        return [q for i in idx for q in materialize(plan.p[i], researched, pole_item)], [g], idx
    groups = [g]
    if g.startswith("power:") and not live.group_done("power"):
        groups.append("power")
    start = set()
    for h in groups:
        start |= set(plan.groups[h])
    up = set()
    for h in groups:     # each group's inputs back to what stands or comes with this increment
        own = set(plan.groups[h])
        up |= plan.flow.upstream(own, (built | start) - own)[0]
    walk = {h for h in plan.groups if WORLD_TRANSPORT.match(h or "")}
    down = plan.flow.downstream(set(plan.groups[g]), built, walk)
    idx = sorted(rear_belts(plan, (start | up | down), live.built) - live.built)
    places = [q for i in idx for q in materialize(plan.p[i], researched, pole_item)]
    return places, groups, idx


MALL_MAKES = set()   # items whose mall maker and chest stand (set each round)
GD = None            # gamedata (set in main)
PLAN = None          # the space plan (set in main; build() checks underground pairing against it)
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


def scout_step(plan, L, q, state, sched, ch):
    """(group, why) for a radar or an outpost, else None. Resource-aware (auto.budget): the whole run's raw budget
    (the phased LP + fuel, calibrated on a finished run) against the patches known so far (generated chunks within
    reach). Short of it: radars, nothing else (outposts wait). Once the known patches cover every resource, the radars
    switch off and a MILP picks the patches to mine (enough amount per resource, drill rate per phase, the least route
    to the base): an outpost goes on the next chosen patch whenever what is left in the mined patches falls short of
    what is left to mine. Crude is a rate: pumpjack outposts on the refineries' deficit (scout.deficits)."""
    from auto import budget, scout
    charted, rows = scout.scan(q)
    ps = scout.patches(q, rows)
    try:
        crude = scout.crude_rates(q)     # crude is a rate: the refineries' need beyond what the wells give now
    except Exception as e:
        crude = None
        log("scout: crude rates:", str(e)[:120])
    x1, y1, x2, y2 = plan.box(range(len(plan.p)), 0)
    base = ((x1 + x2) / 2, (y1 + y2) / 2)
    if getattr(ch, "budget", None) is None:
        ch.budget = budget.need(ch.dem, budget.load_calib(ch.seed))
        log("budget to the rocket:", ch.budget["total"], f"(fuel {ch.budget['fuel']}, idle {ch.budget['idle']}; "
            f"crude {ch.budget['rate']['crude-oil']:.0f}/min)")
    done = budget.mined_so_far(q)
    bl = budget.left(ch.budget, done)
    kn = budget.known(ps, base, ch.budget["run_hours"])
    short = budget.short(bl, kn)
    used = scout.mined(plan, L, ps)
    line = "; ".join(f"{n} {kn.get(n, 0) / 1000:.0f}k/{v / 1000:.0f}k" for n, v in bl["total"].items()) + \
        f"; crude {kn.get('crude-oil', 0):.0f}/{ch.budget['rate']['crude-oil'] * budget.CRUDE_MARGIN:.0f} per min"
    log(f"scout pass: known/needed {line}; {len(charted)} chunks, {len(ps)} patches; "
        + (f"SHORT {sorted(short)}" if short else "budget met"))
    state["budget"] = {"left": bl["total"], "known": {k_: int(v_) for k_, v_ in kn.items()}, "short": short,
                       "done": done, "charted": len(charted), "patches": len(ps)}
    radars = state.get("radars", [])
    rc = [(math.floor(x / scout.CHUNK), math.floor(y / scout.CHUNK)) for x, y in radars]

    def radar(why, most=6):
        if len(radars) >= most or ch.skip.get("radar", 0) > 0:
            return None
        pend = state.get("radar_site")      # a radar planned and not standing yet (no radar item, not researched):
        if pend:                             # the same site again, not a new one beside it with poles of its own
            return f"radar:{pend[0]},{pend[1]}", f"{why}: the radar site picked before"
        site, k = scout.radar_site(plan, L, ch.m, charted, rc)
        if site and k >= 60:
            state["radar_site"] = list(site)
            L.radar = site
            return f"radar:{site[0]},{site[1]}", f"{why}: a radar there reveals {k} dark chunks"
        return None
    # fuel is local: smelters out of fuel while a big coal patch is charted -> one more coal outpost, into the
    # starving network (the target pick puts dry coal mines first); patches only our own outposts mine still count
    if sum(1 for o in state.get("outposts", []) if o["name"] == "coal") < 6 and ch.skip.get("outpost:coal", 0) <= 0 \
            and starving_fuel(plan, L, q):
        pc = scout.outpost_patch(ps, scout.mined(plan, L, ps, own=False), "coal", 100000, base)
        if pc:
            L.outpost = ("coal", pc, 12)
            return "outpost:coal", (f"smelters out of fuel: a coal outpost on the patch of {pc['amount']} at "
                                    f"({pc['center'][0]:.0f},{pc['center'][1]:.0f}) into the starving network")
    if short:
        # scout before any mine beyond the start patches: radars (up to 12 while short) until the map holds it all;
        # crude alone short (a rate, pumpjack outposts) holds no ore outpost
        if state.get("radars_off"):
            budget.radars(q, True)
            state["radars_off"] = False
        r_ = radar(f"the known patches hold less than the run needs ({short})", most=12)
        if r_ or set(short) - {"crude-oil"}:
            return r_
    if not short and not state.get("radars_off") and radars:
        log(f"scout: budget met, {budget.radars(q, False)} radars switched off (300 kW each)")
        state["radars_off"] = True
    drills = {}
    for k_ in used:
        cs = set(ps[k_]["chunks"])
        drills[k_] = sum(1 for p_ in plan.p if p_["item"] == "electric-mining-drill" and
                         (math.floor(p_["x"] / scout.CHUNK), math.floor(p_["y"] / scout.CHUNK)) in cs)
    failed = [tuple(b_) for b_ in state.get("outpost_failed", ())]      # patches no outpost could reach
    sel = budget.select(ps, bl, base, used, drills_have=drills,
                        skip={k_ for k_, p_ in enumerate(ps) if any(overlap(p_["box"], b_) for b_ in failed)})
    key_ = [[c_["name"], c_["center"], c_["drills"]] for c_ in sel.get("chosen", ())]
    if key_ != state.get("mines_key"):
        state["mines_key"] = key_
        log(f"mine plan ({sel['status']}): " + "; ".join(
            f"{c_['name']} {c_['amount'] // 1000}k at {tuple(c_['center'])} {'mined' if c_['mined'] else 'NEW'} "
            f"{c_['drills']} drills, {c_['dist']} tiles" for c_ in sel.get("chosen", ())))
        try:
            (ch.out / "mines.json").write_text(json.dumps({"base": base, "budget": bl["total"], **sel}))
        except Exception as e:
            log("mines.json:", str(e)[:120])
    d = scout.deficits(None, ch.dem, plan, L, ps, crude=crude)          # crude only (a rate)
    held_ = {}
    for k_ in used:
        held_[ps[k_]["name"]] = held_.get(ps[k_]["name"], 0) + budget.USABLE * ps[k_]["amount"]
    for n, v in bl["total"].items():
        if v > held_.get(n, 0):
            d[n] = int(v - held_.get(n, 0))
    state["scout"] = {"short": d, "charted": len(charted), "patches": len(ps)}
    boxes = [tuple(o["box"]) for o in state.get("outposts", [])]
    for n, sh in sorted(d.items(), key=lambda kv: (kv[0] != "coal", -kv[1])):   # coal first: everything burns it
        if sum(1 for o in state.get("outposts", []) if o["name"] == n) >= 6 or ch.skip.get("outpost:" + n, 0) > 0:
            continue
        if n == "crude-oil":   # a patch with free wells (the base's own patch first if it has some)
            pc = scout.crude_patch(lambda b_: scout.free_wells(q, b_), ps, sh, base)
            if pc is None:
                r_ = radar("crude-oil short of the refineries' rate")
                if r_:
                    return r_
                continue
            L.outpost = (n, pc, sh)
        else:          # the next patch the mine plan chose that nothing mines yet
            nxt = [c_ for c_ in sel.get("chosen", ()) if c_["name"] == n and not c_["mined"]
                   and not any(overlap(c_["box"], b_) for b_ in boxes)]
            if not nxt:
                continue
            pc = ps[nxt[0]["k"]]
            # the MILP's drills keep the run's average pace; once the start patch is dry the outpost carries the
            # whole peak rate (the LP's, 30/min a drill)
            nd = max(nxt[0]["drills"], math.ceil(ch.budget["rate"].get(n, 0) / budget.DRILL_PER_MIN))
            L.outpost = (n, pc, max(4, min(12, nd)))
        return "outpost:" + n, (f"{n}: {sh} more to mine than the mined patches hold; the mine plan's patch of "
                                f"{pc['amount']} at ({pc['center'][0]:.0f},{pc['center'][1]:.0f})")
    return None


def overlap(a, b):
    """Two boxes (x1, y1, x2, y2) share area."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def route_poles(plan, L, pieces, q, m, box=None):
    """Poles that join an outpost's pole group to the powered network along its belt route: networkx fewest hops
    over free tiles beside the route (4-neighbours of its belts), from the outpost's poles to any powered live pole
    within reach. [] when already joined or no chain exists."""
    import networkx as nx
    from auto import geom, live as lv, poles
    R = poles.REACH
    net = lv.network(q)
    on = {(round(x, 2), round(y, 2)) for x, y, o_, *_ in net.get("poles") or [] if o_}
    own = [p for p in pieces if "electric-pole" in p["item"] and (box is None or
           box[0] - 8 <= p["x"] <= box[2] + 8 and box[1] - 8 <= p["y"] <= box[3] + 8)]   # chain from the outpost's own
    own = [p for p in own if (round(p["x"], 2), round(p["y"], 2)) not in on]   # the poles still off the grid
    if not own:
        return []
    belts = [p for p in pieces if p["item"] in ("transport-belt", "underground-belt", "splitter")]
    occ = set(geom.tiles(pieces)) | set(plan.tiles)
    for (x, y), n in L.extra.items():
        occ |= geom.footprint({"item": n, "x": x, "y": y})
    cand = {(math.floor(b["x"]) + a, math.floor(b["y"]) + c) for b in belts for a, c in ((1, 0), (-1, 0), (0, 1), (0, -1))}
    cand = {t for t in cand if t not in occ and m.t(*t) in ".t"}
    pts = {("c", t): (t[0] + .5, t[1] + .5) for t in cand}
    pts.update({("s", k): (p["x"], p["y"]) for k, p in enumerate(own)})
    xs = [v[0] for v in pts.values()]
    ys = [v[1] for v in pts.values()]
    x1, x2, y1, y2 = min(xs) - R, max(xs) + R, min(ys) - R, max(ys) + R
    pts.update({("t", k): (x, y) for k, (x, y) in enumerate(sorted(on)) if x1 <= x <= x2 and y1 <= y <= y2})
    buck = {}
    for k, (x, y) in pts.items():
        buck.setdefault((int(x // 8), int(y // 8)), []).append(k)
    G = nx.Graph()
    for k, (x, y) in pts.items():
        bx, by = int(x // 8), int(y // 8)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k2 in buck.get((bx + dx, by + dy), ()):
                    if k2 != k and math.dist((x, y), pts[k2]) <= R and not (k[0] != "c" and k2[0] != "c"):
                        G.add_edge(k, k2)
    G.add_edges_from(("S", k) for k in pts if k[0] == "s")
    G.add_edges_from((k, "T") for k in pts if k[0] == "t")
    try:
        path = nx.shortest_path(G, "S", "T")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []
    return [{"item": poles.ITEM, "x": pts[k][0], "y": pts[k][1]} for k in path[1:-1] if k[0] == "c"]


def outpost_pieces(state, o):
    """Our own pieces of outpost o: what stands in its patch box and its route's corridor to its target group."""
    bx = o["box"]
    pts = state.get("extra_places", [])
    inb = [p for p in pts if bx[0] - 8 <= p["x"] <= bx[2] + 8 and bx[1] - 8 <= p["y"] <= bx[3] + 8]
    # the route: own belts chained to the box's belts through the own pieces (4-neighbour flood over belt tiles)
    tiles = {(math.floor(p["x"]), math.floor(p["y"])): p for p in pts}
    seen = {(math.floor(p["x"]), math.floor(p["y"])) for p in inb}
    fr = list(seen)
    while fr:
        nx_ = []
        for t in fr:
            for a, c in ((1, 0), (-1, 0), (0, 1), (0, -1), (5, 0), (-5, 0), (0, 5), (0, -5), (2, 0), (-2, 0), (0, 2), (0, -2)):
                u = (t[0] + a, t[1] + c)
                if u in tiles and u not in seen and (abs(a) + abs(c) == 1 or tiles[u]["item"] in
                                                     ("underground-belt", "small-electric-pole", "medium-electric-pole")):
                    seen.add(u)
                    nx_.append(u)
        fr = nx_
    return [tiles[t] for t in seen]


def plan_poles(plan, live, places, m, corridor=None):
    """Poles for an increment on the live map: cover its electric consumers (and new steam engines), then chain
    every unpowered pole group to the engines' network. Free tiles: no live entity, no plan piece, no water or
    cliff, no tree. corridor: a tile set poles must stay in (an outpost's chain runs along its belt route, not
    through a search box hundreds of tiles wide)."""
    from auto import geom, live as lv, poles
    if not places:
        return []
    box = plan.box(range(len(plan.p)), 12) if False else None
    xs = [p["x"] for p in places]
    ys = [p["y"] for p in places]
    box = (min(xs) - 30, min(ys) - 30, max(xs) + 30, max(ys) + 30)
    net = lv.network(live.q)
    live_poles = [{"item": pp[3] if len(pp) > 3 else poles.ITEM, "x": pp[0], "y": pp[1]} for pp in net.get("poles") or []]
    occupied = set()
    for (x, y), n in live.extra.items():
        occupied.add((math.floor(x), math.floor(y)))
    for i in live.built:
        occupied |= geom.footprint(plan.p[i])
    obst = lv.obstacles(live.q, box)

    own = geom.tiles([p for p in places if not p.get("remove")])   # pieces not in the plan (remine) included

    def free(t):
        if t in plan.tiles or t in occupied or t in obst or t in own:
            return False
        if corridor is not None and t not in corridor:
            return False
        return m.t(*t) == "."
    engines = [p for p in places if p["item"] == "steam-engine"]
    cov, _ = poles.cover(live_poles + places, free, extra=engines)
    roots = [(pp[0], pp[1]) for pp in net.get("poles") or [] if pp[2]]
    if not roots:
        eng = engines or [plan.p[i] for i in live.built if plan.p[i]["item"] == "steam-engine"]
        cands = [c for c in cov if eng and any(abs(c["x"] - e["x"]) < poles.SUPPLY + 1.5 and abs(c["y"] - e["y"]) < poles.SUPPLY + 2.5 for e in eng)]
        if not cands:
            return cov
        roots = [(cands[0]["x"], cands[0]["y"])]
    occupied |= {(math.floor(c["x"]), math.floor(c["y"])) for c in cov}
    try:
        chains = poles.connect(live_poles + places + cov, free, roots[0])
    except RuntimeError:
        chains = []
    return cov + chains


BOOT_ENTS = ("burner-mining-drill", "stone-furnace", "iron-chest", "wooden-chest", "lab")


def removals(plan, live, places):
    """Burner-phase entities (and the hut lab) standing where the increment builds: picked up first."""
    from auto import geom
    want = set()
    for p in places:
        want |= geom.footprint(p)
    real = set()     # tiles of planned splitters placed for real now: their stand-in belts go first
    for p in places:
        if p["item"] == "splitter":
            real |= geom.footprint(p)
    out = []
    for (x, y), n in live.extra.items():
        if n in BOOT_ENTS and geom.footprint({"item": n, "x": x, "y": y}) & want:
            out.append({"item": n, "x": x, "y": y, "remove": True})
        elif n == "transport-belt" and (math.floor(x), math.floor(y)) in real:
            out.append({"item": n, "x": x, "y": y, "remove": True})
    return out


def validate(plan, live, places):
    """Static checks before building: draftsman (overlaps, bad entities), nothing live in the way but trees."""
    from auto import dcheck, geom
    errs = [w for w in dcheck.check(places)][:5]
    mine = {}
    for i in live.built:
        for t in geom.footprint(plan.p[i]):
            mine[t] = plan.p[i]["item"]
    for (x, y), n in live.extra.items():
        if "electric-pole" not in n and n not in BOOT_ENTS and n != "transport-belt":
            mine[(math.floor(x), math.floor(y))] = n
    for p in places:
        if p.get("upgrade"):
            continue
        for t in geom.footprint(p):
            if t in mine and "electric-pole" not in p["item"]:
                errs.append(f"{p['item']} {p['x']},{p['y']} on live {mine[t]}")
                break
    return errs[:8]


# --------------------------------------------------------------------------------------------- the loop
def main():
    global LOG, JL
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--scratch", action="store_true")
    ap.add_argument("--chars", type=int, default=4)
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--rounds", type=int, default=100000)
    ap.add_argument("--dry", action="store_true", help="choose and plan increments, build nothing")
    ap.add_argument("--boot-minutes", type=float, default=20)
    a = ap.parse_args()
    if a.scratch:
        os.environ["FA_TARGET"] = "scratch"
    global SPEED
    SPEED = a.speed
    from auto import gamedata, mapdata
    out = mapdata.out_dir(a.seed)
    LOG, JL = out / "grow.log", out / "grow.jsonl"
    gd = gamedata.load()
    global GD
    GD = gd
    dem = json.loads((out / "demand.json").read_text())
    PLAN_RECIPES["r"] = dem["recipes"]
    m = mapdata.Map.load(a.seed)
    from auto import scout
    scout.load(m, out)              # scouted ground beyond the seed's map (radars, auto.scout)
    log(f"=== grow seed {a.seed} on {'scratch' if a.scratch else 'live'}")
    plan = Plan(a.seed, gd, dem)
    global PLAN
    PLAN = plan
    log(f"plan: {len(plan.p)} pieces, {len(plan.groups)} groups ({len(plan.prod)} producing)")
    boot_dir = out / "boot"
    boot_files = [boot_dir / f"boot-{k}.json" for k in ("iron", "copper", "coal", "stone")]
    boot = [e for f in boot_files for e in json.loads(f.read_text())["place"]]
    os.environ["FA_BOOT_DIR"] = str(boot_dir)
    from auto.autobuild import pick_depot, clear, split
    dx, dy = pick_depot({"complex": plan.p, "world": []}, boot)
    # once the mall runs, the builders work and fetch around it: their depot moves next to the mall chests
    mall_ch = [p for p in plan.p if p["item"] == "iron-chest" and p.get("mall")]
    cen = (sum(p["x"] for p in mall_ch) / len(mall_ch), sum(p["y"] for p in mall_ch) / len(mall_ch)) if mall_ch else (0, 0)
    mall_depot = pick_depot({"complex": plan.p, "world": []}, boot, cen, m)
    os.environ["FA_DEPOT"] = f"{dx},{dy}"
    from bridge import Bridge   # noqa: E402  (reads FA_TARGET)
    import macros
    import supply
    from auto import control, live as lv, poles, research
    chars = [f"b{i + 1}" for i in range(a.chars)]
    ctl = Bridge(chars[0], autostart=False)

    def q(lua):
        return ctl.raw(lua)
    L = Live(q, plan)
    sfile = out / "grow-state.json"
    state = json.loads(sfile.read_text()) if sfile.exists() else {"boot": False, "handcraft": True, "n": 0}

    def save():
        sfile.write_text(json.dumps(state))
    macros.HANDCRAFT = state["handcraft"]
    L_retired = set(state.get("retired", []))
    ch = Chooser(plan, L, dem, state["handcraft"])
    ch.m = m
    ch.seed, ch.out, ch.budget = a.seed, out, None
    if not a.dry:
        ctl.call("set_mode", mode="realtime")
        q(f"/silent-command game.speed={a.speed} game.tick_paused=false rcon.print(1)")

        def keep_speed():   # from the start, bootstrap included: a player joining resets the speed to 1
            sb = Bridge("r2", autostart=False)
            while True:
                try:
                    r0 = sb.raw(f"/silent-command if game.speed ~= {a.speed} or game.tick_paused then "
                                f"game.speed={a.speed} game.tick_paused=false rcon.print('reset') end")
                    if r0 and "reset" in r0:
                        log(f"game speed was off (a player joined?): set back to {a.speed}x")
                except Exception:
                    pass
                time.sleep(20)
        threading.Thread(target=keep_speed, daemon=True).start()

        def keep_limits():   # the round's mall caps sent again every 10 s: a whitelist set once a round (at 20x a
            sb = Bridge("r3", autostart=False)      # round can be an hour of game time) lets one item refill the bar
            while True:
                time.sleep(10)
                try:
                    if LIMITS["cmd"]:
                        sb.raw(LIMITS["cmd"])
                    if LIMITS["makers"]:
                        sb.raw(LIMITS["makers"])
                except Exception:
                    pass
        threading.Thread(target=keep_limits, daemon=True).start()
    L.retired = L_retired
    L.own = state.get("extra_places", [])
    L.dead = set(state.get("dead", []))
    n = L.refresh()
    L.researched = lv.researched(q)
    log(f"live: {n} entities, {len(L.built)} plan pieces built, {len(L.researched)} techs researched")
    # groups mostly standing were attempted (an earlier run): a hole in them is repaired like any failed build
    state.setdefault("tried", [])
    for g in plan.prod:
        mach = [i for i in plan.groups[g] if not plan.flow.transport(i)]
        nb = sum(1 for i in mach if i in L.built)
        if mach and nb >= 0.5 * len(mach) and g not in state["tried"]:
            state["tried"].append(g)

    env = dict(os.environ)
    import subprocess
    py = sys.executable
    tender = None
    if not a.dry:
        if not state["boot"]:
            plans = ",".join(str(f) for f in boot_files)
            procs = [subprocess.Popen([py, str(AGENT / "bootstrap.py"), c, plans, "--share", f"{i}/{len(chars)}",
                                       "--minutes", str(a.boot_minutes)], env=env, cwd=str(AGENT),
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                     for i, c in enumerate(chars)]
            outs = [p.communicate()[0] for p in procs]
            for c, o in zip(chars, outs):
                log(f"boot {c}: {o.strip().splitlines()[-1] if o.strip() else '(no output)'}")
            state["boot"] = True   # partial shares are fine: a burner spot can be blocked (wreck, character)
            save()
        power_file = out / "grow-power.json"
        power_file.write_text(json.dumps({"place": [plan.p[i] for i in plan.groups.get("power", [])] +
                                          [plan.p[i] for g in plan.groups if g.startswith("power:") for i in plan.groups[g]]}))
        # the tender hand-feeds the boilers only in the bootstrap, on a character of its own; after it the coal belt
        # does (power links first) and one builder does all the work: no two characters race for one chest
        tender = start_tender(py, chars[-1], boot_dir, power_file, env) if state["handcraft"] and len(chars) > 1 else None
    builders = chars[:-1] if len(chars) > 1 and tender else chars
    gate = Gate(plan, L, ch, q, builders, Bridge)      # every build path asks it first: build, alt or skip
    ch.have = gate.have

    def held(g, v, rnd, n=4):
        """A build path the gate did not pass: its alt goes first next round, g itself waits n rounds (a repair
        keeps its skip counter, so it cannot loop)."""
        if v[0] == "alt":
            ch.pending.append((v[1], v[2]))
            log(f"round {rnd}: {g} waits for {v[1]}: {v[2]}")
        else:
            log(f"round {rnd}: {g} short {dict(v[2])}: not in any chest the builders reach yet")
            have_ = dict(gate.have()) if g not in plan.groups else {}
            if g not in plan.groups and any(WANT[k_] < n_ + have_.get(k_, 0) for k_, n_ in v[2].items()):
                # not a plan group: the mall caps know nothing of it, so they make room for all it needs (the stock
                # already there counts against the cap, so the short alone would leave the maker capped)
                for k_, n_ in v[2].items():
                    WANT[k_] = max(WANT[k_], n_ + have_.get(k_, 0))
                try:
                    mall_limits(plan, L, q, L.researched, pole_item)
                except Exception as e:
                    log("mall limits:", str(e)[:120])
        if n:
            ch.skip[g] = n
    hut = hut_spot(plan, boot, m)
    log("hut lab (bootstrap research) at", hut)
    stop = threading.Event()
    rstate = {"want": []}
    if not a.dry:
        threading.Thread(target=research_loop, daemon=True,
                         args=(Bridge("r1", autostart=False), q, rstate, hut, state, stop, gate)).start()

    targets = control.lp_targets(dem)
    track = sorted(set(targets) | {"iron-plate", "copper-plate", "coal", "stone", "iron-ore", "copper-ore"})
    sched = {"s": None, "key": None, "busy": False}
    labs_n = sum(1 for i in plan.groups.get("labs", []) if plan.p[i]["item"] == "lab") or 1

    def sec_per_piece():
        """The crew's measured pace (game seconds per placed piece), from this run's increments."""
        rows = [json.loads(x) for x in open(JL, encoding="utf-8") if x.strip()] if JL.exists() else []
        r = sorted(x["pieces"] / x["minutes"] for x in rows if x.get("pieces") and (x.get("minutes") or 0) > 0.2)
        return 60 * a.speed / r[len(r) // 2] if r else 3.5

    def resolve(built, researched, stock0=None, rates=None, coal=None):
        """CP-SAT schedule from what stands now (auto.schedule), in a thread: the controller keeps working.
        stock0/rates/coal (what the builders can take, measured made rates, coal under the coal drills) feed its
        material flow phase."""
        from auto import schedule
        try:
            r = schedule.solve(plan, built, researched, gd, dem, sec_per_piece=sec_per_piece(), labs=labs_n,
                               seconds=60, retired=set(L.retired), early_mall=EARLY_MALL, stock0=stock0, rates=rates,
                               coal_left=coal)
            sched["s"] = r
            c_ = r.get("coal") or {}
            log(f"schedule {r['status']}: rocket at {(r.get('makespan') or 0) / 3600:.1f} game h; next "
                f"{r['order'][:5]}; research {r['research'][:4]}; hand {len(r['hand'])} groups; "
                f"flow {r.get('flow_status')}" + (f"; coal need {c_.get('need')} left {c_.get('left')}" if c_ else "")
                + (f"; DROPPED {r['conflict']}" if r.get("conflict") else ""))
        except Exception as e:
            log("schedule error:", str(e)[:200])
        finally:
            sched["busy"] = False
    try:
        for rnd in range(a.rounds):
            for kk in list(ch.skip):
                ch.skip[kk] -= 1
            L.refresh()
            HAVE["v"] = None                                  # the gate reads the stock afresh each round
            macros.ALWAYS_HAND = {"small-electric-pole"}      # orphan exceptions last one round (the gate adds them)
            state["placed"] = sorted(set(state.get("placed", ())) | L.built)
            L.state_gone = state.get("gone", [])
            L.researched = lv.researched(q)
            if not a.dry:     # every round: a two-item chest's per-item caps live on its inserter's whitelist
                try:
                    r_ = mall_limits(plan, L, q, L.researched, "medium-electric-pole" if not state["handcraft"] else "small-electric-pole")
                    if r_ != state.get("limits"):
                        state["limits"] = r_
                        log(f"mall limits (chests, slot-filtered, whitelisted inserters): {r_}")
                except Exception as e:
                    log("mall limits:", str(e)[:120])
            # medium poles once researched AND the mall makes them (their maker and chest stand)
            med = list(plan.makers.get("stock:medium-electric-pole", [])) + mall_groups(plan, "medium-electric-pole")
            pole_item = "medium-electric-pole" if ("electric-energy-distribution-1" in L.researched and
                                                   not state["handcraft"] and med and
                                                   all(L.group_done(h) for h in med)) else "small-electric-pole"
            poles.use(pole_item)
            gate.pole_item = pole_item
            set_pending_recipes(q, state, L.researched, log)
            oil_upkeep(plan, L, q)
            try:
                from auto import scout as scout_
                scout_.sweep(q, state)      # the radars' sweep (a playerless server does not run it)
            except Exception as e:
                log("sweep:", str(e)[:120])
            DEAD.clear()
            DEAD.update(state.get("dry", []))
            DEAD.update(L.retired)
            if not state["handcraft"] and supply.DEPOT[0] != mall_depot:
                supply.DEPOT[:] = [(mall_depot[0] + i, mall_depot[1]) for i in range(4)]
                log(f"depot moves next to the mall: {mall_depot}")
            global MALL_MAKES
            MALL_MAKES = {it for it in EARLY_MALL + ["assembling-machine-2", "medium-electric-pole", "chemical-plant",
                                                     "oil-refinery", "pumpjack"]
                          if any(L.group_done(h) for h in plan.makers.get("stock:" + it, []) + mall_groups(plan, it))}
            snap = control.measure(q, track) if not a.dry else {"made": {}, "used": {}, "stock": {}}
            ch.votes.clear()
            done_now = frozenset(h for h in plan.prod if L.group_done(h))
            key = (done_now, frozenset(L.researched))
            if key != sched["key"] and not sched["busy"] and not a.dry:
                sched["key"], sched["busy"] = key, True
                try:      # the flow phase starts from what the builders can take, measured rates, coal left
                    stat_ = {(round(x, 2), round(y, 2)): s_ for n_, x, y, s_ in
                             lv.status(q, plan.box(sorted(L.built), 2))} if L.built else {}
                    sargs = (dict(gate.have()), made_rates(plan, L, snap, stat_), coal_left(q, plan))
                except Exception as e:
                    log("schedule inputs:", str(e)[:120])
                    sargs = (None, None, None)
                threading.Thread(target=resolve, args=(set(L.built), set(L.researched)) + sargs, daemon=True).start()
            if a.dry and sched["s"] is None:
                sched["busy"] = True
                resolve(set(L.built), set(L.researched))
            # rule 2: everything may be hand-crafted while no mall runs: the bootstrap (the early mall not built yet)
            # or a dead grid (no steam: the mall makes nothing); otherwise only what the schedule decides to
            # hand-craft (its objective prices every such piece)
            dead = not a.dry and not state["handcraft"] and grid_dead(q)
            if dead != state.get("grid_dead", False):
                state["grid_dead"] = dead
                log("the grid makes no power: hand-crafting until steam is back" if dead else "steam is back")
            s_ = sched["s"] or {}
            macros.HANDCRAFT = state["handcraft"] or dead
            gate.hand = set(s_.get("hand", []))
            g, why = choose(plan, L, ch, snap, targets, state, q, sched["s"], None if a.dry else gate)
            ch.want_techs = list(s_.get("research", [])) or [t for t, _ in ch.votes.most_common()]
            rstate["want"] = ch.want_techs + [t for t in dem["techs"] if t not in ch.want_techs]
            macros.HANDCRAFT = state["handcraft"] or dead or (bool(s_) and g in s_.get("hand", []))
            if g is None:
                log(f"round {rnd}: nothing to build ({why}); waiting")
                if a.dry:
                    break
                if launched(q):               # the silo is the last group: its rocket goes up while nothing is left
                    log("ROCKET LAUNCHED")
                    break
                time.sleep(30)
                continue
            if g.startswith("relocate:"):       # dry drills moved onto ore that is left (auto.relocate, CP-SAT)
                from auto import geom, relocate
                h = g[9:]
                _, ds = L.relocate
                occ = set()
                for i in L.built:
                    occ |= geom.footprint(plan.p[i])
                for (x_, y_), n_ in L.extra.items():
                    occ |= geom.footprint({"item": n_, "x": x_, "y": y_})
                # plan pieces still to come stay free, except this mine's own drill slots
                occ |= {t for i, p_ in enumerate(plan.p) if i not in L.built and i not in L.dead
                        and p_.get("grp") not in L.retired and p_.get("grp") != h for t in geom.footprint(p_)}
                belts = [plan.p[i] for i in plan.groups[h] if plan.p[i]["item"] == "transport-belt" and i in L.built]
                ore = {"raw-iron-ore": "iron-ore"}.get(h.split(":")[1], h.split(":")[1])
                pl_, info = relocate.plan(q, m, plan, L, ds, belts, ore, occ, both_sides=h.startswith("mine:coal"))
                if not pl_:
                    # no spot for them: only these drills are written off (they stand, mining nothing); the group
                    # keeps supplying and is dry only when the live measurement says so (dry_mine, >= 70%)
                    for d_ in ds:
                        i_ = plan.key.get((round(d_["x"], 2), round(d_["y"], 2)))
                        if i_ is not None and plan.p[i_]["item"] == "electric-mining-drill":
                            L.dead.add(i_)
                        else:
                            state.setdefault("dry_own", []).append([round(d_["x"], 2), round(d_["y"], 2)])
                    state["dead"] = sorted(L.dead)
                    if h not in state.setdefault("no_relocate", []):
                        state["no_relocate"].append(h)
                    log(f"round {rnd}: {g}: {info}; {len(ds)} dry drills written off, {h} mines on")
                    save()
                    continue
                new_ = [p_ for p_ in pl_ if not p_.get("remove")]
                pp = plan_poles(plan, L, new_, m)
                gv = gate(g, new_ + pp, back=[p_ for p_ in pl_ if p_.get("remove")])   # the moved drills come back
                if gv[0] != "build":
                    held(g, gv, rnd, 6)
                    continue
                state["n"] += 1
                k = state["n"]
                log(f"#{k} {g}: {info} + {len(pp)} poles — {why}")
                build(builders[:1], [p_ for p_ in pl_ if p_.get("remove")], clear, split, supply, Bridge)
                res = build(builders, new_ + pp, clear, split, supply, Bridge)
                # moved plan drills are dead (never put back); moved own drills leave our list
                for d_ in ds:
                    i_ = plan.key.get((round(d_["x"], 2), round(d_["y"], 2)))
                    if i_ is not None and plan.p[i_]["item"] == "electric-mining-drill":
                        L.dead.add(i_)
                gone_own = {(round(d_["x"], 2), round(d_["y"], 2)) for d_ in ds}
                state["extra_places"] = [p_ for p_ in state.get("extra_places", [])
                                         if (round(p_["x"], 2), round(p_["y"], 2)) not in gone_own] + \
                    [{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y", "direction")} for p_ in new_ + pp]
                L.own = state["extra_places"]
                state["dead"] = sorted(L.dead)
                L.refresh()
                time.sleep(max(3, 60 / a.speed))
                st_ = lv.status(q, (min(p_["x"] for p_ in new_) - 3, min(p_["y"] for p_ in new_) - 3,
                                    max(p_["x"] for p_ in new_) + 3, max(p_["y"] for p_ in new_) + 3))
                at_ = {(round(x, 2), round(y, 2)): s_ for n_, x, y, s_ in st_}
                cnt = collections.Counter(at_.get((round(p_["x"], 2), round(p_["y"], 2)), "missing") for p_ in new_)
                log(f"#{k} relocated {h}: new drills {dict(cnt)}; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "info": info, "drills": dict(cnt)})
                if cnt.get("missing", 0):
                    ch.skip[g] = 6
                save()
                continue
            if g.startswith("outpower:"):   # an outpost's pole chain along its route to the powered network
                o = state["outposts"][int(g[9:])]
                pcs = outpost_pieces(state, o)
                from auto import poles as poles_
                was = poles_.ITEM
                poles_.use("small-electric-pole")     # hand-made from wood (see the outpost branch)
                try:
                    pp = route_poles(plan, L, pcs, q, m, box=o["box"])
                finally:
                    poles_.use(was)
                if not pp:
                    log(f"round {rnd}: {g}: no pole chain along {len(pcs)} pieces of its route")
                    ch.skip[g] = 40
                    continue
                gv = gate(g, pp)
                if gv[0] != "build":
                    held(g, gv, rnd, 20)
                    continue
                state["n"] += 1
                k = state["n"]
                log(f"#{k} {g}: {len(pp)} poles along {len(pcs)} route pieces — {why}")
                res = build(builders, pp, clear, split, supply, Bridge)
                state.setdefault("extra_places", []).extend([{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y")}
                                                             for p_ in pp])
                L.own = state["extra_places"]
                time.sleep(max(3, 60 / a.speed))
                st_ = lv.status(q, tuple(o["box"]))
                cnt = collections.Counter(s_ for n_, *_x, s_ in st_ if n_ == "electric-mining-drill")
                log(f"#{k} {g}: drills {dict(cnt)}; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(pp), "status": dict(cnt)})
                ch.skip[g] = 10
                save()
                continue
            if g == "trim":                 # belt tails past the last pickup picked up and written off
                state["n"] += 1
                k = state["n"]
                bad = L.trim
                log(f"#{k} trim: picks up {len(bad)} tail belts — {why}")
                res = build(builders[:1], [dict(plan.p[i], remove=True) for i in bad], clear, split, supply, Bridge)
                supply.deposit(Bridge(builders[0], autostart=False))
                L.refresh(plan.box(bad, 4))
                gone_ = [i for i in bad if i not in L.built]
                L.dead |= set(gone_)
                state["dead"] = sorted(L.dead)
                state["placed"] = [i for i in state.get("placed", []) if i not in set(gone_)]
                log(f"#{k} trim: {len(gone_)} of {len(bad)} picked up; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(gone_)})
                save()
                continue
            if g == "face":                 # machines turned to their planned facing (auto: misfaced)
                state["n"] += 1
                k = state["n"]
                r_ = misfaced(plan, L, q, fix=True)
                ok_ = [t for t in r_ if not t.startswith("!")]
                log(f"#{k} face: turned {len(ok_)} machines to their planned facing, {len(r_) - len(ok_)} would not "
                    f"turn — {why}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(ok_)})
                save()
                continue
            if g.startswith(("radar:", "outpost:")):   # scouting (auto.scout): reveal the map / tap a far patch
                from auto import geom, remine, scout
                stock = dict(gate.have())
                if g.startswith("radar:"):
                    x_, y_ = (float(v_) for v_ in g[6:].split(","))
                    new_, cor, h = [{"item": "radar", "x": x_, "y": y_}], None, None
                    info = "radar"
                else:
                    n_, pc, nd = L.outpost
                    bx = pc["box"]
                if g.startswith("outpost:crude-oil"):
                    # pumpjacks on the patch's free wells, piped into the built crude network (scout.pumpjack_outpost)
                    h = "pipe:crude"
                    ci = [i_ for i_ in L.built if plan.p[i_].get("grp") in ("pipe:crude", "oil-wells")]
                    cb = plan.box(ci, 0) if ci else bx
                    rb = (min(bx[0], cb[0]) - 24, min(bx[1], cb[1]) - 24, max(bx[2], cb[2]) + 24, max(bx[3], cb[3]) + 24)
                    try:
                        nx_ = scout.extend(m, out, rb, qf=lambda s_: q("/silent-command " + s_))
                        new_, info = scout.pumpjack_outpost(m, plan, L, pc.get("wells") or scout.free_wells(q, bx), nd)
                        info = f"{info}; map +{nx_} strips"
                    except Exception as e:
                        new_, info = None, f"pumpjack outpost planning failed: {str(e)[:160]}"
                    if not new_:
                        log(f"round {rnd}: {g}: {info}")
                        ch.skip[g] = 40
                        continue
                    cor = {(t_[0] + a_, t_[1] + b_) for t_ in geom.tiles(new_) for a_ in range(-3, 4)
                           for b_ in range(-3, 4)}
                elif g.startswith("outpost:"):
                    # the mine to feed: one whose own ore is gone first (its network starves: a dry coal column's
                    # belt carries the fuel lane of its smelters), one outpost per mine; then the nearest
                    fed = {o_["group"] for o_ in state.get("outposts", [])}
                    tg = [h_ for h_ in plan.prod if h_.startswith("mine:") and L.group_done(h_) and h_ not in fed
                          and {"raw-iron-ore": "iron-ore"}.get(h_.split(":")[1], h_.split(":")[1]) == n_]
                    try:
                        st_ = lv.status(q, plan.box([i_ for h_ in tg for i_ in plan.groups[h_]], 4)) if tg else []
                    except Exception:
                        st_ = []
                    at_ = {(round(x, 2), round(y, 2)): s_ for n__, x, y, s_ in st_ if n__ == "electric-mining-drill"}

                    def dry_(h_):
                        ss = [at_.get((round(plan.p[i_]["x"], 2), round(plan.p[i_]["y"], 2)), "gone")
                              for i_ in plan.groups[h_] if plan.p[i_]["item"] == "electric-mining-drill"]
                        return sum(1 for x_ in ss if x_ in ("no_minable_resources", "gone")) / max(1, len(ss))
                    tg.sort(key=lambda h_: (-round(dry_(h_), 1), math.dist(pc["center"], plan.box(plan.groups[h_], 0)[:2])))
                    new_, info, h = None, "no working mine of that resource to feed", None
                    for h in tg[:3]:
                        hb = plan.box(plan.groups[h], 0)
                        rb = (min(bx[0], hb[0]) - 24, min(bx[1], hb[1]) - 24, max(bx[2], hb[2]) + 24,
                              max(bx[3], hb[3]) + 24)
                        try:
                            nx_ = scout.extend(m, out, rb, qf=lambda s_: q("/silent-command " + s_))
                            pl_, info = remine.plan(q, m, plan, L, h, stock=stock, outpost=bx, n_new=nd)
                        except Exception as e:
                            pl_, info = None, f"outpost planning failed: {str(e)[:160]}"
                        if pl_:
                            new_ = [p_ for p_ in pl_ if not p_.get("remove")]
                            info = f"{info}; map +{nx_} strips"
                            break
                    if not new_:
                        log(f"round {rnd}: {g}: {info}")
                        ch.skip[g] = 10
                        # the mine plan leaves this patch out from now on: the next pass picks the next best one
                        state.setdefault("outpost_failed", []).append(list(bx))
                        continue
                    # the pole chain runs along the route: tiles within 3 of every new piece
                    cor = {(t_[0] + a_, t_[1] + b_) for t_ in geom.tiles(new_) for a_ in range(-3, 4)
                           for b_ in range(-3, 4)}
                # an outpost's long chain uses small poles: the builders make them from wood by hand (the one item
                # always hand-made), while medium poles need steel from a factory that may be starving for the very
                # resource the outpost brings (measured: a coal outpost held for 41 medium poles while coal ran out)
                from auto import poles as poles_
                was = poles_.ITEM
                if g.startswith("outpost:"):
                    poles_.use("small-electric-pole")
                try:
                    pp = plan_poles(plan, L, new_, m, corridor=cor)
                    if g.startswith("outpost:"):
                        pp += route_poles(plan, L, new_ + pp, q, m)
                finally:
                    poles_.use(was)
                gv = gate(g, new_ + pp)
                if gv[0] != "build":
                    held(g, gv, rnd, 60)          # the scout pass runs every 20 rounds: let the mall catch up
                    continue
                state["n"] += 1
                k = state["n"]
                log(f"#{k} {g}: {info} + {len(pp)} poles — {why}")
                res = build(builders, new_ + pp, clear, split, supply, Bridge)
                xs_ = [p_["x"] for p_ in new_ + pp]
                ys_ = [p_["y"] for p_ in new_ + pp]
                L.refresh((min(xs_) - 4, min(ys_) - 4, max(xs_) + 4, max(ys_) + 4))
                here = {(round(x, 2), round(y, 2)) for (x, y) in L.extra}
                have_ = {(round(p_["x"], 2), round(p_["y"], 2)) for p_ in state.get("extra_places", [])}
                # every piece is ours to keep standing (restore retries what did not place this time)
                state.setdefault("extra_places", []).extend(
                    [{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y", "direction", "belt_type")}
                     for p_ in new_ + pp if (round(p_["x"], 2), round(p_["y"], 2)) not in have_])
                L.own = state["extra_places"]
                stood = sum(1 for p_ in new_ + pp if (round(p_["x"], 2), round(p_["y"], 2)) in here)
                time.sleep(max(3, 60 / a.speed))
                dr = [p_ for p_ in new_ if p_["item"] in ("radar", "electric-mining-drill", "pumpjack")]
                st_ = lv.status(q, (min(p_["x"] for p_ in dr) - 4, min(p_["y"] for p_ in dr) - 4,
                                    max(p_["x"] for p_ in dr) + 4, max(p_["y"] for p_ in dr) + 4))
                at_ = {(round(x, 2), round(y, 2)): s_ for n__, x, y, s_ in st_}
                cnt = collections.Counter(at_.get((round(p_["x"], 2), round(p_["y"], 2)), "missing") for p_ in dr)
                if g.startswith("radar:") and (round(x_, 2), round(y_, 2)) in here:   # live.status has no radars
                    state.setdefault("radars", []).append([x_, y_])
                    state.pop("radar_site", None)
                elif g.startswith("outpost:") and cnt.get("missing", 0) <= len(dr) // 2:
                    # it stands: recorded, so the power repair (outpower) can finish its chain when drills lack power
                    state.setdefault("outposts", []).append({"name": n_, "group": h, "box": list(bx), "drills": nd})
                else:
                    ch.skip[g] = 10
                log(f"#{k} {g}: {stood}/{len(new_ + pp)} placed; {dict(cnt)}; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "info": info, "status": dict(cnt)})
                save()
                continue
            if g.startswith("remine:"):         # a dry mine replaced from live ore (auto.remine)
                from auto import remine
                h = g[7:]
                stock = dict(gate.have())     # routes tunnel only with undergrounds the builders can get
                pl_, info = remine.plan(q, m, plan, L, h, stock=stock)
                bridge = False
                if not pl_ and h.startswith("mine:coal") and state.get("bridged", []).count(h) < 3:
                    # no ore left to mine here: feed the network this column fed from a live coal column instead
                    live_src = [x for x in plan.prod if x.startswith("mine:coal") and x not in DEAD and L.group_done(x)]
                    pl_, info2 = remine.fuel_bridge(q, m, plan, L, h, live_src, stock=stock, researched=L.researched)
                    info = f"{info}; fuel bridge: {info2}"
                    bridge = bool(pl_)
                if not pl_:
                    log(f"round {rnd}: {g}: {info}")
                    ch.skip[g] = 20
                    continue
                new_ = [p_ for p_ in pl_ if not p_.get("remove")]
                need_t = techs_of({p_["item"] for p_ in new_}, gd) - L.researched
                if need_t:
                    log(f"round {rnd}: {g} waits for research {sorted(need_t)}")
                    ch.skip[g] = 4
                    continue
                pp = plan_poles(plan, L, new_, m)
                gv = gate(g, new_ + pp)       # the dry drills are picked up only after the new column stands
                if gv[0] != "build":
                    held(g, gv, rnd, 6)
                    continue
                state["n"] += 1
                k = state["n"]
                log(f"#{k} {g}: {info} + {len(pp)} poles — {why}")
                res = build(builders, new_ + pp, clear, split, supply, Bridge)
                xs_ = [p_["x"] for p_ in new_ + pp]
                ys_ = [p_["y"] for p_ in new_ + pp]
                L.refresh((min(xs_) - 4, min(ys_) - 4, max(xs_) + 4, max(ys_) + 4))
                here = {(round(x, 2), round(y, 2)) for (x, y) in L.extra}
                stood = [p_ for p_ in new_ + pp if (round(p_["x"], 2), round(p_["y"], 2)) in here]
                state.setdefault("extra_places", []).extend(
                    [{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y", "direction", "belt_type")}
                     for p_ in stood])
                L.own = state["extra_places"]
                time.sleep(max(3, 60 / a.speed))
                # a fuel bridge is judged by its inserters (whatever type fuel_bridge chose), a column by its drills
                dr = [p_ for p_ in new_ if (p_["item"].endswith("inserter") if bridge else
                                            p_["item"] == "electric-mining-drill")]
                st_ = lv.status(q, (min(p_["x"] for p_ in dr) - 4, min(p_["y"] for p_ in dr) - 4,
                                    max(p_["x"] for p_ in dr) + 4, max(p_["y"] for p_ in dr) + 4)) if dr else []
                at_ = {(round(x, 2), round(y, 2)): s_ for n_, x, y, s_ in st_}
                cnt = collections.Counter(at_.get((round(p_["x"], 2), round(p_["y"], 2)), "missing") for p_ in dr)
                if cnt.get("missing", 0) <= len(dr) // 2:
                    # the new column stands: every planned piece is ours to keep standing (restore retries what did
                    # not place, e.g. an underground the mall had none of yet); the dry drills are picked up (reused)
                    # and the group retires
                    have = {(round(p_["x"], 2), round(p_["y"], 2)) for p_ in state["extra_places"]}
                    state["extra_places"] += [{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y", "direction",
                                                                                     "belt_type")}
                                              for p_ in new_ + pp if (round(p_["x"], 2), round(p_["y"], 2)) not in have]
                    L.own = state["extra_places"]
                    build(builders[:1], [p_ for p_ in pl_ if p_.get("remove")], clear, split, supply, Bridge)
                    if bridge:
                        state.setdefault("bridged", []).append(h)
                    L.retired.add(h)
                    state.setdefault("retired", []).append(h)
                    state["placed"] = [i for i in state.get("placed", []) if plan.p[i].get("grp") != h]
                else:
                    ch.skip[g] = 6
                log(f"#{k} remine {h}: new drills {dict(cnt)}; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "info": info, "drills": dict(cnt)})
                save()
                continue
            if g.startswith("flush:"):          # pick the jammed belts up (their load comes along) and lay them again
                state["n"] += 1
                k = state["n"]
                h = g[6:]
                bad = state.get("jam", {}).get(h, [])
                log(f"#{k} {g}: {len(bad)} belts — {why}")
                # two steps: pick up, then lay again (one plan with both would pick the new belts up again); one
                # builder, the one that carries the belts
                build(builders[:1], [dict(plan.p[i], remove=True) for i in bad], clear, split, supply, Bridge)
                L.refresh(plan.box(bad, 6))
                pl_ = [materialize(plan.p[i], L.researched, pole_item)[0] for i in bad]
                gv = gate(g, pl_)                 # the belts came back with the pickup
                if gv[0] != "build":
                    log(f"#{k} {g}: the belts did not all come back: {gv[2] if gv[0] == 'skip' else gv[1]}")
                res = build(builders[:1], pl_, clear, split, supply, Bridge)   # lays what it has either way
                supply.deposit(Bridge(builders[0], autostart=False))
                state.setdefault("flushed", {})[h] = flushes(state, h, L) + 1
                state.setdefault("flush_at", {})[h] = len(L.built)
                L.refresh(plan.box(bad, 6))
                log(f"#{k} flushed {len(bad)} belts of {h}: {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(bad)})
                save()
                continue
            if g.startswith("drain:"):          # a mall chest full of one item locks its other item out
                state["n"] += 1
                k = state["n"]
                x_, y_ = (float(v_) for v_ in g[6:].split(","))
                b0 = Bridge(builders[0], autostart=False)
                before_ = chest_contents(q, [(x_, y_)])[0]
                log(f"#{k} {g}: holds {before_} — {why}")
                # the builder picks the chest up with its contents (room first: up to 32 stacks come along) and
                # places it again empty; the contents go to the depot, where every build can still fetch them
                supply.unload(b0, collections.Counter(), low=40)
                build(builders[:1], [{"item": "iron-chest", "x": x_, "y": y_, "remove": True}], clear, split, supply, Bridge)
                res = build(builders[:1], [{"item": "iron-chest", "x": x_, "y": y_}], clear, split, supply, Bridge)
                supply.unload(b0, collections.Counter(), low=70)
                dep = supply.deposit(b0)
                try:
                    mall_limits(plan, L, q, L.researched, pole_item)     # the new chest has no bar yet
                except Exception as e:
                    log("mall limits:", str(e)[:120])
                after_ = chest_contents(q, [(x_, y_)])[0]
                L.refresh((x_ - 6, y_ - 6, x_ + 6, y_ + 6))
                log(f"#{k} drained {g}: {sum(before_.values())} items -> depot (last deposit {dep}); chest now "
                    f"{after_}; {str(res)[:120]}")
                record({"n": k, "groups": [g], "why": why, "before": before_, "after": after_})
                ch.skip[g] = 30
                save()
                continue
            if g.startswith("spent:"):          # a block nothing needs any more: picked up, its load to the depot
                state["n"] += 1
                k = state["n"]
                h = g[6:]
                idx = L.spent
                b0 = Bridge(builders[0], autostart=False)
                # inserters first (they hold an item each), then the machines (inputs and outputs), the belts last
                rank = lambda i: (0 if "inserter" in plan.p[i]["item"] else 1 if "assembling" in plan.p[i]["item"] else 2)   # noqa: E731
                log(f"#{k} {g}: picks up {dict(collections.Counter(plan.p[i]['item'] for i in idx))} — {why}")
                res, dep = None, {}
                for _ in range(3):              # a full inventory stops a pickup: empty it into the depot, go on
                    left_ = [i for i in sorted(idx, key=rank) if i in L.built]
                    if not left_:
                        break
                    supply.unload(b0, collections.Counter(), low=40)
                    res = build(builders[:1], [dict(plan.p[i], remove=True, item=L.names.get(
                        (round(plan.p[i]["x"], 2), round(plan.p[i]["y"], 2)), plan.p[i]["item"])) for i in left_],
                        clear, split, supply, Bridge)
                    dep = supply.deposit(b0)
                    L.refresh(plan.box(idx, 4))
                gone_ = [i for i in idx if i not in L.built]
                L.dead |= set(gone_)
                state["dead"] = sorted(L.dead)
                state["placed"] = [i for i in state.get("placed", []) if i not in set(gone_)]
                if len(gone_) == len(idx):
                    L.retired.add(h)          # supplies nothing: repairs, the schedule and the mall caps leave it
                    state.setdefault("retired", []).append(h)
                else:
                    ch.skip[g] = 20
                log(f"#{k} {g}: {len(gone_)} of {len(idx)} picked up, load to the depot (last deposit {dep}); "
                    f"{str(res)[:120]}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(gone_)})
                save()
                continue
            if g == "pairs":                    # own undergrounds the game paired with plan ones: picked up
                state["n"] += 1
                k = state["n"]
                rm_ = L.pairs
                log(f"#{k} pairs: picks up {len(rm_)} own undergrounds {[(p_['x'], p_['y']) for p_ in rm_]} — {why}")
                build(builders[:1], [dict(p_, remove=True) for p_ in rm_], clear, split, supply, Bridge)
                L.refresh((min(p_["x"] for p_ in rm_) - 8, min(p_["y"] for p_ in rm_) - 8,
                           max(p_["x"] for p_ in rm_) + 8, max(p_["y"] for p_ in rm_) + 8))
                # only what is really gone leaves our list (restore would put it back); one still standing stays
                # ours, so the next scan finds it again
                gone_ = {(round(p_["x"], 2), round(p_["y"], 2)) for p_ in rm_
                         if L.names.get((round(p_["x"], 2), round(p_["y"], 2))) != p_["item"]}
                state["extra_places"] = [p_ for p_ in state.get("extra_places", [])
                                         if (round(p_["x"], 2), round(p_["y"], 2)) not in gone_]
                L.own = state["extra_places"]
                # the robbed plan underground should pair with its planned partner again by itself; one left unpaired
                # while that partner stands is laid again (a placed underground pairs with the nearest in reach). Both
                # stay "built", so no other repair would see the cut line
                near_ = [plan.p[i] for i in L.built if plan.p[i]["item"].endswith("underground-belt") and
                         any(abs(plan.p[i]["x"] - p_["x"]) + abs(plan.p[i]["y"] - p_["y"]) <= 12 for p_ in rm_)]
                lone_ = {xy for xy, r_, w_ in (pair_check(q, plan, near_, near_) if near_ else [])
                         if r_ is None and w_ is not None}
                re_ = [materialize(p_, L.researched, pole_item)[0] for p_ in near_ if (p_["x"], p_["y"]) in lone_]
                if re_:
                    log(f"#{k} pairs: {len(re_)} plan undergrounds stand unpaired: laid again {sorted(lone_)}")
                    build(builders[:1], [dict(p_, remove=True) for p_ in re_], clear, split, supply, Bridge)
                    build(builders[:1], re_, clear, split, supply, Bridge)
                    L.refresh((min(p_["x"] for p_ in re_) - 8, min(p_["y"] for p_ in re_) - 8,
                               max(p_["x"] for p_ in re_) + 8, max(p_["y"] for p_ in re_) + 8))
                ch.skip.pop("reconnect", None)    # the cut own route is laid again by reconnect (with spans)
                left_ = bad_pairs(plan, L, q, state)
                log(f"#{k} pairs: {len(left_)} plan undergrounds still paired with own ones")
                record({"n": k, "groups": [g], "why": why, "removed": sorted(gone_), "left": len(left_)})
                ch.skip["pairs_fix"] = 10
                save()
                continue
            if g == "unstick":                  # inserters holding what their machine cannot take: laid again
                state["n"] += 1
                k = state["n"]
                pl_ = [p_ for p_, _held, _rec in L.unstick]
                log(f"#{k} unstick: {[(p_['x'], p_['y'], h_, r_) for p_, h_, r_ in L.unstick][:6]} — {why}")
                build(builders[:1], [dict(p_, remove=True) for p_ in pl_], clear, split, supply, Bridge)
                L.refresh((min(p_["x"] for p_ in pl_) - 4, min(p_["y"] for p_ in pl_) - 4,
                           max(p_["x"] for p_ in pl_) + 4, max(p_["y"] for p_ in pl_) + 4))
                gv = gate(g, pl_)                 # the inserters came back with the pickup
                if gv[0] != "build":
                    log(f"#{k} unstick: not all inserters came back: {gv[2] if gv[0] == 'skip' else gv[1]}")
                res = build(builders[:1], pl_, clear, split, supply, Bridge)
                tries = state.setdefault("unstuck", {})
                for p_ in pl_:
                    kk = f"{p_['x']},{p_['y']}"
                    tries[kk] = tries.get(kk, 0) + 1
                L.refresh((min(p_["x"] for p_ in pl_) - 4, min(p_["y"] for p_ in pl_) - 4,
                           max(p_["x"] for p_ in pl_) + 4, max(p_["y"] for p_ in pl_) + 4))
                log(f"#{k} unstuck {len(pl_)} inserters; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(pl_)})
                ch.skip["unstick"] = 10
                save()
                continue
            if g.startswith("burner:"):        # more burner smelting while plates are what the builders lack
                state["n"] += 1
                k = state["n"]
                pl_ = burner_pairs(plan, L, m, g[7:], boot_dir, k)
                log(f"#{k} {g}: {len(pl_)} pieces — {why}")
                res = build(builders, pl_, clear, split, supply, Bridge) if pl_ else []
                L.refresh(plan.box(range(len(plan.p)), 8))
                if tender:
                    tender.terminate()
                tender = start_tender(py, chars[-1], boot_dir, out / "grow-power.json", env) if len(chars) > 1 else None
                state["short"] = {}
                state.setdefault("burners", {})[g[7:]] = state.get("burners", {}).get(g[7:], 0) + len(pl_) // 2
                log(f"#{k} built {sum(1 for p in pl_ if p['item'] == 'burner-mining-drill')} burner drills; "
                    f"tender restarted; {str(res)[:160]}")
                record({"n": k, "groups": [g], "why": why, "pieces": len(pl_), "build": str(res)[:300]})
                save()
                continue
            places, groups, idx = increment(plan, L, g, L.researched, pole_item)
            if g.startswith("power:") and hut and state["handcraft"] and hut_missing(L, hut):
                places = places + [dict(hut)]     # the bootstrap lab rides with the first power unit (its poles)
            from auto import stage as stage_mod   # every item placed must be craftable (a splitter needs logistics)
            need_t = (set().union(*(plan.techs.get(h, set()) for h in groups)) |
                      techs_of({p["item"] for p in places if p["item"] not in POLE_ITEMS}, gd)) - L.researched
            if need_t and not a.dry:     # e.g. the first steam plant's coal drills: research them first
                ch.want_techs = sorted(need_t) + [t for t in ch.want_techs if t not in need_t]
                rstate["want"] = ch.want_techs + [t for t in dem["techs"] if t not in ch.want_techs]
                log(f"round {rnd}: {g} waits for research {sorted(need_t)}")
                ch.skip[g] = 4            # build something else meanwhile
                time.sleep(5)
                continue
            pl = plan_poles(plan, L, places, m)
            if not a.dry:      # from what the builders can reach (the mall), unless hand-crafting is on for g
                gv = gate(g, places + pl)
                if gv[0] == "alt":
                    h, w = gv[1], gv[2]
                    if h.startswith("drain:"):
                        ch.pending.insert(0, (h, w))     # a repair of its own branch: first thing next round
                        log(f"round {rnd}: {g} waits for {h}: {w}")
                        continue
                    g, why = h, w
                    places, groups, idx = increment(plan, L, g, L.researched, pole_item)
                    pl = plan_poles(plan, L, places, m)
                    macros.HANDCRAFT = state["handcraft"] or dead or g in gate.hand
                    gv = gate(g, places + pl, depth=3)   # the alt passed without its poles: once more with them
                if gv[0] != "build":
                    # a schedule group passed choose()'s gate (without poles): short only of poles or by a race,
                    # so it waits just 2 rounds; a repair keeps its own counter
                    held(g, gv, rnd, 2 if why.startswith("schedule") else 4)
                    time.sleep(2)
                    continue
            errs = validate(plan, L, places + pl)
            state["n"] += 1
            k = state["n"]
            log(f"#{k} {'+'.join(groups)}: {len(places)} pieces + {len(pl)} poles — {why}" + (f" | CHECK {errs}" if errs else ""))
            if a.dry:
                for h in groups:
                    for i in plan.groups.get(h, []):
                        L.built.add(i)
                for i in idx:
                    L.built.add(i)
                record({"n": k, "groups": groups, "why": why, "pieces": len(places), "poles": len(pl), "errs": errs})
                continue
            if errs and any("on live" in e for e in errs):
                ch.skip[g] = 3
                record({"n": k, "groups": groups, "why": why, "errs": errs, "result": "invalid"})
                continue
            before = {it: round(snap["made"].get(it, 0), 1) for it in plan.made.get(g.removeprefix("link:"), ()) if it in snap["made"]}
            t0 = time.time()
            state.setdefault("tried", [])
            state["tried"] += [h for h in groups if h in plan.groups and h not in state["tried"]]
            if g == "reconnect":       # orphan underground entrances are picked up before the routes are laid
                build(builders[:1], [p_ for p_ in places if p_.get("remove")], clear, split, supply, Bridge)
                places = [p_ for p_ in places if not p_.get("remove")]
                state.setdefault("extra_places", []).extend(
                    [{k_: v for k_, v in p_.items() if k_ in ("item", "x", "y", "direction", "belt_type")} for p_ in places])
                L.own = state["extra_places"]
            rm = removals(plan, L, places + pl)
            if rm:
                log(f"#{k} picks up {len(rm)} burner-phase entities in the way")
                build(builders[:1], rm, clear, split, supply, Bridge)
            res = build(builders, places + pl, clear, split, supply, Bridge)
            for p_ in places:
                if p_.get("final"):
                    state.setdefault("pending_recipes", {})[f"{p_['x']},{p_['y']}"] = p_["final"]
            sh = collections.Counter()
            for r_ in res:
                sh.update({k_: v_ for k_, v_ in (r_.get("short") or {}).items() if isinstance(v_, (int, float))})
            state["short"] = dict(sh)
            L.refresh(plan.box(idx, 10) if idx else None)
            left = [i for i in idx if i not in L.built]
            time.sleep(max(3, 60 / a.speed))          # a game minute of running before judging it
            cnt, bad = L.statuses(idx)
            snap2 = control.measure(q, track)
            after = {it: round(snap2["made"].get(it, 0), 1) for it in plan.made.get(g.removeprefix("link:"), ()) if it in snap2["made"]}
            if bad and any(s in ("no_power", "low_power") for *_, s, _g in bad):
                more = plan_poles(plan, L, [plan.p[i] for i in idx], m)
                gv = gate(g, more) if more else None
                if gv and gv[0] == "build":
                    build(builders, more, clear, split, supply, Bridge)
                    cnt, bad = L.statuses(idx)
                elif gv:
                    if gv[0] == "alt":
                        ch.pending.append((gv[1], gv[2]))
                    log(f"#{k} more poles wait: {gv[2] if gv[0] == 'skip' else gv[1:]}")
            # belt ends of this increment that feed a plan piece not built yet (by design only where a trunk waits
            # for the bus or a fuel line runs past future boilers; anything else is a bug to look at)
            open_ends = collections.Counter()
            for i in idx:
                if i in L.built and plan.p[i]["item"] in ("transport-belt", "underground-belt", "splitter"):
                    for j in plan.flow.feeds[i]:
                        if j not in L.built:
                            open_ends[plan.p[j].get("grp")] += 1
            ok = not left and not [b for b in bad if b[3] in ("no_power", "low_power", "missing") or
                                   (b[3] == "no_fuel" and b[0] != "boiler")]   # the tender fuels boilers at first
            log(f"#{k} {'OK' if ok else 'NOT OK'} in {(time.time() - t0) / 60:.1f} min; unbuilt {len(left)}; "
                f"status {dict(cnt)}; made {before} -> {after}" + (f"; open belt ends into {dict(open_ends)}" if open_ends else "") + (f"; bad {bad[:4]}" if bad else "") +
                (f"; build {str(res)[:200]}" if left else ""))
            record({"n": k, "tick": snap2.get("tick"), "groups": groups, "why": why, "pieces": len(places),
                    "poles": len(pl), "unbuilt": len(left), "status": dict(cnt), "bad": bad[:20],
                    "open_ends": dict(open_ends),
                    "before": before, "after": after, "minutes": round((time.time() - t0) / 60, 2)})
            # a plan piece whose spot is taken by something that stands (e.g. our own route over a mine's old
            # drill row), or that no character can walk to (walled in by the build), fails the same way every
            # time: after 3 tries it is given up (dead), not retried forever
            blk = state.setdefault("blocked", {})
            for r_ in res if isinstance(res, list) else []:
                for f_ in (r_ or {}).get("failed") or []:
                    mt = re.match(r"\S+ (-?[\d.]+),(-?[\d.]+): .*(blocked by|no path to)", str(f_))
                    i_ = plan.key.get((round(float(mt[1]), 2), round(float(mt[2]), 2))) if mt else None
                    if i_ is not None and i_ in left:
                        blk[str(i_)] = blk.get(str(i_), 0) + 1
                        if blk[str(i_)] >= 3:
                            L.dead.add(i_)
                            state["dead"] = sorted(L.dead)
                            log(f"#{k} gives up {plan.p[i_]['item']} {mt[1]},{mt[2]}: its spot stays blocked")
            if left:
                ch.skip[g] = 2
            elif not ok and g.startswith(("repower:", "link:", "restore", "rears", "finish:")):
                ch.skip[g] = 6           # a repair that did not take: try other work before the next attempt
            if state["handcraft"] and mall_ready(plan, L):
                state["handcraft"] = False
                log("the early mall stands: hand-crafting only where the schedule still says so")
            save()
            if launched(q):
                log("ROCKET LAUNCHED")
                break
    finally:
        stop.set()
        if tender:
            tender.terminate()


BURNER_CAP = {"iron-plate": 24, "copper-plate": 12, "stone": 6}   # extra burner pairs per item at most
BURNER_ORE = {"iron-plate": ("i", "stone-furnace"), "copper-plate": ("c", "stone-furnace"), "stone": ("s", "iron-chest")}


def burner_pairs(plan, L, m, item, boot_dir, k, n=4):
    """n more burner drill + furnace (chest) pairs on the nearest patch, off every plan tile and live entity;
    written to boot/boot-extra-<k>.json for the tender."""
    from auto import boot as bootmod, geom
    blocked = set(plan.tiles)
    for i in L.built:
        blocked |= geom.footprint(plan.p[i])
    for (x, y), nm in L.extra.items():
        blocked |= geom.footprint({"item": nm, "x": x, "y": y})
    for f in boot_dir.glob("boot-*.json"):
        blocked |= geom.tiles(json.loads(f.read_text())["place"])
    code, out_item = BURNER_ORE[item]
    pl = bootmod.pairs(m, code, n, out_item, blocked)
    (boot_dir / f"boot-extra-{k}.json").write_text(json.dumps({"place": pl, "gate": {"fuel": "coal"}}))
    return pl


def start_tender(py, char, boot_dir, power_file, env):
    import subprocess
    files = [str(f) for f in sorted(boot_dir.glob("boot-*.json"))] + ([str(power_file)] if power_file.exists() else [])
    return subprocess.Popen([py, str(AGENT / "bootstrap.py"), char, ",".join(files), "--tend", "--minutes", "100000"],
                            env=env, cwd=str(AGENT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hut_spot(plan, boot, m):
    """A free 3x3 spot (with a free ring) for the bootstrap lab, as near the first boiler unit as possible."""
    from auto import geom
    blocked = set(plan.tiles) | geom.tiles(boot)
    eng = [plan.p[i] for i in plan.groups.get("power:0", []) if plan.p[i]["item"] == "steam-engine"] or           [p for p in plan.p if p["item"] == "steam-engine"]
    if not eng:
        return None
    cx, cy = math.floor(eng[0]["x"]), math.floor(eng[0]["y"])
    for r in range(4, 30):
        for dx in range(-r, r + 1):
            for dy in (-r, r) if abs(dx) != r else range(-r, r + 1):
                x, y = cx + dx, cy + dy
                lab = {"item": "lab", "x": x + .5, "y": y + .5}
                ring = {(a + i, b + j) for a, b in geom.footprint(lab) for i in (-1, 0, 1) for j in (-1, 0, 1)}
                if not ring & blocked and all(m.t(*t) == "." for t in ring):
                    return lab
    return None


def hut_missing(L, hut):
    return not any(n == "lab" and abs(x - hut["x"]) < .1 and abs(y - hut["y"]) < .1 for (x, y), n in L.extra.items())


EARLY_MALL = ["transport-belt", "inserter", "pipe", "electric-mining-drill", "stone-furnace",
              "assembling-machine-1", "underground-belt", "splitter", "steam-engine", "boiler", "offshore-pump",
              "pipe-to-ground", "lab", "iron-chest", "burner-inserter", "long-handed-inserter", "fast-inserter"]


def mall_groups(plan, item):
    """Groups that put item into a mall chest: its maker plus the chest (mall:<item> on the bus, or the maker's own)."""
    out = [g for g in plan.groups if g.startswith("mall:") and item in g[5:].split("+")]
    return out


def grid_dead(q):
    """No generator has steam (the boilers ran out of fuel): nothing electric runs, the mall included."""
    from auto import live as lv
    try:
        return lv.power(q).get("cap", 0) <= 0
    except Exception:
        return False


def mall_ready(plan, L):
    for it in EARLY_MALL:
        gs = mall_groups(plan, it) + [g for g in plan.makers.get("stock:" + it, [])]
        if not gs or not any(L.group_done(g) for g in gs):
            return False
    return True


FUEL = {"coal"}

# c = [x, y, [[item, cap], ...]]: slots per item ceil(cap/stack) (at least 1), the bar after their sum. A chest fed
# two items reserves each its slots: slot filters where the chest supports them; an iron chest does not (its
# inventory is "with_bar" only, measured), so there the inserter that fills it keeps a whitelist of the items still
# under their cap, and one item can no longer fill all slots and lock the other out. The filters are written first
# and use_filters last: a write failing half-way must never leave an empty whitelist on (the chest would take
# nothing at all); on a failure the inserter goes back to unfiltered. (No Lua comments: newlines become spaces.)
LIMIT_LUA = r"""
local S=game.surfaces[1] local n,f,w=0,0,0
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},name='iron-chest'}[1]
  if e then local inv=e.get_inventory(defines.inventory.chest) local want={} local slots=0
    for _,it in pairs(c[3]) do local pr=prototypes.item[it[1]]
      if pr then local k=math.max(1,math.ceil(it[2]/pr.stack_size)) want[#want+1]={it[1],k,it[2]} slots=slots+k end end
    slots=math.max(1,math.min(#inv,slots))
    if #want>1 and inv.supports_filters() then
      local left={} for _,it in pairs(want) do left[it[1]]=it[2] end
      local free={}
      for s=1,#inv do inv.set_filter(s,nil) end
      for s=1,slots do local st=inv[s]
        if st.valid_for_read then if (left[st.name] or 0)>0 then inv.set_filter(s,st.name) left[st.name]=left[st.name]-1 end
        else free[#free+1]=s end end
      for _,it in pairs(want) do while left[it[1]]>0 and #free>0 do inv.set_filter(table.remove(free,1),it[1]) left[it[1]]=left[it[1]]-1 end end
      f=f+1
    elseif #want>1 then
      for _,i in pairs(S.find_entities_filtered{area={{c[1]-3,c[2]-3},{c[1]+3,c[2]+3}},type='inserter',force='player'}) do
        if i.drop_target==e and i.filter_slot_count>0 then
          local l={} for _,it in pairs(want) do
            if inv.get_item_count(it[1])<it[3] and #l<i.filter_slot_count then l[#l+1]=it[1] end end
          local ok=pcall(function()
            for s=1,i.filter_slot_count do i.set_filter(s,l[s]) end
            i.inserter_filter_mode='whitelist' i.use_filters=true end)
          if ok then w=w+1 else pcall(function() i.use_filters=false end) end
        end end
    end
    inv.set_bar(slots+1) n=n+1 end
end
rcon.print(n..' chests, '..f..' slot-filtered, '..w..' inserters whitelisted')
"""


def mall_caps(plan, L, researched, pole_item):
    """{(x, y): {item: cap}} for every built mall chest: what the unbuilt plan still needs of the item (+10%) plus a
    reserve of 10% of the plan's total (at most RESERVE: one silo, one pump keep none); pieces of retired groups and
    written-off drills are not coming. The rocket's cargo (demand.CARGO) is needed once."""
    from auto.demand import CARGO
    rem, tot = collections.Counter(), collections.Counter()
    for i, p in enumerate(plan.p):
        for m in materialize(p, researched, pole_item):
            tot[m["item"]] += 1
            if i not in L.built and i not in L.dead and p.get("grp") not in L.retired:
                rem[m["item"]] += 1
    for c_ in (rem, tot):
        c_["assembling-machine-1"] += c_.get("assembling-machine-2", 0)   # asm2 is made from asm1
        c_.update({it: 1 for it in CARGO})
        c_.update(WANT)    # builds outside the plan (outposts, remines) the gate holds for want of these
    return {(p["x"], p["y"]): {it: math.ceil(rem.get(it, 0) * 1.1) + min(RESERVE, tot.get(it, 0) // 10)
                               for it in p["mall"]}
            for i, p in enumerate(plan.p) if p["item"] == "iron-chest" and p.get("mall") and i in L.built}


def mall_limits(plan, L, q, researched, pole_item):
    """Cap every mall chest at what the unbuilt plan still needs of its items (+10%): the mall stops taking plates
    off the bus for stock nobody will place. Chests fed two items cap each item (LIMIT_LUA), never uncapped.
    A chest's bar keeps one slot even at cap 0, and a maker loads the ingredients of its next craft while it crafts:
    the makers of mall-only items are switched off as well (mall_makers) once their chests hold the cap."""
    caps = mall_caps(plan, L, researched, pole_item)
    chests = [[x, y, [[it, c] for it, c in cs.items()]] for (x, y), cs in caps.items()]
    if not chests:
        return "0 chests"
    LIMITS["cmd"] = "/silent-command " + (LIMIT_LUA % json.dumps(chests)).replace(chr(10), " ")
    LIMITS["makers"] = mall_makers(plan, L, caps)
    r = q(LIMITS["cmd"])
    if LIMITS["makers"]:
        r = f"{r}; makers {q(LIMITS['makers'])}"
    return r


# a mall maker (its product only fills mall chests) runs while its item is short of the chests' cap: stock in the
# chests + its output + the craft under way; off (with the inserters loading it) once nothing still needs the item,
# so it stops pulling a silo's 1000 concrete off the bus for a second silo; back on when the cap grows again
MAKER_LUA = r"""
local S=game.surfaces[1] local d=helpers.json_to_table('%s') local stock={}
for _,c in pairs(d.chests) do
  local e=S.find_entities_filtered{position={c[1],c[2]},name='iron-chest'}[1]
  if e then for _,it in pairs(e.get_inventory(defines.inventory.chest).get_contents()) do
    stock[it.name]=(stock[it.name] or 0)+it.count end end end
local on,off=0,0
for _,m in pairs(d.makers) do
  local e=S.find_entities_filtered{position={m[1],m[2]},type='assembling-machine',force='player'}[1]
  if e then
    local out=e.get_inventory(defines.inventory.assembling_machine_output)
    local left=m[4]-(stock[m[3]] or 0)-(out and out.get_item_count(m[3]) or 0)-(e.is_crafting() and m[5] or 0)
    local act=left>0
    if e.active~=act then e.active=act if act then on=on+1 else off=off+1 end end
    for _,i in pairs(S.find_entities_filtered{area={{m[1]-4,m[2]-4},{m[1]+4,m[2]+4}},type='inserter',force='player'}) do
      if i.drop_target==e and i.active~=act then i.active=act end end
  end
end
rcon.print(off..' off, '..on..' on')
"""


def mall_makers(plan, L, caps):
    """MAKER_LUA command for the built makers of mall-only items (no recipe of the plan reads them), or None."""
    cap = {}
    for cs in caps.values():
        for it, c in cs.items():
            cap[it] = max(cap.get(it, 0), c)
    rec = PLAN_RECIPES.get("r") or {}
    read = {i for v in rec.values() for i, _ in v["ing"]}
    makers = []
    for i in L.built:
        p = plan.p[i]
        r = rec.get(p.get("recipe"))
        if not r or "assembling-machine" not in p["item"]:
            continue
        for it, k in r["prod"]:
            if it in cap and it not in read:
                makers.append([p["x"], p["y"], it, cap[it], k])
    if not makers:
        return None
    ch = [[x, y] for (x, y) in caps]
    return "/silent-command " + (MAKER_LUA % json.dumps({"chests": ch, "makers": makers})).replace(chr(10), " ")


WANT = collections.Counter()   # {item: n} short for builds outside the plan, added to the mall caps
RESERVE = 10                   # stock every mall item keeps beyond the plan: repairs, restores, own routes
LIMITS = {"cmd": None, "makers": None}   # the last mall_limits commands (main's keep_limits thread sends them again)
PLAN_RECIPES = {}              # {"r": demand.json's recipes}: which recipes read which items (set in main)


CHEST_LUA = r"""
local S=game.surfaces[1] local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type='container',force='player'}[1] local r={} local f=0
  if e then local inv=e.get_inventory(defines.inventory.chest)
    for _,it in pairs(inv.get_contents()) do r[it.name]=(r[it.name] or 0)+it.count end
    f=inv.count_empty_stacks(false,false) end
  o[#o+1]={r,f}
end
rcon.print(helpers.table_to_json(o))
"""


def chest_contents(q, positions, room=False):
    """[{item: count}] of the chest at each (x, y) ({} where none stands); room=True: [({item: count}, empty slots
    below the bar)]."""
    out = []
    for i in range(0, len(positions), 200):
        r = json.loads(q("/silent-command " + (CHEST_LUA % json.dumps([[x, y] for x, y in positions[i:i + 200]]))
                         .replace(chr(10), " ")) or "[]")
        for d in (r if isinstance(r, list) else []):
            c, f = (d + [{}, 0])[:2] if isinstance(d, list) else ({}, 0)    # an empty Lua table prints as []
            out.append((c if isinstance(c, dict) else {}, int(f or 0)))
    out += [({}, 0)] * (len(positions) - len(out))
    return out if room else [c for c, _ in out]


# status of each crafting machine / furnace / drill at the positions, and the recipe ingredients an assembler lacks
MACH_LUA = r"""
local S=game.surfaces[1] local sn={} for k,v in pairs(defines.entity_status) do sn[v]=k end local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type={'assembling-machine','furnace','mining-drill'},force='player'}[1]
  local r={s='missing',m={}}
  if e then r.s=sn[e.status or 0] or 'none'
    if e.type=='assembling-machine' then local rc=e.get_recipe()
      if rc then local inv=e.get_inventory(defines.inventory.assembling_machine_input)
        for _,g in pairs(rc.ingredients) do
          if g.type=='item' and inv.get_item_count(g.name)<g.amount then r.m[#r.m+1]=g.name end
          if g.type=='fluid' and (e.get_fluid_count(g.name) or 0)<g.amount then r.m[#r.m+1]=g.name end
        end end end end
  o[#o+1]=r
end
rcon.print(helpers.table_to_json(o))
"""
STARVED = {"item_ingredient_shortage", "no_ingredients", "fluid_ingredient_shortage", "no_input_fluid",
           "missing_required_fluid"}


def machine_state(q, pieces):
    """[(status, [missing ingredients])] for each machine piece ('missing' where none stands)."""
    out = []
    for i in range(0, len(pieces), 200):
        r = json.loads(q("/silent-command " + (MACH_LUA % json.dumps([[p["x"], p["y"]] for p in pieces[i:i + 200]]))
                         .replace(chr(10), " ")) or "[]")
        for d in (r if isinstance(r, list) else []):
            d = d if isinstance(d, dict) else {}
            out.append((d.get("s", "missing"), list(d.get("m") or []) if isinstance(d.get("m"), list) else []))
    return out + [("missing", [])] * (len(pieces) - len(out))


def maker_pieces(plan, groups, item):
    """Plan indices of the machines in groups that make item: an assembler on a recipe with it among the
    products, a furnace or a drill of a group that makes it."""
    rec = plan.gd["recipes"]
    out = []
    for h in groups:
        for i in plan.groups.get(h, []):
            p = plan.p[i]
            r = p.get("recipe")
            if r and item in {x[0] for x in rec.get(r, {}).get("prod", [])}:
                out.append(i)
            elif not r and ("furnace" in p["item"] or "mining-drill" in p["item"]) and item in plan.made.get(h, ()):
                out.append(i)
    return out


RECLAIM = ("transport-belt", "underground-belt", "inserter", "fast-inserter", "long-handed-inserter",
           "burner-inserter", "assembling-machine-1", "assembling-machine-2", "assembling-machine-3")


def spent_blocks(plan, L, caps, researched, pole_item):
    """Built assembler blocks (B<n>) whose every product nothing needs any more: no unbuilt plan piece is one, no mall
    chest's cap wants one (mall_caps: the unbuilt plan + reserve + WANT), no machine of another live group takes it
    as an ingredient (the silo, labs, smelters, other blocks), and it is not the rocket's cargo. Measured (seed 1060,
    after the silo stood): B13, the silo + landing pad makers, kept 129 belt tiles of steel, concrete, pipe,
    processing units and electric engines forever, its silo maker switched off by the mall caps.
    Returns [(group, [piece indices to pick up])]: the block's and its band:/conn: feeders' belts, undergrounds,
    inserters and machines that stand (no splitter: a tap sits on the bus; no pole, pipe or chest)."""
    from auto.demand import CARGO
    rem = collections.Counter()
    for i, p in enumerate(plan.p):
        if i not in L.built and i not in L.dead and p.get("grp") not in L.retired:
            for m in materialize(p, researched, pole_item):
                rem[m["item"]] += 1
    capped = {it for cs in caps.values() for it, c in cs.items() if c > 0}
    live = {g for g in plan.prod if g not in L.retired and any(i in L.built for i in plan.groups[g])}
    takes = {}             # group -> what its machines eat (mall chests left out: their caps speak for them)
    for g in live:
        ents = [plan.p[i] for i in plan.groups[g] if not plan.p[i].get("mall")]
        takes[g] = plan._io(g, ents)[1]
    out = []
    for g in sorted(live):
        if not re.match(r"^B\d+$", g):
            continue
        made = {m for m in plan.made[g] if not m.startswith("stock:")}
        if not made or made & CARGO:
            continue
        if any(rem[it] > 0 or it in capped or any(it in takes[h] for h in live if h != g) for it in made):
            continue
        idx = [i for h in (g, "band:" + g, "conn:" + g) for i in plan.groups.get(h, ())
               if i in L.built and i not in L.dead and plan.p[i]["item"] in RECLAIM]
        if idx:
            out.append((g, idx))
    return out


def drain_due(plan, L, q, caps, item=None, skip=None):
    """A built mall chest fed two items that holds more of one than its cap while another (item, if given) is under
    its cap and locked out (no empty slot below the bar) with its maker full_output: (x, y, over item, locked-out
    item), or None. Draining it lets the other item in. Locked out, not only absent: 10 steam engines left in a chest
    full of burner inserters lock out the 14 more the power units need, and nobody takes the 10 while the unit
    waits for all of them. skip: the controller's skip counters (a chest drained lately rests)."""
    mixed = [(xy, cs) for xy, cs in caps.items() if len(cs) > 1 and (item is None or item in cs)
             and (skip or {}).get(f"drain:{xy[0]},{xy[1]}", 0) <= 0]
    if not mixed:
        return None
    cont = chest_contents(q, [xy for xy, _ in mixed], room=True)
    for (xy, cs), (have, room) in zip(mixed, cont):
        over = [it for it in cs if have.get(it, 0) > cs[it]]
        locked = [it for it in cs if have.get(it, 0) < cs[it] and (item is None or it == item)] if room <= 0 else []
        if not over or not locked:
            continue
        for it in locked:
            mk = [i for i in maker_pieces(plan, [h for h in plan.makers.get(it, []) if L.group_done(h)], it)
                  if i in L.built]
            if mk and any(s == "full_output" for s, _ in machine_state(q, [plan.p[i] for i in mk])):
                return xy[0], xy[1], over[0], it
    return None


TRIG_LUA = r"""
local F=game.forces.player local o={}
for _,n in pairs(helpers.json_to_table('%s')) do local t=F.technologies[n]
  if t and not t.researched then local tr=t.prototype.research_trigger
    local ok=true for _,p in pairs(t.prerequisites) do if not p.researched then ok=false end end
    if tr and ok then
      local ent=tr.entity and (type(tr.entity)=='string' and tr.entity or tr.entity.name) or nil
      local it=tr.item and (type(tr.item)=='string' and tr.item or tr.item.name) or nil
      o[#o+1]={name=n, type=tr.type, entity=ent, item=it} end end end
rcon.print(helpers.table_to_json(o))
"""


# a machine's facing sticks only once its recipe has fluid boxes (an assembler placed idle or on a stand-in
# recipe comes up north): every built plan machine is turned to its planned facing (a player's R, nothing else)
DIR_NUM = {"north": 0, "east": 4, "south": 8, "west": 12}
FACE_LUA = r"""
local S=game.surfaces[1] local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type={'assembling-machine','furnace'},force='player'}[1]
  if e and e.direction~=c[3] then
    if c[4]==1 then e.direction=c[3]
      if e.direction==c[3] then o[#o+1]=c[1]..','..c[2] else o[#o+1]='!'..c[1]..','..c[2] end
    else o[#o+1]=c[1]..','..c[2] end end
end
rcon.print(table.concat(o,';'))
"""


def misfaced(plan, L, q, fix=False):
    """Built plan machines (with a planned facing) facing another way live; fix=True turns them. Returns the
    positions found ('!' prefix: the game would not turn it, e.g. a recipe without fluids)."""
    idx = [i for i in L.built if plan.p[i]["item"].startswith(("assembling-machine", "chemical-plant", "oil-refinery"))
           and plan.p[i].get("direction")]
    pos = [[plan.p[i]["x"], plan.p[i]["y"], DIR_NUM.get(plan.p[i].get("direction", "north"), 0), int(fix)] for i in idx]
    out = []
    for k in range(0, len(pos), 300):
        r = q("/silent-command " + (FACE_LUA % json.dumps(pos[k:k + 300])).replace(chr(10), " ")) or ""
        out += [t for t in r.strip().split(";") if t]
    return out


# cracking follows demand, as a player's circuit condition would: a refinery stops whole when one output has no
# room (petroleum 55 per cycle), so light oil cracked into petroleum that backs up starves rocket fuel and solid fuel;
# light-oil cracking is switched off (its recipe taken out) while the refineries' petroleum has no room for a cycle,
# and back on when petroleum has room again
CRACK_LUA = r"""
local S=game.surfaces[1] local full,room=0,0
for _,e in pairs(S.find_entities_filtered{name='oil-refinery',force='player'}) do
  local r=e.get_recipe()
  if r and r.name=='advanced-oil-processing' then
    for k=1,#e.fluidbox do local fl=e.fluidbox.get_filter(k)
      if fl and fl.name=='petroleum-gas' then local f=e.fluidbox[k]
        if f and e.fluidbox.get_capacity(k)-f.amount<55 then full=full+1 else room=room+1 end end end end end
local off,on=0,0
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},name='chemical-plant',force='player'}[1]
  if e then local r=e.get_recipe()
    if full>0 and room==0 and r and r.name=='light-oil-cracking' then e.set_recipe(nil) off=off+1
    elseif room>0 and full==0 and not r then e.set_recipe('light-oil-cracking') on=on+1 end end
end
rcon.print(off..','..on..','..full..','..room)
"""


def balance_cracking(plan, L, q):
    """Light-oil cracking plants of the plan switched off while the refineries' petroleum is backed up, back on when
    it has room. Returns (switched off, switched on, refineries full, refineries with room)."""
    pos = [[plan.p[i]["x"], plan.p[i]["y"]] for i in L.built if plan.p[i].get("recipe") == "light-oil-cracking"]
    if not pos:
        return 0, 0, 0, 0
    r = (q("/silent-command " + (CRACK_LUA % json.dumps(pos)).replace(chr(10), " ")) or "0,0,0,0").strip()
    return tuple(int(v) for v in r.split(","))


SETREC_LUA = r"""
local S=game.surfaces[1] local o={}
for _,c in pairs(helpers.json_to_table('%s')) do
  local e=S.find_entities_filtered{position={c[1],c[2]},type={'assembling-machine'},force='player'}[1]
  if e and (not e.get_recipe() or e.get_recipe().name~=c[3]) then e.set_recipe(c[3]) o[#o+1]=c[3] end
end
rcon.print(helpers.table_to_json(o))
"""


def oil_upkeep(plan, L, q):
    try:
        off, on, full, room = balance_cracking(plan, L, q)
        if off or on:
            log(f"cracking: {off} light-oil crackers off, {on} on (refineries: {full} petroleum-full, {room} with room)")
    except Exception as e:
        log("cracking:", str(e)[:120])


def set_pending_recipes(q, state, researched, log):
    """Machines placed idle or on a stand-in recipe get their planned recipe once it is researched."""
    pend = state.get("pending_recipes") or {}
    ready = [[float(k.split(",")[0]), float(k.split(",")[1]), r] for k, r in pend.items() if recipe_ok(r, researched)]
    if not ready:
        return
    try:
        r = q("/silent-command " + (SETREC_LUA % json.dumps(ready)).replace(chr(10), " "))
        log(f"recipes set now that they are researched: {r}")
        for x, y, _ in ready:
            pend.pop(f"{x},{y}", None)
    except Exception as e:
        log("set recipes:", str(e)[:120])


def trigger_techs(q, techs):
    """Trigger techs whose prerequisites are researched: [{name, type, entity, item}]."""
    try:
        r = json.loads(q("/silent-command " + (TRIG_LUA % json.dumps(techs)).replace(chr(10), " ")) or "[]")
        return r if isinstance(r, list) else []
    except Exception:
        return []


def starving_fuel(plan, L, q):
    """True when half the furnaces of some built smelting group report no fuel."""
    from auto import live as lv
    try:
        st = lv.status(q, plan.box(sorted(L.built), 2)) if L.built else []
    except Exception:
        return False
    at = {(round(x, 2), round(y, 2)): s_ for n, x, y, s_ in st if "furnace" in n}
    for g in plan.prod:
        if g.startswith("smelt") and L.group_done(g):
            fur = [plan.p[i] for i in plan.groups[g] if "furnace" in plan.p[i]["item"]]
            nf = sum(1 for p in fur if at.get((round(p["x"], 2), round(p["y"], 2))) == "no_fuel")
            if fur and nf >= 0.5 * len(fur):
                return True
    return False


def dry_drills(plan, L, q, state, skip=None):
    """(group, [dry drills]) for the first mine group with a drill (plan piece or our own one beside its belts)
    reporting no minable resources; groups where relocation found no spot are left to the re-mine."""
    from auto import live as lv
    skip = skip or {}
    try:
        st = lv.status(q, L.plan.box(sorted(L.built), 2) if L.built else (0, 0, 1, 1))
        own_d = [p for p in L.own if p["item"] == "electric-mining-drill"]
        if own_d:
            st += lv.status(q, (min(p["x"] for p in own_d) - 2, min(p["y"] for p in own_d) - 2,
                                max(p["x"] for p in own_d) + 2, max(p["y"] for p in own_d) + 2))
    except Exception:
        return None
    dry = {(round(x, 2), round(y, 2)) for n, x, y, s_ in st
           if n == "electric-mining-drill" and s_ == "no_minable_resources"}
    off = {tuple(t) for t in state.get("dry_own", [])}     # own drills already written off (no spot to move to)
    for g in sorted(plan.prod):
        if not g.startswith("mine:") or g in L.retired or skip.get("relocate:" + g, 0) > 0 \
                or g in state.get("no_relocate", []):
            continue
        idx = plan.groups[g]
        ds = [plan.p[i] for i in idx if plan.p[i]["item"] == "electric-mining-drill" and i in L.built
              and i not in L.dead and (round(plan.p[i]["x"], 2), round(plan.p[i]["y"], 2)) in dry]
        ds += [p for p in own_beside(plan, L, g) if (round(p["x"], 2), round(p["y"], 2)) in dry - off]
        if ds:
            return g, ds
    return None


DV4 = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def own_beside(plan, L, g):
    """Our own (relocated) drills that drop onto group g's belts."""
    bt = {(math.floor(plan.p[i]["x"]), math.floor(plan.p[i]["y"])) for i in plan.groups[g]
          if plan.p[i]["item"] == "transport-belt"}
    out = []
    for p in L.own:
        if p["item"] == "electric-mining-drill":
            f = DV4.get(p.get("direction", "north"), (0, -1))
            if (math.floor(p["x"]) + 2 * f[0], math.floor(p["y"]) + 2 * f[1]) in bt:
                out.append(p)
    return out


def dry_mine(plan, L, q, state, skip=None):
    """A built, not retired mine group whose dry drills found no spot to move to (no_relocate), with >= 70% of its
    standing drills (plan drills and our own beside its belt) reporting no minable resources: measured live every
    time. A group found dry before goes on to the remine only while that holds, or when its drills are picked up
    already; one that measures below 70% is not dry any more (one dry drill never retires a mining group)."""
    from auto import live as lv
    try:
        st = lv.status(q, plan.box(sorted(L.built), 2)) if L.built else []
        own_d = [p for p in L.own if p["item"] == "electric-mining-drill"]
        if own_d:
            st += lv.status(q, (min(p["x"] for p in own_d) - 2, min(p["y"] for p in own_d) - 2,
                                max(p["x"] for p in own_d) + 2, max(p["y"] for p in own_d) + 2))
    except Exception:
        return None
    at = {(round(x, 2), round(y, 2)): s_ for n, x, y, s_ in st if n == "electric-mining-drill"}
    for g in sorted(plan.prod):
        if not g.startswith("mine:") or g in L.retired or (skip or {}).get("remine:" + g, 0) > 0:
            continue
        if g not in state.get("no_relocate", []):
            continue                      # drills are moved one by one first (dry_drills / auto.relocate)
        dr = [i for i in plan.groups[g] if plan.p[i]["item"] == "electric-mining-drill"]
        if g in state.get("dry", ()) and dr and not any(i in L.built for i in dr):
            return g                      # found dry before and its drills are picked up already: remine on
        if not L.group_done(g):
            continue
        stand = [plan.p[i] for i in dr if i in L.built] + own_beside(plan, L, g)
        nd = sum(1 for p in stand if at.get((round(p["x"], 2), round(p["y"], 2))) == "no_minable_resources")
        if stand and nd >= 0.7 * len(stand):
            if g not in state.setdefault("dry", []):
                state["dry"].append(g)
            return g
        if g in state.get("dry", ()):
            state["dry"].remove(g)
            log(f"{g}: {nd} of {len(stand)} standing drills dry (< 70%): not dry, it keeps supplying")
    return None


def unpowered(plan, L, q, state):
    """A built group with an electric machine reporting no power while the plant has capacity."""
    from auto import live as lv
    try:
        if not L.built or not lv.power(q).get("cap"):
            return None
        st = lv.status(q, plan.box(range(len(plan.p)), 8))     # the whole plan area: our own machines too
    except Exception:
        return None
    for n, x, y, s_ in st:
        if s_ == "no_power":
            i = plan.key.get((round(x, 2), round(y, 2)))
            if i is not None and i in L.built:
                return plan.p[i].get("grp")
            if i is None and n != "inserter":       # a machine of our own (the bootstrap lab): poles for it alone
                return f"extra@{n}@{x}@{y}"
    return None


def flushes(state, g, L):
    """Flushes of g since the map last changed: a jam that comes back on the same belts is not flushed forever,
    but once something new stands (the missing side of a lane merge) it may be flushed again."""
    if state.get("flush_at", {}).get(g) != len(L.built):
        return 0
    return state.get("flushed", {}).get(g, 0)


def jammed(plan, L, q, state):
    """A built smelting group with most furnaces starved and belts that carry items but none of its ore."""
    from auto import live as lv
    try:
        st = lv.status(q, plan.box(sorted(L.built), 2)) if L.built else []
    except Exception:
        return None
    at = {(round(x, 2), round(y, 2)): s for n, x, y, s in st}
    for g in sorted(plan.prod):
        if not g.startswith("smelt") or not L.group_done(g) or flushes(state, g, L) >= 3:
            continue
        fur = [plan.p[i] for i in plan.groups[g] if "furnace" in plan.p[i]["item"]]
        starved = sum(1 for p in fur if at.get((round(p["x"], 2), round(p["y"], 2))) in ("no_ingredients",
                                                                                        "item_ingredient_shortage"))
        if not fur or starved < 0.6 * len(fur):
            continue
        ore = plan.need[g] - FUEL
        belts = [i for i in plan.groups[g] if plan.p[i]["item"] == "transport-belt"]
        # backed up (a moving belt at start-up is sparse) and still so a few minutes later: stale load, not start-up
        lanes = lv.belt_lanes(q, [(plan.p[i]["x"], plan.p[i]["y"]) for i in belts])
        # stale fuel on the ore lane: packed, the same item on both lanes, no ore (one lane of coal and an empty ore
        # lane is a mine that delivers nothing, not a jam)
        bad = [i for i, (a_, b_) in zip(belts, lanes) if sum(a_.values()) + sum(b_.values()) >= 5 and
               len(a_) == 1 and set(a_) == set(b_) and not set(a_) & ore]
        seen = state.setdefault("jam_seen", {})
        if len(bad) >= 3:
            seen.setdefault(g, time.time())
            if time.time() - seen[g] >= 180:
                state.setdefault("jam", {})[g] = bad
                seen.pop(g, None)
                return g
        else:
            seen.pop(g, None)
    return jammed_block(plan, L, q, state, at)


def jammed_block(plan, L, q, state, at):
    """An assembler starved of an ingredient whose input belt is packed (>= 6 items a tile) without that
    ingredient: stale load from a lane mix-up blocks the lane. The jammed stretch = the belts of that line in the
    group, both ways from the read tile, that are packed and lack the ingredient."""
    from auto import live as lv
    f = plan.flow
    rec = plan.gd["recipes"]
    seen = state.setdefault("jam_seen", {})
    for g in sorted(plan.prod):
        if not L.group_done(g) or flushes(state, g, L) >= 6:
            continue
        mach = [i for i in plan.groups[g] if plan.p[i].get("recipe") and
                at.get((round(plan.p[i]["x"], 2), round(plan.p[i]["y"], 2))) == "item_ingredient_shortage"]
        for m in mach[:6]:
            ings = {x[0] for x in rec.get(plan.p[m]["recipe"], {}).get("ing", [])}
            reads = [j for ins in f.fed_by[m] if "inserter" in plan.p[ins]["item"] for j in f.fed_by[ins]
                     if plan.p[j]["item"] == "transport-belt"]
            if not reads:
                continue
            cont = lv.belt_items(q, [(plan.p[j]["x"], plan.p[j]["y"]) for j in reads])
            have = set().union(*[set(c) for c in cont])
            packed = [j for j, c in zip(reads, cont) if sum(c.values()) >= 6]
            missing = ings - have
            if not packed or not missing:
                continue
            # the belt line through the packed read tile, inside the group, both directions
            line, fr = set(packed), list(packed)
            while fr:
                nx_ = []
                for i in fr:
                    for j in list(f.feeds[i]) + list(f.fed_by[i]):
                        if j not in line and plan.p[j]["item"] == "transport-belt" and plan.p[j].get("grp") == g:
                            line.add(j)
                            nx_.append(j)
                fr = nx_
            line = sorted(line)
            lc = lv.belt_lanes(q, [(plan.p[j]["x"], plan.p[j]["y"]) for j in line])
            # the mix-up signature: packed, one and the same item on both lanes, none of what the machine lacks
            bad = [j for j, (a_, b_) in zip(line, lc) if sum(a_.values()) + sum(b_.values()) >= 6 and
                   len(a_) == 1 and set(a_) == set(b_) and not set(a_) & missing]
            key = f"{g}@{m}"
            if len(bad) >= 3:
                seen.setdefault(key, time.time())
                if time.time() - seen[key] >= 180:
                    state.setdefault("jam", {})[g] = bad
                    seen.pop(key, None)
                    return g
            else:
                seen.pop(key, None)
    return None


def choose(plan, L, ch, snap, targets, state, q, sched=None, gate=None):
    """Repairs from measurements first (pieces gone, what a gate named last round, half-built groups, dry mines,
    underground pairing, gaps, links, power, jams, a locked-out mall chest, stuck inserters), then the next group of
    the CP-SAT schedule (auto.schedule) that can be built now and that the gate passes; returns (group, why).
    gate=None (a dry run): no material check and none of the repairs that only pick up and lay again."""
    # pieces that stood and are gone (a builder walled in by its own building picks one up to walk out): put back
    gone = {i for i in set(state.get("placed", ())) - L.built - L.dead if plan.p[i].get("grp") not in L.retired}
    # pieces of our own (remine columns and routes, not in the plan) that stood and are gone
    here = {(round(x, 2), round(y, 2)) for (x, y) in L.extra}
    gone_x = [p for p in state.get("extra_places", []) if (round(p["x"], 2), round(p["y"], 2)) not in here
              and "electric-pole" not in p["item"]]
    if (gone or gone_x) and ch.skip.get("restore", 0) <= 0:
        state["gone"] = sorted(gone)
        L.state_gone = state["gone"]
        L.gone_extra = gone_x
        return "restore", f"{len(gone) + len(gone_x)} pieces that stood are gone"
    # what a build path's gate named last round: the group (or repair) behind the deficit that held it
    while ch.pending:
        h, w = ch.pending.pop(0)
        if ch.skip.get(h, 0) <= 0 and (h.startswith("drain:") or (h in plan.groups and not L.group_done(h))):
            return h, w
    held = {}        # group -> short: skipped by the gate this round, without a skip counter

    def tell():
        s_ = "; ".join(f"{g_} {dict(v_)}" for g_, v_ in held.items())[:500]
        if held and s_ != ch.held_line:
            ch.held_line = s_
            log(f"short, skipped for now (the next groups get their turn): {s_}")
    # repair first: a producing group with some pieces standing and some not (a failed or interrupted build)
    for g in sorted(plan.prod):
        n = sum(1 for i in plan.groups[g] if i in L.built)
        nm = sum(1 for i in plan.groups[g] if i in L.built and not plan.flow.transport(i))
        if g in state.get("tried", ()) and nm and not L.group_done(g) and ch.skip.get(g, 0) <= 0                 and ch.techs_ok(g) and g not in state.get("dry", ()) and g not in L.retired:
            v = gate(g) if gate else ("build", g, None)
            if v[0] == "skip":
                held[g] = v[2]
                continue
            if v[0] == "alt":
                return v[1], v[2]
            return g, f"repair: {n} of {len(plan.groups[g])} pieces stand"
    # bootstrap: the builders ran out of plates or stone -> more burner pairs (up to a cap per item)
    if state.get("handcraft"):
        short = state.get("short") or {}
        for it in ("iron-plate", "copper-plate", "stone"):
            if short.get(it, 0) > 0 and state.get("burners", {}).get(it, 0) < BURNER_CAP[it]:
                return "burner:" + it, f"builders short of {short[it]} {it}"
    # built side-loads without their rear belt mix lanes: lay the missing rear belts
    bb = {i for i in L.built if plan.p[i]["item"] == "transport-belt"}
    if ch.skip.get("rears", 0) <= 0 and rear_belts(plan, bb, L.built) - L.built:
        return "rears", "side-loaded belts without the rear belt that keeps them straight"
    # drills that found no ore left: moved beside the same belt onto ore that is left (auto.relocate)
    rel = dry_drills(plan, L, q, state, ch.skip)
    if rel:
        L.relocate = rel
        return "relocate:" + rel[0], f"{len(rel[1])} drills of {rel[0]} found no ore left"
    # a mine run dry: new columns on ore that is left (auto.remine), the old belt keeps feeding its smelter
    d = dry_mine(plan, L, q, state, ch.skip)
    if d:
        return "remine:" + d, f"{d}: its drills found no ore left"
    # plan undergrounds the game paired with a piece of our own (an own route ran along them): ours are picked up,
    # the plan pair pairs again, reconnect lays the cut route anew. A scan every 5 rounds, at once after a build saw
    # a mismatch; 10 rounds' rest after a repair
    if gate and ch.skip.get("pairs_fix", 0) <= 0 and (BAD_PAIRS or ch.skip.get("pairs", 0) <= 0):
        BAD_PAIRS.clear()
        ch.skip["pairs"] = 5
        try:
            rm_ = bad_pairs(plan, L, q, state)
        except Exception as e:
            log("pairs:", str(e)[:120])
            rm_ = []
        if rm_:
            L.pairs = rm_
            return "pairs", f"{len(rm_)} own undergrounds pair with plan ones"
    # remined columns whose new ore dead-ends before their old belt (a route piece failed or was picked up)
    remined = [h for h in L.retired if h.startswith("mine:") and h not in state.get("bridged", [])]
    if remined and ch.skip.get("reconnect", 0) <= 0:
        from auto import remine
        pl_, info = remine.reconnect(q, ch.m, plan, L, remined, stock=dict(ch.have()) if ch.have else None)
        ch.skip["reconnect"] = 10
        if pl_:
            L.reconnect = pl_
            return "reconnect", info
    # smelters out of fuel while coal backs up elsewhere: one more fuel bridge into a dry coal column's network
    if ch.skip.get("fuel", 0) <= 0:
        dry_coal = [h for h in plan.prod if h.startswith("mine:coal") and h in DEAD and state.get("bridged", []).count(h) < 3]
        if dry_coal and starving_fuel(plan, L, q):
            ch.skip["fuel"] = 15
            ch.skip.pop("remine:" + dry_coal[0], None)
            L.retired.discard(dry_coal[0])
            return "remine:" + dry_coal[0], "smelters out of fuel: another fuel bridge"
    # a working producer's output runs into a missing piece between two built stretches: lay it
    if ch.skip.get("gap", 0) <= 0 and gaps(plan, L.built):
        return "gap", "a missing piece between a working supplier and built transport"
    # built groups whose suppliers now stand but are not connected yet (coal to the boilers, a new pack to the labs)
    ok_ = supplied(plan, L.built)
    for g in sorted(plan.prod, key=lambda g: (not g.startswith("power"), g)):   # fuel to the boilers first
        if ch.skip.get("link:" + g, 0) > 0 or not L.group_done(g):
            continue
        own = set(plan.groups[g])
        if plan.flow.upstream(own, ok_ - own)[0] - L.built:
            return "link:" + g, f"connect {g} to its built suppliers"
        if link_paths(plan, L.built, g):
            return "link:" + g, f"connect {g} to a built supplier (a branch into a working belt)"
    # a transport group (pipes, belts, bands) partly built while every machine it touches stands: the plan meant all
    # of it (measured: 27 missing pipes left 2 of 4 pumpjacks waiting for space, the refineries short of crude);
    # the link walks miss it once one supplier gets through
    f_ = plan.flow
    for g in sorted(plan.groups):
        if g in plan.prod or g in L.retired or ch.skip.get("finish:" + g, 0) > 0:
            continue
        idx = plan.groups[g]
        miss = [i for i in idx if i not in L.built and i not in L.dead]
        if not miss or len(miss) == len(idx):
            continue
        near = {plan.p[j].get("grp") for i in idx for j in list(f_.feeds[i]) + list(f_.fed_by[i]) + list(f_.fluid[i])
                if not f_.transport(j)}
        if all(L.group_done(h) for h in near if h in plan.groups):
            return "finish:" + g, f"{g}: {len(miss)} of {len(idx)} pieces missing, every machine it joins stands"
    # belt tails past the last pickup (auto.trim): items that drift there are never taken; picked up (their load
    # comes back to the builders) and written off, so restore and finish leave them
    if ch.skip.get("trim", 0) <= 0:
        ch.skip["trim"] = 30
        from auto import trim
        tb = trim.tails(plan.p, plan.flow, among=L.built,
                        ahead=set(range(len(plan.p))) - L.dead) - L.dead   # planned belts ahead still count
        if tb:
            L.trim = sorted(tb)
            return "trim", f"{len(tb)} belts run past the last inserter that takes from them"
    # built machines without power (their pole chain failed, or a pole was picked up): a pole pass of their own
    u = unpowered(plan, L, q, state)
    if u and ch.skip.get("repower:" + u, 0) <= 0:
        return "repower:" + u, f"{u} has machines without power"
    # a starved smelter whose belts hold no ore at all is jammed (stale fuel on the ore lane): flush those belts
    j = jammed(plan, L, q, state)
    if j:
        return "flush:" + j, f"{j} starves with belts full of what it cannot smelt"
    # a mall chest fed two items, full of one over its cap, the other locked out while its maker stands full:
    # emptied into the depot (every 5 rounds at most; a drained chest rests 30)
    if gate and ch.skip.get("drain", 0) <= 0:
        ch.skip["drain"] = 5
        try:
            d = drain_due(plan, L, q, mall_caps(plan, L, L.researched, gate.pole_item), skip=ch.skip)
        except Exception as e:
            log("drain:", str(e)[:120])
            d = None
        if d:
            return f"drain:{d[0]},{d[1]}", f"holds {d[2]} over its cap, no room for {d[3]}, whose maker is full"
    # a block whose products nothing needs any more (the silo stands): its belts, inserters and machines picked up,
    # what they hold back to the depot as building stock, the pieces written off (every 20 rounds at most)
    if gate and ch.skip.get("spent", 0) <= 0:
        ch.skip["spent"] = 20
        try:
            sp = [s_ for s_ in spent_blocks(plan, L, mall_caps(plan, L, L.researched, gate.pole_item), L.researched,
                                            gate.pole_item) if ch.skip.get("spent:" + s_[0], 0) <= 0]
        except Exception as e:
            log("spent:", str(e)[:120])
            sp = []
        if sp:
            h, idx = sp[0]
            L.spent = idx
            return f"spent:{h}", (f"nothing needs what {h} makes ({sorted(m for m in plan.made[h] if ':' not in m)}): "
                                  f"its {len(idx)} belts, inserters and machines hold stock nobody takes")
    # inserters that hold what their machine's recipe does not take never drop it: laid again (twice at most each)
    if gate and ch.skip.get("unstick", 0) <= 0:
        ch.skip["unstick"] = 5
        try:
            st_ = stuck_inserters(plan, L, q, state)
        except Exception as e:
            log("unstick:", str(e)[:120])
            st_ = []
        if st_:
            L.unstick = st_
            return "unstick", f"{len(st_)} inserters hold what their machine cannot take"
    if ch.skip.get("face", 0) <= 0:
        ch.skip["face"] = 10
        try:
            mf = [t for t in misfaced(plan, L, q) if not t.startswith("!")]
        except Exception:
            mf = []
        if mf:
            L.misfaced = mf
            return "face", f"{len(mf)} machines face another way than planned (their fluid ports miss the pipes)"
    # (its skip counter: a gate that holds it would otherwise pick it again every round, ahead of the schedule)
    if L.standin and "logistics" in L.researched and ch.skip.get("upgrade:splitters", 0) <= 0:
        return "upgrade:splitters", f"{len(L.standin)} splitters stand as belts; logistics is researched"
    # an outpost whose drills stand without power: its pole chain along the route (route_poles)
    from auto import live as lv
    for k_, o in enumerate(state.get("outposts", [])):
        if ch.skip.get(f"outpower:{k_}", 0) > 0:
            continue
        try:
            st_ = lv.status(q, tuple(o["box"]))
        except Exception:
            continue
        dr_ = [s_ for n_, *_x, s_ in st_ if n_ == "electric-mining-drill"]
        if dr_ and sum(1 for s_ in dr_ if s_ == "no_power") * 2 > len(dr_):
            return f"outpower:{k_}", f"outpost {k_} ({o['name']}): {len(dr_)} drills without power"
    # scouting (auto.scout): the rest of the run needs more of a resource than the patches mined hold -> an outpost
    # on a charted patch that covers it, or else a radar that charts more of the map (one pass every 20 rounds)
    if sched and ch.skip.get("scout", 0) <= 0:
        ch.skip["scout"] = 8 if (state.get("budget") or {}).get("short", True) else 20   # often while scouting
        try:
            sc = scout_step(plan, L, q, state, sched, ch)
        except Exception as e:
            sc = None
            log("scout:", str(e)[:200])
        if sc:
            return sc
    # the schedule (CP-SAT over build and research tasks) says what comes next; a group the gate finds short is
    # passed over without a skip counter, so every buildable group behind it gets its turn
    if not sched or not sched.get("order"):
        tell()
        return None, "no schedule yet"
    waiting = []
    for k, g in enumerate(sched["order"]):
        if L.group_done(g) or ch.skip.get(g, 0) > 0:
            continue
        ok, w = ch.buildable(g)
        if ok:
            v = gate(g) if gate else ("build", g, None)
            if v[0] == "skip":
                held[g] = v[2]
                continue
            tell()
            if v[0] == "alt":
                return v[1], v[2]
            return g, f"schedule #{k} of {len(sched['order'])} ({sched['status']}, rocket at "                       f"{(sched.get('makespan') or 0) / 3600:.1f} game h)"
        waiting.append(f"{g}: {w}")     # the whole order is looked at: a buildable group far down gets its turn
    tell()
    return None, "schedule: " + "; ".join(waiting[:6] + [f"{g_}: short" for g_ in held])[:300]


def build(builders, places, clear, split, supply, Bridge):
    """Builders take spatial chunks of the increment; each clears trees/rocks and runs supply.build_plan."""
    chunks = split(places, len(builders))
    results = [None] * len(chunks)

    def work(i, c, chunk):
        try:
            bb = Bridge(c, autostart=False)
            clear(bb, [c for c in chunk if not c.get("remove")], margin=1)
            import macros
            # game minutes (supply.build_plan measures game time): hand-crafting waits for the burner furnaces, an
            # hour (6 wall minutes at 10x, as before); the mall phase never waits, the limit only bounds its passes
            results[i] = supply.build_plan(bb, chunk, minutes=60 if macros.HANDCRAFT else 30)
        except Exception as e:
            results[i] = {"done": False, "error": str(e)[:200]}
    ts = [threading.Thread(target=work, args=(i, builders[i % len(builders)], ch_)) for i, ch_ in enumerate(chunks)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    HAVE["v"] = None          # the stock moved: the gate reads it again
    # the game pairs an underground with the nearest counterpart in reach, not the planned one: check every
    # placed underground live against its planned partner (the "pairs" repair fixes what it finds)
    ug = [p for p in places if p["item"].endswith("underground-belt") and not p.get("remove")]
    if ug and PLAN is not None:
        try:
            bad = pair_check(Bridge(builders[0], autostart=False).raw, PLAN, ug, places)
            log(f"underground pairing: {len(ug)} placed, {len(bad)} not with their planned partner"
                + (f": {bad[:4]}" if bad else ""))
            BAD_PAIRS.extend(bad)
        except Exception as e:
            log("underground pairing check:", str(e)[:120])
    return [{k: v for k, v in (r or {}).items() if k in ("done", "short", "error")} |
            {"failed": ((r or {}).get("result") or {}).get("failed")} for r in results]


HAVE = {"v": None}    # the round's reachable stock + builders' inventories (cleared by every build and each round)


def stock_have(q, chars, Bridge):
    """What the builders' fetch can take (supply.reachable_stock: chests, furnace and assembler outputs, the whole
    surface) plus what the characters carry."""
    import supply
    have = collections.Counter(supply.reachable_stock(q))
    for c in chars:
        try:
            have.update(Bridge(c, autostart=False).call("state")["agent"].get("inventory") or {})
        except Exception:
            pass
    return have


def short_of(places, have, names=None, back=()):
    """Items the places need that `have` does not cover: pieces to remove, pieces already standing (names: live
    (x, y) -> name) and the items always hand-made (poles from wood, orphans) excepted; back: pieces picked up
    first, whose items come back to the builders."""
    import macros
    names = names or {}
    need = collections.Counter(p["item"] for p in places if not p.get("remove") and
                               names.get((round(p["x"], 2), round(p["y"], 2))) != p["item"])
    have = collections.Counter(have)
    have.update(p["item"] for p in back)
    return {k: v - have[k] for k, v in need.items() if have[k] < v and k not in macros.ALWAYS_HAND}


class Gate:
    """Asked before every build: never waits. gate(g, places) ->
      ("build", g, places)  the reachable stock covers what g places (or hand-crafting is on: bootstrap, dead grid,
                            a group the schedule hand-crafts)
      ("alt", h, why)       the group (or drain repair) behind the largest deficit, itself passing the gate
      ("skip", g, short)    nothing to do about it now: the makers stand and run, the mall needs time"""

    def __init__(self, plan, L, ch, q, builders, Bridge):
        self.plan, self.L, self.ch, self.q = plan, L, ch, q
        self.builders, self.Bridge = builders, Bridge
        self.pole_item = "small-electric-pole"
        self.hand = set()           # groups the schedule hand-crafts
        self.told = set()           # orphan items logged this run

    def have(self, fresh=False):
        if fresh or HAVE["v"] is None:
            HAVE["v"] = stock_have(self.q, self.builders, self.Bridge)
        return HAVE["v"]

    def __call__(self, g, places=None, depth=0, who=(), back=()):
        import macros
        plan, L = self.plan, self.L
        if places is None:
            places = increment(plan, L, g, L.researched, self.pole_item)[0]
        if macros.HANDCRAFT or g in self.hand:
            return "build", g, places
        have = collections.Counter(self.have())
        for c in who:               # a character outside the builders (the research runner)
            try:
                have.update(self.Bridge(c, autostart=False).call("state")["agent"].get("inventory") or {})
            except Exception:
                pass
        short = short_of(places, have, L.names, back)
        # placed by the plan but made by no mall block (added after the mall was sized, e.g. a pump that splits a
        # long fluid header): hand-crafted, and logged, until the plan's mall covers it
        orphan = {k for k in short if not plan.makers.get("stock:" + k) and not mall_groups(plan, k)}
        if orphan:
            macros.ALWAYS_HAND = macros.ALWAYS_HAND | orphan
            if orphan - self.told:
                self.told |= orphan
                log(f"no mall block makes {sorted(orphan)}: hand-crafting them (first for {g})")
            short = {k: v for k, v in short.items() if k not in orphan}
        if not short:
            return "build", g, places
        if depth < 3:
            for it in sorted(short, key=lambda k: -short[k]):
                try:            # live reads behind the search: one that fails holds g this round, never the loop
                    h, why = self.deficit(it, g)
                except Exception as e:
                    log(f"gate {g}: deficit of {it}: {str(e)[:120]}")
                    continue
                if not h or h == g:
                    continue
                why = f"{it} short by {short[it]} for {g}: {why}"
                if h.startswith("drain:"):
                    return "alt", h, why          # needs nothing: the chest it picks up is the chest it places
                v = self(h, None, depth + 1)
                if v[0] == "build":
                    return "alt", h, why
                if v[0] == "alt":
                    return "alt", v[1], f"{why} <- {v[2]}"
        return "skip", g, short

    def deficit(self, it, g, depth=0, seen=None):
        """(h, why): what to build so that `it` reaches the stock: an unbuilt maker (Chooser.pick, upstream
        included); behind a built maker that starves, the deficit of what it lacks (depth <= 3); a drain of its
        mall chest when the maker is full but the chest holds none. (None, why): the makers run, give them time."""
        plan, L = self.plan, self.L
        seen = set() if seen is None else seen
        if it in seen:
            return None, f"{it} again"
        seen.add(it)
        stocked = bool(plan.makers.get("stock:" + it) or mall_groups(plan, it))
        h, w = self.ch.pick("stock:" + it if depth == 0 and stocked else it)
        if h:
            return h, w
        mk = [x for x in plan.makers.get(it, []) if L.group_done(x) and x not in DEAD]
        idx = [i for i in maker_pieces(plan, mk, it) if i in L.built]
        if not idx:
            return None, w
        st = machine_state(self.q, [plan.p[i] for i in idx])
        cnt = collections.Counter(s for s, _ in st)
        miss = collections.Counter()
        for i, (s, ms) in zip(idx, st):
            if s in STARVED:
                if plan.p[i].get("recipe"):
                    miss.update(ms)
                elif "furnace" in plan.p[i]["item"]:      # an idle furnace has no recipe: its group's ore
                    miss.update(plan.need.get(plan.p[i].get("grp"), set()) - FUEL)
            elif s == "no_fuel":
                miss["coal"] += 1
        if depth < 3:
            for ing, _ in miss.most_common():
                h, w2 = self.deficit(ing, g, depth + 1, seen)
                if h:
                    return h, f"its makers lack {ing}; {w2}"
        if cnt.get("full_output"):
            d = drain_due(plan, L, self.q, mall_caps(plan, L, L.researched, self.pole_item), it, self.ch.skip)
            if d:
                return f"drain:{d[0]},{d[1]}", f"its maker is full, its chest holds {d[2]} over the cap and no room for {it}"
        return None, f"{it}: makers {dict(cnt)}"


FULL = {"full_output", "waiting_for_space_in_destination", "output_full"}
ITEM_STARVED = {"item_ingredient_shortage", "no_ingredients"}


def made_rates(plan, L, snap, status=None):
    """{group: {item: per second}} for built producing groups: the measured 10-minute made rate of each tracked
    item, split over its built makers by their machine count (the game's statistics are per force, not per group).
    status ((x, y) -> live status): a group whose makers of the item stand full is output-bound, its rate measures
    the demand, not what it can make: left out (the schedule takes the nameplate rate there). So is a group whose
    makers starve of items off the bus (assemblers, the steel furnaces): its rate measures the plates the bus brings,
    which the schedule's item levels model themselves (passed on, a frozen bus reads 0/min for every mall item until
    the next solve). A starved ore smelter stays measured: its ore comes over its own mine's belt, which the levels
    do not see."""
    made = (snap or {}).get("made") or {}
    status = status or {}
    out = collections.defaultdict(dict)
    for it, per_min in made.items():
        mk = [h for h in plan.makers.get(it, []) if L.group_done(h) and h not in DEAD]
        idx = {h: [i for i in maker_pieces(plan, [h], it) if i in L.built] for h in mk}
        tot = sum(len(v) for v in idx.values())
        for h in mk:
            st = [status.get(_k(plan.p[i]["x"], plan.p[i]["y"])) for i in idx[h]]
            full = sum(1 for s in st if s in FULL)
            ore_fed = h.startswith("smelt") and all(any(m.startswith("mine:") for m in plan.makers.get(x, ()))
                                                     for x in plan.need.get(h, set()) - FUEL)
            starved = 0 if ore_fed else sum(1 for s in st if s in ITEM_STARVED)
            if idx[h] and full * 2 < len(idx[h]) and starved * 2 < len(idx[h]):
                out[h][it] = round(per_min / 60 * len(idx[h]) / tot, 5)
    return dict(out) or None


COAL_LUA = ("local a=0 for _,r in pairs(game.surfaces[1].find_entities_filtered{area={{%d,%d},{%d,%d}},"
            "type='resource',name='coal'}) do a=a+r.amount end rcon.print(a)")


def coal_left(q, plan):
    """Coal left in the ground under the plan's coal drills (the box of every mine:coal group, mining area in)."""
    idx = [i for g in plan.groups if g.startswith("mine:coal") for i in plan.groups[g]
           if plan.p[i]["item"] == "electric-mining-drill"]
    if not idx:
        return None
    x1, y1, x2, y2 = plan.box(idx, 3)
    return int(float(q("/silent-command " + COAL_LUA % (math.floor(x1), math.floor(y1), math.ceil(x2), math.ceil(y2)))
                     or 0))


# ------------------------------------------------------------------------------------- underground pairing
UG_REACH = {"underground-belt": 5, "fast-underground-belt": 7, "express-underground-belt": 9}


def _k(x, y):
    return round(x, 2), round(y, 2)


def ug_partner(p, pool):
    """The underground of pool that p pairs with by the game's rule: the nearest one of the same kind and
    direction and the other belt type along p's travel (an entrance looks ahead, an exit behind), within reach."""
    d = p.get("direction", "north")
    dx, dy = DV4[d]
    want = "output" if p.get("belt_type") == "input" else "input"
    sg = 1 if want == "output" else -1
    at = {(math.floor(r["x"]), math.floor(r["y"])): r for r in pool
          if r["item"] == p["item"] and r.get("direction", "north") == d and r.get("belt_type") == want}
    t = (math.floor(p["x"]), math.floor(p["y"]))
    for k in range(1, UG_REACH.get(p["item"], 5) + 1):
        r = at.get((t[0] + sg * k * dx, t[1] + sg * k * dy))
        if r is not None:
            return r
    return None


def pair_check(q, plan, ug, places):
    """Placed undergrounds whose live partner is not the planned one: [(piece, live partner xy, planned xy)].
    Planned: a plan piece pairs within the plan's pieces, any other piece within its own place list."""
    from auto import live as lv
    plan_ug = [p for p in plan.p if p["item"].endswith("underground-belt")]
    pk = {_k(p["x"], p["y"]) for p in plan_ug}
    own = [p for p in places if p["item"].endswith("underground-belt") and not p.get("remove")]
    rows = [(p, ug_partner(p, plan_ug if _k(p["x"], p["y"]) in pk else own)) for p in ug]
    pos = [(p["x"], p["y"]) for p, _ in rows] + [(w["x"], w["y"]) for _, w in rows if w]
    got = dict(zip([_k(*xy) for xy in pos], lv.underground_partners(q, pos)))
    bad = []
    for p, w in rows:
        r = got.get(_k(p["x"], p["y"]))
        if r is False:
            continue                      # not standing (it did not place)
        wk = _k(w["x"], w["y"]) if w and got.get(_k(w["x"], w["y"])) is not False else None
        rk = _k(*r) if r else None
        if rk != wk:
            bad.append(((p["x"], p["y"]), r and tuple(r), wk))
    return bad


BAD_PAIRS = []     # mismatches build() saw: the "pairs" repair looks at once


def bad_pairs(plan, L, q, state):
    """Built plan undergrounds that the game paired with a piece of our own (an own route ran along the plan
    pair): the own pieces to pick up, the intruder and its own partner, so the plan pair pairs again."""
    from auto import live as lv
    own = [p for p in state.get("extra_places", []) if p["item"].endswith("underground-belt")]
    if not own:
        return []
    plan_ug = [p for p in plan.p if p["item"].endswith("underground-belt")]
    near = [plan.p[i] for i in L.built if plan.p[i]["item"].endswith("underground-belt") and
            any(abs(plan.p[i]["x"] - o["x"]) + abs(plan.p[i]["y"] - o["y"]) <= 2 * UG_REACH.get(o["item"], 5) + 1
                for o in own)]
    if not near:
        return []
    got = lv.underground_partners(q, [(p["x"], p["y"]) for p in near])
    ownk = {_k(p["x"], p["y"]): p for p in own}
    out, seen = [], set()
    for p, r in zip(near, got):
        if not r:
            continue                      # unpaired or gone: gap/restore handle the plan side
        w = ug_partner(p, plan_ug)
        intr = ownk.get(_k(*r))
        if intr is None or (w and _k(w["x"], w["y"]) == _k(*r)):
            continue
        for x in (intr, ug_partner(intr, own)):
            if x is not None and _k(x["x"], x["y"]) not in seen:
                seen.add(_k(x["x"], x["y"]))
                out.append(x)
    return out


# inserters waiting for space in an assembler while holding an item its recipe does not take (they never drop it)
STUCK_LUA = r"""
local S=game.surfaces[1] local o={} local W=defines.entity_status.waiting_for_space_in_destination
for _,e in pairs(S.find_entities_filtered{area={{%d,%d},{%d,%d}},type='inserter',force='player'}) do
  local h=e.held_stack local t=e.drop_target
  if e.status==W and h and h.valid_for_read and t and t.type=='assembling-machine' then
    local r=t.get_recipe() local ok=false
    if r then for _,g in pairs(r.ingredients) do if g.name==h.name then ok=true end end end
    if r and not ok then o[#o+1]=e.name..'|'..e.position.x..'|'..e.position.y..'|'..h.name..'|'..r.name end
  end
end
rcon.print(table.concat(o,';'))
"""


def stuck_inserters(plan, L, q, state, tries=2):
    """[(place, held item, recipe)] for stuck inserters we know how to lay again (plan pieces, our own pieces),
    each at most `tries` times."""
    from auto import live as lv
    x1, y1, x2, y2 = plan.box(range(len(plan.p)), 8)
    own = {_k(p["x"], p["y"]): p for p in state.get("extra_places", []) if p["item"].endswith("inserter")}
    done = state.get("unstuck", {})
    out = []
    for n, x, y, held, rec in lv._run(q, STUCK_LUA, (x1, y1, x2, y2)):
        x, y = float(x), float(y)
        if done.get(f"{x},{y}", 0) >= tries:
            continue
        i = plan.key.get(_k(x, y))
        if i is not None and plan.p[i]["item"] == n:
            p = materialize(plan.p[i], L.researched, "small-electric-pole")[0]     # an inserter: no pole kind
        elif _k(x, y) in own and own[_k(x, y)]["item"] == n:
            p = {k: v for k, v in own[_k(x, y)].items() if k in ("item", "x", "y", "direction")}
        else:
            continue
        out.append((dict(p, x=x, y=y), held, rec))
    return out


# a finished rocket waits in its silo; 2.0 will not send it empty: the satellite (the plan's cargo, made into a mall
# chest) goes into the cargo slot, else one of our characters puts an item it carries there (a satellite if it has
# one, else what it holds most of), then launch is pressed
LAUNCH_LUA = ("local n=0 for _,e in pairs(game.surfaces[1].find_entities_filtered{type='rocket-silo',force='player'}) do "
              "if e.status==defines.entity_status.waiting_to_launch_rocket then "
              "local ri=e.get_inventory(defines.inventory.rocket_silo_rocket) "
              "if ri and ri.is_empty() then for _,c in pairs(game.surfaces[1].find_entities_filtered{name='iron-chest',force='player'}) do "
              "if c.get_item_count('satellite')>0 and ri.insert{name='satellite',count=1}>0 then c.remove_item{name='satellite',count=1} break end end end "
              "if ri and ri.is_empty() then for _,ch in pairs(game.surfaces[1].find_entities_filtered{type='character',force='player'}) do "
              "local inv=ch.get_main_inventory() local best,bc=nil,0 "
              "for _,it in pairs(inv.get_contents()) do local c=(it.name=='satellite' and 1e9 or it.count) if c>bc then best,bc=it.name,c end end "
              "if best and ri.insert{name=best,count=1}>0 then inv.remove{name=best,count=1} break end end end "
              "if e.launch_rocket() then n=n+1 end end end rcon.print(n)")


def launched(q):
    """True once a rocket has gone up. A finished rocket waits in its silo for the launch button (2.0): pressed here."""
    try:
        n = int(q("/silent-command " + LAUNCH_LUA) or 0)
        if n:
            log(f"launch pressed on {n} silo(s)")
        return int(q("/silent-command rcon.print(game.forces.player.rockets_launched)") or 0) > 0
    except ValueError:
        return False


SMELTED = {"iron-plate", "copper-plate", "steel-plate", "stone-brick"}
SPEED = 10       # the run's game speed (set from --speed)


def research_loop(b, q, rstate, hut, state, stop, gate=None):
    """Keep the queue filled with what the next increments need (rstate["want"]); do trigger techs; during the
    bootstrap run the hut lab on hand-crafted red packs. The hut lab is placed only when the gate passes it (this
    runner's own inventory counts; no alt search from this thread)."""
    from auto import research
    import supply

    def hut_ok():
        return hut is not None and (gate is None or gate("hut", [hut], depth=3, who=(b.agent,))[0] == "build")
    while not stop.is_set():
        try:
            want = rstate["want"]
            r = json.loads(q("/silent-command " + (research.TICK_LUA % json.dumps(want)).replace("\n", " ")))
            if r.get("queued"):
                log("research queued", r["queued"])
            st = json.loads(q("/silent-command " + (research.STATUS_LUA % (json.dumps(want), hut["x"], hut["y"]))
                              .replace("\n", " ")))
            for t in st.get("triggers") or []:
                if t["type"] == "craft-item" and t.get("item") in SMELTED:
                    continue          # plates count when the furnaces make them
                if t["type"] == "craft-item" and t.get("item"):
                    log(f"trigger {t['name']}: craft {t['count']} {t['item']}")
                    supply.craft_items(b, t["item"], t["count"])
                    if t["item"] == "lab" and not st.get("lab") and hut_ok():
                        supply.build_plan(b, [hut], minutes=5)
            if state.get("handcraft") and st.get("current") and set(st.get("ing") or []) <= research.HAND:
                if not st.get("lab"):
                    if hut_ok():
                        supply.build_plan(b, [hut], minutes=5)
                elif st.get("lab_packs", 0) < 5:
                    supply.feed_labs(b, [hut], "automation-science-pack", 10)
        except Exception as e:
            log("research error", str(e)[:200])
        stop.wait(20)


if __name__ == "__main__":
    main()
