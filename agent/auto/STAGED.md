# Staged builder (design)

Goal: on a fresh map, characters build `plan.json` from nothing to a rocket launch, with no agents in the loop.
The plan says *what* goes *where*. The staged builder decides *when* and *who*.

## Inputs

- `plan.json`: every placement, with a `grp` tag (added by `plan2`) naming the piece it belongs to:
  - `power`
  - `mine:<ore>:<i>` and `smelt:<ore>:<i>`
  - `trunk:<item>` and `coal`
  - `bus`, `band:<module>` and `conn:<module>`
  - the module's own name: `B1` … `B9`, `oil-0`, `labs`, `acid`, `silo`, and so on
  - `fluid`, `pipe:water`, `pipe:crude` and `poles`
- `gamedata.json`: recipes, plus which technology unlocks each recipe and entity.

## Stages (`auto/stage.py` turns the plan into runner queue items)

| # | Stage | Groups | Needs research |
|---|-------|--------|----------------|
| 0 | Bootstrap | Burner drills and stone furnaces on the nearest iron, copper, coal and stone (`bootstrap.py`), plus the depot | — |
| 1 | Power | `power` plus the poles that reach it | — |
| 2 | Raw | `mine:*`, `smelt:*`, `trunk:*`, `coal`, and the `bus` head for the raw items | — |
| 3 | Red science | The block that makes red science, its bands, `labs`, and the poles | — |
| 4… | Research-gated | Every other group, ordered by the earliest point its technologies can be done (topological order over the tech tree, then by distance down the bus) | Per group, from gamedata |
| last | Silo | `silo` | `rocket-silo` |

- Each stage becomes one or more runner items: a plan file with only that stage's placements, and `after:` set to the previous stage.
- The runner already retries an item when a machine's research isn't done yet (`supply.build_plan` → `failed`).

## Research driver (`auto/research.py`, one thread in the runner)

- Keeps the force's research queue filled with the technologies the next unbuilt stage needs, in tech-tree order.
- Feeds labs by hand only until the labs' belt is built (`supply.feed_labs`).

## Materials

- `supply.build_plan` crafts entities from the depot and chests, and mines wood.
- Early on, a tender character keeps the burner miners fuelled (`runner.py` `tend`).
- Once the power and raw stages exist, plates come from the smelter columns: `fetch` finds them in furnaces and chests.

## Checks

- Every stage is first built on the scratch server instantly (`verify.py` with only the groups up to that stage), so a stage that can't run on its own (no power, no input) shows up before any character walks there.
- In the live game, the runner log and `stallwatch.py` report stuck items. The overseer acts on the numbers.
