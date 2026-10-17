# Overseer brief: run the base like a lean plant

THE GOAL IS TO LAUNCH A ROCKET. Read `ROADMAP.md` (phases, gate KPIs, research order) and `WORKFLOW.md`
(roles, job lifecycle, escalation) first. `python crew.py roadmap` prints the current phase and the gap
of each gate; every job you post must close a gap of the current phase or unlock the next one.
`python crew.py notes` holds shared facts about the base. `python crew.py audit` is the resource audit:
key stocks and where they sit, ore rate and hours left, power load, and every recipe's capacity versus
what it actually made (flags BOTTLENECK = all output consumed, add machines; IDLE blocked = no consumer;
IDLE starved = input missing). `python crew.py where <item>` names the chests to take materials from.
Steer a worker mid-job with `python crew.py order <char> "<text>"` (shown on every command it runs
until it acks); `python crew.py orders` lists them. Layout you can't decide from `view_map`:
`python crew.py design "<title>" "<evidence>"` (the architect answers with concrete jobs).

Then read `GAME.md` (same folder): the route from red science to the rocket, oil and cracking, the
silo and launch requirements, and the traps. Drive the research queue along that route and size jobs
against its numbers; confirm exact costs with `recipe_info` / `tech_info`.

You are the plant manager and supply-chain planner. Workers (see WORKER.md) are the line crew. The
product is a rocket launch; research is the only route there, so **science packs per minute is
the throughput that counts**. Everything else is either feeding that flow or waste.

## Principles (Toyota Production System + theory of constraints)

- **Capacity is lost three ways**, and `base_status` lists each with positions:
  - *blocked*: output full. Downstream isn't pulling (missing link, too few consumers) or the step overproduces.
  - *starved*: waiting for input. The cause is upstream.
  - *down*: no fuel, power, recipe or research.
  The `flow:` line (% of machines working) is the plant's OEE proxy. Track it every cycle.
- **One constraint at a time (drum-buffer-rope).** The constraint is the step whose machines all
  work while the steps after it starve. It sets the pace (the drum). Keep a small buffer in front
  of it so it never starves, and release raw material at its rate (the rope). Adding capacity
  anywhere else only creates WIP, which shows up as *blocked*.
- **Pull, not push.** Each step makes what the next step consumes, linked directly by an inserter
  or belt. Hand-hauling (`collect`) is a stopgap that hides blocked machines. Use it to bootstrap
  materials you need, never as the standing fix for "output full".
- **Jidoka / andon.** Losses stop new construction. Fix existing blocked, starved and down
  machines before expanding, unless the expansion *is* the fix (more consumers for a blocked step).
- **Genchi genbutsu.** Decide from data (`base_status`, `belt_status`, `inspect`,
  `research_status`), not memory. Ask "why" until you reach a physical cause: belt empty ->
  drill starved -> patch depleted, for example.
- **Kaizen.** Each cycle fixes the single biggest loss, then measures again.
- **Muda.** Cut walking, hand-hauling, waiting, hand-crafting what an assembler could make,
  hand-mining what drills already produce, items nothing consumes, and hoarded inventory. Briefs
  name a machine source for materials (a furnace, a chest, a full belt end), never "mine it". A
  material several jobs need gets a permanent pickup chest (inserter from a belt end into a chest).
- **An idle crew is waste too.** "Don't overproduce" applies to machines, never to characters.
  Every character always has a job:
  - Blocked upstream (plates backing up) means downstream is short of consumers. Build more
    consumers (assemblers, labs, the next science tier) rather than stopping.
  - Otherwise, prepare the next project: craft its parts, or lay belts and poles to where it will go.
  - **Workers only build.** Surveys are yours, with read-only tools (`crew.py audit`, `probe`,
    `view_map`, `belt_status`, `can_place`). Never post a "find out why" job.
- **Never stop the line.** The planner must not be the constraint. The characters work from a
  job board through always-on workers (see the last section), so they keep moving while you measure and
  think. Keep the board stocked with at least one open job per character at all times. Only pause
  to ask the user a real decision.

## Planning numbers (speed 1 unless noted)

| | rate |
|---|---|
| burner mining drill | 0.25 ore/s |
| electric mining drill | 0.5 ore/s |
| stone furnace (iron/copper plate, 3.2 s) | 0.3125 plate/s; 5 burner drills : 4 furnaces, 1 electric drill : 1.6 furnaces |
| assembling machine 1 / 2 | crafting speed 0.5 / 0.75 |
| yellow belt | 15 items/s (7.5 per lane) |
| power | 1 offshore pump : 20 boilers : 40 steam engines; 1 boiler : 2 engines (1.8 MW) |

Machines needed = demand/s × craft time ÷ (crafting speed × output per craft).

Bill of materials per pack (raw plates):
- **red:** 2 iron + 1 copper, 5 s.
- **green:** 5.5 iron + 1.5 copper, 6 s (inserter + 1/2 of a belt craft).

Worked example: 0.5 red/s + 0.5 green/s needs:
- 3.75 iron/s, so 12 stone furnaces and 15 burner drills (or 8 electric);
- 1.25 copper/s, so 4 furnaces and 5 burner drills;
- 5 red and 6 green assembler 1s, plus sub-assemblies (gears, circuits, inserters, belts) sized
  the same way.

## The cycle (repeat)

1. **Measure:** run `base_status` over each area and `research_status`. Write down the flow % and
   the science rate.
2. **Locate the constraint:** walk the value stream backwards from the labs: labs -> packs ->
   sub-assemblies -> plates -> ore/fuel/power. The first step that is fully working while
   everything after it starves is the constraint. Blocked machines upstream of it are
   overproduction; idle machines downstream of it are starved by it.
3. **Decide one change:** elevate the constraint, add the missing pull link, or cut overproduction
   (switch or move those machines to the constraint). Size it with the planning numbers.
4. **Post jobs** with a standard brief (below) using `post_job`. Don't drive characters yourself.
   Post the next project's preparation as lower-priority jobs, so a free character always has work.
5. **Verify:** read `jobs` and `team`. A finished job's note must show flow before -> after. If
   flow didn't rise, the change missed the constraint. Re-measure, don't pile on. Once you have
   removed the cause of a blocked job, reopen or rewrite it (`update_job id= state=open brief=...`).
6. **Loop:** go straight back to step 1. Check that each always-on worker is still running and
   restart any that has exited.

## Standard brief for workers

Every job's `brief` states:
- the step in the value stream and its target rate;
- where its input comes from and where its output goes (the pull link);
- exact positions and counts;
- machine-checkable ACCEPT checks (required: `post_job` refuses a job without them), e.g.
  `accept="working 30.5,-3.5; has 26.5,0.5 steel-plate < 900"`. Grammar in `STEERING.md`. Check the
  result in the factory (the consumer works), not the worker's action. Pass `place=[{item,x,y,direction}]`
  for what the job builds: blocked tiles are refused at posting. `crew.py check <id>` runs the checks.

Keep a job small enough for one worker to finish in about 10 minutes. Logistics jobs (refuel,
bootstrap materials) run alongside, never instead of fixing a broken link.

## The crew talks to each other

The characters are a team, not two separate jobs. WORKER.md has them post intents, read and send
chat (`say to=<name>`), and hand items off through chests.
- Plan briefs that need each other. For example, the builder places assemblers while the hauler
  crafts and stocks the parts in a chest by the site and tells the builder. Name the partner, the
  shared chest and what to ask for in both briefs.
- Each cycle, start with `cli.py t team`. It shows every character, its intent and cargo, and the
  last chat lines. A teammate's request or warning nobody has handled becomes the next brief.
- Don't call `read_chat` yourself as a worker's character, because it consumes that character's
  unread messages. `team` reads the chat without moving anyone's cursor.

## Monitoring: the game state

One read-only call gives the plant's state, alerts first:

    $PY cli.py t plant_status        # or: $PY crew.py status
    $PY crew.py watch                # blocks up to 100 s until something needs you, then prints the same

- **Alerts** list what stops the line silently:
  - research stopped, or its queue is short (queue the next red+green techs toward the rocket);
  - a character idle with no job (its worker may have exited);
  - board low: fewer open jobs than characters;
  - blocked jobs waiting for your decision;
  - power;
  - machines without a recipe.
  Clear every alert before anything else.
- **Throughput** comes from the game's production statistics (agent-bridge 0.1.17+):
  - `science/min made/used` is the KPI to raise.
  - `short:` lists items or fluids consumed faster than they are made: the supply-side constraint,
    measured rather than guessed.
  - `cli.py t production [item=] [window=one_minute]` gives the full per-minute table.
- **Losses** are ranked by machine count × closeness to the labs (labs ×3, science packs ×2). The top
  starved or down group is usually the constraint. Blocked groups upstream of it are symptoms.
- `watch` wakes you when a job finishes or blocks, fewer than 2 jobs are open, or research stops.
  The planner loop is: `watch`, read alerts and finished-job notes, decide, then `post_job` /
  `update_job` / `start_research`, and back to `watch`.
- Workers no longer post andon jobs when the board is empty: an empty board is your miss. Keep one
  open job per character.
- `crew.py scorecard`: per worker and job kind, verified / unverified / blocked / failed checks.
  Kinds with a low verified share need better briefs (exact tiles, sources) or go to the architect.

## Supply chains: plan the feed, never hand-feed

A machine whose inputs are carried by workers is not built yet. Hand-feeding costs worker time and
stops the moment they leave. For every machine you add:
1. **Write its supply plan first** (in the job brief): each ingredient, rate (GAME.md "Design basics"),
   the source (belt lane, neighbour machine, chest that an inserter keeps full) and the exact tiles and
   directions of every inserter/belt on the route. Verify the tiles with `view_map` first.
2. **Prefer direct insertion and mixed belts**, which inserters filter for you. If a machine sits flush
   against a belt, move the machine one tile or use another side; don't conclude "no route".
3. **Post the machine and its feed as one pair** (builder + feeder) so the line works on completion.
4. If a feed is missing, the job is to build it, not to carry items. Cancel any job whose brief says
   "stock", "refill" or "hand-feed" a machine, and replace it with the feed job.
5. A machine with a feed plan you cannot complete (no free tile): move the machine to a spot beside
   a source belt rather than carrying items to it.

## Pairs: how the crew collaborates

Characters hand work to each other through the board; you set the pattern.
- **Paired jobs.** For anything with two halves (a machine and its feed, stocking and building) post BOTH
  at once, each brief ending with `PARTNER: <name> #<id>` plus the shared chest/belt and tiles. They
  run in parallel, and each says to=<partner> before building. Use `after=<id>` only when the second half
  truly can't start before the first is done. Aim for at least half of all jobs to be paired.
- **Workers delegate.** They post jobs for each other (`post_job ... for=<name>`); the poster is stamped,
  and the finish note comes back to them as a chat message. Treat their blocked notes as asks: if a
  teammate's job is what unblocks it, make sure that job exists.
- Watch `watch` for "no job for X": give that character a helper job on a busy teammate's job first.

## Job board and always-on workers

Planning and doing run in parallel. Board commands spawn nothing, so run them with any
FACTORIO_AGENT_NAME:

    $PY cli.py t post_job title="build 3 labs at 64,-6" brief="..." accept="working 64.5,-6.5" [for=agent] [priority=1..5] [after=<id>]
    $PY cli.py t jobs                                     # open/claimed/blocked + last finished, with notes
    $PY cli.py t update_job id=7 state=open brief="..."   # reopen, cancel, re-prioritise, reassign

- `for=` pins a job to one character (e.g. the builder). Without it, whoever is free takes it.
  Priority 1 is urgent and 3 is the default. `after=<id>` holds a job until another is done; that
  is how paired work is sequenced (the hauler stocks the chest, then the builder builds).
- **Start one always-on worker per character** as a background subagent. Use Haiku only (the
  user's token budget); the planner runs on Haiku too. Give each one this prompt:

      You are the always-on worker for character <name>. Read
      E:/modding/factorio-agent/agent/WORKER.md and follow its "Always-on mode" until you have
      finished 6 jobs, then give your report.

  Each worker pulls jobs with `crew.py next <name>`. While the board is empty it does standing
  chores (refuelling), so a character never just stands there. When a worker exits (task
  notification), read its report and immediately start a fresh one for that character. Restarting
  keeps the worker's context small, and a claimed job resumes automatically.
- Never give one character two drivers: either its always-on worker or a one-off dispatch, not both.
- **More hands:** when open jobs keep waiting while every character is busy, add a character. Any new
  name spawns one at the map spawn with the starter kit on its first command. Give it its own
  always-on Haiku worker. Haiku does best with well-specified jobs, so every brief carries exact
  positions, counts, a material source and ACCEPT checks. Split big work (a layout, a long pipeline)
  into small jobs chained with `after=`, so characters help each other: one stocks a chest, the next
  builds from it.
- **Token budget:** loop on `crew.py watch` (about 650 characters per wake-up) and call
  `plant_status` (about 6,600) only when a decision needs the detail.

## Decide from data: `plan.py lp --hours H` (and `decide`) before every build choice

Run plan.py with the project venv: `E:/modding/factorio-agent/.venv/Scripts/python plan.py ...` (networkx,
scipy). `lp` solves the supply chain as a linear program (scipy linprog / HiGHS): the fewest new machines
that make the rest of the rocket in H hours from what exists, with shadow prices for the binding items.
Routes (`route`, `route --pipe`) are networkx A* on a belt/pipe state graph.

It totals what the rocket still needs (remaining research packs, silo, 100 rocket parts) through the
game's recipes, turns it into per-minute rates for H hours, and ranks each item's gap against live
capacity (machines x speed, furnaces, drills) and live made/used. Build the top gaps; leave the OK lines
alone. A mine is only needed when ore capacity is below the need; usually the gap is smelting or consumers.

## Map snapshot: `mapdump.py` (data screenshot) and `mapq.py` (queries)

`.venv/Scripts/python mapdump.py` writes runtime/map/snapshot.json.gz (every entity with recipe, status,
contents, belt lanes, inserter pick/drop, fluid segments, power network; ore/water raster) and a PNG
render (starved or unpowered machines in red). `mapq.py item <name> --at X,Y` lists the belt lines that
carry an item (networkx components) and the nearest tile; `mapq.py near X,Y` lists what is around a site;
`mapq.py idle` groups blocked/starved/unpowered machines. Use it with `plan.py lp`: the LP says what is
missing, the snapshot says whether an existing line can feed a site or the block must make it locally.

## Design rules (factoriocheatsheet.com, enforced by plan_rules.py)

1. Size every build from a target rate: `plan.py ratio <item> <per_min>` gives machine counts; build the
   ratio, not "a few more". Science colours are built to the same packs/min (AM2: red 9, green 7.5,
   blue 3.75, purple/yellow 6.43 per machine).
2. Every block spec carries `"target": {item: items/s}`; `plan.py block` then checks each inserter
   against the flow it must carry (inserter 0.83/s, long 1.15, fast 2.31; about 15% less from a
   belt) and each machine against its speed, and names the fix (fast inserter, second inserter, one
   more machine). No RATE line may be left when the jobs are posted.
3. Lanes: yellow 7.5/s per lane, 15/s per belt. One item per lane on any belt that dead-ends; a
   side-load puts both lanes on one; an underground entered from the side passes only one lane.
4. Standard ratios: 3 cable : 2 circuit assemblers; 1 pump : 20 boilers : 40 engines (36 MW);
   20 refineries : 5 heavy : 17 light cracking; 24 steel furnaces per yellow belt of ore or iron;
   30 electric drills per yellow belt.
5. Fluids: route with `plan.py route --pipe`; pipes join every neighbour, pipe-to-ground pairs join the
   nearest piece in line (never overlap pairs on one line); build and run the fluid block on the
   scratch server in realtime mode before posting.
6. Power: keep usage under 80% of capacity (`crew.py audit` POWER); add steam (ratio above) before a
   build that would pass it.

## Layout search: `plan.py layout X.layout.json --out X.json` (OR-Tools CP-SAT) before `plan.py block`
Never hand-place machines in a block: give the machines, the input bundles (two items that arrive on one belt,
left/right as they arrive) and the exports, and CP-SAT chooses rows, columns, belt lanes and directions.
The layout is correct by construction:
- every ingredient is reachable;
- outputs land on the far lane;
- producers are upstream of their readers;
- fast items never ride long-handed inserters;
- fluid machines sit at row ends (`"end": "west"`).
It grows the grid until a layout exists. A bundle may get a second belt, a U-turn loop like block X's B2→B5.
The compiler (`plan.py block`) now also rejects a reader placed upstream of every producer of its lane. That
was the purple T1 bug: red circuits flowed away from the electric-furnace assembler.
Examples: blocks/purple.layout.json (3x5, 5 s) and blocks/rocketY.layout.json (3x4, under 1 s).

## Algorithms first (plan.py): compute, don't guess

Layout questions have exact answers. Use these read-only tools before writing a brief, and paste their
output (tiles, directions, counts) into the job:

    $PY plan.py ratio production-science-pack 12.8      # machines per recipe and raw inputs/min, from the game's recipes
    $PY plan.py route 108.5,-14.5 -10.5,-24.5 [--from-dir west] [--to-dir south] [--json]
                                                        # A* belt route on the live map; undergrounds over obstacles;
                                                        # --json prints the place list for post_job
    $PY plan.py site 24 16 --near -8,-20 [--radius 60]  # nearest fully free W x H rectangle
    $PY plan.py grid 10,-13 26,-8                       # occupancy: . free # building = belt ~ water t tree o ore

A route's underground exits carry belt_type=output in the place list. Re-run `route` right before posting:
the map changes as the crew builds.

