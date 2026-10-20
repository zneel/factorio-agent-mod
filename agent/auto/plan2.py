"""Seed -> bus factory plan (stage driver). Bus complex first (local), then site + global links."""
import collections
import json
import math
import os
import sys
import time
from pathlib import Path

if os.environ.get("PYTHONHASHSEED") != "0":   # set iteration order must not change between runs
    import subprocess
    os.environ["PYTHONHASHSEED"] = "0"
    sys.exit(subprocess.call([sys.executable, "-m", "auto.plan2"] + sys.argv[1:], cwd=str(Path(__file__).resolve().parent.parent)))
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from auto import bus, demand as demand_mod, gamedata, geom, interiors, mapdata, modules  # noqa: E402
from auto.factory import silo_interior  # noqa: E402

RAW = ["iron-plate", "copper-plate", "coal", "stone", "iron-ore"]
TAG = ""   # --tag T: read demand.T.json, write plan.T.json, complex.T.pkl, ... (an offline re-plan beside the live one)


def fname(name):
    """runtime file name with the run's tag: plan.json -> plan.T.json."""
    if not TAG:
        return name
    stem, ext = name.rsplit(".", 1)
    return f"{stem}.{TAG}.{ext}"
ZONE_STEP = 6   # a module whose connectors or pipes fail gets this much wider a zone (bus.ZONES), up to 4 times


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class NoLayout(Exception):
    """A block of the partition has no buildable layout: its fluid recipes go to separate blocks (modules.APART)."""

    def __init__(self, mo, e):
        super().__init__(f"{mo['name']}: {e}")
        self.mo = mo


def apart(mo, dem):
    """Pairs of mo's recipes to keep apart: the fluid ones (fluid machines sit at row ends, their pipes come from
    outside), else every pair."""
    fl = [r for r in mo["recipes"] if any(i in modules.FLUIDS for i, _ in dem["recipes"][r]["ing"] + dem["recipes"][r]["prod"])]
    rs = fl if len(fl) > 1 else list(mo["recipes"])
    return {(a, b) for a in rs for b in rs if a < b}


def lays_out(mo, r, dem):
    """Whether recipe r of block mo, alone in a block with its count (+ modules.BUMP), has a layout."""
    rec = dem["recipes"][r]
    made = {p for p, _ in rec["prod"]}
    one = {"name": f"{mo['name']}-{r}", "kind": "block", "recipes": {r: mo["recipes"][r]},
           "inputs": sorted({i for i, _ in rec["ing"]} - made - modules.FLUIDS),
           "fluids": sorted({i for i, _ in rec["ing"]} & modules.FLUIDS),
           "exports": sorted(made & set(mo["exports"])) or sorted(made), "chest": [], "mall": []}
    try:
        interiors.block_interior(one, dem, 0)
        return True
    except RuntimeError:
        return False


def bus_modules(dem, skips=None):
    """Module interiors; skips[name] asks that module for its n-th buildable variant (the repair loop)."""
    skips = skips or {}
    mods = modules.plan(dem)
    inner = {}
    for mo in mods:
        if mo["kind"] == "smelt" and mo["input"] in ("iron-plate", "stone"):
            it = interiors.smelter_interior(max(1, math.ceil(mo["furnaces"] / 2)), mo["input"])
            for p in it["ports"]:
                p.pop("fed", None)
            inner[mo["name"]] = it
        elif mo["kind"] == "block":
            try:
                inner[mo["name"]] = interiors.block_interior(mo, dem, skips.get(mo["name"], 0))
            except RuntimeError as e:
                raise NoLayout(mo, e)
        elif mo["kind"] == "labs":
            inner["labs"] = interiors.labs_interior(mo["labs"], mo["inputs"])
        elif mo["kind"] == "oil":
            units = math.ceil(dem["machines"].get("advanced-oil-processing", 1) * modules.MARGIN - 1e-6)   # same build margin as every block
            rc = {"advanced-oil-processing": 1, "lubricant": 1 if "lubricant" in dem["recipes"] else 0,
                  "heavy-oil-cracking": max(1, math.ceil(dem["machines"].get("heavy-oil-cracking", 0) / units)),
                  "light-oil-cracking": max(1, math.ceil(dem["machines"].get("light-oil-cracking", 0) / units))}
            for u in range(units):
                inner[f"oil-{u}"] = interiors.oil_interior(rc, skips.get(f"oil-{u}", 0))
    if "sulfuric-acid" in dem["recipes"]:
        inner["acid"] = interiors.acid_interior(skips.get("acid", 0))
    inner["silo"] = silo_interior()
    return inner


def tap_priority(inner, dem):
    """Bus order priority of each module: the leaves (their item exports, if any, feed only the labs or the silo,
    the rest of their output fills mall chests; no fluid output) after all others, then by science tier (red-only
    and none share a class, as blocks do), within a tier the smelters after the blocks. Taps nearer the bus head get
    scarce plates first (the trunk's iron is one belt): circuits and gears before the steel smelter (its output
    mostly fills its long bus column at first), chest fillers and packs last (only labs wait)."""
    tier = dem.get("tier", {})
    sinks = {"labs", "silo"}
    reads = {}   # item -> modules with a bundle port that reads it
    for n, it in inner.items():
        for p_ in it["ports"]:
            if p_["kind"] == "bundle":
                for i in (p_.get("left"), p_.get("right")):
                    if i:
                        reads.setdefault(i, set()).add(n)
    out = {}
    for n, it in inner.items():
        rs = {q["recipe"] for q in it["places"] if q.get("recipe")}
        smelter = n.startswith("smelt-")
        if smelter:
            rs.add(n[len("smelt-"):])
        exports = {p_["item"] for p_ in it["ports"] if p_["kind"] == "out"}
        fluid_out = it.get("networks") or any(p_["kind"] == "fluid" and p_["io"] == "output" for p_ in it["ports"])
        leaf = not fluid_out and all(reads.get(i, set()) <= sinks for i in exports)
        out[n] = (leaf, max([1] + [tier.get(r, 0) for r in rs]), smelter)
    return out


def culprit(fail, inner):
    """The module a connection failure belongs to: "B6 bundle 1 ...", "port water acid (x, y)",
    "oil link oil-0 crude-oil", "B2 out plastic-bar", "B2 second export ..."."""
    w = fail.split()
    if w[0] == "port" and len(w) > 2 and w[2] in inner:
        return w[2]
    if w[0] == "oil" and len(w) > 2 and w[2] in inner:
        return w[2]
    if w[0] in inner:
        return w[0]
    return None


def render_local(cx, path, scale=3):
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = cx["box"]
    img = Image.new("RGB", ((x2 - x1) * scale, (y2 - y1) * scale), (30, 30, 30))
    d = ImageDraw.Draw(img)
    pal = {"transport-belt": (200, 180, 40), "underground-belt": (230, 120, 30), "splitter": (255, 255, 255),
           "pipe": (90, 150, 220), "pipe-to-ground": (60, 110, 200), "small-electric-pole": (140, 90, 40)}
    for p in cx["places"]:
        for (tx, ty) in geom.footprint(p):
            d.rectangle([(tx - x1) * scale, (ty - y1) * scale, (tx - x1 + 1) * scale - 1, (ty - y1 + 1) * scale - 1],
                        fill=pal.get(p["item"], (170, 170, 170)))
    for n, (x, y, side) in cx["bus"]["modules"].items():
        d.text(((x - x1) * scale + 2, (y - y1) * scale + 2), n, fill=(255, 80, 80))
    img.save(path)


MINES = False      # --mines: columns only on the patches the run's mine plan chose (mines.json, auto.budget.select)
MINES_REACH = 320  # chosen patches farther from the start ores are the controller's outposts, not plan columns


def mine_boxes(seed, m, anchor):
    """{ore: [box]} of the chosen patches near the start ores, from runtime/auto/<seed>/mines.json (written by the
    growth controller once the scouted map covers the budget); the scouted ground (scout-map.json) joins the map."""
    from auto import scout
    f = mapdata.out_dir(seed) / "mines.json"
    if not f.exists():
        log("--mines: no mines.json yet (the controller writes it once the budget is met)")
        return {}
    scout.load(m, mapdata.out_dir(seed))
    out = {}
    for c in json.loads(f.read_text()).get("chosen", ()):
        if math.dist(c["center"], anchor) <= MINES_REACH:
            out.setdefault(c["name"], []).append(tuple(c["box"]))
    log("--mines: chosen patches", {k: len(v) for k, v in out.items()})
    return out


def world_stage(seed, dem, cx):
    from auto import connect, mines, rules, site
    from auto.factory import POWER_KW
    m = mapdata.Map.load(seed)
    anchor = None
    sp = tuple(m.spawn)
    for code in ("i", "c"):   # the iron and copper patches nearest the spawn
        pt = min((q for q in mines.patches(m, code) if len(q) >= 150),
                 key=lambda q: math.dist(sp, (sum(x for x, _ in q) / len(q), sum(y for _, y in q) / len(q))))
        c = (sum(x for x, _ in pt) / len(pt), sum(y for _, y in pt) / len(pt))
        anchor = c if anchor is None else ((anchor[0] + c[0]) / 2, (anchor[1] + c[1]) / 2)
    raw = dem["raw_per_min"]
    taken, columns = set(), {}
    only = mine_boxes(seed, m, anchor) if MINES else {}
    for key, code, need in (("iron-ore", "i", raw.get("iron-ore", 0) / 30 * 1.35),   # iron also covers steel and belt fill-up
                            ("copper-ore", "c", raw.get("copper-ore", 0) / 30 * 1.15),
                            ("stone", "s", max(2, raw.get("stone", 0) / 30 * 1.5)),
                            ("raw-iron-ore", "i", 1)):
        cols = mines.plan_columns(m, code, math.ceil(need), anchor, taken, max_cols=1 if key.startswith("raw") else 12,
                                  only=only.get({"raw-iron-ore": "iron-ore"}.get(key, key)))
        for c in cols:
            taken |= mines.footprint(c["places"])
        columns[key] = cols
    kw = sum(POWER_KW.get(p["item"], 0) for p in cx["places"]) + 90 * sum(c["drills"] for cs in columns.values() for c in cs)
    furnaces = sum(1 for p in cx["places"] if p["item"] == "stone-furnace") +         sum(math.ceil(c["drills"] * 0.5 * 3.2) for k_ in ("iron-ore", "copper-ore") for c in columns[k_])
    boilers = rules.boilers_for(kw / 1000 * 1.3)
    coal_drills = rules.coal_drills_for(boilers, furnaces, 0.2)
    columns["coal"] = mines.plan_columns(m, "k", coal_drills, anchor, taken, only=only.get("coal"))
    for c in columns["coal"]:
        taken |= mines.footprint(c["places"])
    log("columns:", {k_: sum(c["drills"] for c in v) for k_, v in columns.items()}, f"| {kw / 1000:.1f} MW -> {boilers} boilers")
    avoid = set()
    for (x, y) in taken:
        for ddx in range(-6, 7):
            for ddy in range(-6, 7):
                avoid.add((x + ddx, y + ddy))
    for w in m.wells:
        for ddx in range(-4, 5):
            for ddy in range(-4, 5):
                avoid.add((int(w[0]) + ddx, int(w[1]) + ddy))
    for ore in ("iron-ore", "copper-ore"):   # where connect puts each column's smelter (free_rect from end + 3)
        for c in columns[ore]:
            plate = ore.replace("ore", "plate")
            fur = math.ceil(c["drills"] * rules.DRILL["electric-mining-drill"] * rules.SMELT_TIME[plate])
            sm = interiors.smelter_interior(math.ceil(fur / 2), ore)
            ex, ey = math.floor(c["end"][0]) + 3, math.floor(c["end"][1]) + 3
            avoid |= {(x, y) for x in range(ex - 3, ex + sm["w"] + 3) for y in range(ey - 3, ey + sm["h"] + 3)}
    # the bus head as near as it gets to where the trunks start: every raw source's column ends, on average
    ends = [(sum(c["end"][0] for c in cs) / len(cs), sum(c["end"][1] for c in cs) / len(cs))
            for cs in columns.values() if cs]
    k, dx, dy = site.place_complex(m, cx, avoid, ends, cx.get("inlets"))
    log("complex at rotation", k, "offset", dx, dy)
    # the LP's pumpjacks (bought at the yield the wells keep while pumped); old demand files: the refineries' crude
    # at the initial yield of the poorest well
    wells = math.ceil(dem["pumpjacks"] - 1e-6) if "pumpjacks" in dem else math.ceil(
        sum(1 for p in cx["places"] if p["item"] == "oil-refinery") * 20 / max(1, min(w[2] for w in m.wells) / 30000))
    out, fails, notes, world = connect.connect(m, cx, k, dx, dy, columns, boilers, wells)
    log("world links:", len(out), "pieces;", notes, "fails", fails)
    from auto import poles
    allp = site.transform(cx["places"], k, dx, dy) + out

    def free(t):
        return world.free(*t) and t not in world.reserved
    cov, used = poles.cover(allp, free)
    world.add(cov)
    eng = next(p for p in allp if p["item"] == "steam-engine")
    root = min(((p["x"], p["y"]) for p in allp + cov if "electric-pole" in p["item"]),
               key=lambda q: math.dist(q, (eng["x"], eng["y"])))
    try:
        chains = poles.connect(allp + cov, free, root)
    except RuntimeError as e:
        chains = []
        fails.append(str(e))
    out += geom.tag(cov + chains, "poles")
    log("poles:", len(cov), "cover +", len(chains), "chain")
    # apply removals (a splitter tap replaces a trunk belt) and check that no two entities share a tile
    rem = {(p["item"], p["x"], p["y"]) for p in out if p.get("remove")}
    out = [p for p in out if not p.get("remove") and (p["item"], p["x"], p["y"]) not in rem]
    cplaces = site.transform(cx["places"], k, dx, dy)
    # last layout step: no belt runs past the last inserter that takes from it (auto.trim: items in a tail are
    # never taken); the checks below see the trimmed plan
    from auto import flow as flow_mod, ports, trim
    allp = cplaces + out
    tl = trim.tails(allp, flow_mod.Flow(allp, ports.ports))
    log("belt tails past the last pickup:", len(tl), "belts trimmed",
        dict(collections.Counter(allp[i].get("grp") for i in tl).most_common(6)))
    nc = len(cplaces)
    cplaces = [p for i, p in enumerate(cplaces) if i not in tl]
    out = [p for i, p in enumerate(out) if i + nc not in tl]
    owner = {}
    for src, pl in (("complex", cplaces), ("world", out)):
        for p in pl:
            for t in geom.footprint(p):
                if t in owner:
                    fails.append(f"overlap {t}: {owner[t]} / {src}:{p['item']}")
                owner[t] = f"{src}:{p['item']}"
    log("static check:", sum(1 for f in fails if f.startswith("overlap")), "overlaps")
    from auto import dcheck, fluidseg, pipecheck
    sj = pipecheck.steam_joints(cplaces + out)
    fails += [f"pipe on the steam joint of the engine at {e}: {t}" for e, t in sj]
    log("steam joints:", len(sj), "pipes on engine ends")
    segs = fluidseg.segments(cplaces + out)   # a segment past the game's pipeline extent holds its fluid still
    fails += [f"fluid segment {fluidseg.extent(s)} tiles > {fluidseg.EXTENT} at {fluidseg.bbox(s)}" for s in segs
              if fluidseg.extent(s) > fluidseg.EXTENT]
    log("fluid segments:", len(segs), "| longest", sorted((fluidseg.extent(s) for s in segs), reverse=True)[:6],
        "| pumps", sum(1 for p in cplaces + out if p["item"] == "pump"))
    dw = dcheck.check(cplaces + out)
    fails += ["draftsman " + w_ for w_ in dw]
    log("draftsman check:", len(dw), "warnings", dw[:3])
    plan = {"seed": seed, "complex": cplaces, "world": out,
            "fails": cx["fails"] + fails, "rotation": k, "offset": [dx, dy], "boilers": boilers,
            "techs": dem["techs"]}
    (mapdata.out_dir(seed) / fname("plan.json")).write_text(json.dumps(plan))
    render_world(m, plan, mapdata.out_dir(seed) / fname("plan.png"))
    bad, unl = pipecheck.check(seed, TAG)
    log("pipe check:", len(bad), "mixed fluid joints,", len(unl), "unlabelled pipes")
    from auto.demand import BUILD_SWAP
    placed = {BUILD_SWAP.get(e["item"], e["item"]) for e in plan["complex"] + plan["world"]}
    orphans = sorted(placed - set(dem.get("mall", [])))
    log("mall check:", "every placed item has a mall goal" if not orphans else
        f"NO MALL GOAL for {orphans}: re-run auto.demand then plan2 (the mall is sized from the last plan)")


def render_world(m, plan, path, scale=2):
    from PIL import Image, ImageDraw
    allp = plan["complex"] + plan["world"]
    x1, y1, x2, y2 = geom.bbox(allp)
    x1, y1, x2, y2 = x1 - 10, y1 - 10, x2 + 10, y2 + 10
    img = Image.new("RGB", ((x2 - x1) * scale, (y2 - y1) * scale), (30, 30, 30))
    d = ImageDraw.Draw(img)
    col = {"~": (40, 70, 140), "C": (90, 70, 50)}
    ore = {"i": (70, 90, 120), "c": (130, 80, 50), "k": (10, 10, 10), "s": (110, 100, 80)}
    for y in range(y1, y2):
        for x in range(x1, x2):
            c = col.get(m.t(x, y)) or ore.get(m.o(x, y))
            if c:
                d.rectangle([(x - x1) * scale, (y - y1) * scale, (x - x1 + 1) * scale - 1, (y - y1 + 1) * scale - 1], fill=c)
    pal = {"transport-belt": (200, 180, 40), "underground-belt": (230, 120, 30), "splitter": (255, 255, 255),
           "pipe": (90, 150, 220), "pipe-to-ground": (60, 110, 200), "small-electric-pole": (140, 90, 40)}
    for p in allp:
        if p.get("remove"):
            continue
        for (tx, ty) in geom.footprint(p):
            d.rectangle([(tx - x1) * scale, (ty - y1) * scale, (tx - x1 + 1) * scale - 1, (ty - y1 + 1) * scale - 1],
                        fill=pal.get(p["item"], (170, 170, 170)))
    img.save(path)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--mines" in args:
        args.remove("--mines")
        MINES = True
    if "--tag" in args:
        k_ = args.index("--tag")
        TAG = args[k_ + 1]
        del args[k_:k_ + 2]
    seed = int(args[0])
    if len(args) > 1 and args[1] == "world":   # reuse the cached complex
        import pickle
        dem = json.loads((mapdata.out_dir(seed) / fname("demand.json")).read_text())
        world_stage(seed, dem, pickle.load(open(mapdata.out_dir(seed) / fname("complex.pkl"), "rb")))
        sys.exit(0)
    dem = json.loads((mapdata.out_dir(seed) / fname("demand.json")).read_text())
    bus.MALL = set(dem.get("mall", []))
    skips, cx = {}, None
    for attempt in range(20):   # repair loop: a module the bus can't connect is re-solved as its next variant
        log(f"modules (attempt {attempt}, variants {skips or '-'})...")
        bus.ZONES = {}   # every attempt starts narrow: a widening may only have been needed beside the old variant
        try:
            inner = bus_modules(dem, skips)
        except NoLayout as e:
            alone = [r_ for r_ in e.mo["recipes"] if not lays_out(e.mo, r_, dem)]
            for r_ in alone:     # no layout even alone (measured: 3 processing units fail, 2 and 4 lay out): the next
                n_ = e.mo["recipes"][r_]                               # count that lays out alone
                up = next((k_ for k_ in range(1, 4) if lays_out(dict(e.mo, recipes={r_: n_ + k_}), r_, dem)), 1)
                modules.BUMP[r_] = modules.BUMP.get(r_, 0) + up
            if not alone:
                modules.APART |= apart(e.mo, dem)
            log(f"{e}: re-partition with {sorted(modules.APART)} apart, {modules.BUMP} more machines")
            continue
        bus.PRIO = tap_priority(inner, dem)
        order = bus.order_modules(list(inner), inner, RAW, bus.PRIO)
        log("order", order)
        culprits = {}
        for _ in range(4):   # widen the connector zone of each module whose routes fail, until all fit
            cx = bus.complex_layout(inner, order, RAW)
            log(f"complex zones {bus.ZONES or bus.ZONE}:", len(cx["places"]), "pieces; fails", cx["fails"])
            if not cx["fails"]:
                bus.route_fluids_bus(cx)
                log("fluids: fails", cx["fails"])
            if not cx["fails"]:
                break
            blamed = {culprit(f, inner) for f in cx["fails"]} - {None}
            for m in blamed:
                culprits[m] = culprits.get(m, 0) + 1
                bus.ZONES[m] = bus.ZONES.get(m, bus.ZONE) + ZONE_STEP
            if not blamed:
                break
        if not cx["fails"]:
            break
        if not culprits:
            log("no module to blame for", cx["fails"])
            break
        worst = max(culprits, key=culprits.get)   # the module failing in the most zone widths
        skips[worst] = skips.get(worst, 0) + 1
    if "inlets" not in cx:   # the loop gave up with the complex unconnected: still route its fluids for the report
        bus.route_fluids_bus(cx)
    import pickle
    pickle.dump(cx, open(mapdata.out_dir(seed) / fname("complex.pkl"), "wb"))
    world_stage(seed, dem, cx)
    render_local(cx, mapdata.out_dir(seed) / fname("bus.png"))
