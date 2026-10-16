// Game-sprite renderer for viewer.html. Sprites come from the local Factorio install through
// viewer.py (/gfx/...), laid out by assets.json (built by viewer_assets.py from `--dump-data`).
// Uses the page's globals: ctx, view, toScreen, toWorld, data.
"use strict";

const SPR = {
  A: null,            // assets manifest
  ready: false,
  imgs: {},
  dirty: true,
  animT: 0,           // seconds of un-paused game time, drives animations
  terrain: {version: 0, names: [], chunks: new Map()},  // key "cx,cy" -> chunk
  trees: [], rocks: [], // flattened from chunks: {x, y, name, v, c}
  tintCache: new Map(),
  groundFiles: new Set(),  // tile + ore textures: chunk images are rebuilt when one finishes loading
};

SPR.load = async function () {
  try {
    const r = await fetch("assets.json");
    if (!r.ok) return;
    SPR.A = await r.json();
    for (const t of Object.values(SPR.A.tiles)) if (t.f) SPR.groundFiles.add(t.f);
    for (const o of Object.values(SPR.A.resources)) for (const l of o.l) SPR.groundFiles.add(l.f);
    SPR.ready = true;
    SPR.pollTerrain();
  } catch (e) { console.warn("no sprites", e); }
};

SPR.img = function (f) {
  let i = SPR.imgs[f];
  if (!i) {
    i = SPR.imgs[f] = new Image();
    i.onload = () => {
      SPR.dirty = true;
      if (SPR.groundFiles.has(f)) for (const c of SPR.terrain.chunks.values()) c.stale = true;  // re-render with the texture
    };
    i.src = "gfx/" + f;
  }
  return i.complete && i.naturalWidth ? i : null;
};

// ---------------------------------------------------------------- terrain chunks

SPR.pollTerrain = async function () {
  try {
    const r = await fetch("terrain.json?since=" + SPR.terrain.version, {cache: "no-store"});
    const t = await r.json();
    SPR.terrain.names = t.names;
    if (t.chunks.length) {
      for (const c of t.chunks) {
        // tiles: run-length "gid*count,..." over 32x32, row by row
        const arr = new Int16Array(1024).fill(-1);
        let i = 0;
        for (const part of c.tiles.split(",")) {
          const [g, n] = part.split("*").map(Number);
          arr.fill(g, i, i + n); i += n;
        }
        c.arr = arr;
        const prev = SPR.terrain.chunks.get(c.cx + "," + c.cy);
        c.canvas = prev && prev.canvas;  // keep showing the old image until the rebuild
        c.stale = true;
        SPR.terrain.chunks.set(c.cx + "," + c.cy, c);
      }
      SPR.flattenObjects();
      SPR.dirty = true;
    }
    SPR.terrain.version = t.version;
  } catch (e) { /* viewer restarting */ }
  setTimeout(SPR.pollTerrain, 2000);
};

SPR.flattenObjects = function () {
  const N = SPR.terrain.names, trees = [], rocks = [];
  for (const c of SPR.terrain.chunks.values()) {
    for (let i = 0; i < c.trees.length; i += 5) trees.push({x: c.trees[i + 1], y: c.trees[i + 2], name: N[c.trees[i]], v: c.trees[i + 3], c: c.trees[i + 4]});
    for (let i = 0; i < c.rocks.length; i += 4) rocks.push({x: c.rocks[i + 1], y: c.rocks[i + 2], name: N[c.rocks[i]], v: c.rocks[i + 3]});
  }
  SPR.trees = trees; SPR.rocks = rocks;
};

function hash2(x, y) {
  let n = (Math.imul(x, 374761393) + Math.imul(y, 668265263)) | 0;
  n = Math.imul(n ^ (n >>> 13), 1274126177);
  return (n ^ (n >>> 16)) >>> 0;
}
const mod = (a, n) => ((a % n) + n) % n;
function mapColor(c) {
  if (!c) return "#2a2a2a";
  let r, g, b;
  if (Array.isArray(c)) [r, g, b] = c; else ({r, g, b} = c);
  if (r <= 1 && g <= 1 && b <= 1) { r *= 255; g *= 255; b *= 255; }
  return `rgb(${r | 0},${g | 0},${b | 0})`;
}

// draw one tile (world x,y) into context g at pixel rect
function drawTile(g, name, x, y, px, py, size) {
  const T = SPR.A.tiles[name];
  if (!T) return;
  const im = T.f && SPR.img(T.f);
  if (!im) { g.fillStyle = mapColor(T.map); g.fillRect(px, py, size, size); return; }
  let sx, sy;
  if (T.block === 8) {
    const v = hash2(Math.floor(x / 8), Math.floor(y / 8)) % T.n;
    sx = v * 512 + mod(x, 8) * 64; sy = mod(y, 8) * 64;
  } else {
    sx = (hash2(x, y) % T.n) * 64; sy = T.y || 0;
  }
  g.drawImage(im, sx, sy, 64, 64, px, py, size + 0.5, size + 0.5);
}

function oreStage(R, amount) {
  const st = R.stages || [0];
  for (let i = 0; i < st.length; i++) if (amount >= st[i]) return i;
  return st.length - 1;
}
// ore sprite: 2x2 tiles centred on the tile, frame = stage, row = variation
function drawOre(g, name, x, y, amount, toPx, scale) {
  const R = SPR.A.resources[name];
  const l = R && R.l[0];
  const im = l && SPR.img(l.f);
  if (!im) return;
  const stage = oreStage(R, amount), varn = hash2(x, y) % (l.vc || 1);
  const tw = l.w * l.sc / 32, th = l.h * l.sc / 32;
  const [px, py] = toPx(x + 0.5 + l.sx - tw / 2, y + 0.5 + l.sy - th / 2);
  g.drawImage(im, stage * l.w, varn * l.h, l.w, l.h, px, py, tw * scale, th * scale);
}

const CHUNK_PX = 16;  // px per tile in cached chunk images (used when zoomed out)
SPR.chunkCanvas = function (c) {
  c.stale = false;
  const pad = 1, n = 32 + 2 * pad, cv = c.canvas || document.createElement("canvas");
  cv.width = cv.height = n * CHUNK_PX;
  const g = cv.getContext("2d");
  const x0 = c.cx * 32, y0 = c.cy * 32, N = SPR.terrain.names;
  for (let j = 0; j < 32; j++) for (let i = 0; i < 32; i++) {
    const gid = c.arr[j * 32 + i];
    if (gid >= 0) drawTile(g, N[gid], x0 + i, y0 + j, (i + pad) * CHUNK_PX, (j + pad) * CHUNK_PX, CHUNK_PX);
  }
  const toPx = (wx, wy) => [(wx - x0 + pad) * CHUNK_PX, (wy - y0 + pad) * CHUNK_PX];
  for (let k = 0; k < c.res.length; k += 4) drawOre(g, N[c.res[k]], c.res[k + 1], c.res[k + 2], c.res[k + 3], toPx, CHUNK_PX);
  c.canvas = cv;
  return cv;
};

SPR.drawGround = function (W, H) {
  const s = view.s, chunks = SPR.terrain.chunks;
  if (!chunks.size) return false;
  const [wx1, wy1] = toWorld(0, 0), [wx2, wy2] = toWorld(W, H);
  ctx.imageSmoothingEnabled = true;
  if (s >= 24) {
    // zoomed in: draw the visible tiles and ore straight from the textures (sharp)
    const N = SPR.terrain.names;
    for (let y = Math.floor(wy1); y <= wy2; y++) for (let x = Math.floor(wx1); x <= wx2; x++) {
      const c = chunks.get(Math.floor(x / 32) + "," + Math.floor(y / 32));
      if (!c) continue;
      const gid = c.arr[mod(y, 32) * 32 + mod(x, 32)];
      if (gid < 0) continue;
      const [px, py] = toScreen(x, y);
      drawTile(ctx, N[gid], x, y, px, py, s);
    }
    for (const c of chunks.values()) {
      if ((c.cx + 1) * 32 < wx1 - 1 || c.cx * 32 > wx2 + 1 || (c.cy + 1) * 32 < wy1 - 1 || c.cy * 32 > wy2 + 1) continue;
      for (let k = 0; k < c.res.length; k += 4) {
        const x = c.res[k + 1], y = c.res[k + 2];
        if (x < wx1 - 2 || x > wx2 + 1 || y < wy1 - 2 || y > wy2 + 1) continue;
        drawOre(ctx, N[c.res[k]], x, y, c.res[k + 3], toScreen, s);
      }
    }
  } else {
    let built = 0;
    for (const c of chunks.values()) {
      if ((c.cx + 1) * 32 < wx1 || c.cx * 32 > wx2 || (c.cy + 1) * 32 < wy1 || c.cy * 32 > wy2) continue;
      if (c.stale) {
        if (built < 4) { SPR.chunkCanvas(c); built++; }  // spread rebuilds over frames
        else SPR.dirty = true;
      }
      if (!c.canvas) continue;
      const [px, py] = toScreen(c.cx * 32 - 1, c.cy * 32 - 1);
      ctx.drawImage(c.canvas, px, py, 34 * s, 34 * s);
    }
  }
  return true;
};

// ---------------------------------------------------------------- sprite layers

// draw a layer (from the manifest) centred on world (cx, cy)
function drawLayer(l, cx, cy, frame, dir, opts) {
  const im = SPR.img(l.f);
  if (!im) return false;
  const fc = l.fc || 1, dc = l.dc || 1;
  const ll = l.ll || (fc > 1 ? fc : dc);
  const idx = (dir % dc) * fc + (fc > 1 ? frame % fc : 0);
  const sx = (l.x || 0) + (idx % ll) * l.w, sy = (l.y || 0) + Math.floor(idx / ll) * l.h;
  const tw = l.w * l.sc / 32, th = l.h * l.sc / 32, s = view.s;
  const [px, py] = toScreen(cx + l.sx - tw / 2, cy + l.sy - th / 2);
  ctx.drawImage(opts && opts.tinted || im, sx, sy, l.w, l.h, px, py, tw * s, th * s);
  return true;
}

function drawLayers(ls, cx, cy, frame, dir, pass) {
  if (!ls) return;
  for (const l of ls) {
    if (!!l.sh !== (pass === "shadow")) continue;
    drawLayer(l, cx, cy, l.fc > 1 ? Math.floor(frame * (l.as || 1)) : 0, dir);
  }
}

const dir4 = d => Math.round((d || 0) / 4) % 4;
const BELT_STRAIGHT = [3, 1, 4, 2];                 // N, E, S, W (1-based rows of the belt sheet)
// sheet rows are named by the side the belt enters from ("east_to_north" = enters from the east)
const BELT_CURVE = {left: [7, 6, 10, 11], right: [5, 9, 12, 8]};
const BELT_START = [17, 19, 13, 15], BELT_END = [18, 20, 14, 16];
const PIPE = {0: "straight_vertical_single", 1: "ending_up", 2: "ending_right", 4: "ending_down", 8: "ending_left",
  5: "straight_vertical", 10: "straight_horizontal", 3: "corner_up_right", 9: "corner_up_left", 6: "corner_down_right",
  12: "corner_down_left", 7: "t_right", 13: "t_left", 11: "t_up", 14: "t_down", 15: "cross"};

function beltRow(E, cx, cy, row, frame) {
  if (E.belt && E.belt[0]) drawLayer(E.belt[0], cx, cy, frame, row - 1);
}

const WORKING = new Set(["working", "normal", "low_power"]);

// returns false if the entity has no sprite (caller draws a box)
SPR.drawEntity = function (e, pass) {
  const E = SPR.A.entities[e.n];
  if (!E) return false;
  const d = dir4(e.d), running = !data.paused;
  const working = running && WORKING.has(e.s);
  const t = SPR.animT * 60;
  const frame = working ? Math.floor(t) : 0;
  if (E.type === "transport-belt") {
    if (pass === "shadow") return true;
    const row = e.bs && e.bs !== "straight" ? BELT_CURVE[e.bs][d] : BELT_STRAIGHT[d];
    beltRow(E, e.x, e.y, row, running ? Math.floor(t * (E.belt_speed || 1)) : 0);
    return true;
  }
  if (E.type === "underground-belt") {
    const input = e.bt !== "output";
    if (pass !== "shadow") beltRow(E, e.x, e.y, input ? BELT_END[d] : BELT_START[d], running ? Math.floor(t * (E.belt_speed || 1)) : 0);
    drawLayers((input ? E.in : E.out)[d], e.x, e.y, 0, 0, pass);
    return true;
  }
  if (E.type === "splitter") {
    if (pass !== "shadow") {
      const off = (d % 2 === 0) ? [0.5, 0] : [0, 0.5], bf = running ? Math.floor(t * (E.belt_speed || 1)) : 0;
      beltRow(E, e.x - off[0], e.y - off[1], BELT_STRAIGHT[d], bf);
      beltRow(E, e.x + off[0], e.y + off[1], BELT_STRAIGHT[d], bf);
    }
    drawLayers(E.dirs[d], e.x, e.y, frame, 0, pass);
    return true;
  }
  if (E.type === "inserter") {
    drawLayers(E.dirs[d], e.x, e.y, 0, 0, pass);
    if (pass !== "shadow" && E.hand_base[0]) {
      // arm resting toward the drop side (items move opposite to the inserter's direction)
      const ang = (d * 90 + 180) * Math.PI / 180, s = view.s, [px, py] = toScreen(e.x, e.y - 0.15);
      const hb = E.hand_base[0], hand = E.hand[0], len = 0.75;
      ctx.save(); ctx.translate(px, py); ctx.rotate(ang);
      const im = SPR.img(hb.f), him = hand && SPR.img(hand.f);
      if (im) ctx.drawImage(im, 0, 0, hb.w, hb.h, -hb.w * hb.sc / 64 * s, -len * s, hb.w * hb.sc / 32 * s, len * s);
      if (him) { const hw = hand.w * hand.sc / 32 * s, hh = hand.h * hand.sc / 32 * s; ctx.drawImage(him, 0, 0, hand.w, hand.h, -hw / 2, -len * s - hh * 0.6, hw, hh); }
      ctx.restore();
    }
    return true;
  }
  if (E.type === "pipe") {
    const ls = E.pipe[PIPE[e.pc || 0]] || E.pipe.straight_vertical_single;
    drawLayers(ls, e.x, e.y, 0, 0, pass);
    return true;
  }
  if (E.type === "lab") {
    drawLayers((working && E.on ? E.on : E.dirs)[d], e.x, e.y, frame, 0, pass);
    return true;
  }
  if (E.dirs) {
    const ls = E.dirs[d] && E.dirs[d].length ? E.dirs[d] : E.dirs[0];
    if (!ls || !ls.length) return false;
    drawLayers(ls, e.x, e.y, frame, 0, pass);
    if (pass !== "shadow" && e.r && view.s >= 10) SPR.drawIcon("recipe:" + e.r, e.x, e.y, Math.min(1.2, (e.w || 2) * 0.45));
    return true;
  }
  if (pass !== "shadow" && E.icon) return SPR.drawIcon(null, e.x, e.y, Math.min(e.w || 1, e.h || 1) * 0.8, E.icon);
  return false;
};

// alt-mode style icon on a dark disc
SPR.drawIcon = function (key, x, y, sizeTiles, icon) {
  const I = icon || SPR.A.icons[key];
  const im = I && SPR.img(I.f);
  if (!im) return false;
  const s = view.s, sz = sizeTiles * s, [px, py] = toScreen(x, y);
  if (!icon) { ctx.fillStyle = "#000000a0"; ctx.beginPath(); ctx.arc(px, py, sz * 0.62, 0, 7); ctx.fill(); }
  ctx.drawImage(im, 0, 0, I.sz, I.sz, px - sz / 2, py - sz / 2, sz, sz);
  return true;
};

// ---------------------------------------------------------------- trees, rocks, characters

function tintedFrame(l, rgb) {
  const key = l.f + "|" + rgb.join(",");
  let c = SPR.tintCache.get(key);
  if (c) return c;
  const im = SPR.img(l.f);
  if (!im) return null;
  c = document.createElement("canvas"); c.width = l.w; c.height = l.h;
  const g = c.getContext("2d");
  g.drawImage(im, l.x || 0, l.y || 0, l.w, l.h, 0, 0, l.w, l.h);
  g.globalCompositeOperation = "multiply"; g.fillStyle = `rgb(${rgb})`; g.fillRect(0, 0, l.w, l.h);
  g.globalCompositeOperation = "destination-in"; g.drawImage(im, l.x || 0, l.y || 0, l.w, l.h, 0, 0, l.w, l.h);
  SPR.tintCache.set(key, c);
  return c;
}

SPR.drawTree = function (t, pass) {
  if (pass === "shadow" && view.s < 8) return;
  const T = SPR.A.trees[t.name];
  if (!T || !T.v.length) return;
  const V = T.v[(t.v - 1) % T.v.length];
  if (pass === "shadow") { if (V.shadow) drawLayer(V.shadow[0], t.x, t.y, 0, 0); return; }
  if (V.trunk) drawLayer(V.trunk[0], t.x, t.y, 0, 0);
  if (V.leaves && V.leaves[0]) {
    const l = V.leaves[0], col = T.colors.length ? T.colors[(t.c - 1) % T.colors.length] : null;
    const tinted = col && tintedFrame(l, col);
    if (tinted) {
      const tw = l.w * l.sc / 32, th = l.h * l.sc / 32, [px, py] = toScreen(t.x + l.sx - tw / 2, t.y + l.sy - th / 2);
      ctx.drawImage(tinted, px, py, tw * view.s, th * view.s);
    } else drawLayer(l, t.x, t.y, 0, 0);
  }
};

SPR.drawRock = function (r, pass) {
  const R = SPR.A.rocks[r.name];
  if (!R || !R.length) return;
  drawLayers(R[(r.v - 1) % R.length], r.x, r.y, 0, 0, pass);
};

SPR.drawCharacter = function (a, pass) {
  const C = SPR.A.entities.character;
  if (!C || !a.position) return false;
  const anim = a._moving ? C.running : a.mining ? C.mining : C.idle;
  const l0 = anim && anim[0];
  const frame = Math.floor(SPR.animT * 60 * ((l0 && l0.as) || 0.2));
  for (const l of anim || []) {
    if (!!l.sh !== (pass === "shadow")) continue;
    drawLayer(l, a.position.x, a.position.y, frame, a._dir8 || 0);
  }
  return true;
};
