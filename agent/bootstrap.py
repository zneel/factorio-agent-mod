"""Burner-phase bootstrap: one character builds its share of gated burner plans, keeping them fuelled.

    python bootstrap.py <character> <plan.json>[,<plan2.json>...] [--share k/n] [--minutes 10]

A deterministic loop (no agent thinking), run by a worker as one job:
  1. fuel:  keep >= 20 coal on hand (from the boot-coal chests, else hand-mine the nearest coal);
  2. tend:  top up every burner of the plans to 5 coal, take plates/stone out of their furnaces/chests;
  3. build: the next unbuilt entry of its share (pairs i with i % n == k), crafting it from what it carries
            (iron from the iron furnaces, stone from the stone chests or hand-mined).
Stops when its share is built (then prints DONE) or after --minutes of wall time (prints PARTIAL).
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from bridge import Bridge, BridgeError  # noqa: E402
import macros  # noqa: E402
import supply  # noqa: E402

import os  # noqa: E402
_BOOT = Path(os.environ["FA_BOOT_DIR"]) if os.environ.get("FA_BOOT_DIR") else ROOT / "runtime" / "blocks" / "new"
COAL_CHESTS = _BOOT / "boot-coal.json"     # FA_BOOT_DIR: a seed's generated burner plans (auto/boot.py)
STONE_CHESTS = _BOOT / "boot-stone.json"


def load(p):
    p = Path(p)
    if not p.is_absolute():
        p = ROOT / p
    s = json.loads(p.read_text())
    return s["place"] if isinstance(s, dict) else s


def inv(b):
    return dict(b.call("state")["agent"].get("inventory") or {})


def built(b, e):
    for x in b.call("probe", x=e["x"], y=e["y"]).get("entities") or []:
        if x["name"] == e["item"] and abs(x["pos"]["x"] - e["x"]) < 0.01 and abs(x["pos"]["y"] - e["y"]) < 0.01:
            return x
    return None


def take_from(b, entries, item, want):
    """Take up to `want` of item from the built chests/furnaces in entries (nearest first)."""
    got = 0
    pos = b.call("state")["agent"]["position"]
    cands = sorted(entries, key=lambda e: (e["x"] - pos["x"]) ** 2 + (e["y"] - pos["y"]) ** 2)
    for e in cands:
        if got >= want:
            break
        x = built(b, e)
        if not x or (x.get("items") or {}).get(item, 0) < 1:
            continue
        try:
            pos = macros._near(b, pos, (e["x"], e["y"]))
            r = b.call("take", x=e["x"], y=e["y"], item=item)
            got += sum(v for k, v in (r.get("taken") or r.get("inv+") or {}).items() if k == item) if isinstance(r, dict) else 0
        except BridgeError:
            pass
    return got


def mine(b, name, count):
    try:
        b.run("mine", name=name, count=count, timeout=600)
    except BridgeError as e:
        print("mine", name, e)


def ensure_coal(b, n=20):
    have = inv(b).get("coal", 0)
    if have >= n:
        return
    supply.fetch(b, "coal", n - have)
    have = inv(b).get("coal", 0)
    if have < n // 2:
        mine(b, "coal", n - have)


def tend(b, plan):
    """Refuel the plan's burners to 5 coal and take their output."""
    pos = b.call("state")["agent"]["position"]
    for e in sorted(plan, key=lambda e: (e["x"] - pos["x"]) ** 2 + (e["y"] - pos["y"]) ** 2):
        x = built(b, e)
        if not x:
            continue
        items = x.get("items") or {}
        try:
            if e["item"] in ("burner-mining-drill", "stone-furnace", "boiler", "burner-inserter") and items.get("coal", 0) < 3:
                c = inv(b).get("coal", 0)
                if c < 2:
                    ensure_coal(b)
                    c = inv(b).get("coal", 0)
                pos = macros._near(b, pos, (e["x"], e["y"]))
                b.call("insert", item="coal", count=min(5 - items.get("coal", 0), c), x=e["x"], y=e["y"], inventory="fuel")
            mine_inv = inv(b)
            out = {k: v for k, v in items.items() if k in ("iron-plate", "copper-plate", "stone", "coal") and v >= 5
                   and not (k == "coal" and e["item"] != "iron-chest") and mine_inv.get(k, 0) < supply.KEEP[k]}
            for k in out:
                pos = macros._near(b, pos, (e["x"], e["y"]))
                b.call("take", x=e["x"], y=e["y"], item=k)
        except BridgeError as err:
            print("tend", e["item"], e["x"], e["y"], str(err)[:80])


def craft(b, item, n=1):
    try:
        r = b.run("craft", recipe=item, count=n, wait=True, timeout=600)
        return r.get("state") == "done"
    except BridgeError:
        return False


def get_materials(b, item, iron_src, stone_src):
    """Raw needs per item (iron plates, stone): fetch what is short."""
    need = {"burner-mining-drill": (9, 5), "stone-furnace": (0, 5), "iron-chest": (8, 0)}[item]
    have = inv(b)
    if have.get("stone-furnace", 0) and item == "burner-mining-drill":
        need = (need[0], 0)
    iron_short = need[0] - have.get("iron-plate", 0) - 2 * have.get("iron-gear-wheel", 0)
    if iron_short > 0:
        supply.fetch(b, "iron-plate", iron_short + 10)
        if inv(b).get("iron-plate", 0) < need[0]:
            return False
    stone_short = need[1] - have.get("stone", 0)
    if stone_short > 0:
        supply.fetch(b, "stone", stone_short + 10)
        if inv(b).get("stone", 0) < need[1]:
            mine(b, "stone", need[1] - inv(b).get("stone", 0) + 5)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("char")
    ap.add_argument("plans")
    ap.add_argument("--share", default="0/1")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--tend", action="store_true", help="only keep the plans fuelled and share output")
    a = ap.parse_args()
    k, n = (int(v) for v in a.share.split("/"))
    b = Bridge(a.char)
    plan = [e for p in a.plans.split(",") for e in load(p)]
    # entries come in pairs (drill + its furnace/chest); share by pair index
    mine_list = [e for i, e in enumerate(plan) if (i // 2) % n == k]
    every = plan + load(COAL_CHESTS) + load(STONE_CHESTS)
    iron_src = [e for e in every if e["item"] == "stone-furnace"]
    stone_src = [e for e in load(STONE_CHESTS) if e["item"] == "iron-chest"]
    t_end = time.time() + a.minutes * 60
    stuck = 0
    if a.tend:
        rounds = 0
        while time.time() < t_end:
            ensure_coal(b, 40)
            tend(b, plan)
            supply.deposit(b, everything=False)
            rounds += 1
        print(f"DONE tended {len(plan)} entities, {rounds} rounds")
        return
    while time.time() < t_end:
        supply.deposit(b, everything=False)
        ensure_coal(b)
        left = [e for e in mine_list if not built(b, e)]
        if not left:
            tend(b, plan)
            supply.deposit(b, everything=False)
            print(f"DONE {len(mine_list)} entries built")
            return
        e = left[0]
        if inv(b).get(e["item"], 0) < 1:
            if not craft(b, e["item"]):
                if get_materials(b, e["item"], iron_src, stone_src):
                    craft(b, e["item"])
        if inv(b).get(e["item"], 0) >= 1:
            r = macros.build(b, [e])
            if r.get("placed"):
                stuck = 0
                tend(b, [e])  # fuel it right away
                continue
            print("build", e, r.get("failed"))
        stuck += 1
        tend(b, plan)
        if stuck > 3:
            b.run("wait", seconds=10, timeout=60)
    left = [e for e in mine_list if not built(b, e)]
    print(f"PARTIAL {len(mine_list) - len(left)}/{len(mine_list)} built; next {left[0]['item']} {left[0]['x']},{left[0]['y']}"
          if left else "DONE")


if __name__ == "__main__":
    main()
