# Game knowledge: Factorio 2.0 base game, from red science to the rocket

Read once. Source: an external progression guide plus the bridge's own data. Costs below are
approximate; confirm exact numbers with `recipe_info name=` and `tech_info name=` before sizing
anything. The map is peaceful (no biters), so skip military science.

## The route

Each science pack needs the previous tier's products, so the tech tree sets the order. Purple and
yellow can be built in parallel. The rocket silo needs all five packs: red, green, blue, purple, yellow.

1. **Red** (copper plate + gear): automation, labs, steam power. Limits: coal for boilers, furnaces.
2. **Green** (inserter + belt): steel processing, engine, logistics 2, fluid handling, oil gathering
   (needs red+green), railway (needed later for purple and the silo). Limit: electronic circuits.
   Everything later also eats circuits, so overbuild them.
3. **Oil**: oil gathering unlocks the pumpjack; oil processing unlocks the refinery and chemical plant
   and fires when crude is mined. Then plastics and sulfur processing.
4. **Blue** (3 advanced circuits + 2 engine units + 1 sulfur -> 2 packs, 24 s): research advanced circuit,
   sulfur processing, chemical science pack. Limit is usually plastic, then engine units (slow, 10 s;
   they need steel). Plan the next power step before brownouts.
5. **Purple and yellow** (both need only red+green+blue):
   - purple: advanced material processing 2, productivity module, railway; needs electric furnaces
     (steel, stone bricks, advanced circuits), productivity modules and rails; bottleneck iron and steel;
   - yellow: low density structure, processing unit, robotics; needs lubricant, so advanced oil
     processing; bottlenecks processing units and copper.
6. **Silo**: rocket silo research costs about 1000 of each of the five packs. Prerequisites include
   concrete, accumulators, productivity module 3, speed module 3, radar, rocket fuel, solar energy,
   utility science pack. The silo itself costs roughly 1000 concrete, 1000 steel, 200 electric engine
   units, 200 processing units. Also build the cargo landing pad before the launch.
7. **Rocket**: each rocket part is about 10 processing units + 10 low density structures + 10 rocket fuel,
   and a launch needs 100 parts, plus the satellite payload. That is on the order of 1300 processing
   units, 1100 low density structures and 1000 rocket fuel in total: copper, plastic and light oil
   throughput decide how long it takes.

Optional: trains (when ore is far or the bus needs several belts of one plate), modules and beacons
(a boost for slow lines, not needed for a first rocket), military science (skip).

## Oil in one screen

- Basic oil processing: 100 crude -> 45 petroleum gas (5 s). Advanced: 100 crude + 50 water -> 25 heavy,
  45 light, 55 petroleum gas; needs blue science.
- Plastic: 20 gas + 1 coal -> 2 plastic. Sulfur: 30 gas + 30 water -> 2 sulfur.
  Blue pack: about 3 plastic and 0.5 sulfur each. Rocket fuel: 10 solid fuel + 10 light oil (15 s).
- Cracking: with advanced oil, heavy and light oil must be cracked into gas or they back up and stop every
  refinery. Rough ratio 8 refineries : 2 heavy crackers : 7 light crackers. Never switch to advanced
  processing without cracking and a place for the surplus (solid fuel, lubricant).
- Long fluid lines can throttle; a pump inline splits the segment.

## Design basics (read before planning any feed)

- **An inserter only moves what its target machine can use.** An assembler's input inserter takes only
  the ingredients of its recipe from a belt, so one belt carrying iron on one lane and copper on the
  other (or mixed) can feed gear, circuit and science assemblers directly. "No iron-only belt nearby"
  is NOT a blocker; "no free tile" is.
- **Direct insertion beats belts.** Machine A -> inserter -> machine B needs no belt (gear assembler
  right next to the science assembler). Chain: source belt -> inserter -> maker -> inserter -> user.
- **A 3x3 machine next to a belt needs a one-tile gap for the inserter.** Inserter tile between belt and
  machine; its arrow = where items go. A long-handed inserter reaches 2 tiles (it skips one tile).
  Machines flush against a belt (no gap) can't take from it: move the machine one tile or use the other
  sides.
- **Chests are buffers, not sources:** a chest must itself be filled by an inserter from a belt or a
  machine. Never size a chest by what workers can carry.
- **Rates:** a basic inserter moves about 0.83 items/s, a fast inserter about 2.3/s; a yellow belt lane
  7.5/s. A red-science assembler (assembler 2, 5 s recipe, speed 0.75) makes 0.15 packs/s and needs
  0.15 gears/s + 0.15 copper/s. A gear assembler 1 (speed 0.5, 0.5 s recipe... check recipe_info) easily
  covers several of them, so one gear maker can sit beside two science assemblers.
- **Plan each product line as inputs -> source -> route.** For every consumer list: ingredient, rate,
  where it comes from (belt/machine), the exact inserter tile and direction. Keep the plan next to the
  job, not in a worker's head.
- Keep lanes in mind: sideloading a belt onto another puts everything on one lane; never connect a belt
  into a feed you haven't checked with `belt_status`.

## Traps that stall runs

- Thinking in colours instead of throughput: measure packs per minute (`production`), not machines.
- Too few electronic circuits; too little copper once yellow science and low density structures start.
- Advanced oil without cracking; plastic starvation for blue science.
- Researching late: railway and module techs gate purple and the silo, so queue them early.
- Brownouts: extra consumers without extra boilers/engines; boilers need a steady coal feed.
- Labs idle: an empty research queue (keep 2+ techs queued), or packs that never reach the labs.
- Launching with a half-built silo or no cargo landing pad.

## Priorities when unsure

1. Keep research running (queue the rocket path first). 2. Feed the labs all packs they need.
3. Fix the constraint step (the one that is fully busy while later steps starve). 4. Power ahead of demand.
5. Only then expand. A blocked machine means the consumer is missing, not that more is needed.
