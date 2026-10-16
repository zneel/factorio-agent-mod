"""High-level client for the agent-bridge mod: sends `/agent <json>` over RCON and waits on tasks."""

import json
import time

from rcon import Rcon, RconError
import server


class BridgeError(Exception):
    pass


def _tidy(v):
    """Round floats so replies stay short (Factorio prints doubles at full precision)."""
    if isinstance(v, float):
        r = round(v, 2)
        return int(r) if r == int(r) else r
    if isinstance(v, dict):
        return {k: _tidy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_tidy(x) for x in v]
    return v


class Bridge:
    def __init__(self, agent: str = "agent", autostart: bool = True):
        self.agent = agent
        self.autostart = autostart
        self.rcon = None

    def _conn(self) -> Rcon:
        if self.rcon is None:
            if self.autostart and not server.ping():
                server.start()
            s = server.rcon_settings()
            self.rcon = Rcon(s["host"], s["port"], s["password"])
        return self.rcon

    def call(self, cmd: str, **args):
        req = {"cmd": cmd, "agent": self.agent}
        req.update({k: v for k, v in args.items() if v is not None})
        try:
            text = self._conn().command("/agent " + json.dumps(req, separators=(",", ":")))
        except (OSError, RconError) as e:
            self.rcon = None
            raise BridgeError(f"cannot reach the Factorio server: {e}")
        if not text.strip():
            raise BridgeError("empty reply from server (is the agent-bridge mod loaded?)")
        try:
            reply = json.loads(text)
        except json.JSONDecodeError:
            raise BridgeError(f"unexpected reply: {text[:500]}")
        if not reply.get("ok"):
            raise BridgeError(reply.get("error", "unknown error"))
        return _tidy(reply.get("result"))

    def raw(self, text: str) -> str:
        return self._conn().command(text)

    def run(self, cmd: str, timeout: float = 180.0, **args):
        """Start a task-producing command and block until the task finishes (or timeout)."""
        started = self.call(cmd, **args)
        tid = started.get("task_id") if isinstance(started, dict) else None
        if not tid:
            return started
        deadline = time.time() + timeout
        delay = 0.05
        while True:
            t = self.call("task", id=tid)
            if t.get("state") != "running":
                extra = {k: v for k, v in started.items() if k not in ("task_id", "kind")}
                if extra:
                    t["started_with"] = extra
                return t
            if time.time() > deadline:
                t["note"] = (f"task still running after {timeout:.0f}s wall time; "
                             "call wait_for_task or cancel_task")
                return t
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)
