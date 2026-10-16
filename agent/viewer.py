"""Live browser spectator view of the agent map. Read-only; stdlib only.

    python agent/viewer.py [--port 8765] [--interval 1.5]

then open http://localhost:8765. One background thread polls the server over RCON and caches the
latest snapshot, so any number of open tabs cost the same. It never starts or restarts the server,
never sends action commands, and never reads chat through `chat` (that would move an agent's
read cursor).

With agent-bridge >= 0.1.7 it uses the `snapshot` command (whole base, chat, agent tasks). Against an
older mod it falls back to `agents` + `find` + `state` + `resources` around the agent named by
--anchor: base within 300 tiles of it, no chat. With >= 0.1.8 it also loads the ground (tiles, ore,
trees, rocks) chunk by chunk through `terrain` and draws everything with the game's own sprites,
read from the local Factorio install (see viewer_assets.py).
"""

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge import Bridge, BridgeError
import viewer_assets

HERE = Path(__file__).resolve().parent
PAGE = HERE / "viewer.html"

DIRS = {"north": 0, "northnortheast": 1, "northeast": 2, "eastnortheast": 3, "east": 4,
        "eastsoutheast": 5, "southeast": 6, "southsoutheast": 7, "south": 8, "southsouthwest": 9,
        "southwest": 10, "westsouthwest": 11, "west": 12, "westnorthwest": 13, "northwest": 14,
        "northnorthwest": 15}

# entity types `find` is asked for in fallback mode (it has no force filter, so list buildings only)
BUILDING_TYPES = [
    "transport-belt", "underground-belt", "splitter", "inserter", "mining-drill", "furnace",
    "assembling-machine", "lab", "boiler", "generator", "offshore-pump", "pipe", "pipe-to-ground",
    "electric-pole", "container", "logistic-container", "storage-tank", "pump", "radar", "rocket-silo",
    "solar-panel", "accumulator", "chemical-plant", "oil-refinery", "beacon", "roboport", "wall", "gate",
    "ammo-turret", "electric-turret", "fluid-turret", "lamp", "loader", "rail", "straight-rail",
]


class Terrain:
    """Chunk cache for the `terrain` command. Names are remapped to one global list so the page keeps
    a single lookup table; each chunk carries the version it was (re)loaded at."""

    BATCH = 8          # chunks per RCON call (about 13 ms of game time)
    REFRESH = 60.0     # re-read a chunk after this many seconds (ore depletes, trees get cut)
    MARGIN = 2         # chunks of ground shown around the base

    def __init__(self):
        self.lock = threading.Lock()
        self.names, self.index = [], {}
        self.chunks = {}
        self.version = 0
        self.supported = None
        self.failed_at = 0.0

    def _gid(self, name):
        i = self.index.get(name)
        if i is None:
            i = self.index[name] = len(self.names)
            self.names.append(name)
        return i

    def wanted(self, snap):
        want = set()
        bb = snap.get("bbox")
        if bb:
            for cx in range(bb["x1"] // 32 - self.MARGIN, bb["x2"] // 32 + self.MARGIN + 1):
                for cy in range(bb["y1"] // 32 - self.MARGIN, bb["y2"] // 32 + self.MARGIN + 1):
                    want.add((cx, cy))
        for a in snap.get("agents") or []:
            p = a.get("position")
            if p:
                ax, ay = math.floor(p["x"] / 32), math.floor(p["y"] / 32)
                want.update((ax + dx, ay + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        return want

    def step(self, bridge, snap):
        if self.supported is False and time.time() - self.failed_at < 60:
            return  # older mod; probe again now and then in case the server was upgraded
        want = self.wanted(snap)
        if not want or len(want) > 600:
            return
        cx0 = sum(c[0] for c in want) / len(want)
        cy0 = sum(c[1] for c in want) / len(want)
        batch = sorted((c for c in want if c not in self.chunks),
                       key=lambda c: (c[0] - cx0) ** 2 + (c[1] - cy0) ** 2)[:self.BATCH]
        if not batch:
            now = time.time()
            stale = sorted((ch["at"], k) for k, ch in self.chunks.items() if k in want and now - ch["at"] > self.REFRESH)
            batch = [k for _, k in stale[:2]]
        if not batch:
            return
        try:
            r = bridge.call("terrain", chunks=[list(c) for c in batch])
        except BridgeError as e:
            if "unknown cmd" in str(e):
                self.supported, self.failed_at = False, time.time()
                return
            raise
        self.supported = True
        with self.lock:
            gmap = [self._gid(n) for n in (r.get("names") or [])]

            def remap(flat, stride):
                flat = list(flat) if isinstance(flat, list) else []  # empty Lua table arrives as {}
                for i in range(0, len(flat), stride):
                    flat[i] = gmap[flat[i]]
                return flat

            for ch in r.get("chunks") or []:
                if ch.get("missing"):
                    continue
                runs = []
                for part in (ch.get("tiles") or "").split(","):
                    t, _, n = part.partition("*")
                    runs.append(f"{gmap[int(t)] if int(t) >= 0 else -1}*{n}")
                self.version += 1
                self.chunks[(ch["cx"], ch["cy"])] = {
                    "cx": ch["cx"], "cy": ch["cy"], "v": self.version, "at": time.time(), "tiles": ",".join(runs),
                    "res": remap(ch.get("res"), 4), "trees": remap(ch.get("trees"), 5), "rocks": remap(ch.get("rocks"), 4),
                }

    def since(self, v: int) -> bytes:
        with self.lock:
            chunks = [{k: x for k, x in c.items() if k != "at"} for c in self.chunks.values() if c["v"] > v]
            return json.dumps({"version": self.version, "names": self.names, "chunks": chunks},
                              separators=(",", ":")).encode()


class Poller:
    def __init__(self, interval: float, anchor: str, fast: float, res_every: float = 30.0):
        self.interval, self.anchor, self.fast, self.res_every = interval, anchor, fast, res_every
        self.bridge = Bridge(agent=anchor, autostart=False)
        self.fast_bridge = Bridge(agent=anchor, autostart=False)  # own RCON connection
        self.paused = True
        self.fast_cond = threading.Condition()
        self.fast_seq = 0
        self.fast_data = b"{}"
        self.lock = threading.Lock()
        self.data = json.dumps({"error": "starting up"}).encode()
        self.resources = None
        self.res_at = 0.0
        self.mode = None  # "snapshot" | "fallback"
        self.version = None
        self.terrain = Terrain()

    def loop(self):
        while True:
            t0 = time.time()
            try:
                snap = self.poll()
                snap["viewer"] = {"mode": self.mode, "mod_version": self.version, "polled_ms": int((time.time() - t0) * 1000),
                                  "time": time.time()}
                body = json.dumps(snap, separators=(",", ":")).encode()
                self.paused = bool(snap.get("paused"))
            except (BridgeError, OSError) as e:
                self.mode = None
                self.bridge.rcon = None
                self.paused = True
                body = json.dumps({"error": str(e), "time": time.time()}).encode()
            with self.lock:
                self.data = body
            if self.mode == "snapshot":
                try:
                    self.terrain.step(self.bridge, snap)
                except (BridgeError, OSError):
                    pass
            time.sleep(max(0.2, self.interval - (time.time() - t0)))

    def fast_loop(self):
        """Agent positions only (the cheap `agents` command), every --fast seconds while the game runs.
        Factorio answers RCON once per tick, so the real rate tops out near 60/s."""
        while True:
            t0 = time.time()
            try:
                body = json.dumps({"agents": self.fast_bridge.call("agents"), "time": t0}, separators=(",", ":")).encode()
                with self.fast_cond:
                    self.fast_data = body
                    self.fast_seq += 1
                    self.fast_cond.notify_all()
            except (BridgeError, OSError):
                self.fast_bridge.rcon = None
                time.sleep(1)
            wait = self.fast if not self.paused else 0.25
            time.sleep(max(0.0, wait - (time.time() - t0)))

    def poll(self) -> dict:
        b = self.bridge
        if self.mode != "snapshot" and time.time() - self.res_at > self.res_every:
            self.mode = None  # re-detect: the server may have been restarted with a newer mod
        if self.mode is None:
            self.version = b.call("ping").get("version")
            self.terrain.supported = None  # re-probe: the server may now run a newer mod
            try:
                b.call("snapshot", chat=0)
                self.mode = "snapshot"
            except BridgeError as e:
                if "unknown cmd" not in str(e):
                    raise
                self.mode = "fallback"
        want_res = time.time() - self.res_at > self.res_every
        snap = self.snapshot(want_res) if self.mode == "snapshot" else self.fallback(want_res)
        # Factorio encodes an empty Lua table as {}; the page wants lists
        for k in ("entities", "agents", "chat", "humans_online", "water", "patches"):
            if snap.get(k) == {}:
                snap[k] = []
        if isinstance(snap.get("research"), dict) and snap["research"].get("queue") == {}:
            snap["research"]["queue"] = []
        if want_res and "resources" in snap:
            self.resources = {k: snap.pop(k) for k in ("resources", "water", "patches", "area") if k in snap}
            self.res_at = time.time()
        snap.update(self.resources or {})
        return snap

    def snapshot(self, want_res: bool) -> dict:
        # with `terrain` available the ground comes from there; else the coarse resources layer
        return self.bridge.call("snapshot", chat=30, resources=(want_res and self.terrain.supported is not True) or None)

    def fallback(self, want_res: bool) -> dict:
        b = self.bridge
        agents_raw = b.call("agents")
        agents = [{"name": n, "alive": a.get("alive"), "position": a.get("position"),
                   "task": {"kind": a["task"], "state": "running"} if a.get("task") else None}
                  for n, a in agents_raw.items()]
        st = b.call("state")
        found = b.call("find", type=BUILDING_TYPES, radius=300, limit=20000)
        ents = []
        for e in found.get("nearest", []):
            ents.append({"n": e["name"], "x": e["pos"]["x"], "y": e["pos"]["y"], "d": DIRS.get(e.get("dir")),
                         "s": e.get("status"), "r": e.get("recipe")})
        out = {"tick": st.get("tick"), "paused": st.get("paused"), "mode": st.get("mode"), "speed": st.get("speed"),
               "humans_online": st.get("humans_online"), "entities": ents, "agents": agents, "chat": None,
               "research": st.get("research") or {}}
        if want_res:
            out["patches"] = b.call("resources", radius=300, limit=40).get("patches", [])
            out["resources"] = {}
        return out

    def get(self) -> bytes:
        with self.lock:
            return self.data


def make_handler(poller: Poller, assets):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/events":
                return self.stream()
            cache = "no-store"
            gfx = viewer_assets.gfx_path(path[5:]) if path.startswith("/gfx/") else None
            if path == "/":
                body, ctype = PAGE.read_bytes(), "text/html; charset=utf-8"
            elif path == "/viewer_sprites.js":
                body, ctype = (HERE / "viewer_sprites.js").read_bytes(), "text/javascript; charset=utf-8"
            elif path == "/data.json":
                body, ctype = poller.get(), "application/json"
            elif path == "/terrain.json":
                since = query.partition("since=")[2]
                body, ctype = poller.terrain.since(int(since) if since.isdigit() else 0), "application/json"
            elif path == "/assets.json" and assets:
                body, ctype = assets.read_bytes(), "application/json"
            elif gfx:  # game sprites, straight from the local install
                body, ctype, cache = gfx.read_bytes(), "image/png", "max-age=86400"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def stream(self):
            # server-sent events: one message per fast agent poll
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            try:
                while True:
                    with poller.fast_cond:
                        poller.fast_cond.wait_for(lambda: poller.fast_seq != seq, timeout=15)
                        seq, body = poller.fast_seq, poller.fast_data
                    self.wfile.write(b"data: " + body + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=1.5, help="seconds between RCON polls")
    ap.add_argument("--fast", type=float, default=0.02,
                    help="seconds between agent-position polls while the game is running (streamed to the page)")
    ap.add_argument("--anchor", default="agent", help="existing agent used as the origin in fallback mode")
    args = ap.parse_args()
    try:
        assets = viewer_assets.ensure()
    except Exception as e:  # no install or the dump failed: the page draws plain shapes instead
        print(f"no game sprites ({e})", flush=True)
        assets = None
    poller = Poller(args.interval, args.anchor, args.fast)
    threading.Thread(target=poller.loop, daemon=True).start()
    threading.Thread(target=poller.fast_loop, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(poller, assets))
    print(f"viewer on http://localhost:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
