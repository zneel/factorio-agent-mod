"""Stage 5b: put the bus complex on the map and connect it to the world.

- complex: the free spot for its footprint (pieces and module bodies plus a margin, not its bounding box; 4
  rotations, FFT correlation with water/cliffs/mines) with the bus head nearest the raw sources (column ends)
- ore units: per iron/copper drill column, a smelting column placed near the column end; ore belt column->smelter
- raw trunks: smelter plates, the stone column, the raw iron-ore column -> their bus head entries (extra sources
  side-load onto the first trunk)
- coal tree: coal columns -> every ore column start (far-lane side-load stub), the boilers' belt, the bus head
- steam plant at the shore; water pipe pump -> complex inlet; crude pipe pumpjacks -> complex inlet
"""
import math

import numpy as np

import plan_nx
from auto import geom, interiors, mines, rules
from auto.route import DV, LEFT, OPP, T, World, add_spans, route_belt, splitter_tap, step


def rot_tile(t, k):
    """Rotate a tile (the unit square at x, y) k quarter turns clockwise about the origin: (x, y) -> (-y-1, x)."""
    x, y = t
    for _ in range(k % 4):
        x, y = -y - 1, x
    return (x, y)


def rot_dir(d, k):
    for _ in range(k % 4):
        d = geom.ROT[d]
    return d


def transform(places, k, dx, dy):
    """Rotate k quarter turns about (0, 0) then shift by (dx, dy). Tile-centre coordinates rotate with the grid:
    a tile (x, y) maps to rot_tile; entity centres are rotated as points and shifted."""
    return geom.translate(geom.rotate(places, k), dx, dy)


MARGIN = 2   # free tiles kept around everything the complex occupies
APPROACH = 8   # free tiles north of each bus head entry (the trunks come in there)


def footprint(cx, margin=MARGIN):
    """Local tiles the complex needs: its pieces and module bodies grown by margin, the trunks' approach to the bus
    head and the ground around the fluid inlets."""
    from scipy.ndimage import binary_dilation
    occ = set(geom.tiles(cx["places"]))
    for n, (x, y, side) in cx["bus"]["modules"].items():
        it = cx["bus"]["inner"][n]
        occ |= {(x + i, y + j) for i in range(it["w"]) for j in range(it["h"])}
    for hx, hy in cx["head"].values():
        occ |= {(hx + i, hy - j) for i in (-1, 0, 1) for j in range(APPROACH)}
    for ix, iy in (cx.get("inlets") or {}).values():
        occ |= {(ix + i, iy + j) for i in range(-3, 4) for j in range(-3, 4)}
    x1, y1 = min(x for x, _ in occ), min(y for _, y in occ)
    x2, y2 = max(x for x, _ in occ), max(y for _, y in occ)
    a = np.zeros((y2 - y1 + 1 + 2 * margin, x2 - x1 + 1 + 2 * margin), dtype=bool)
    for x, y in occ:
        a[y - y1 + margin, x - x1 + margin] = True
    a = binary_dilation(a, np.ones((2 * margin + 1, 2 * margin + 1), dtype=bool))
    ys, xs = np.nonzero(a)
    return {(int(x) + x1 - margin, int(y) + y1 - margin) for x, y in zip(xs, ys)}


def place_complex(m, cx, avoid, target, inlets=None):
    """Best (k, dx, dy) for the complex: every tile of its footprint (not its whole bounding box) on buildable
    ground (trees are fine), away from `avoid` tiles; minimise the distance from the bus head to `target` (a point,
    or a list of points: the sum of their distances), plus each fluid inlet's distance to its nearest source.
    All offsets of the 4 rotations at once: an FFT correlation of the footprint with the bad ground."""
    from scipy.ndimage import distance_transform_cdt
    from scipy.signal import fftconvolve
    W, H = m.w, m.h
    bad = np.zeros((H, W), dtype=np.float32)
    for r, row in enumerate(m.terrain):
        for c, ch in enumerate(row):
            if ch in "~C":
                bad[r, c] = 1
    for (x, y) in avoid:
        if 0 <= y - m.y0 < H and 0 <= x - m.x0 < W:
            bad[y - m.y0, x - m.x0] = 1
    tg = [target] if isinstance(target[0], (int, float)) else list(target)
    head_local = (sum(t[0] for t in cx["head"].values()) / len(cx["head"]), -1)
    near = {}   # fluid -> taxicab distance of every map tile to its nearest source (pump site / well)
    for f, pts in (("water", m.pumps), ("crude-oil", m.wells)):
        if inlets and pts:
            z = np.ones((H, W), dtype=np.int32)
            for q in pts:
                if 0 <= int(q[1]) - m.y0 < H and 0 <= int(q[0]) - m.x0 < W:
                    z[int(q[1]) - m.y0, int(q[0]) - m.x0] = 0
            near[f] = distance_transform_cdt(z, metric="taxicab")
    fp = footprint(cx)
    best = None
    for k in range(4):
        pts = [rot_tile(t, k) for t in fp]
        x1, y1 = min(x for x, _ in pts), min(y for _, y in pts)
        x2, y2 = max(x for x, _ in pts), max(y for _, y in pts)
        if x2 - x1 + 1 > W or y2 - y1 + 1 > H:
            continue
        mask = np.zeros((y2 - y1 + 1, x2 - x1 + 1), dtype=np.float32)
        for x, y in pts:
            mask[y - y1, x - x1] = 1
        hit = fftconvolve(bad, mask[::-1, ::-1], mode="valid")   # [oy, ox]: bad tiles under the mask placed there
        oy, ox = np.nonzero(hit < 0.5)
        if not len(oy):
            continue
        dx, dy = ox + m.x0 - x1, oy + m.y0 - y1
        hx, hy = rot_tile(head_local, k)
        dist = sum(np.abs(hx + dx - a) + np.abs(hy + dy - b) for a, b in tg).astype(np.float64)
        for f, d in near.items():   # the fluid side should face water and oil
            ix, iy = rot_tile(inlets[f], k)
            cy, cx_ = np.clip(iy + dy - m.y0, 0, H - 1), np.clip(ix + dx - m.x0, 0, W - 1)
            dist += d[cy, cx_]
        i = int(np.argmin(dist))
        if best is None or dist[i] < best[0]:
            best = (float(dist[i]), k, int(dx[i]), int(dy[i]))
    if not best:
        raise RuntimeError("no free site for the bus complex")
    return best[1:]


def free_rect(world, w, h, near, margin=2):
    """Top-left of the nearest free w x h rectangle (with margin) around `near`."""
    for r in range(0, 120):
        for ox in range(-r, r + 1):
            for oy in range(-r, r + 1):
                if max(abs(ox), abs(oy)) != r:
                    continue
                x0, y0 = near[0] + ox, near[1] + oy
                if all(world.free(x, y) and (x, y) not in world.reserved
                       for x in range(x0 - margin, x0 + w + margin) for y in range(y0 - margin, y0 + h + margin)):
                    return (x0, y0)
    return None
