# Factorio autoplayer: closed-loop growth controller

## Goal
Write a program that plays Factorio 2.0 (base game, peaceful) by itself, on **any map seed**, from an empty map to a
**rocket launch**. No agents in the loop and no hand-placed layouts: everything is computed from map data and live game
data, using solver libraries.

## Hard rules
1. **Everything built works the moment it is built.** Every increment is complete: power, inputs, outputs, and the
   connection to what already exists. Never place a mine without its belt, a furnace without its fuel, or a pole that
   is not wired to power.
2. **Characters only build.** Belts, inserters and the factory move all materials. Hand-crafting and hauling happen
   only in the burner bootstrap, before any automation exists. After that, building materials come from an automated
   mall (assemblers into chests).
3. **Closed loop.** Measure real production in the running game, compare it with the targets, and grow or rebalance
   the factory toward them. Nothing is sized once and then left alone.
4. **Dynamic, not hard-coded.** Layouts come from solvers working on the current map state (OR-Tools CP-SAT,
   scipy/HiGHS LP, networkx A*). Game rules come from factoriocheatsheet.com and the game's own data. Use open-source
   libraries only; search GitHub before writing something yourself (factorio-draftsman is already installed for
   entity data and validation).
5. **Debug with data, not screenshots.** Log numbers to files: rates, statuses, and per-entity checks.

## The loop
1. **Measure** every item's made and used rate (production statistics), machine statuses (no power, no input, full
   output) and stock.
2. **Target**: the LP plan for the rocket and the science rates (`plan_lp`), scaled to the current stage of the game.
   That stage is decided by the technologies researched.
3. **Choose** the worst bottleneck: the largest deficit, or broken machines first (unpowered, unfed).
4. **Plan one increment** that fixes it, against the *current* map and entities. For example: an iron mine column, its
   smelter column, the belt into the bus, and poles chained to the grid. Validate it statically: no overlaps
   (draftsman), no mixed fluids, no belt end pointing into another line, every port reachable.
5. **Build it** with the characters, using the bridge and runner scripts.
6. **Verify it runs**: powered, producing at the expected rate. If it doesn't, diagnose from the data and repair.
7. **Research**: keep labs busy with the next technologies the plan needs. Some 2.0 technologies unlock by crafting or
   building something; the controller does that action.
8. Repeat until the rocket launches.

## Existing code to reuse (E:\modding\factorio-agent\agent)
- **Bridge and tools:** `bridge.py` (RCON `/agent` commands), `macros.build`, `supply.build_plan` (fetch, craft,
  place), `bootstrap.py` (burner phase), `runner.py`.
- **Servers:** `server.py`. The scratch server is `fa_scratch.py` (`fresh` creates a new map with the live seed,
  port 34297); set `FA_TARGET=scratch` to point every tool at it.
- **Planning (`auto/`):**
  - `plan_lp` / `demand`: machine counts.
  - `mapdata`: map scan.
  - `mines`: ore patches and drill columns.
  - `interiors` + `plan_layout` + `blocks`: CP-SAT block layouts with lane, rate and reachability constraints.
  - `bus`: main bus, fluid bus and the repair loop.
  - `connect` / `route` / `plan_nx`: A* belts and pipes.
  - `poles`, `geom`.
  - `boot`: burner plans from map data.
  - `research`: the research driver, the hut lab, trigger technologies.
  - `control`: live rates against targets, mall recipe assignment.
  - `verify`: instant spawn and run on the scratch server.
  - `dcheck` / `pipecheck`: validation.
- **Lessons already learned:**
  - The inserter plan direction is the drop side.
  - A belt end feeds whatever is in front of it.
  - Symmetric entities store a west direction as east.
  - Characters can wall themselves in.
  - Trees can block placement while the game reports only "invalid spot".
  - Pause the game while bulk-spawning.

## Constraints
- **Ask the user before:** deleting things, pushing to GitHub, pip installs, and CMake/FetchContent fetches.
- **Don't commit** unless told to.
- **Save tokens:** prefer algorithms over agents, and keep outputs short.
- **Test on the scratch server first** (watch at `localhost:34297`).

## Done when
On a fresh map of a seed that wasn't used for tuning, the program reaches a rocket launch unattended. It logs, at
every increment, what it built, why, and the measured rates before and after.
