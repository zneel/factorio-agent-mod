"""Building (build) and the material gate every build path asks first (Gate); underground pairing checks."""
import collections
import math
import threading

from auto.growth.core import FUEL, log
from auto.growth.increments import DEAD, increment
from auto.growth.mall import STARVED, drain_due, machine_state, maker_pieces, mall_caps, mall_groups
from auto.growth.sensors import DV4, _k


def build(builders, places, clear, split, supply, Bridge, plan=None, bad_pairs=None, gate=None):
    """Builders take spatial chunks of the increment; each clears trees/rocks and runs supply.build_plan. Then the
    gate's stock is stale, and the undergrounds placed are checked against their planned partners in plan (the
    mismatches go to bad_pairs)."""
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
    if gate is not None:
        gate.stale()          # the stock moved: the gate reads it again
    # the game pairs an underground with the nearest counterpart in reach, not the planned one: check every
    # placed underground live against its planned partner (the "pairs" repair fixes what it finds)
    ug = [p for p in places if p["item"].endswith("underground-belt") and not p.get("remove")]
    if ug and plan is not None:
        try:
            bad = pair_check(Bridge(builders[0], autostart=False).raw, plan, ug, places)
            log(f"underground pairing: {len(ug)} placed, {len(bad)} not with their planned partner"
                + (f": {bad[:4]}" if bad else ""))
            if bad_pairs is not None:
                bad_pairs.extend(bad)
        except Exception as e:
            log("underground pairing check:", str(e)[:120])
    return [{k: v for k, v in (r or {}).items() if k in ("done", "short", "error")} |
            {"failed": ((r or {}).get("result") or {}).get("failed")} for r in results]


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
        self.want = None            # the controller's Context.want (the mall caps' extra for builds outside the plan)
        self._have = None           # the round's reachable stock + builders' inventories (stale after every build)

    def stale(self):
        self._have = None

    def have(self, fresh=False):
        if fresh or self._have is None:
            self._have = stock_have(self.q, self.builders, self.Bridge)
        return self._have

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
            d = drain_due(plan, L, self.q, mall_caps(plan, L, L.researched, self.pole_item, self.want), it,
                          self.ch.skip)
            if d:
                return f"drain:{d[0]},{d[1]}", f"its maker is full, its chest holds {d[2]} over the cap and no room for {it}"
        return None, f"{it}: makers {dict(cnt)}"


# ------------------------------------------------------------------------------------- underground pairing
UG_REACH = {"underground-belt": 5, "fast-underground-belt": 7, "express-underground-belt": 9}



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
