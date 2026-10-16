"""Sprite manifest for viewer.html, built from the local Factorio install. Stdlib only.

Factorio's own `--dump-data` writes data.raw (every prototype, with its sprite sheet layout) as JSON.
This turns it into a small manifest: for each entity, tile, resource, tree and rock, which PNG to
draw and where the frame sits. The PNGs themselves stay in the install and are served to the
local page by viewer.py; nothing is copied.

    python agent/viewer_assets.py      # (re)build runtime/viewer-cache/assets.json
"""

import json
import shutil
import subprocess
from pathlib import Path

import server

CACHE = server.RUNTIME / "viewer-cache"
DUMP = CACHE / "script-output" / "data-raw-dump.json"
MANIFEST = CACHE / "assets.json"

DIR_KEYS = ("north", "east", "south", "west")


def dump_data():
    """Run `factorio --dump-data` in a private write-data dir (never touches the live server)."""
    (CACHE / "mods").mkdir(parents=True, exist_ok=True)
    cfg = CACHE / "config.ini"
    cfg.write_text(f"[path]\nread-data={server.FACTORIO_DIR / 'data'}\nwrite-data={CACHE}\n[other]\ncheck-updates=false\n")
    mods = [{"name": "base", "enabled": True}]
    for dlc in ("elevated-rails", "quality", "space-age"):
        if (server.FACTORIO_DIR / "data" / dlc).exists():
            mods.append({"name": dlc, "enabled": False})
    (CACHE / "mods" / "mod-list.json").write_text(json.dumps({"mods": mods}))
    r = subprocess.run([str(server.FACTORIO_EXE), "--config", str(cfg), "--mod-directory", str(CACHE / "mods"),
                        "--dump-data"], capture_output=True, text=True, errors="replace", timeout=300)
    if not DUMP.exists():
        raise RuntimeError("factorio --dump-data failed:\n" + r.stdout[-2000:])


# ------------------------------------------------------------------ sprite specs -> layers

def _layer(s: dict):
    """One drawable layer: f=file, x/y=sheet offset, w/h=frame size, sc=scale, sx/sy=shift (tiles),
    fc=frame_count, ll=line_length, dc=direction_count, sh=shadow, as=animation_speed."""
    if s.get("draw_as_light") or s.get("draw_as_glow") or s.get("blend_mode") == "additive":
        return None
    if "stripes" in s:  # multi-file sheet: only the first stripe's first frame
        st = s["stripes"][0]
        s = dict(s, filename=st["filename"], frame_count=1, line_length=1)
    if "filenames" in s:
        s = dict(s, filename=s["filenames"][0])
    f = s.get("filename")
    if not f or f.endswith("empty.png"):
        return None
    size = s.get("size")
    w = s.get("width") or (size if isinstance(size, int) else size[0] if size else None)
    h = s.get("height") or (size if isinstance(size, int) else size[1] if size else None)
    if not (w and h):
        return None
    shift = s.get("shift") or [0, 0]
    if isinstance(shift, dict):
        shift = [shift.get("x", 0), shift.get("y", 0)]
    out = {"f": f, "w": w, "h": h, "sc": s.get("scale", 1), "sx": shift[0], "sy": shift[1]}
    for src, dst in (("x", "x"), ("y", "y"), ("frame_count", "fc"), ("line_length", "ll"),
                     ("direction_count", "dc"), ("animation_speed", "as"), ("variation_count", "vc")):
        if s.get(src):
            out[dst] = s[src]
    if s.get("draw_as_shadow"):
        out["sh"] = 1
    if s.get("apply_runtime_tint"):
        return None  # player-colour masks: skipped
    return out


def layers(spec, direction=0):
    """Flatten a Sprite/Animation/Sprite4Way/Animation4Way spec into layers for one direction (0..3)."""
    if not isinstance(spec, dict):
        return []
    if any(k in spec for k in DIR_KEYS) and "filename" not in spec and "layers" not in spec:
        return layers(spec.get(DIR_KEYS[direction]) or spec.get("north"), direction)
    if "sheets" in spec:
        return [l for sh in spec["sheets"] for l in layers({"sheet": sh}, direction)]
    if "sheet" in spec:  # Sprite4Way sheet: the four directions side by side
        out = []
        for l in layers(spec["sheet"], direction):
            frames = spec["sheet"].get("frames", 4)
            l["x"] = l.get("x", 0) + (direction % frames) * l["w"]
            out.append(l)
        return out
    if "layers" in spec:
        return [l for s in spec["layers"] for l in layers(s, direction)]
    l = _layer(spec)
    return [l] if l else []


def dir4(spec):
    return [layers(spec, d) for d in range(4)]


def find_sprite(p: dict):
    """Generic fallback: the first picture/animation-like value in a prototype."""
    for key in ("picture", "pictures", "animation", "animations", "structure", "graphics_set", "base_picture",
                "base_day_sprite", "chargable_graphics", "integration_patch", "sprite", "sprites"):
        v = p.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            for inner in ("animation", "picture", "structure", "idle_animation"):
                if inner in v and isinstance(v[inner], (dict, list)):
                    v = v[inner][0] if isinstance(v[inner], list) else v[inner]
                    break
            if layers(v):
                return v
    return None


def icon_of(p: dict):
    if p.get("icon"):
        return {"f": p["icon"], "sz": p.get("icon_size", 64)}
    if p.get("icons"):
        i = p["icons"][0]
        return {"f": i["icon"], "sz": i.get("icon_size", 64)}
    return None


ENTITY_TYPES = (
    "transport-belt", "underground-belt", "splitter", "inserter", "mining-drill", "furnace", "assembling-machine",
    "lab", "boiler", "generator", "offshore-pump", "pipe", "pipe-to-ground", "electric-pole", "container",
    "logistic-container", "storage-tank", "pump", "radar", "rocket-silo", "solar-panel", "accumulator", "beacon",
    "roboport", "wall", "gate", "ammo-turret", "electric-turret", "fluid-turret", "lamp", "reactor", "heat-pipe",
    "steam-turbine", "heat-exchanger", "constant-combinator", "arithmetic-combinator", "decider-combinator",
    "power-switch", "programmable-speaker", "electric-energy-interface", "simple-entity-with-force",
    "simple-entity-with-owner", "character",
)


def entity_entry(t: str, p: dict) -> dict:
    e = {"type": t}
    icon = icon_of(p)
    if icon:
        e["icon"] = icon
    if t == "transport-belt":
        e["belt"] = layers(p["belt_animation_set"]["animation_set"])
        e["belt_speed"] = p.get("speed", 0.03125) * p.get("animation_speed_coefficient", 32)
    elif t in ("underground-belt", "splitter"):
        e["belt"] = layers(p["belt_animation_set"]["animation_set"])
        e["belt_speed"] = p.get("speed", 0.03125) * p.get("animation_speed_coefficient", 32)
        if t == "underground-belt":
            e["in"] = dir4(p["structure"]["direction_in"])
            e["out"] = dir4(p["structure"]["direction_out"])
        else:
            e["dirs"] = [layers(p["structure"], d) + layers(p.get("structure_patch"), d) for d in range(4)]
    elif t == "inserter":
        e["dirs"] = dir4(p["platform_picture"])
        e["hand_base"] = layers(p["hand_base_picture"])
        e["hand"] = layers(p["hand_open_picture"])
    elif t in ("mining-drill", "furnace", "assembling-machine", "offshore-pump"):
        gs = p.get("graphics_set") or {}
        e["dirs"] = dir4(gs.get("animation") or gs.get("idle_animation") or find_sprite(p))
    elif t == "lab":
        e["dirs"] = dir4(p["off_animation"])
        e["on"] = dir4(p["on_animation"])
    elif t == "boiler":
        e["dirs"] = [layers(p["pictures"][k]["structure"]) for k in DIR_KEYS]
    elif t == "generator":
        v, h = layers(p["vertical_animation"]), layers(p["horizontal_animation"])
        e["dirs"] = [v, h, v, h]
    elif t == "pipe":
        e["pipe"] = {k: layers(v) for k, v in p["pictures"].items()
                     if not k.endswith("visualization") and k.startswith(("straight", "corner", "t_", "cross", "ending"))}
    elif t == "pipe-to-ground":
        e["dirs"] = dir4(p["pictures"])
    elif t == "electric-pole":
        e["dirs"] = [layers(p["pictures"])] * 4
    elif t == "character":
        a = p["animations"][0]
        e["idle"] = layers(a["idle"])
        e["running"] = layers(a["running"])
        e["mining"] = layers(a["mining_with_tool"])
    else:
        sp = find_sprite(p)
        if sp:
            e["dirs"] = dir4(sp)
    return e


def tile_entry(p: dict) -> dict:
    v = p.get("variants") or {}
    out = {"map": p.get("map_color")}
    main = v.get("main")
    if main:
        m = main[0]
        out.update(f=m["picture"], n=m.get("count", 1), y=m.get("y", 0), block=1)
    elif v.get("material_background"):
        m = v["material_background"]
        out.update(f=m["picture"], n=m.get("count", 1), y=0, block=8)  # each variant covers an 8x8 tile block
    return out


def build() -> dict:
    d = json.loads(DUMP.read_text(encoding="utf-8"))
    ents = {}
    for t in ENTITY_TYPES:
        for name, p in d.get(t, {}).items():
            try:
                ents[name] = entity_entry(t, p)
            except (KeyError, TypeError, IndexError) as ex:
                ents[name] = {"type": t, "icon": icon_of(p), "error": str(ex)}
    tiles = {name: tile_entry(p) for name, p in d.get("tile", {}).items()}
    res = {}
    for name, p in d.get("resource", {}).items():
        st = p.get("stages", {})
        sheet = st.get("sheet") or st
        l = layers(sheet)
        res[name] = {"l": l, "stages": p.get("stage_counts", [0]), "map": p.get("map_color")}
    trees = {}
    for name, p in d.get("tree", {}).items():
        vs = []
        for v in p.get("variations") or []:
            vs.append({k: layers(v[k]) for k in ("trunk", "leaves", "shadow") if k in v})
        if not vs and p.get("pictures"):
            vs = [{"trunk": layers(x)} for x in p["pictures"]]
        cols = [[c.get("r", 255), c.get("g", 255), c.get("b", 255)] for c in p.get("colors") or []]
        trees[name] = {"v": vs, "colors": cols}
    rocks = {}
    for name, p in d.get("simple-entity", {}).items():
        pics = p.get("pictures") or p.get("picture") or p.get("animations")
        if isinstance(pics, dict):
            pics = [pics]
        rocks[name] = [layers(x) for x in pics or []]
    icons = {}
    for t in ("item", "fluid", "tool", "capsule", "module", "ammo", "armor", "gun", "item-with-entity-data",
              "rail-planner", "repair-tool", "selection-tool", "blueprint", "spidertron-remote"):
        for name, p in d.get(t, {}).items():
            i = icon_of(p)
            if i:
                icons[name] = i
    for name, p in d.get("recipe", {}).items():  # recipe icon = own icon or main product's
        i = icon_of(p)
        if not i:
            results = p.get("results") or []
            main = p.get("main_product") or (results[0]["name"] if len(results) == 1 else None)
            i = icons.get(main)
        if i:
            icons["recipe:" + name] = i
    return {"entities": ents, "tiles": tiles, "resources": res, "trees": trees, "rocks": rocks, "icons": icons}


def ensure(force: bool = False) -> Path:
    if force or not DUMP.exists() or DUMP.stat().st_mtime < server.FACTORIO_EXE.stat().st_mtime:
        dump_data()
    if force or not MANIFEST.exists() or MANIFEST.stat().st_mtime < DUMP.stat().st_mtime \
            or MANIFEST.stat().st_mtime < Path(__file__).stat().st_mtime:
        MANIFEST.write_text(json.dumps(build(), separators=(",", ":")))
    return MANIFEST


def gfx_path(url_path: str):
    """Map '__base__/graphics/x.png' to the install; None if outside data/ or not a png."""
    if not url_path.endswith(".png") or ".." in url_path:
        return None
    mod, _, rest = url_path.partition("/")
    if not (mod.startswith("__") and mod.endswith("__")):
        return None
    p = (server.FACTORIO_DIR / "data" / mod.strip("_") / rest).resolve()
    data = (server.FACTORIO_DIR / "data").resolve()
    return p if data in p.parents and p.is_file() else None


if __name__ == "__main__":
    m = ensure(force=True)
    j = json.loads(m.read_text())
    errs = {k: v["error"] for k, v in j["entities"].items() if "error" in v}
    print(m, f"{m.stat().st_size // 1024} KB", {k: len(v) for k, v in j.items()}, "errors:", errs)
    shutil.rmtree(CACHE / "temp", ignore_errors=True)
