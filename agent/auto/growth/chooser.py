"""Choosing what to build next: the Chooser (what can be built, the upstream walk to an item's first buildable
maker) and choose(), the repair rules in order, then the CP-SAT schedule's next group."""
import collections
from dataclasses import dataclass, field

from auto.growth import core
from auto.growth.core import ELECTRIC, log, recipe_now, techs_of
from auto.growth.increments import DEAD, gaps, link_paths, rear_belts, supplied
from auto.growth.mall import drain_due, mall_caps, spent_blocks
from auto.growth.scouting import scout_step
from auto.growth.sensors import dry_drills, dry_mine, jammed, misfaced, starving_fuel, stuck_inserters, unpowered
from auto.growth.gate import bad_pairs
from auto.growth.world import BURNER_CAP


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
                out |= techs_of([r], core.GD) - R
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



# --------------------------------------------------------------------------------------------- choose()
@dataclass
class Look:
    """What the rules of one choose() read. gate=None (a dry run): no material check and none of the repairs that
    only pick up and lay again. held: groups the gate skipped this round (without a skip counter) -> short."""
    plan: object
    L: object
    ch: Chooser
    state: dict
    q: object
    sched: dict = None
    gate: object = None
    want: object = None          # Context.want (the mall caps' extra for builds outside the plan)
    bad_pairs: list = field(default_factory=list)    # underground mismatches build() saw
    held: dict = field(default_factory=dict)
    dry: bool = False            # a dry run: the rules write nothing to the game

    def tell(self):
        s_ = "; ".join(f"{g_} {dict(v_)}" for g_, v_ in self.held.items())[:500]
        if self.held and s_ != self.ch.held_line:
            self.ch.held_line = s_
            log(f"short, skipped for now (the next groups get their turn): {s_}")

    def caps(self):
        return mall_caps(self.plan, self.L, self.L.researched, self.gate.pole_item, self.want)


def choose(c):
    """Repairs from measurements first (pieces gone, what a gate named last round, half-built groups, dry mines,
    underground pairing, gaps, links, power, jams, a locked-out mall chest, stuck inserters), then the next group of
    the CP-SAT schedule (auto.schedule) that can be built now and that the gate passes; returns (group, why).
    Each rule returns (group, why) or None; the first that names something wins (the last always does)."""
    for rule in RULES:
        r = rule(c)
        if r:
            return r


def restore(c):
    """Pieces that stood and are gone (a builder walled in by its own building picks one up to walk out), and
    pieces of our own (remine columns and routes, not in the plan) that stood and are gone: put back."""
    plan, L, state = c.plan, c.L, c.state
    gone = {i for i in set(state.get("placed", ())) - L.built - L.dead if plan.p[i].get("grp") not in L.retired}
    here = {(round(x, 2), round(y, 2)) for (x, y) in L.extra}
    gone_x = [p for p in state.get("extra_places", []) if (round(p["x"], 2), round(p["y"], 2)) not in here
              and "electric-pole" not in p["item"]]
    if (gone or gone_x) and c.ch.skip.get("restore", 0) <= 0:
        state["gone"] = sorted(gone)
        L.state_gone = state["gone"]
        L.gone_extra = gone_x
        return "restore", f"{len(gone) + len(gone_x)} pieces that stood are gone"


def pending(c):
    """What a build path's gate named last round: the group (or repair) behind the deficit that held it."""
    ch = c.ch
    while ch.pending:
        h, w = ch.pending.pop(0)
        if ch.skip.get(h, 0) <= 0 and (h.startswith("drain:") or (h in c.plan.groups and not c.L.group_done(h))):
            return h, w


def repair(c):
    """A producing group with some pieces standing and some not (a failed or interrupted build)."""
    plan, L, ch, state, gate = c.plan, c.L, c.ch, c.state, c.gate
    for g in sorted(plan.prod):
        n = sum(1 for i in plan.groups[g] if i in L.built)
        nm = sum(1 for i in plan.groups[g] if i in L.built and not plan.flow.transport(i))
        if g in state.get("tried", ()) and nm and not L.group_done(g) and ch.skip.get(g, 0) <= 0 \
                and ch.techs_ok(g) and g not in state.get("dry", ()) and g not in L.retired:
            v = gate(g) if gate else ("build", g, None)
            if v[0] == "skip":
                c.held[g] = v[2]
                continue
            if v[0] == "alt":
                return v[1], v[2]
            return g, f"repair: {n} of {len(plan.groups[g])} pieces stand"


def burner(c):
    """Bootstrap: the builders ran out of plates or stone -> more burner pairs (up to a cap per item)."""
    state = c.state
    if state.get("handcraft"):
        short = state.get("short") or {}
        for it in ("iron-plate", "copper-plate", "stone"):
            if short.get(it, 0) > 0 and state.get("burners", {}).get(it, 0) < BURNER_CAP[it]:
                return "burner:" + it, f"builders short of {short[it]} {it}"


def rears(c):
    """Built side-loads without their rear belt mix lanes: lay the missing rear belts."""
    plan, L = c.plan, c.L
    bb = {i for i in L.built if plan.p[i]["item"] == "transport-belt"}
    if c.ch.skip.get("rears", 0) <= 0 and rear_belts(plan, bb, L.built) - L.built:
        return "rears", "side-loaded belts without the rear belt that keeps them straight"


def relocate(c):
    """Drills that found no ore left: moved beside the same belt onto ore that is left (auto.relocate)."""
    rel = dry_drills(c.plan, c.L, c.q, c.state, c.ch.skip)
    if rel:
        c.L.relocate = rel
        return "relocate:" + rel[0], f"{len(rel[1])} drills of {rel[0]} found no ore left"


def remine(c):
    """A mine run dry: new columns on ore that is left (auto.remine), the old belt keeps feeding its smelter."""
    d = dry_mine(c.plan, c.L, c.q, c.state, c.ch.skip)
    if d:
        return "remine:" + d, f"{d}: its drills found no ore left"


def pairs(c):
    """Plan undergrounds the game paired with a piece of our own (an own route ran along them): ours are picked
    up, the plan pair pairs again, reconnect lays the cut route anew. A scan every 5 rounds, at once after a build
    saw a mismatch; 10 rounds' rest after a repair."""
    ch = c.ch
    if c.gate and ch.skip.get("pairs_fix", 0) <= 0 and (c.bad_pairs or ch.skip.get("pairs", 0) <= 0):
        c.bad_pairs.clear()
        ch.skip["pairs"] = 5
        try:
            rm_ = bad_pairs(c.plan, c.L, c.q, c.state)
        except Exception as e:
            log("pairs:", str(e)[:120])
            rm_ = []
        if rm_:
            c.L.pairs = rm_
            return "pairs", f"{len(rm_)} own undergrounds pair with plan ones"


def reconnect(c):
    """Remined columns whose new ore dead-ends before their old belt (a route piece failed or was picked up)."""
    L, ch = c.L, c.ch
    remined = [h for h in L.retired if h.startswith("mine:") and h not in c.state.get("bridged", [])]
    if remined and ch.skip.get("reconnect", 0) <= 0:
        from auto import remine as remine_
        pl_, info = remine_.reconnect(c.q, ch.m, c.plan, L, remined, stock=dict(ch.have()) if ch.have else None)
        ch.skip["reconnect"] = 10
        if pl_:
            L.reconnect = pl_
            return "reconnect", info


def fuel(c):
    """Smelters out of fuel while coal backs up elsewhere: one more fuel bridge into a dry coal column's network."""
    plan, ch = c.plan, c.ch
    if ch.skip.get("fuel", 0) <= 0:
        dry_coal = [h for h in plan.prod if h.startswith("mine:coal") and h in DEAD
                    and c.state.get("bridged", []).count(h) < 3]
        if dry_coal and starving_fuel(plan, c.L, c.q):
            ch.skip["fuel"] = 15
            ch.skip.pop("remine:" + dry_coal[0], None)
            c.L.retired.discard(dry_coal[0])
            return "remine:" + dry_coal[0], "smelters out of fuel: another fuel bridge"


def gap(c):
    """A working producer's output runs into a missing piece between two built stretches: lay it."""
    if c.ch.skip.get("gap", 0) <= 0 and gaps(c.plan, c.L.built):
        return "gap", "a missing piece between a working supplier and built transport"


def link(c):
    """Built groups whose suppliers now stand but are not connected yet (coal to the boilers, a new pack to the
    labs); fuel to the boilers first."""
    plan, L = c.plan, c.L
    ok_ = supplied(plan, L.built)
    for g in sorted(plan.prod, key=lambda g: (not g.startswith("power"), g)):
        if c.ch.skip.get("link:" + g, 0) > 0 or not L.group_done(g):
            continue
        own = set(plan.groups[g])
        if plan.flow.upstream(own, ok_ - own)[0] - L.built:
            return "link:" + g, f"connect {g} to its built suppliers"
        if link_paths(plan, L.built, g):
            return "link:" + g, f"connect {g} to a built supplier (a branch into a working belt)"


def finish(c):
    """A transport group (pipes, belts, bands) partly built while every machine it touches stands: the plan meant
    all of it (measured: 27 missing pipes left 2 of 4 pumpjacks waiting for space, the refineries short of crude);
    the link walks miss it once one supplier gets through."""
    plan, L = c.plan, c.L
    f_ = plan.flow
    for g in sorted(plan.groups):
        if g in plan.prod or g in L.retired or c.ch.skip.get("finish:" + g, 0) > 0:
            continue
        idx = plan.groups[g]
        miss = [i for i in idx if i not in L.built and i not in L.dead]
        if not miss or len(miss) == len(idx):
            continue
        near = {plan.p[j].get("grp") for i in idx for j in list(f_.feeds[i]) + list(f_.fed_by[i]) + list(f_.fluid[i])
                if not f_.transport(j)}
        if all(L.group_done(h) for h in near if h in plan.groups):
            return "finish:" + g, f"{g}: {len(miss)} of {len(idx)} pieces missing, every machine it joins stands"


def trim(c):
    """Belt tails past the last pickup (auto.trim): items that drift there are never taken; picked up (their load
    comes back to the builders) and written off, so restore and finish leave them."""
    plan, L, ch = c.plan, c.L, c.ch
    if ch.skip.get("trim", 0) <= 0:
        ch.skip["trim"] = 30
        from auto import trim as trim_
        tb = trim_.tails(plan.p, plan.flow, among=L.built,
                         ahead=set(range(len(plan.p))) - L.dead) - L.dead   # planned belts ahead still count
        if tb:
            L.trim = sorted(tb)
            return "trim", f"{len(tb)} belts run past the last inserter that takes from them"


def repower(c):
    """Built machines without power (their pole chain failed, or a pole was picked up): a pole pass of their own."""
    u = unpowered(c.plan, c.L, c.q, c.state)
    if u and c.ch.skip.get("repower:" + u, 0) <= 0:
        return "repower:" + u, f"{u} has machines without power"


def flush(c):
    """A starved smelter whose belts hold no ore at all is jammed (stale fuel on the ore lane): flush those belts."""
    j = jammed(c.plan, c.L, c.q, c.state)
    if j:
        return "flush:" + j, f"{j} starves with belts full of what it cannot smelt"


def drain(c):
    """A mall chest fed two items, full of one over its cap, the other locked out while its maker stands full:
    emptied into the depot (every 5 rounds at most; a drained chest rests 30)."""
    ch = c.ch
    if c.gate and ch.skip.get("drain", 0) <= 0:
        ch.skip["drain"] = 5
        try:
            d = drain_due(c.plan, c.L, c.q, c.caps(), skip=ch.skip)
        except Exception as e:
            log("drain:", str(e)[:120])
            d = None
        if d:
            return f"drain:{d[0]},{d[1]}", f"holds {d[2]} over its cap, no room for {d[3]}, whose maker is full"


def spent(c):
    """A block whose products nothing needs any more (the silo stands): its belts, inserters and machines picked
    up, what they hold back to the depot as building stock, the pieces written off (every 20 rounds at most)."""
    plan, L, ch = c.plan, c.L, c.ch
    if c.gate and ch.skip.get("spent", 0) <= 0:
        ch.skip["spent"] = 20
        try:
            sp = [s_ for s_ in spent_blocks(plan, L, c.caps(), L.researched, c.gate.pole_item)
                  if ch.skip.get("spent:" + s_[0], 0) <= 0]
        except Exception as e:
            log("spent:", str(e)[:120])
            sp = []
        if sp:
            h, idx = sp[0]
            L.spent = idx
            return f"spent:{h}", (f"nothing needs what {h} makes ({sorted(m for m in plan.made[h] if ':' not in m)}): "
                                  f"its {len(idx)} belts, inserters and machines hold stock nobody takes")


def unstick(c):
    """Inserters that hold what their machine's recipe does not take never drop it: laid again (twice at most each)."""
    ch = c.ch
    if c.gate and ch.skip.get("unstick", 0) <= 0:
        ch.skip["unstick"] = 5
        try:
            st_ = stuck_inserters(c.plan, c.L, c.q, c.state)
        except Exception as e:
            log("unstick:", str(e)[:120])
            st_ = []
        if st_:
            c.L.unstick = st_
            return "unstick", f"{len(st_)} inserters hold what their machine cannot take"


def face(c):
    """Machines facing another way than planned (their fluid ports miss the pipes): turned (every 10 rounds)."""
    ch = c.ch
    if ch.skip.get("face", 0) <= 0:
        ch.skip["face"] = 10
        try:
            mf = [t for t in misfaced(c.plan, c.L, c.q) if not t.startswith("!")]
        except Exception:
            mf = []
        if mf:
            c.L.misfaced = mf
            return "face", f"{len(mf)} machines face another way than planned (their fluid ports miss the pipes)"


def upgrade(c):
    """Splitters standing as belts once logistics is researched (its skip counter: a gate that holds it would
    otherwise pick it again every round, ahead of the schedule)."""
    L = c.L
    if L.standin and "logistics" in L.researched and c.ch.skip.get("upgrade:splitters", 0) <= 0:
        return "upgrade:splitters", f"{len(L.standin)} splitters stand as belts; logistics is researched"


def outpower(c):
    """An outpost whose drills stand without power: its pole chain along the route (route_poles)."""
    from auto import live as lv
    for k_, o in enumerate(c.state.get("outposts", [])):
        if c.ch.skip.get(f"outpower:{k_}", 0) > 0:
            continue
        try:
            st_ = lv.status(c.q, tuple(o["box"]))
        except Exception:
            continue
        dr_ = [s_ for n_, *_x, s_ in st_ if n_ == "electric-mining-drill"]
        if dr_ and sum(1 for s_ in dr_ if s_ == "no_power") * 2 > len(dr_):
            return f"outpower:{k_}", f"outpost {k_} ({o['name']}): {len(dr_)} drills without power"


def scout(c):
    """Scouting (auto.scout): the rest of the run needs more of a resource than the patches mined hold -> an
    outpost on a charted patch that covers it, or else a radar that charts more of the map (one pass every 20
    rounds, every 8 while short)."""
    ch, state = c.ch, c.state
    if c.sched and ch.skip.get("scout", 0) <= 0:
        ch.skip["scout"] = 8 if (state.get("budget") or {}).get("short", True) else 20
        try:
            sc = scout_step(c.plan, c.L, c.q, state, c.sched, ch, dry=c.dry)
        except Exception as e:
            sc = None
            log("scout:", str(e)[:200])
        if sc:
            return sc


def schedule(c):
    """The schedule (CP-SAT over build and research tasks) says what comes next; a group the gate finds short is
    passed over without a skip counter, so every buildable group behind it gets its turn. Always answers."""
    sched, L, ch, gate = c.sched, c.L, c.ch, c.gate
    if not sched or not sched.get("order"):
        c.tell()
        return None, "no schedule yet"
    waiting = []
    for k, g in enumerate(sched["order"]):
        if L.group_done(g) or ch.skip.get(g, 0) > 0:
            continue
        ok, w = ch.buildable(g)
        if ok:
            v = gate(g) if gate else ("build", g, None)
            if v[0] == "skip":
                c.held[g] = v[2]
                continue
            c.tell()
            if v[0] == "alt":
                return v[1], v[2]
            return g, f"schedule #{k} of {len(sched['order'])} ({sched['status']}, rocket at " \
                      f"{(sched.get('makespan') or 0) / 3600:.1f} game h)"
        waiting.append(f"{g}: {w}")     # the whole order is looked at: a buildable group far down gets its turn
    c.tell()
    return None, "schedule: " + "; ".join(waiting[:6] + [f"{g_}: short" for g_ in c.held])[:300]


RULES = [restore, pending, repair, burner, rears, relocate, remine, pairs, reconnect, fuel, gap, link, finish, trim,
         repower, flush, drain, spent, unstick, face, upgrade, outpower, scout, schedule]
