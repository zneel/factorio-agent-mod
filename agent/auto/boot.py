"""Burner-phase plans for a seed, from map data: burner drill pairs on the ore patches nearest the spawn.

    python -m auto.boot <seed>   -> runtime/auto/<seed>/boot/boot-{iron,copper,coal,stone}.json

Each pair is a burner drill (2x2, facing east) on pure ore, and right in front of it a stone furnace (iron,
copper) or an iron chest (coal, stone). No tile may be water, a cliff, or anything the factory plan builds later.
"""
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from auto import geom, mapdata, mines  # noqa: E402

COUNT = {"iron": ("i", 8, "stone-furnace"), "copper": ("c", 4, "stone-furnace"), "coal": ("k", 8, "iron-chest"),
         "stone": ("s", 2, "iron-chest")}


def pairs(m, code, n, out_item, blocked):
    sp = tuple(m.spawn)
    # the nearest patches first; one the factory plan covers completely gives nothing, so the next one is tried
    cand = []
    for patch in sorted((p for p in mines.patches(m, code) if len(p) >= 40),
                        key=lambda p: min(math.dist(sp, t) for t in p))[:4]:
        cand += sorted(patch, key=lambda t: math.dist(sp, t))
    got, taken = [], set()
    for x, y in cand:
        drill = {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)}
        out = {(x + 2, y), (x + 3, y), (x + 2, y + 1), (x + 3, y + 1)} if out_item == "stone-furnace" else {(x + 2, y)}
        tiles = drill | out
        ring = {(a + dx, b + dy) for a, b in tiles for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        if any(m.o(*t) != code for t in drill) or any(m.t(*t) in "~C#" for t in tiles):
            continue
        if tiles & blocked or ring & taken:
            continue
        got.append({"item": "burner-mining-drill", "x": x + 1, "y": y + 1, "direction": "east"})
        if out_item == "stone-furnace":
            got.append({"item": "stone-furnace", "x": x + 3, "y": y + 1})
        else:
            got.append({"item": "iron-chest", "x": x + 2.5, "y": y + .5})
        taken |= tiles
        if len(got) >= 2 * n:
            break
    return got


def main():
    seed = int(sys.argv[1])
    m = mapdata.Map.load(seed)
    out = mapdata.out_dir(seed)
    plan = json.loads((out / "plan.json").read_text())
    blocked = geom.tiles(plan["complex"] + plan["world"])
    d = out / "boot"
    d.mkdir(exist_ok=True)
    coal_cols = geom.tiles([p for p in plan["world"] if str(p.get("grp", "")).startswith("mine:coal")])
    for name, (code, n, item) in COUNT.items():
        # burner coal may stand where electric coal columns go later (the growth controller picks it up then):
        # a map can have a single coal patch that the plan covers completely
        pl = pairs(m, code, n, item, blocked - coal_cols if code == "k" else blocked)
        blocked |= geom.tiles(pl)
        (d / f"boot-{name}.json").write_text(json.dumps({"place": pl, "gate": {"fuel": "coal"}}))
        print(f"boot-{name}: {len(pl) // 2} pairs", pl[:2])


if __name__ == "__main__":
    main()
