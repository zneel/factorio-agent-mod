"""Belt tails past the last pickup: a belt run that dead-ends beyond the last inserter taking from it holds items
nobody ever takes (measured: rocket fuel stranded on 3 belts past the silo's last inserter; 38 such tails, 201 belts
in the seed-1060 plan). The run must end on the last pickup tile.

    idx = trim.tails(places, flow)       placement indices of every tail belt (plan time: drop them; live: pick up)

A tail is walked back from a belt that feeds nothing: belts no inserter picks from, each with exactly one belt
feeding it straight and nothing side-loading it, up to the first belt something takes from or merges into.
"""
BELTS = ("transport-belt", "fast-transport-belt", "express-transport-belt")
CARRY = ("transport-belt", "underground-belt", "splitter")


def tails(places, flow, among=None, ahead=None):
    """Indices of tail belts (see the module doc). among: only belts in this set count (live: the built ones);
    ahead: pieces that still count as the run going on although not standing (live: planned and not given up, so a
    line under construction is never cut back). Default: among."""
    ahead = among if ahead is None else ahead
    out = set()
    for i, p in enumerate(places):
        if p["item"] not in BELTS or p.get("remove") or (among is not None and i not in among):
            continue
        if any(any(c in places[k]["item"] for c in CARRY) and (ahead is None or k in ahead) for k in flow.feeds[i]):
            continue                       # it carries on into another (standing) belt: not the end of a run
        j = i
        while j is not None and places[j]["item"] in BELTS and (among is None or j in among):
            if any(not any(c in places[k]["item"] for c in CARRY) for k in flow.feeds[j]):
                break                      # an inserter (or a machine) takes from here: the run ends at this tile
            back = [k for k in flow.fed_by[j] if any(c in places[k]["item"] for c in CARRY)]
            side = [k for k in flow.fed_by[j] if not any(c in places[k]["item"] for c in CARRY)]
            if side or len(back) != 1:
                break                      # something drops onto it or two runs merge here: keep it
            out.add(j)
            j = back[0]
    return out
