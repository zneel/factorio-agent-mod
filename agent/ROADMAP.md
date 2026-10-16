# Rocket roadmap (the goal is to launch a rocket)

Every job must serve a phase below. A job that doesn't move a gate KPI or unlock the next phase is waste.
`python crew.py roadmap` prints the live status (current phase, KPI gaps, packs still needed). The
supervisor keeps this file up to date.

## Facts (computed from the game's tech tree; `crew.py roadmap` recomputes)

- The rocket silo tech needs 16 more techs. Together they still cost about **3475 red, 3475 green,
  3275 blue, 1600 purple, 1000 yellow** research packs. The silo tech itself is 1000 of all five (60 s
  per unit).
- After the research: the silo (roughly 1000 concrete, 1000 steel, 200 electric engines, 200 processing
  units, 100 pipes), a cargo landing pad, 100 rocket parts (each ~10 processing units + 10 low density
  structures + 10 rocket fuel) and a satellite. Confirm every cost with `recipe_info`.
- Raw volume is the real constraint: purple is ~50 iron + 20 copper per pack, yellow ~33 iron +
  50 copper, plus modules, rails, processing units. Plan for several times today's plate output.

## Phases (in order; a phase is done when its gate KPIs hold, measured per minute over 10 minutes)

| Phase | What | Gate (made per minute) |
|---|---|---|
| P1 Blue online | red/green/blue flowing to the labs | blue >= 6 made (red and green are in surplus, consumption-limited) |
| P2 Industry | scale mining, smelting (electric furnaces after advanced material processing 2), steel, power | iron-plate >= 700, copper-plate >= 500, steel-plate >= 80 (smelting capacity) |
| P3 Purple | production science: electric furnace + productivity module + rails | production-science-pack >= 4 |
| P4 Yellow + oil | advanced oil + cracking, lubricant, batteries, processing units, low density structures, robotics | utility-science-pack >= 3 |
| P5 Silo research | all five packs reach the labs | rocket-silo researched |
| P6 Silo and parts | silo, cargo landing pad, 100 rocket parts, satellite | parts and satellite ready |
| P7 Launch | insert, launch | rocket launched |

Phases overlap: P2 (raw volume) starts now, because every later phase is limited by plates. Always
work the biggest KPI gap of the current phase first, and start the next phase's prerequisites when a
character would otherwise idle.

## Research order (queue it all; dependency order, cheapest first)

advanced-material-processing-2, low-density-structure, flammables, electric-energy-accumulators,
lubricant, electric-engine, processing-unit, robotics, production-science-pack, utility-science-pack,
rocket-fuel, productivity-module-2, speed-module-2, productivity-module-3, speed-module-3, rocket-silo.
Red+green-only techs (flammables, accumulators) soak up surplus red/green while blue is scarce.

## Levers

- More science per minute: more assemblers (copy proven cells), speed modules in assembler slots,
  more labs only if packs are available (labs are not the limit while blue is).
- Game speed (`set_game_speed`): raise it when the crew is not the limiter (workers idle < 10% of the
  time) and the base is, because production scales with game time but construction is bound by agent
  calls.

## Phase 2 ground truth (measured; update as it changes)

- Nearby ore: iron 626 tiles / 335k (centre 39.5,9.6), copper 394 / 307k (79,-17), stone 236 / 178k
  (113,-6), coal 329 / 248k (96,20). Remote: iron 679k at (-248,-3), copper 1.24M at (87,229).
  The whole rocket needs about 260k iron and 250k copper: the nearby patches suffice, remote ones are
  the follow-up for speed.
- Iron is MINING-limited: 14 drills (~420 ore/min) feed ~12 furnaces. Furnace upgrades on the iron rows
  gain nothing until more ore arrives. Copper furnaces (8 stone, ~150/min) and the steel row (y=28)
  are saturated: upgrade them (jobs: craft 31 steel furnaces, then upgrade in place).
- Free iron strip y=1..4 (x 30-50): 6 verified drill spots at y=3.5 facing north: x=30.5 33.5 36.5 39.5
  42.5 48.5 (~64k ore, +180 ore/min). Where the ore goes is the open design (`crew.py designs`).
- Free building land: x=7..21, y=-7..23 (west of the plastic trunk x=22.5); north of the pack belt
  y=-12.5, x 24-46, y -20..-14; north-east near copper/stone for purple/yellow.
- Smelting capacity gates count furnace capacity (plates/min if fed), not plates made (those can't
  exceed what the factory consumes). Steel/electric furnaces smelt at twice the stone-furnace speed.
