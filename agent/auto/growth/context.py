"""The controller's state in one place: what main() used to keep in local variables and nested closures, and the
module globals that only the controller writes (the mall caps' extra, the last mall limit commands, underground
mismatches). Module state that stays (read deep inside helpers auto.schedule shares): growth.core.GD and
MALL_MAKES, growth.increments.DEAD, macros.HANDCRAFT / ALWAYS_HAND, poles.ITEM, supply.DEPOT."""
import collections
import contextlib
import json
import time
from dataclasses import dataclass, field

from auto.growth import core
from auto.growth.gate import build
from auto.growth.mall import mall_limits


@dataclass
class Context:
    args: object                 # argparse: seed, scratch, chars, speed, rounds, dry, boot_minutes
    out: object                  # runtime/auto/<seed>
    gd: dict                     # gamedata
    dem: dict                    # demand.json
    m: object                    # the map (mapdata.Map, scouted ground included)
    plan: object                 # growth.core.Plan
    L: object                    # growth.core.Live
    state: dict                  # grow-state.json
    ch: object                   # growth.chooser.Chooser
    q: object                    # raw RCON on the controller's bridge
    Bridge: object               # bridge.Bridge
    supply: object               # the supply module (build_plan, deposit, unload, DEPOT)
    macros: object               # the macros module (HANDCRAFT, ALWAYS_HAND)
    clear: object                # autobuild.clear
    split: object                # autobuild.split
    chars: list                  # every character: b1..bN (the last one tends the burners in the bootstrap)
    boot_dir: object
    boot_files: list
    boot: list                   # the bootstrap's placements
    mall_depot: tuple            # the builders' depot once the mall runs
    gate: object = None          # growth.gate.Gate
    builders: list = None
    hut: dict = None             # the bootstrap lab
    tender: object = None        # the burner tender's process
    env: dict = None
    py: str = None
    targets: dict = None         # control.lp_targets(demand)
    track: list = None           # items measured each round
    labs_n: int = 1
    sched: dict = field(default_factory=lambda: {"s": None, "key": None, "busy": False})
    rstate: dict = field(default_factory=lambda: {"want": []})    # techs the research runner queues
    stop: object = None          # threading.Event: set when the loop ends
    want: collections.Counter = field(default_factory=collections.Counter)   # short for builds outside the plan
    limits: dict = field(default_factory=lambda: {"cmd": None, "makers": None})   # last mall_limits commands
    bad_pairs: list = field(default_factory=list)   # underground mismatches build() saw (the pairs repair)
    # this round
    rnd: int = 0
    pole_item: str = "small-electric-pole"
    grid_dead: bool = False      # no steam: everything may be hand-crafted
    snap: dict = None            # control.measure at the round's start

    @staticmethod
    def log(*a):
        core.log(*a)

    @staticmethod
    def record(d):
        core.record(d)

    def save(self):
        """grow-state.json written (not in a dry run: the next live run starts from what the game holds)."""
        if self.args.dry:
            return
        (self.out / "grow-state.json").write_text(json.dumps(self.state))

    def begin(self):
        """The next increment's number."""
        self.state["n"] += 1
        return self.state["n"]

    def build(self, places, one=False):
        """places built by every builder (one=True: the first only, e.g. the one that carries what it picks up)."""
        return build(self.builders[:1] if one else self.builders, places, self.clear, self.split, self.supply,
                     self.Bridge, plan=self.plan, bad_pairs=self.bad_pairs, gate=self.gate)

    def settle(self):
        """A game minute of running before judging it."""
        time.sleep(max(3, 60 / self.args.speed))

    def mall_limits(self, pole_item=None):
        return mall_limits(self.plan, self.L, self.q, self.L.researched, pole_item or self.pole_item, self.limits,
                           self.want)

    @contextlib.contextmanager
    def poles(self, item):
        """poles.ITEM (the pole kind auto.poles places) swapped for the block, then back."""
        from auto import poles
        was = poles.ITEM
        poles.use(item)
        try:
            yield
        finally:
            poles.use(was)

    def held(self, g, v, n=4):
        """A build path the gate did not pass: its alt goes first next round, g itself waits n rounds (a repair
        keeps its skip counter, so it cannot loop)."""
        if v[0] == "alt":
            self.ch.pending.append((v[1], v[2]))
            self.log(f"round {self.rnd}: {g} waits for {v[1]}: {v[2]}")
        else:
            self.log(f"round {self.rnd}: {g} short {dict(v[2])}: not in any chest the builders reach yet")
            have_ = dict(self.gate.have()) if g not in self.plan.groups else {}
            if g not in self.plan.groups and any(self.want[k_] < n_ + have_.get(k_, 0) for k_, n_ in v[2].items()):
                # not a plan group: the mall caps know nothing of it, so they make room for all it needs (the stock
                # already there counts against the cap, so the short alone would leave the maker capped)
                for k_, n_ in v[2].items():
                    self.want[k_] = max(self.want[k_], n_ + have_.get(k_, 0))
                try:
                    self.mall_limits()
                except Exception as e:
                    self.log("mall limits:", str(e)[:120])
        if n:
            self.ch.skip[g] = n
