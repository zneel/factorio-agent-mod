"""Queries over runtime/map/snapshot.json.gz (take one with mapdump.py first).

    .venv/Scripts/python mapq.py near X,Y [--r 30]     items on belts / in chests / made by machines within r tiles
    .venv/Scripts/python mapq.py item NAME [--at X,Y]  belt lines carrying NAME (networkx components), their
                                                        tile count, how full, and the nearest tile to X,Y
    .venv/Scripts/python mapq.py idle                  machines starved / blocked / unpowered, grouped
"""
import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapdump  # noqa: E402

SNAP = os.path.join(mapdump.OUT, "snapshot.json.gz")


def load():
    with gzip.open(SNAP, "rt", encoding="utf-8") as f:
        return json.load(f)


def near(snap, x, y, r):
    belts, chests, makers = {}, {}, {}
    for e in snap["entities"]:
        if abs(e["x"] - x) > r or abs(e["y"] - y) > r:
            continue
        for lane in ("L1", "L2"):
            for it, n in (e.get(lane) or {}).items():
                belts[it] = belts.get(it, 0) + n
        if e["t"] in ("container", "logistic-container"):
            for it, n in (e.get("inv") or {}).items():
                chests[it] = chests.get(it, 0) + n
        if e.get("r"):
            makers[e["r"]] = makers.get(e["r"], 0) + 1
    return belts, chests, makers


def item_lines(snap, item, at=None):
    belt, _ = mapdump.graphs(snap)
    import networkx as nx
    ents = {(e["x"], e["y"]): e for e in snap["entities"] if e["t"] in ("transport-belt", "underground-belt", "splitter")}
    out = []
    for comp in nx.weakly_connected_components(belt):
        n = sum((ents.get(t, {}).get("L1") or {}).get(item, 0) + (ents.get(t, {}).get("L2") or {}).get(item, 0)
                for t in comp)
        if not n:
            continue
        near_t, dist = None, None
        if at:
            near_t = min(comp, key=lambda t: abs(t[0] - at[0]) + abs(t[1] - at[1]))
            dist = abs(near_t[0] - at[0]) + abs(near_t[1] - at[1])
        out.append((dist if dist is not None else 0, len(comp), n, near_t, min(comp), max(comp)))
    return sorted(out)


def idle(snap):
    groups = {}
    for e in snap["entities"]:
        s = e.get("s")
        if s in ("no_power", "no_fuel", "item_ingredient_shortage", "fluid_ingredient_shortage", "full_output",
                 "no_input_fluid", "waiting_for_space_in_destination") and e["t"] in (
                "assembling-machine", "furnace", "mining-drill", "lab", "chemical-plant", "oil-refinery"):
            k = (s, e.get("r") or e["n"])
            groups.setdefault(k, []).append((e["x"], e["y"]))
    return sorted(groups.items(), key=lambda kv: -len(kv[1]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("near")
    a1.add_argument("at")
    a1.add_argument("--r", type=float, default=30)
    a2 = sub.add_parser("item")
    a2.add_argument("name")
    a2.add_argument("--at")
    sub.add_parser("idle")
    args = sys.argv[1:]
    a = ap.parse_args([(" " + x) if len(x) > 1 and x[0] == "-" and x[1].isdigit() else x for x in args])
    snap = load()
    print(f"snapshot tick {snap['tick']}")
    if a.cmd == "near":
        x, y = (float(v) for v in a.at.split(","))
        belts, chests, makers = near(snap, x, y, a.r)
        print("on belts:", ", ".join(f"{k} {v}" for k, v in sorted(belts.items(), key=lambda kv: -kv[1])[:15]))
        print("in chests:", ", ".join(f"{k} {v}" for k, v in sorted(chests.items(), key=lambda kv: -kv[1])[:15]))
        print("machines:", ", ".join(f"{k} x{v}" for k, v in sorted(makers.items(), key=lambda kv: -kv[1])[:20]))
    elif a.cmd == "item":
        at = tuple(float(v) for v in a.at.split(",")) if a.at else None
        for dist, size, n, nt, lo, hi in item_lines(snap, a.name, at)[:10]:
            print(f"line of {size} tiles holding {n} {a.name}, spans {lo}..{hi}"
                  + (f", nearest tile {nt} at {dist:g} tiles" if at else ""))
    else:
        for (s, what), spots in idle(snap)[:15]:
            print(f"{len(spots):3d} x {what} {s}: " + " ".join(f"{x:g},{y:g}" for x, y in spots[:5]))


if __name__ == "__main__":
    main()
