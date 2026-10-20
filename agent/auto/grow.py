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

Where things are: auto.growth (core: Plan, Live, materialize; increments; chooser: the repair rules and the
schedule; gate: build and the material gate; mall, sensors, world, scouting, upkeep; context: the controller's
state; setup and rounds) and auto.repairs (one handler per kind of increment).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto import repairs  # noqa: E402
from auto.growth import rounds, setup  # noqa: E402
# what auto.schedule and the tests use from here
from auto.growth.core import ASM1_CATS, POLE_ITEMS, RECIPE_SUB, Live, Plan, materialize, techs_of  # noqa: E402,F401
from auto.growth.increments import DEAD, increment, link_paths, rear_belts  # noqa: E402,F401
from auto.growth.mall import spent_blocks  # noqa: E402,F401
from auto.growth.sensors import flushes  # noqa: E402,F401


def main(argv=None):
    ctx = setup.context(setup.args(argv))
    try:
        for rnd in range(ctx.args.rounds):
            ctx.rnd = rnd
            rounds.begin(ctx)
            g, why = rounds.pick(ctx)
            if g is None:
                if rounds.idle(ctx, why):
                    break
                continue
            if repairs.run(ctx, g, why):
                break
    finally:
        ctx.stop.set()
        if ctx.tender:
            ctx.tender.terminate()


if __name__ == "__main__":
    main()
