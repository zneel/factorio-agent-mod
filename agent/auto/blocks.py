"""Block interiors: a module's recipes -> CP-SAT layout spec -> solved and compiled placements.

The block's solid inputs arrive as bus belts (two items each, the global pairing in `pairs`); fluid machines sit
at row ends with their ports facing out (pipes come from outside). Exports leave on clean belts.
"""
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import plan_layout  # noqa: E402

ENTITY = {"chemistry": "chemical-plant", "oil-processing": "oil-refinery"}
FLUIDS = {"water", "crude-oil", "heavy-oil", "light-oil", "petroleum-gas", "lubricant", "sulfuric-acid", "steam"}
GRIDS = [(2, 3), (2, 4), (3, 3), (3, 4), (2, 5), (3, 5), (4, 4), (4, 5), (3, 6), (4, 6)]


def spec_for(block, demand, pairs, origin=(0.5, 0.5), hours=None):
    rec = demand["recipes"]
    machines = []
    for r, n in block["recipes"].items():
        for k in range(n):
            m = {"id": f"{r[:10]}{k}", "recipe": r}
            if rec[r]["cat"] in ENTITY:
                m["entity"] = ENTITY[rec[r]["cat"]]
            if any(i in FLUIDS for i, _ in rec[r]["ing"] + rec[r]["prod"]):
                m["end"] = "west"   # fluid ports face away from the bus (the fluid side is west)
            machines.append(m)
    inputs = []
    for it in block["inputs"]:
        pr = next((p for p in pairs if it in p), (it, None))
        b = {"name": "/".join(x for x in pr if x), "left": pr[0], "right": pr[1]}
        if b not in inputs:
            inputs.append(b)
    # crafts per second the block must sustain = LP rate x margin, as targets on its exports
    target = {}
    for r in block["recipes"]:
        for p, n in rec[r]["prod"]:
            if p in block["exports"] and p not in FLUIDS:
                target[p] = demand["crafts_per_min"][r] * n / 60 * 1.15
    # what other blocks read of each export (items/s, with margin): the block's belts must carry at least that out
    export_need = {}
    for r, c in demand["crafts_per_min"].items():
        if r in block["recipes"] or r not in rec:
            continue
        for i, n in rec[r]["ing"]:
            if i in target:
                export_need[i] = export_need.get(i, 0) + c * n / 60 * 1.15
    return {"name": block["name"], "origin": list(origin), "rows": 2, "cols": 3, "machines": machines,
            "inputs": inputs, "exports": [e for e in block["exports"] if e not in FLUIDS], "clean_exports": True,
            "chest": block.get("chest", []),
            "target": target, "export_need": export_need}


def solve(spec, demand, seconds=60):
    rec = {r: (v["ing"], v["prod"], v["energy"]) for r, v in demand["recipes"].items()}
    n = len(spec["machines"])
    last = None
    for rows, cols in GRIDS:
        if rows * cols < n:
            continue
        spec = dict(spec, rows=rows, cols=cols)
        t = time.time()
        try:
            out, _status = plan_layout.solve(spec, rec, seconds=seconds)
            return out, spec, f"{rows}x{cols} {time.time() - t:.1f}s"
        except Exception as e:  # infeasible at this size: grow
            last = str(e)[:120]
    raise RuntimeError(f"{spec['name']}: no layout ({last})")


def pair_bus(mods):
    """Two items per bus belt: maximum-weight matching (networkx) on how often two items are read by the same
    block, so a block taps as few belts as possible."""
    import networkx as nx
    items = sorted({i for m in mods if m["kind"] == "block" for i in m["inputs"]} |
                   {"automation-science-pack", "logistic-science-pack", "chemical-science-pack",
                    "production-science-pack", "utility-science-pack"})
    G = nx.Graph()
    G.add_nodes_from(items)
    for m in mods:
        ins = m.get("inputs", [])
        for a in ins:
            for b in ins:
                if a < b:
                    G.add_edge(a, b, weight=G.get_edge_data(a, b, {"weight": 0})["weight"] + 1)
    for a in items:  # every pair allowed (weight ~0), so the matching covers as much as possible
        for b in items:
            if a < b and not G.has_edge(a, b):
                G.add_edge(a, b, weight=0.01)
    match = nx.max_weight_matching(G, maxcardinality=True)
    pairs = [tuple(sorted(p)) for p in match]
    left = [i for i in items if not any(i in p for p in pairs)]
    return pairs + [(i, None) for i in left]


def pairings(items):
    """All ways to put items on belts of at most two, fewest belts first (singles allowed)."""
    items = list(items)
    out = []

    def rec(left, cur):
        if not left:
            out.append(list(cur))
            return
        a = left[0]
        for i in range(1, len(left)):
            rec(left[1:i] + left[i + 1:], cur + [(a, left[i])])
        rec(left[1:], cur + [(a, None)])
    rec(items, [])
    out.sort(key=len)
    seen, uniq = set(), []
    for p in out:
        k = tuple(sorted(p, key=str))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def solve_block(block, demand, seconds=20, max_tries=12):
    """Try the block's own input pairings (fewest belts first; each pairing also mirrored left/right) until the
    layout solver finds one. Returns (layout, spec, pairing, info)."""
    last = None
    for pr in pairings(block["inputs"])[:max_tries]:
        for mirror in (False, True):
            pp = [((b, a) if mirror and b else (a, b)) for a, b in pr]
            spec = spec_for(block, demand, pp)
            try:
                out, sp, info = solve(spec, demand, seconds)
                return out, sp, pp, info
            except RuntimeError as e:
                last = e
    raise RuntimeError(f"{block['name']}: no pairing lays out ({last})")
