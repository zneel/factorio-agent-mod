"""Seed -> factory layout. Runs the planning stages and writes runtime/auto/<seed>/{layout.json, layout.png}.

    .venv/Scripts/python -m auto.factory --seed N [--hours 8]
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import plan_gen  # noqa: E402
from auto import blocks, demand as demand_mod, gamedata, geom, interiors, mapdata, mines, modules, place, rules  # noqa: E402

POWER_KW = {"assembling-machine-2": 150, "chemical-plant": 210, "oil-refinery": 420, "electric-mining-drill": 90,
            "lab": 60, "inserter": 13, "fast-inserter": 46, "long-handed-inserter": 20, "pumpjack": 90,
            "rocket-silo": 250, "electric-furnace": 180, "offshore-pump": 0, "pump": 30}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def power_plant(site, boilers):
    """plan_gen.power built facing east at the origin, rotated to the site's pump direction and moved onto it."""
    pl, _ = plan_gen.power((0.5, 0.5), -3, boilers)
    k = ["east", "south", "west", "north"].index(site[2])
    pl = geom.rotate(pl, k)
    pump = next(p for p in pl if p["item"] == "offshore-pump")
    return geom.translate(pl, site[0] - pump["x"], site[1] - pump["y"])


def fits(m, places, taken):
    for t in geom.tiles(places):
        if t in taken or m.t(*t) in "~C#":
            return False
    return True


def silo_interior():
    pl = [{"item": "rocket-silo", "x": 0.5, "y": 0.5}]
    for k, x in enumerate((-2.5, 0.5, 3.5)):
        pl.append({"item": "fast-inserter", "x": x, "y": -4.5, "direction": "south"})
    for x in (-1.5, 2.5):
        pl.append({"item": "fast-inserter", "x": x, "y": 5.5, "direction": "north"})
    xs = range(-4, 6)
    pl += [{"item": "transport-belt", "x": x + .5, "y": -5.5, "direction": "west"} for x in reversed(xs)]
    pl += [{"item": "transport-belt", "x": x + .5, "y": 6.5, "direction": "east"} for x in xs]
    ports = [{"kind": "bundle", "left": "processing-unit", "right": "low-density-structure", "x": 6.5, "y": -5.5, "dir": "west"},
             {"kind": "bundle", "left": "rocket-fuel", "right": None, "x": -4.5, "y": 6.5, "dir": "east"}]
    return interiors.finish(pl, ports)


def build(seed, hours):
    out = mapdata.out_dir(seed)
    if not (out / "map.json").exists():
        d = mapdata.extract(seed)
        d["pumps"] = mapdata.pump_sites(seed)
        (out / "map.json").write_text(json.dumps(d))
    m = mapdata.Map.load(seed)
    gd = gamedata.load()
    dem = demand_mod.solve(hours, gd)
    (out / "demand.json").write_text(json.dumps(dem, indent=1))
    mods = modules.plan(dem)
    log("demand + modules:", len(mods), "modules")
    taken = set()
    fixed, movable, flows = [], [], []
    # ---- mines: columns on the patches, belts flowing toward the iron/copper centre
    anchor = None
    cols = {}
    for code, ore in (("i", "iron-ore"), ("c", "copper-ore")):
        p = mines.patches(m, code)[0]
        c = (sum(x for x, _ in p) / len(p), sum(y for _, y in p) / len(p))
        anchor = c if anchor is None else ((anchor[0] + c[0]) / 2, (anchor[1] + c[1]) / 2)
    need = {mo["ore"]: mo["drills"] for mo in mods if mo["kind"] == "mine"}
    for code, ore in (("i", "iron-ore"), ("c", "copper-ore"), ("s", "stone")):
        cols[ore] = mines.plan_columns(m, code, need.get(ore, 2), anchor, taken)
        for k, col in enumerate(cols[ore]):
            taken |= mines.footprint(col["places"])
            name = f"col-{ore}-{k}"
            x1, y1, x2, y2 = geom.bbox(col["places"])
            fixed.append({"name": name, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "places": col["places"],
                          "tiles": mines.footprint(col["places"]), "col": col, "ore": ore})
        log(ore, sum(c["drills"] for c in cols[ore]), "drills in", len(cols[ore]), "columns")
    # ---- interiors of movable modules
    inner = {}
    for ore, plate in (("iron-ore", "iron-plate"), ("copper-ore", "copper-plate")):
        for k, col in enumerate(cols[ore]):
            fur = math.ceil(col["drills"] * rules.DRILL["electric-mining-drill"] / (1 / rules.SMELT_TIME[plate]))
            inner[f"smelt-{ore}-{k}"] = dict(interiors.smelter_interior(math.ceil(fur / 2), ore), feeds=f"col-{ore}-{k}")
            flows.append((f"col-{ore}-{k}", f"smelt-{ore}-{k}", 8))
    for mo in mods:
        if mo["kind"] == "smelt" and mo["input"] in ("iron-plate", "stone"):
            inner[mo["name"]] = interiors.smelter_interior(max(1, math.ceil(mo["furnaces"] / 2)), mo["input"])
        elif mo["kind"] == "block":
            inner[mo["name"]] = dict(interiors.block_interior(mo, dem), module=mo)
        elif mo["kind"] == "labs":
            inner["labs"] = interiors.labs_interior(mo["labs"], mo["inputs"])
        elif mo["kind"] == "oil":
            units = math.ceil(dem["machines"].get("advanced-oil-processing", 1) - 1e-6)
            rc = {"advanced-oil-processing": 1,
                  "heavy-oil-cracking": max(1, math.ceil(dem["machines"].get("heavy-oil-cracking", 0) / units)),
                  "light-oil-cracking": max(1, math.ceil(dem["machines"].get("light-oil-cracking", 0) / units))}
            for u in range(units):
                inner[f"oil-{u}"] = interiors.oil_interior(rc)
    inner["silo"] = silo_interior()
    log("interiors:", len(inner))
    # ---- power: boilers for the estimated load, at the pump site nearest the anchor
    kw = sum(POWER_KW.get(p["item"], 0) for it in inner.values() for p in it["places"])
    kw += sum(POWER_KW.get(p["item"], 0) for f in fixed for p in f.get("places", []))
    mw = kw / 1000 * 1.3
    boilers = rules.boilers_for(mw)
    sites = sorted(m.pumps, key=lambda s: (s[0] - anchor[0]) ** 2 + (s[1] - anchor[1]) ** 2)
    plant = None
    for s in sites:
        pl = power_plant(s, boilers)
        if fits(m, pl, taken):
            plant, psite = pl, s
            break
    if not plant:
        raise RuntimeError("no shore site fits the steam plant")
    taken |= geom.tiles(plant)
    x1, y1, x2, y2 = geom.bbox(plant)
    fixed.append({"name": "power", "x1": x1 - 1, "y1": y1 - 1, "x2": x2 + 1, "y2": y2 + 1, "places": plant,
                  "tiles": geom.tiles(plant)})
    log(f"power: {mw:.1f} MW -> {boilers} boilers at pump {psite}")
    # water pump for oil and blocks: the next site that does not touch the plant
    wpump = None
    for s in sites:
        p = [{"item": "offshore-pump", "x": s[0], "y": s[1], "direction": s[2]}]
        if fits(m, p, taken) and min(abs(s[0] - q[0]) + abs(s[1] - q[1]) for q in [psite]) > 6:
            wpump = p[0]
            taken |= geom.tiles(p)
            fixed.append({"name": "water", "x1": math.floor(s[0]) - 1, "y1": math.floor(s[1]) - 1,
                          "x2": math.floor(s[0]) + 2, "y2": math.floor(s[1]) + 2, "places": p, "tiles": geom.tiles(p)})
            break
    # ---- pumpjacks on the wells nearest the anchor, until the refineries' crude is covered
    crude_need = sum(1 for n in inner if n.startswith("oil-")) * rules.REFINERY_CRUDE
    have = 0
    for w in sorted(m.wells, key=lambda w: (w[0] - anchor[0]) ** 2 + (w[1] - anchor[1]) ** 2):
        if have >= crude_need:
            break
        pj = [{"item": "pumpjack", "x": w[0], "y": w[1], "direction": "north"}]
        if not fits(m, pj, taken):
            continue
        taken |= geom.tiles(pj)
        fixed.append({"name": f"pj-{len([f for f in fixed if f['name'].startswith('pj')])}", "x1": math.floor(w[0]) - 2,
                      "y1": math.floor(w[1]) - 2, "x2": math.floor(w[0]) + 3, "y2": math.floor(w[1]) + 3,
                      "places": pj, "tiles": geom.tiles(pj)})
        have += w[2] / 30000
    log(f"pumpjacks: {sum(1 for f in fixed if f['name'].startswith('pj'))} for {crude_need} crude/s (have {have:.1f}/s)")
    # ---- raw ores read by blocks (e.g. iron ore for concrete): a small column of their own as the source
    raw_reads = {i for it in inner.values() for p in it["ports"] if p["kind"] == "bundle"
                 for i in (p.get("left"), p.get("right")) if i in ("iron-ore", "copper-ore", "stone")}
    for ore in raw_reads:
        code = {"iron-ore": "i", "copper-ore": "c", "stone": "s"}[ore]
        if ore == "stone":
            continue   # the stone column already has a free trunk (bricks take stone through a merge)
        for k, col in enumerate(mines.plan_columns(m, code, 1, anchor, taken, max_cols=1)):
            taken |= mines.footprint(col["places"])
            x1, y1, x2, y2 = geom.bbox(col["places"])
            fixed.append({"name": f"raw-{ore}-{k}", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "places": col["places"],
                          "tiles": mines.footprint(col["places"]), "col": col, "ore": ore, "raw": True})
    # ---- coal: boilers + stone furnaces + plastic
    furnaces = sum(1 for it in inner.values() for p in it["places"] if p["item"] == "stone-furnace")
    coal_drills = rules.coal_drills_for(boilers, furnaces, 0.2)
    cols["coal"] = mines.plan_columns(m, "k", coal_drills, anchor, taken)
    for k, col in enumerate(cols["coal"]):
        taken |= mines.footprint(col["places"])
        x1, y1, x2, y2 = geom.bbox(col["places"])
        fixed.append({"name": f"col-coal-{k}", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "places": col["places"],
                      "tiles": mines.footprint(col["places"]), "col": col, "ore": "coal"})
    log("coal:", sum(c["drills"] for c in cols["coal"]), "drills for", boilers, "boilers +", furnaces, "furnaces")
    # ---- flows between modules: producer -> consumer of every item / fluid
    prod, cons = {}, {}
    for n, it in inner.items():
        for p in it["ports"]:
            if p["kind"] == "out":
                prod.setdefault(p["item"], set()).add(n)
            elif p["kind"] == "bundle":
                for i in (p.get("left"), p.get("right")):
                    if i:
                        cons.setdefault(i, set()).add(n)
            elif p["kind"] == "fluid":
                cons.setdefault(p["fluid"], set()).add(n)
    for n in inner:
        if n.startswith("oil-"):
            for f in ("petroleum-gas", "light-oil", "heavy-oil"):
                prod.setdefault(f, set()).add(n)
    prod.setdefault("water", set()).add("water")
    prod.setdefault("crude-oil", set()).update(f["name"] for f in fixed if f["name"].startswith("pj"))
    for n in inner:
        if n.startswith("oil-"):
            cons.setdefault("crude-oil", set()).add(n)
            cons.setdefault("water", set()).add(n)
    prod.setdefault("coal", set()).update(f["name"] for f in fixed if f.get("ore") == "coal")
    for item, cs in cons.items():
        for c in cs:
            for p_ in prod.get(item, ()):
                flows.append((p_, c, 1.0))
    for n, it in inner.items():   # smelter units: coal to the column start, ore column end to the unit
        if n.startswith("smelt-"):
            for p_ in prod.get("coal", ()):
                flows.append((p_, n, 0.5))
    movable = [{"name": n, "w": it["w"], "h": it["h"]} for n, it in inner.items()]
    xs = [f["x1"] for f in fixed] + [f["x2"] for f in fixed]
    ys = [f["y1"] for f in fixed] + [f["y2"] for f in fixed]
    area = sum((mo["w"] + 4) * (mo["h"] + 4) for mo in movable)
    pad = int(math.sqrt(area)) // 2 + 20
    window = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    log("placing", len(movable), "modules in window", window, f"(module area {area})")
    pos, status = place.solve(movable, fixed, flows, m, window, seconds=90)
    log("placement", status)
    layout = {"seed": seed, "hours": hours, "fixed": [{k: v for k, v in f.items() if k != "tiles"} for f in fixed],
              "modules": {n: dict(inner[n], x=pos[n][0], y=pos[n][1]) for n in inner}, "flows": flows,
              "window": window, "power": {"mw": mw, "boilers": boilers, "site": psite}, "water_pump": wpump}
    for n, it in layout["modules"].items():
        it.pop("module", None)
    (out / "layout.json").write_text(json.dumps(layout))
    render(m, layout, out / "layout.png")
    return layout


def render(m, layout, path, scale=3):
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = layout["window"]
    img = Image.new("RGB", ((x2 - x1) * scale, (y2 - y1) * scale), (30, 30, 30))
    d = ImageDraw.Draw(img)
    col = {"~": (40, 70, 140), "t": (35, 60, 35), "C": (90, 70, 50)}
    ore = {"i": (90, 110, 140), "c": (150, 90, 60), "k": (15, 15, 15), "s": (130, 120, 90)}
    for y in range(y1, y2):
        for x in range(x1, x2):
            c = col.get(m.t(x, y)) or ore.get(m.o(x, y))
            if c:
                d.rectangle([(x - x1) * scale, (y - y1) * scale, (x - x1 + 1) * scale - 1, (y - y1 + 1) * scale - 1], fill=c)

    def draw(places, dx, dy, fill):
        for p in places:
            for (tx, ty) in geom.footprint(dict(p, x=p["x"] + dx, y=p["y"] + dy)):
                d.rectangle([(tx - x1) * scale, (ty - y1) * scale, (tx - x1 + 1) * scale - 1, (ty - y1 + 1) * scale - 1],
                            fill=fill.get(p["item"], (200, 200, 200)) if isinstance(fill, dict) else fill)
    pal = {"transport-belt": (200, 180, 40), "underground-belt": (230, 120, 30), "pipe": (90, 150, 220),
           "pipe-to-ground": (60, 110, 200), "small-electric-pole": (140, 90, 40)}
    for f in layout["fixed"]:
        draw(f.get("places", []), 0, 0, pal)
    for n, it in layout["modules"].items():
        draw(it["places"], it["x"], it["y"], pal)
        d.rectangle([(it["x"] - x1) * scale, (it["y"] - y1) * scale, (it["x"] + it["w"] - x1) * scale,
                     (it["y"] + it["h"] - y1) * scale], outline=(255, 80, 80))
        d.text(((it["x"] - x1) * scale + 2, (it["y"] - y1) * scale + 2), n, fill=(255, 255, 255))
    img.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--hours", type=float, default=8)
    a = ap.parse_args()
    build(a.seed, a.hours)


if __name__ == "__main__":
    main()
