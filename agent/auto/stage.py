"""Cut a seed's plan.json into build stages for the runner (see STAGED.md).

    python -m auto.stage <seed>     -> runtime/auto/<seed>/stages/NN-<group>.json + stages.json

Each placement carries a "grp" tag from plan2. A group needs the technologies that unlock the recipes of the
entities it places and of the recipes it sets; its stage is ordered by how deep in the tech tree those are, then
by where it sits on the bus. Power and raw materials come first, the silo last.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from auto import mapdata  # noqa: E402

GAMEDATA = HERE.parent.parent / "runtime" / "auto" / "gamedata.json"
# supply chains in order, each complete before the next (mine -> smelter -> trunk), so belts carry materials as
# early as possible: power, coal to the boilers and furnaces, iron, copper, stone, raw iron ore, the bus, the mall
EARLY = [r"^power$", r"^mine:coal:", r"^coal$", r"^mine:iron-ore:", r"^smelt:iron-ore:", r"^trunk:iron-plate$",
         r"^mine:copper-ore:", r"^smelt:copper-ore:", r"^trunk:copper-plate$", r"^mine:stone:", r"^trunk:stone$",
         r"^mine:raw-iron-ore:", r"^trunk:iron-ore$", r"^bus$", r"^mall$"]


def techs_for(recipes, gd):
    """Every technology (with prerequisites) needed to use these recipes."""
    unlock = {r: t for t, v in gd["techs"].items() for r in v["unlocks"]}
    need, todo = set(), [unlock[r] for r in recipes if r in unlock]
    while todo:
        t = todo.pop()
        if t in need:
            continue
        need.add(t)
        todo += gd["techs"][t]["pre"]
    return need


def stages(plan, gd):
    groups = {}
    for p in plan["complex"] + plan["world"]:
        if p.get("remove"):
            continue
        g = p.get("grp", "untagged")
        g = re.sub(r"^(band|conn):", "", g)   # a module's bands and connector belts are built with it
        groups.setdefault(g, []).append(p)
    # poles join the group of the nearest powered thing they serve (each stage brings its own power); a pole
    # with nothing powered near it joins the group of the nearest other pole's assignment (a chain link)
    if "poles" in groups:
        from auto.poles import ELECTRIC
        pts = [(q["x"], q["y"], g) for g, pl in groups.items() if g != "poles" for q in pl
               if any(k in q["item"] for k in ELECTRIC) or "electric-pole" in q["item"]]
        buck = {}
        for x, y, g in pts:
            buck.setdefault((int(x // 8), int(y // 8)), []).append((x, y, g))
        left = []
        for q in groups.pop("poles"):
            bx, by = int(q["x"] // 8), int(q["y"] // 8)
            near = [c for dx in (-1, 0, 1) for dy in (-1, 0, 1) for c in buck.get((bx + dx, by + dy), [])]
            if near:
                g = min(near, key=lambda c: (c[0] - q["x"]) ** 2 + (c[1] - q["y"]) ** 2)[2]
                groups[g].append(q)
                buck.setdefault((bx, by), []).append((q["x"], q["y"], g))
            else:
                left.append(q)
        for _ in range(50):   # chains: inherit from the nearest assigned pole, repeatedly
            if not left:
                break
            rest = []
            for q in left:
                bx, by = int(q["x"] // 8), int(q["y"] // 8)
                near = [c for dx in (-1, 0, 1) for dy in (-1, 0, 1) for c in buck.get((bx + dx, by + dy), [])]
                if near:
                    g = min(near, key=lambda c: (c[0] - q["x"]) ** 2 + (c[1] - q["y"]) ** 2)[2]
                    groups[g].append(q)
                    buck.setdefault((bx, by), []).append((q["x"], q["y"], g))
                else:
                    rest.append(q)
            left = rest
        if left:
            groups["poles"] = left
    info = {}
    for g, pl in groups.items():
        recipes = {p["item"] for p in pl} | {p["recipe"] for p in pl if p.get("recipe")}
        need = techs_for(recipes, gd)
        y = sum(p["y"] for p in pl) / len(pl)
        early = next((i for i, rx in enumerate(EARLY) if re.search(rx, g)), None)
        info[g] = {"techs": sorted(need), "n": len(pl), "early": early, "y": y}

    def key(g):
        i = info[g]
        if g == "poles":
            return (2, 0, 0)      # poles go with each stage below; the leftovers at the end
        if g == "silo":
            return (3, 0, 0)
        if any(p.get("recipe") == "automation-science-pack" for p in groups[g]):
            return (0, len(EARLY), 0)       # red science right after the raw materials: research starts
        if g == "labs":
            return (0, len(EARLY) + 1, 0)
        if i["early"] is not None:
            return (0, i["early"], i["y"])
        return (1, len(i["techs"]), i["y"])
    order = sorted(groups, key=key)
    # power reaches every stage when it is built: each stage also takes the poles on the shortest wire path from
    # its own poles to the steam plant that no earlier stage has placed (a pole is built in its first stage)
    import networkx as nx
    from auto.poles import REACH
    poles = [(q["x"], q["y"]) for g in order for q in groups[g] if "electric-pole" in q["item"]]
    owner = {(q["x"], q["y"]): g for g in order for q in groups[g] if "electric-pole" in q["item"]}
    pole_place = {(q["x"], q["y"]): q for g in order for q in groups[g] if "electric-pole" in q["item"]}
    eng = [(q["x"], q["y"]) for q in plan["world"] if q["item"] == "steam-engine"]
    if poles and eng:
        G = nx.Graph()
        G.add_nodes_from(poles)
        buck = {}
        for a in poles:
            buck.setdefault((int(a[0] // 8), int(a[1] // 8)), []).append(a)
        for a in poles:
            bx, by = int(a[0] // 8), int(a[1] // 8)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for c in buck.get((bx + dx, by + dy), []):
                        if c != a and (a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2 <= REACH * REACH:
                            G.add_edge(a, c)
        root = min(poles, key=lambda a: min((a[0] - e[0]) ** 2 + (a[1] - e[1]) ** 2 for e in eng))
        paths = nx.single_source_shortest_path(G, root)
        placed = set()
        for g in order:
            mine = [(q["x"], q["y"]) for q in groups[g] if "electric-pole" in q["item"]]
            extra = []
            for a in mine:
                for c in paths.get(a, []):
                    if c not in placed and owner[c] != g and c not in extra:
                        extra.append(c)
            for c in extra:            # moved into this stage (built now, not when its old stage comes)
                groups[owner[c]].remove(pole_place[c])
                groups[g].append(pole_place[c])
                owner[c] = g
            placed |= set(mine) | set(extra)
            info[g]["n"] = len(groups[g])
    return order, groups, info


def main():
    seed = int(sys.argv[1])
    out = mapdata.out_dir(seed)
    plan = json.loads((out / "plan.json").read_text())
    gd = json.loads(GAMEDATA.read_text())
    order, groups, info = stages(plan, gd)
    sd = out / "stages"
    sd.mkdir(exist_ok=True)
    rows = []
    for k, g in enumerate(order):
        f = sd / f"{k:02d}-{re.sub(r'[^a-z0-9-]+', '_', g)}.json"
        f.write_text(json.dumps({"place": groups[g]}))
        rows.append({"stage": k, "group": g, "file": str(f.relative_to(out.parent.parent.parent)),
                     "entities": info[g]["n"], "techs": info[g]["techs"]})
    (out / "stages.json").write_text(json.dumps(rows, indent=1))
    for r in rows:
        print(f"{r['stage']:3d} {r['group']:28s} {r['entities']:5d} entities, {len(r['techs']):2d} techs")


if __name__ == "__main__":
    main()
