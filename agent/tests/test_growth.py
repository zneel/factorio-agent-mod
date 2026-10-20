"""Regression tests for the growth controller's rules (offline: small hand-built layouts, no game).

    E:\\modding\\factorio-agent\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v     (from agent/)

Each test pins one stall measured on seed 1060 (2026-10-19): the rule that fixed it must keep holding.
"""
import math
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto import budget, flow, geom, grow, remine, scout, trim  # noqa: E402


def belt(x, y, d):
    return {"item": "transport-belt", "x": x + .5, "y": y + .5, "direction": d}


def plan_of(places, ports=None, **kw):
    """A plan stand-in: the placements, their flow graph, and whatever group tables a rule reads."""
    return SimpleNamespace(p=places, flow=flow.Flow(places, ports), **kw)


class FluidLinks(unittest.TestCase):
    """A steam engine's port facing a belt is no fluid link (the belt made the boiler side look supplied, so the
    half-built coal line to the boilers was never finished and the grid died)."""

    def setUp(self):
        self.ports = lambda item, recipe, d: ([("steam", "input-output", 0, -3), ("steam", "input-output", 0, 3)]
                                              if item == "steam-engine" else [])

    def test_engine_port_on_belt_is_not_linked(self):
        eng = {"item": "steam-engine", "x": 0.5, "y": 0.5, "direction": "north"}
        b = belt(0, -3, "east")           # the tile in front of the engine's north port
        f = flow.Flow([eng, b], self.ports)
        self.assertNotIn(1, f.fluid[0])
        self.assertNotIn(0, f.fluid[1])

    def test_engines_facing_each_other_are_linked(self):
        a = {"item": "steam-engine", "x": 0.5, "y": 0.5, "direction": "north"}
        b = {"item": "steam-engine", "x": 0.5, "y": -4.5, "direction": "north"}
        f = flow.Flow([a, b], self.ports)
        self.assertIn(1, f.fluid[0])
        self.assertIn(0, f.fluid[1])


class Tails(unittest.TestCase):
    """A belt past the last inserter strands what drifts onto it (rocket fuel on 3 belts past the silo)."""

    def test_tail_past_last_pickup(self):
        places = [belt(x, 0, "east") for x in range(6)]
        places.append({"item": "inserter", "x": 2.5, "y": 1.5, "direction": "south"})   # picks from belt x=2
        places.append({"item": "iron-chest", "x": 2.5, "y": 2.5})
        f = flow.Flow(places)
        self.assertIn(6, f.feeds[2], "the inserter must pick from belt x=2")
        self.assertEqual(trim.tails(places, f), {3, 4, 5})

    def test_run_ending_at_pickup_has_no_tail(self):
        places = [belt(x, 0, "east") for x in range(3)]
        places.append({"item": "inserter", "x": 2.5, "y": 1.5, "direction": "south"})
        places.append({"item": "iron-chest", "x": 2.5, "y": 2.5})
        self.assertEqual(trim.tails(places, flow.Flow(places)), set())

    def test_only_built_belts_count(self):
        places = [belt(x, 0, "east") for x in range(6)]
        places.append({"item": "inserter", "x": 2.5, "y": 1.5, "direction": "south"})
        places.append({"item": "iron-chest", "x": 2.5, "y": 2.5})
        f = flow.Flow(places)
        # belt 5 given up: the standing run ends at 4, so 3 and 4 are the tail
        self.assertEqual(trim.tails(places, f, among={0, 1, 2, 3, 4}, ahead={0, 1, 2, 3, 4, 6, 7}), {3, 4})

    def test_line_under_construction_is_not_cut(self):
        places = [belt(x, 0, "east") for x in range(6)]
        places.append({"item": "inserter", "x": 5.5, "y": 1.5, "direction": "south"})    # the reader at the far end
        places.append({"item": "iron-chest", "x": 5.5, "y": 2.5})
        f = flow.Flow(places)
        # belts 4-5 not placed yet but still planned: nothing standing is a tail
        self.assertEqual(trim.tails(places, f, among={0, 1, 2, 3, 6, 7}, ahead=set(range(8))), set())


class LinkPaths(unittest.TestCase):
    """The route from a built supplier to a built consumer is found from the supplier side (the upstream walk stopped
    at the first fed belt and never saw the unbuilt B2 -> B5 circuit branch)."""

    def setUp(self):
        self.saved = set(grow.DEAD)
        grow.DEAD.clear()
        p = [{"item": "assembling-machine-2", "x": 0.5, "y": 0.5, "recipe": "r1", "grp": "S"},
             {"item": "inserter", "x": 2.5, "y": 0.5, "direction": "east", "grp": "S"}]
        p += [dict(belt(x, 0, "east"), grp="conn") for x in range(3, 9)]
        p += [{"item": "inserter", "x": 9.5, "y": 0.5, "direction": "east", "grp": "C"},
              {"item": "assembling-machine-2", "x": 11.5, "y": 0.5, "recipe": "r2", "grp": "C"}]
        groups = {}
        for i, q in enumerate(p):
            groups.setdefault(q["grp"], []).append(i)
        gd = {"recipes": {"r1": {"prod": [["x", 1]], "ing": []}, "r2": {"prod": [["y", 1]], "ing": [["x", 1]]}}}
        self.plan = plan_of(p, groups=groups, need={"C": {"x"}}, deps={"C": {"S"}}, made={"S": {"x"}}, gd=gd)

    def tearDown(self):
        grow.DEAD.clear()
        grow.DEAD.update(self.saved)

    def test_missing_belts_are_found(self):
        built = set(range(len(self.plan.p))) - {5, 6}
        self.assertEqual(grow.link_paths(self.plan, built, "C"), {5, 6})

    def test_connected_needs_nothing(self):
        self.assertEqual(grow.link_paths(self.plan, set(range(len(self.plan.p))), "C"), set())


class RearBelts(unittest.TestCase):
    """A T merge with one side built is a curve: that side's item takes both lanes (pipes over the circuit lane into
    B5). The other side's last belt keeps it a side-load."""

    def test_t_merge_gets_its_other_side(self):
        places = [belt(0, 0, "south"), belt(-1, 0, "east"), belt(1, 0, "west"), belt(0, 1, "south")]
        plan = plan_of(places)
        self.assertIn(2, grow.rear_belts(plan, {1}, {0, 1, 3}))

    def test_side_load_gets_its_rear_belt(self):
        places = [belt(0, 0, "south"), belt(-1, 0, "east"), belt(0, -1, "south")]
        plan = plan_of(places)
        self.assertIn(2, grow.rear_belts(plan, {1}, {0, 1}))


class Flushes(unittest.TestCase):
    """The flush cap counts flushes since the map last changed (the B5 jam came back before its merge was whole)."""

    def test_counter_resets_when_the_map_changes(self):
        st = {"flushed": {"B5": 6}, "flush_at": {"B5": 10}}
        self.assertEqual(grow.flushes(st, "B5", SimpleNamespace(built=set(range(10)))), 6)
        self.assertEqual(grow.flushes(st, "B5", SimpleNamespace(built=set(range(11)))), 0)


class Scout(unittest.TestCase):
    def test_patches_cluster_neighbour_chunks(self):
        rows = [(0, 0, "coal", 10, 1000, 5, 5), (1, 0, "coal", 10, 3000, 40, 5), (5, 5, "coal", 5, 500, 170, 170),
                (0, 0, "iron-ore", 3, 100, 1, 1)]
        ps = sorted((p for p in scout.patches(None, rows) if p["name"] == "coal"), key=lambda p: -p["amount"])
        self.assertEqual([p["amount"] for p in ps], [4000, 500])
        self.assertAlmostEqual(ps[0]["center"][0], (5 * 1000 + 40 * 3000) / 4000)
        self.assertEqual(ps[0]["box"], (0, 0, 64, 32))

    def test_deficits(self):
        sched = {"makespan": 36000, "coal": {"need": 100, "left": 50}}
        dem = {"raw_per_min": {"iron-ore": 10}}
        d = scout.deficits(sched, dem, SimpleNamespace(p=[]), SimpleNamespace(own=[]), [])
        self.assertEqual(d["coal"], 70)               # 100 * 1.2 - 50
        self.assertEqual(d["iron-ore"], 7200)         # 10/min * 600 min * 1.2, nothing mined yet
        self.assertNotIn("copper-ore", d)

    def test_drills_for(self):
        self.assertEqual(scout.drills_for(0, {"makespan": 36000}), 4)
        self.assertEqual(scout.drills_for(10 ** 9, {"makespan": 36000}), 12)


def patch(name, amount, x, y, tiles=900):
    return {"name": name, "amount": amount, "tiles": tiles, "center": (x, y), "box": (x - 16, y - 16, x + 16, y + 16),
            "chunks": [(x // 32, y // 32)]}


class Budget(unittest.TestCase):
    """The whole run's raw budget and the patches that cover it (seed 1060 ran out of coal twice: the only coal
    patch it knew held 252k of the ~380k the run mined, and the big patches were found after the fuel ran out)."""

    def setUp(self):
        # one hour of 30 iron plates/min (stone furnaces) and 15 gears/min (assembler 2)
        self.dem = {"hours": 1.0, "crafts_per_min": {"iron-plate": 30, "iron-gear-wheel": 15},
                    "machines": {"iron-plate": 1.6, "iron-gear-wheel": 0.17},
                    "recipes": {"iron-plate": {"cat": "smelting", "energy": 3.2, "ing": [["iron-ore", 1]],
                                               "prod": [["iron-plate", 1]]},
                                "iron-gear-wheel": {"cat": "crafting", "energy": 0.5, "ing": [["iron-plate", 2]],
                                                    "prod": [["iron-gear-wheel", 1]]}}}

    def test_need_counts_ore_and_fuel(self):
        b = budget.need(self.dem, calib={}, margin=1.0)
        self.assertEqual(b["total"]["iron-ore"], 1800)
        # furnaces 1.6 busy x 90 kW + gears 1/6 busy x 150 kW + one drill 90 kW, 4 MJ a coal, 60 min
        fuel = (1.6 * 90 + 15 * 0.5 / 45 * 150 + 90) * 60 / 4000 * 60
        self.assertAlmostEqual(b["total"]["coal"], fuel, delta=1)
        self.assertEqual(b["total"]["stone"], 0)

    def test_calibration_scales_work_and_adds_idle_burn(self):
        b0 = budget.need(self.dem, calib={}, margin=1.0)
        b = budget.need(self.dem, calib={"calib": {"coal": 2, "iron-ore": 1.5}, "idle": 10, "hours": 5}, margin=1.0)
        self.assertEqual(b["total"]["iron-ore"], 2700)
        self.assertAlmostEqual(b["total"]["coal"], 2 * b0["total"]["coal"] + 10 * 60 * 5, delta=2)

    def test_calibrate_cuts_at_the_launch(self):
        z = [0] * 300
        ser = {k: list(z) for k in ("coal:input", "coal:output", "iron-ore:input", "iron-ore:output",
                                    "copper-ore:input", "stone:input")}
        for i in range(2, 8):              # newest first: samples 2..7 the run, 0..1 after the launch
            ser["coal:input"][i] = 1000
            ser["iron-ore:input"][i] = 900
        ser["coal:output"][0] = ser["coal:output"][1] = 100
        c = budget.calibrate(ser, self.dem)
        self.assertEqual(c["measured"]["iron-ore"], 5400)
        self.assertEqual(c["hours"], 5.0)                     # 6 samples of 50 min
        self.assertEqual(c["idle"], 2.0)                      # 100 per 50 min
        self.assertAlmostEqual(c["calib"]["iron-ore"], 3.0)
        lp = budget.need(self.dem, calib={}, margin=1.0)["lp"]["coal"]
        self.assertAlmostEqual(c["calib"]["coal"], (6000 - 2 * 60 * 5) / lp, places=2)

    def test_left_and_short(self):
        b = {"total": {"coal": 300, "iron-ore": 100}, "rate": {"crude-oil": 100},
             "phases": [{"name": "a", "coal": 200, "iron-ore": 50}, {"name": "b", "coal": 100, "iron-ore": 50}]}
        bl = budget.left(b, {"coal": 250})
        self.assertEqual(bl["total"]["coal"], 50)
        self.assertEqual([p["coal"] for p in bl["phases"]], [0, 50])      # the oldest phase is mined first
        self.assertEqual(budget.short(bl, {"coal": 60, "iron-ore": 40, "crude-oil": 200}), {"iron-ore": 60})
        self.assertEqual(budget.short(bl, {"coal": 60, "iron-ore": 100, "crude-oil": 50}), {"crude-oil": 60})

    def test_known_counts_patches_in_reach(self):
        ps = [patch("coal", 1000, 0, 0), patch("coal", 1000, 2000, 0), patch("crude-oil", 600000, 50, 0, tiles=2)]
        k = budget.known(ps, (0, 0), hours=1000)
        self.assertAlmostEqual(k["coal"], 1000 * budget.USABLE)
        self.assertAlmostEqual(k["crude-oil"], 2 * 120)      # two wells at their floor: 2 crude/s each

    def test_select_nearest_patches_that_cover_the_amount(self):
        ps = [patch("coal", 100000, 0, 0), patch("coal", 1000000, 500, 0), patch("coal", 300000, 200, 0)]
        b = {"total": {"coal": 300000}, "phases": [{"name": "run", "hours": 100, "coal": 300000}]}
        s = budget.select(ps, b, (0, 0), mined=[0])
        self.assertEqual(sorted(c["k"] for c in s["chosen"]), [0, 2])       # the start patch + the near one
        b = {"total": {"coal": 400000}, "phases": [{"name": "run", "hours": 100, "coal": 400000}]}
        s = budget.select(ps, b, (0, 0), mined=[0])
        self.assertEqual(sorted(c["k"] for c in s["chosen"]), [0, 1])       # the big far one alone, not both

    def test_select_skips_a_patch_no_outpost_reached(self):
        ps = [patch("iron-ore", 100000, 0, 0), patch("iron-ore", 1000000, 200, 0), patch("iron-ore", 1000000, 400, 0)]
        b = {"total": {"iron-ore": 300000}, "phases": [{"name": "run", "hours": 100, "iron-ore": 300000}]}
        self.assertEqual(sorted(c["k"] for c in budget.select(ps, b, (0, 0), mined=[0])["chosen"]), [0, 1])
        self.assertEqual(sorted(c["k"] for c in budget.select(ps, b, (0, 0), mined=[0], skip={1})["chosen"]), [0, 2])

    def test_select_needs_drill_rate_not_only_amount(self):
        # 100k in one hour = 56 drills; the start patch holds the amount but only 20 drills fit on its 300 tiles
        ps = [patch("coal", 1000000, 0, 0, tiles=300), patch("coal", 1000000, 300, 0, tiles=1500)]
        b = {"total": {"coal": 100000}, "phases": [{"name": "run", "hours": 1, "coal": 100000}]}
        s = budget.select(ps, b, (0, 0), mined=[0])
        self.assertEqual(sorted(c["k"] for c in s["chosen"]), [0, 1])
        self.assertGreaterEqual(sum(c["drills"] for c in s["chosen"]), 56)

    def test_select_reports_what_no_patch_covers(self):
        s = budget.select([patch("coal", 1000, 0, 0)], {"total": {"coal": 5000},
                                                        "phases": [{"name": "run", "hours": 9, "coal": 5000}]},
                          (0, 0), mined=[0])
        self.assertEqual(s["status"], "infeasible")
        self.assertEqual(s["short"], {"coal": 5000 - 850})


class SpentBlocks(unittest.TestCase):
    """A block nothing needs any more is picked up (seed 1060: B13, the silo maker, kept 129 belt tiles of steel,
    concrete, pipe, processing units and electric engines after the silo stood)."""

    def setUp(self):
        rec = {"rocket-silo": {"ing": [["concrete", 1000], ["processing-unit", 200]], "prod": [["rocket-silo", 1]]},
               "processing-unit": {"ing": [["electronic-circuit", 20]], "prod": [["processing-unit", 1]]}}
        self.p = [{"item": "assembling-machine-2", "x": 0.5, "y": 0.5, "recipe": "rocket-silo", "grp": "B13"},
                  {"item": "inserter", "x": 2.5, "y": 0.5, "grp": "B13"},
                  {"item": "iron-chest", "x": 3.5, "y": 0.5, "mall": ["rocket-silo"], "grp": "B13"},
                  {"item": "small-electric-pole", "x": 0.5, "y": 2.5, "grp": "B13"},
                  {"item": "transport-belt", "x": 0.5, "y": -2.5, "grp": "band:B13"},
                  {"item": "splitter", "x": 1.0, "y": -3.5, "grp": "band:B13"},
                  {"item": "assembling-machine-2", "x": 10.5, "y": 0.5, "recipe": "processing-unit", "grp": "B5"},
                  {"item": "rocket-silo", "x": 30.5, "y": 0.5, "grp": "silo"}]
        ns = SimpleNamespace(gd={"recipes": rec}, dem={})
        groups = {}
        for i, q in enumerate(self.p):
            groups.setdefault(q["grp"], []).append(i)
        self.plan = SimpleNamespace(p=self.p, groups=groups, prod={"B13", "B5", "silo"},
                                    _io=lambda g, ents: grow.Plan._io(ns, g, ents))
        self.plan.made = {g: self.plan._io(g, [self.p[i] for i in idx])[0] for g, idx in groups.items()}

    def live(self, built):
        return SimpleNamespace(built=set(built), dead=set(), retired=set())

    def test_silo_maker_is_spent_once_the_silo_stands(self):
        L = self.live(range(len(self.p)))
        sp = grow.spent_blocks(self.plan, L, {(3.5, 0.5): {"rocket-silo": 0}}, set(), "small-electric-pole")
        self.assertEqual([g for g, _ in sp], ["B13"])                  # B5 still feeds the silo
        self.assertEqual(sorted(sp[0][1]), [0, 1, 4])                  # machine, inserter, band belt; no chest/pole/splitter

    def test_not_before_the_silo_or_while_a_chest_wants_it(self):
        L = self.live(range(len(self.p) - 1))                          # the silo is not built yet
        self.assertEqual(grow.spent_blocks(self.plan, L, {}, set(), "small-electric-pole"), [])
        L = self.live(range(len(self.p)))
        self.assertEqual(grow.spent_blocks(self.plan, L, {(3.5, 0.5): {"rocket-silo": 1}}, set(),
                                           "small-electric-pole"), [])


class Undergrounds(unittest.TestCase):
    """Underground pairs as the game matches them (a new pair laid along an old one stole the smelter feed's
    partner and iron went to 0/min)."""

    def test_pair(self):
        ug = [{"item": "underground-belt", "x": 0.5, "y": 0.5, "direction": "north", "belt_type": "input"},
              {"item": "underground-belt", "x": 0.5, "y": -2.5, "direction": "north", "belt_type": "output"}]
        self.assertIn(("v", (0, 0), (0, -3)), remine._pairs(ug))

    def test_spans_cover_the_axis(self):
        sp = remine._spans([("v", (0, 0), (0, -3))])
        self.assertEqual(sp["v"], {(0, 0), (0, -1), (0, -2), (0, -3)})


class LayoutSpan(unittest.TestCase):
    """Block belts run from their first to their last user, reaching an edge only where items enter or leave."""

    def test_belt_ends_at_last_user(self):
        import plan_layout
        rec = {"mk": ([("a", 1)], [("b", 1)], 1.0), "use": ([("b", 1)], [("c", 1)], 1.0)}
        spec = {"name": "t", "origin": [0, 0], "rows": 1, "cols": 4,
                "machines": [{"id": "m0", "recipe": "mk"}, {"id": "m1", "recipe": "use"}],
                "inputs": [{"left": "a", "right": None}], "exports": ["c"]}
        blk, status = plan_layout.solve(spec, rec, seconds=10)
        self.assertIn(status, ("OPTIMAL", "FEASIBLE"))
        xs = [m["x"] for m in blk["machines"]]
        lo_x, hi_x = min(xs) - 1, max(xs) + 1
        for b in blk["belts"]:
            items = {i for i in b["lanes"].values() if i}
            if items and items <= {"a"}:          # the input belt: from its entry edge to its last reader only
                end = b["x2"]
                self.assertTrue(lo_x <= end <= hi_x, f"input belt runs past its readers: {b} machines at {xs}")



class Dispatch(unittest.TestCase):
    """main() hands what choose() names to one handler per kind (auto.repairs); the rules keep their order."""

    def test_handlers(self):
        from auto import repairs
        r = repairs
        for g, mod in (("relocate:mine:coal:0", r.relocate), ("outpower:2", r.outpower), ("trim", r.trim),
                       ("face", r.face), ("radar:1.5,2.5", r.radar), ("outpost:crude-oil", r.outpost),
                       ("remine:mine:coal:1", r.remine), ("flush:smelt:iron-ore:0", r.flush), ("drain:1.5,2.5", r.drain),
                       ("spent:B13", r.spent), ("pairs", r.pairs), ("unstick", r.unstick), ("burner:stone", r.burner),
                       ("B1", r.increment), ("restore", r.increment), ("link:labs", r.increment),
                       ("finish:pipe:crude", r.increment), ("trimmed", r.increment), ("pairs_fix", r.increment)):
            self.assertIs(repairs.handler(g), mod, g)

    def test_rule_order(self):
        from auto.growth import chooser
        self.assertEqual([f.__name__ for f in chooser.RULES],
                         ["restore", "pending", "repair", "burner", "rears", "relocate", "remine", "pairs", "reconnect",
                          "fuel", "gap", "link", "finish", "trim", "repower", "flush", "drain", "spent", "unstick",
                          "face", "upgrade", "outpower", "scout", "schedule"])


if __name__ == "__main__":
    unittest.main()
