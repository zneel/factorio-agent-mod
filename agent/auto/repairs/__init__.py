"""One handler per kind of increment choose() names (growth.chooser): run(ctx, g, why) -> True once the rocket
went up. A kind ending in ":" is a prefix (relocate:<mine>), any other is the whole name; everything else is a
plan group or a repair that only selects plan pieces (increment.py). Shared steps: common.py."""
from auto.repairs import (burner, common, drain, face, flush, increment, outpost, outpower, pairs, radar, relocate, remine,
                          spent, trim, unstick)

HANDLERS = [("relocate:", relocate), ("outpower:", outpower), ("trim", trim), ("face", face), ("radar:", radar),
            ("outpost:", outpost), ("remine:", remine), ("flush:", flush), ("drain:", drain), ("spent:", spent),
            ("pairs", pairs), ("unstick", unstick), ("burner:", burner)]


def handler(g):
    for kind, mod in HANDLERS:
        if g.startswith(kind) if kind.endswith(":") else g == kind:
            return mod
    return increment


PICKUP_ONLY = (drain, spent, pairs, unstick)    # repairs that only pick up and lay again: a dry run plans none


def run(ctx, g, why):
    h = handler(g)
    if ctx.args.dry and h in PICKUP_ONLY:     # choose() names them only with a gate, which a dry run has not
        common.planned(ctx, g, why, "a pick-up repair (not planned in a dry run)", [])
        return
    return h.run(ctx, g, why)
