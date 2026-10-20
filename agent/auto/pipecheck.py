"""Static fluid check of a plan: adjacent pipe pieces that connect but carry different fluids (labels from the
complex's pipe_fluids), and pipe pieces with no label.   python -m auto.pipecheck <seed>"""
import json, math, pickle, sys
from auto import mapdata
from auto.site import rot_tile

DV = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def conns(q):
    if q["item"] == "pipe":
        return set(DV.values())
    if q["item"] == "pipe-to-ground":
        return {DV[q.get("direction", "north")]}
    return set()


def check(seed, tag=""):
    out = mapdata.out_dir(seed)
    t = f".{tag}" if tag else ""
    p = json.loads((out / f"plan{t}.json").read_text())
    cx = pickle.loads((out / f"complex{t}.pkl").read_bytes())
    k, (dx, dy) = p["rotation"], p["offset"]
    lab = {}
    for t, f in cx["pipe_fluids"].items():
        w = rot_tile(t, k)
        lab[(w[0] + dx, w[1] + dy)] = f
    pieces = {}
    for q in p["complex"]:
        if q["item"] in ("pipe", "pipe-to-ground") and not q.get("remove"):
            pieces[(math.floor(q["x"]), math.floor(q["y"]))] = q
    bad, unl = [], []
    for t, q in pieces.items():
        if t not in lab:
            unl.append(t)
        for d in conns(q):
            n = (t[0] + d[0], t[1] + d[1])
            o = pieces.get(n)
            if o and (-d[0], -d[1]) in conns(o) and t in lab and n in lab and lab[t] != lab[n] and t < n:
                bad.append((t, lab[t], n, lab[n]))
    return bad, unl


def steam_joints(places):
    """Pipe pieces on a steam engine's joint (the tile past either end): they join the steam to their segment, and
    a segment holding another fluid starves the engines. Returns [(engine position, tile)]."""
    pipes = {(math.floor(q["x"]), math.floor(q["y"])) for q in places if q["item"] in ("pipe", "pipe-to-ground")}
    out = []
    for q in places:
        if q["item"] == "steam-engine":
            d = DV[q.get("direction", "north")]
            for s in (3, -3):
                t = (math.floor(q["x"] + s * d[0]), math.floor(q["y"] + s * d[1]))
                if t in pipes:
                    out.append(((q["x"], q["y"]), t))
    return out


if __name__ == "__main__":
    bad, unl = check(int(sys.argv[1]))
    print(f"{len(bad)} mixed joints, {len(unl)} unlabelled pieces")
    for b in bad[:30]:
        print("  MIX", b)
    print("  unlabelled:", unl[:20])
