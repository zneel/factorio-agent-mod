"""Stream board: the crew's job tickets and who is doing what, for an OBS browser source. Read-only; stdlib.

    python agent/board.py [--port 8766] [--interval 2] [--host 127.0.0.1]

then open http://localhost:8766 (OBS: Browser source, 1920x1080, "Shutdown source when not visible" off).
One background thread polls the game over RCON and caches the latest state, so any number of open
tabs cost the same. It never starts the server, sends no action commands, spawns no characters and
does not move anyone's chat cursor (it uses ping, team, jobs, production and snapshot only).

/api/state returns the cached state as JSON; if the game is unreachable the last good state is served
with "stale": true so the page can show it is reconnecting.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge import Bridge, BridgeError
import crew

HERE = Path(__file__).resolve().parent
PAGE = HERE / "board.html"

PACKS = {"automation-science-pack": "red", "logistic-science-pack": "green",
         "military-science-pack": "black", "chemical-science-pack": "blue",
         "production-science-pack": "purple", "utility-science-pack": "yellow",
         "space-science-pack": "white"}
SLOW_EVERY = 6.0   # seconds between the heavy reads (whole-base snapshot and production statistics)


def _dict(v):
    return v if isinstance(v, dict) else {}   # Factorio encodes an empty Lua table as {}


def _list(v):
    return v if isinstance(v, list) else []


def short_job(j, with_brief=False):
    out = {"id": j.get("id"), "title": j.get("title"), "priority": j.get("priority", 3), "state": j.get("state"),
           "for": j.get("for"), "by": j.get("by"), "after": j.get("after"), "age_s": j.get("age_s"),
           "note": (j.get("note") or "")[:220] or None}
    if with_brief and j.get("brief"):
        out["brief"] = j["brief"][:160]
    return out


class Poller:
    def __init__(self, interval):
        self.interval = interval
        self.bridge = Bridge("board", autostart=False)
        self.lock = threading.Lock()
        self.state = {"ok": False, "stale": True, "error": "starting"}
        self.slow = {}
        self.slow_at = 0.0
        self.road = {}
        self.road_at = 0.0

    def collect(self):
        b = self.bridge
        ping = b.call("ping")
        team = _dict(b.call("team", chat=8))
        members = _dict(team.get("members"))
        jobs = [j for j in _list(b.call("jobs", recent=8).get("jobs")) if isinstance(j, dict)]
        if time.time() - self.slow_at > SLOW_EVERY:
            self.slow = self.collect_slow()
            self.slow_at = time.time()
        if time.time() - self.road_at > 20:
            self.road, self.road_at = self.collect_road(), time.time()
        by_job = {j.get("by"): j for j in jobs if j.get("state") == "claimed"}
        crew_list = []
        for name, m in sorted(members.items()):
            m = _dict(m)
            job = by_job.get(name)
            age = crew._worker_age(name)
            crew_list.append({
                "name": name, "pos": m.get("pos"), "dead": bool(m.get("dead")), "task": m.get("task"),
                "idle_s": m.get("idle_s"), "intent": m.get("intent"),
                "offline": age > (600 if job else 240),
                "carries": _dict(m.get("carries")), "job": {"id": job["id"], "title": job["title"]} if job else None,
            })
        groups = {"open": [], "claimed": [], "blocked": [], "done": []}
        for j in jobs:
            s = j.get("state")
            if s in groups:
                groups[s].append(short_job(j, with_brief=(s == "claimed")))
        groups["open"].sort(key=lambda j: (j["priority"], j["id"]))
        groups["blocked"].sort(key=lambda j: -j["id"])
        groups["done"].sort(key=lambda j: (j["age_s"] if j["age_s"] is not None else 1 << 30, -j["id"]))
        chat = [{"from": c.get("from"), "to": c.get("to"), "message": c.get("message")}
                for c in _list(team.get("recent_chat")) if isinstance(c, dict)]
        state = {"ok": True, "stale": False, "t": time.time(), "tick": ping.get("tick"), "version": ping.get("version"),
                 "crew": crew_list, "jobs": groups, "chat": chat}
        state.update(self.slow)
        state["road"] = self.road
        return state

    def collect_road(self):
        """The rocket roadmap: current phase, each gate with its gap, what the silo path still costs."""
        try:
            phase, lines = crew.roadmap(self.bridge)
            path = crew.silo_path()
        except (BridgeError, KeyError, TypeError, ValueError):
            return self.road
        gates = []
        for ln in lines[1:]:
            name, _, rest = ln.partition(": ")
            gates.append({"name": name, "ok": rest == "ok", "gap": rest[4:] if rest.startswith("GAP ") else ""})
        return {"phase": phase, "techs_left": len(path["todo"]), "packs": path["packs"], "gates": gates,
                "silo_done": path["silo_done"]}

    def collect_slow(self):
        b = self.bridge
        s, count, groups = crew.losses(b)
        total = sum(count.values()) or 1
        r = s.get("research") or {}
        science = {}
        try:
            p = _dict(b.call("production", window="ten_minutes"))
            for name, v in _dict(p.get("items")).items():
                if name in PACKS:
                    science[PACKS[name]] = {"made": v.get("made"), "used": v.get("used")}
        except BridgeError:
            pass
        return {"flow": {"pct": round(100 * count["working"] / total), "total": sum(count.values()),
                         "working": count["working"], "blocked": count["blocked"], "starved": count["starved"],
                         "down": count["down"]},
                "research": {"current": r.get("current"), "progress": r.get("progress") or 0,
                             "queue": _list(r.get("queue")), "done": r.get("researched_count")},
                "science": science}

    def loop(self):
        while True:
            try:
                st = self.collect()
            except (BridgeError, OSError, KeyError, ValueError, TypeError) as e:
                with self.lock:
                    self.state = dict(self.state, ok=False, stale=True, error=str(e)[:200])
                time.sleep(max(self.interval, 3.0))
                continue
            with self.lock:
                self.state = st
            time.sleep(self.interval)

    def get(self):
        with self.lock:
            return json.dumps(self.state, separators=(",", ":")).encode()


def make_handler(poller):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                body, ctype = PAGE.read_bytes(), "text/html; charset=utf-8"
            elif path == "/api/state":
                body, ctype = poller.get(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open it to other machines on your network")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between light polls")
    a = ap.parse_args()
    poller = Poller(a.interval)
    threading.Thread(target=poller.loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(poller))
    print(f"stream board on http://{a.host}:{a.port}  (OBS browser source 1920x1080)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
