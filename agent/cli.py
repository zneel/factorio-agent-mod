"""Manual console for the bridge.

    python cli.py state
    python cli.py move_to x=10 y=-5
    python cli.py mine name=iron-ore count=5
    python cli.py raw "/sc rcon.print(game.tick)"
    python cli.py repl

Arguments are key=value; values are parsed as JSON when possible (numbers, true/false, objects).
Commands that start a task (move_to, mine, craft, wait) block until the task ends.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge, BridgeError  # noqa: E402

TASK_CMDS = {"move_to", "mine", "craft", "wait"}


def parse(argv):
    args = {}
    for a in argv:
        k, _, v = a.partition("=")
        try:
            args[k] = json.loads(v)
        except json.JSONDecodeError:
            args[k] = v
    return args


# tools that never need (or spawn) a character
NO_CHARACTER = {"plant_status", "production", "jobs", "post_job", "update_job", "team", "belt_status", "set_time_mode",
                "set_game_speed"}


def unknown_character(b, argv):
    """Any command for a new name spawns a character, so a typo in FACTORIO_AGENT_NAME silently adds one.
    Refuse names that don't exist yet unless FACTORIO_SPAWN=1."""
    tool = argv[1] if argv[0] == "t" and len(argv) > 1 else argv[0]
    if tool in NO_CHARACTER or tool in ("raw", "ping", "agents", "snapshot", "terrain") \
            or os.environ.get("FACTORIO_SPAWN") == "1":
        return False
    try:
        known = b.call("agents") or {}
    except BridgeError:
        return False
    if b.agent in known:
        return False
    print(f"error: no character named '{b.agent}' (known: {', '.join(sorted(known))}). Check FACTORIO_AGENT_NAME; "
          "set FACTORIO_SPAWN=1 to create a new character on purpose.")
    return True


def touch_heartbeat(char):
    """Every command a worker runs proves it is alive (crew.py offline reads this)."""
    try:
        import server
        path = server.RUNTIME / f"crew-{char}.json"
        try:
            st = json.loads(path.read_text())
        except (OSError, ValueError):
            st = {}
        st["seen"] = time.time()
        path.write_text(json.dumps(st))
    except OSError:
        pass


def run(b, argv):
    cmd, rest = argv[0], argv[1:]
    if unknown_character(b, argv):
        return
    tool = rest[0] if cmd == "t" and rest else cmd
    if tool not in NO_CHARACTER and b.agent not in ("planner", "board"):
        touch_heartbeat(b.agent)
    if cmd == "t":
        # call an MCP tool exactly as an agent would, with the same terse output
        import mcp_server
        import terse
        name, args = rest[0], parse(rest[1:])
        try:
            res = mcp_server.HANDLERS[name](b, args)
            if isinstance(res, dict) and res.get("state") in ("done", "failed"):
                res.pop("id", None)
            print(terse.render(res))
        except BridgeError as e:
            print("error:", e)
        return
    if cmd == "raw":
        print(b.raw(" ".join(rest)))
        return
    args = parse(rest)
    try:
        res = b.run(cmd, **args) if cmd in TASK_CMDS else b.call(cmd, **args)
    except BridgeError as e:
        print("ERROR:", e)
        return
    if cmd == "map" and isinstance(res, dict):
        x0, y0 = res["origin"]["x"], res["origin"]["y"]
        for i, row in enumerate(res["rows"]):
            print(f"{y0 + i:>5} {row}")
        print("x0 =", x0, "legend:", res["legend"])
    else:
        print(json.dumps(res, indent=1))


def main():
    agent = os.environ.get("FACTORIO_AGENT_NAME", "agent")
    b = Bridge(agent)
    if len(sys.argv) < 2:
        print(__doc__)
        return
    if sys.argv[1] == "repl":
        import shlex
        while True:
            try:
                line = input(f"{agent}> ")
            except EOFError:
                break
            if line.strip():
                run(b, shlex.split(line))
    else:
        run(b, sys.argv[1:])


if __name__ == "__main__":
    main()
