"""The steps every handler shares: gate -> build (step), the game minute and the live statuses after it (look),
the log line, the grow.jsonl record and the state save (done); plus our own pieces kept in the state (keep_own) and
a dry run's stand-in for a build (planned)."""
import collections

OWN_KEYS = ("item", "x", "y", "direction", "belt_type")    # what grow-state.json keeps of a piece of our own
DRY_SKIP = 10 ** 6       # a dry run plans a repair once: planned, not chosen again


def xy(p):
    return round(p["x"], 2), round(p["y"], 2)


def around(pieces, m):
    """The box around pieces, m tiles out."""
    return (min(p["x"] for p in pieces) - m, min(p["y"] for p in pieces) - m,
            max(p["x"] for p in pieces) + m, max(p["y"] for p in pieces) + m)


def step(ctx, g, why, places, line, hold, back=(), first=None):
    """Gate -> build. The gate holds g (ctx.held, `hold` rounds): None. Else the increment's number k is taken,
    "#k g: line — why" logged, `first` (pick-ups, if not None) built by one builder, then places by all:
    (k, build results). A dry run: planned, nothing built (None)."""
    if ctx.args.dry:
        planned(ctx, g, why, line, list(first or []) + list(places))
        return None
    gv = ctx.gate(g, places, back=back)
    if gv[0] != "build":
        ctx.held(g, gv, hold)
        return None
    k = ctx.begin()
    ctx.log(f"#{k} {g}: {line} — {why}")
    if first is not None:
        ctx.build(first, one=True)
    return k, ctx.build(places)


def statuses(ctx, box, names=None):
    """{(x, y): live status} in box (only entities named in names, if given)."""
    from auto import live as lv
    return {(round(x, 2), round(y, 2)): s_ for n_, x, y, s_ in lv.status(ctx.q, box) if names is None or n_ in names}


def look(ctx, pieces, m, optional=False):
    """A game minute of running, then the live status of each piece (box: pieces, m tiles out; "missing" where
    none stands). optional: no pieces, no read."""
    ctx.settle()
    at_ = statuses(ctx, around(pieces, m)) if pieces or not optional else {}
    return collections.Counter(at_.get(xy(p), "missing") for p in pieces)


def done(ctx, k, g, why, line, rec, skip=None):
    """"#k line" logged, {n, groups, why, **rec} recorded, g skipped `skip` rounds, the state saved."""
    ctx.log(f"#{k} {line}")
    ctx.record({"n": k, "groups": [g], "why": why, **rec})
    if skip:
        ctx.ch.skip[g] = skip
    ctx.save()


def keep_own(ctx, pieces, keys=OWN_KEYS, new_only=False):
    """pieces become ours to keep standing (grow-state's extra_places; restore puts back what goes missing);
    new_only: not those already kept at the same spot."""
    have = {xy(p) for p in ctx.state.get("extra_places", [])} if new_only else set()
    ctx.state.setdefault("extra_places", []).extend(
        [{k_: v for k_, v in p_.items() if k_ in keys} for p_ in pieces if xy(p_) not in have])
    ctx.L.own = ctx.state["extra_places"]


def planned(ctx, g, why, line, places, k=None, rec=None):
    """A dry run's build: "#k g: line — why" logged with what would be built (pick-ups marked), recorded, g marked
    planned (skipped for the rest of the run). Nothing goes to the game. k: the number taken already, if any."""
    k = ctx.begin() if k is None else k
    would = dict(collections.Counter(p["item"] + (" (pick up)" if p.get("remove") else "") for p in places))
    ctx.log(f"#{k} {g}: {line} — {why} | dry: would build {would}")
    ctx.record({"n": k, "groups": [g], "why": why, "dry": True, "pieces": len(places), "would": would, **(rec or {})})
    ctx.ch.skip[g] = DRY_SKIP
    return k
