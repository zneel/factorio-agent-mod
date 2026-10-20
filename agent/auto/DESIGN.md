# autoplay: seed -> rocket, by program

`python -m auto run --seed N` plays a fresh peaceful map to a rocket launch with no agent in the loop.
Every decision comes from data (the map, the game's recipes and techs) and an algorithm; nothing is a
hand-picked coordinate, so it works on any seed.

## Pipeline

| stage | module | algorithm / library | output (runtime/auto/<seed>/) |
|---|---|---|---|
| 1 map | `mapdata.py` | scratch server generates the seed; Lua dumps ore tiles, water, wells, trees, rocks, cliffs | `map.json` |
| 2 demand | `demand.py` | scipy linprog (HiGHS): research to the silo + silo + 100 parts + satellite, for H game hours -> crafts/min per recipe, machine counts, ore/fluid rates | `demand.json` |
| 3 modules | `modules.py` | group recipes into modules; interiors from CP-SAT (assembler blocks) or rule generators (mines, smelters, power, oil, silo); each module = footprint + typed ports (item, lane, rate) | `modules.json` |
| 4 placement | `place.py` | CP-SAT: module positions on free ground; mines on their patch, power and pumps at water, pumpjacks on wells; no overlap, keep-out corridors; minimise sum flow x distance | `layout.json` |
| 5 routing | `route.py` | networkx A*: belts (lanes, undergrounds with span rules, belt-end fronts kept free) and pipes (pipe-to-ground pairing, foreign ports), longest first; poles by set cover + hop chains | `factory.json` |
| 6 verify | `verify.py` | static checks (overlap, lanes, curves at side-loads, port reservations, pole grid), then cheat-build the whole factory on scratch and run it fast: every machine works, measured rates >= demand | `verify.txt` |
| 7 build | `build.py` | stages in dependency order (bootstrap burners, power, mines, smelting, science, oil, rocket); characters run build macros; research queued by the program; each stage gated by its checks | live game |

## Rules learned the hard way (enforced in code, not by care)
- Inserter direction in plans = drop side; the engine direction is the pickup side.
- A belt end feeds whatever is built in front of it: route around planned and live belt-end fronts.
- A belt tile with one side input and no rear input becomes a curve (both lanes): side-load stubs get a rear belt.
- Underground pairs on the same axis may not overlap; pipe-to-ground pieces at most 10 apart.
- Electric drills mine their whole 5x5 area: no other resource under it.
- Every producer has a consumer and every consumer a feed: the flow graph is checked before layout.
- Fixed-geometry generators are collision-checked against everything already planned.

## Growth controller (`grow.py`, GROWTH_PROMPT.md)

The plan above is the *space plan*; `python -m auto.grow <seed> [--scratch]` decides what to build when, one
complete increment at a time, from measurements of the running game:

- **Groups** (plan2 `grp` tags) are the units. The steam plant is split into `power` (pump, pipes, belts) and
  `power:k` (a boiler, its engines, its fuel inserter); a mall chest at a bus column's end is `mall:<item>`.
- **Flow graph** (`flow.py`): which placement feeds which (belt fronts, underground pairs, splitters, inserter
  pickup/drop, drill drops) and fluid connections (pipes, pipe-to-ground pairs, machine fluid boxes from `ports`).
  An increment = the group + the transport paths from its inputs back to *built* pieces (`upstream`) + its outputs
  walked onto their trunk (`downstream`) + poles planned on the live map and chained to the engines' network.
- **Choose** (in order): the first power unit; `link:<group>` increments that connect a built group to suppliers
  built since (coal to the boilers, a new pack to the labs); more power units when use > 75% of capacity; during
  the bootstrap the mall (EARLY_MALL items: maker + chest); sinks (labs, silo) once fed; then LP deficits, science
  packs first, walking upstream to the first buildable group (techs researched, inputs made by a built group).
- **Mall** (`demand.py` BUILD goal, `modules.py`, `interiors.py`, `bus.py`): every entity the characters place is
  an LP goal; a block export nobody else reads ends in inserter + chest, a mall item that rides the bus ends in a
  chest below its last tap. Blocks are tier-pure (same science needed) so the early mall waits for no research.
  Hand-crafting stops once the early mall stands (`macros.HANDCRAFT`); small poles (wood) are the exception until
  medium poles are researched.
- **Verify**: after a build, a game minute of running, then every machine of the increment must be placed, powered
  and fuelled (boilers excepted: the tender feeds them until their coal belt comes); unpowered -> another pole pass.
- Log: `runtime/auto/<seed>/grow.log` and `grow.jsonl` (groups, why, pieces, statuses, rates before/after).

### Build order: CP-SAT schedule (`schedule.py`)
The order of builds and research is no longer a chain of rules: `schedule.solve` builds one OR-Tools CP-SAT model and
the controller follows it, re-solving in a thread whenever a group is finished or a tech researched.
- Tasks: one interval per unbuilt group (size = pieces x the crew's measured seconds per piece), one per tech
  (alternative optional intervals: the labs, or the bootstrap lab for red-only techs).
- `AddNoOverlap`: one increment at a time; one tech at a time per lab set.
- Precedences: a group after its entity techs and after the earliest *ready* maker of each input (`AddMinEquality`);
  ready = `AddMaxEquality`(built, first recipe researched, stand-ins included); techs after prerequisites, packs'
  makers and the labs; trigger techs after their action (crude oil mined).
- Mall: a group after the mall makers of its entities, unless `hand[g]` (enforcement literals); hand-made only
  before the early mall is ready; each hand-made piece costs `HAND_COST` seconds in the objective.
- Power: `AddReservoirConstraintWithActive` — optional boiler units (chain order) supply, built groups draw.
- Objective: when the silo is ready. Solved in phases (crew -> +mall -> +power), each solution `AddHint`-ing the next.
- Infeasible: every constraint family has an assumption literal; the model is re-solved minimising the dropped
  families, which names the cycle (e.g. "tech advanced-oil-processing after a maker of chemical-science-pack" when
  refineries had no basic-oil stand-in), and the relaxed schedule still orders the work.
What stays hand-written in `grow.choose` are the repairs driven by measurements (pieces gone, half-built groups, dry
mines, gaps, links, unpowered machines, jams); the sequencing itself is the solver's.
