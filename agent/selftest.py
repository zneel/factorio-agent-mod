"""End-to-end smoke test: talks to mcp_server.py over stdio like an MCP client and plays a short
early-game sequence (mine stone, craft a furnace, set up a burner drill feeding it, collect plates).

    python selftest.py            (server must be startable; uses agent name "selftest")
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


class Client:
    def __init__(self, agent):
        env = dict(os.environ, FACTORIO_AGENT_NAME=agent)
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "mcp_server.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        self.id = 0

    def rpc(self, method, params=None):
        self.id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        reply = json.loads(self.p.stdout.readline())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def tool(self, tool_name, **args):
        r = self.rpc("tools/call", {"name": tool_name, "arguments": args})
        text = r["content"][0]["text"]
        flag = "FAIL" if r.get("isError") else "ok"
        print(f"--> {tool_name} {args} [{flag}]\n    {text[:600]}")
        try:
            return json.loads(text), r.get("isError")
        except json.JSONDecodeError:
            return text, r.get("isError")


def main():
    c = Client("selftest")
    init = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "selftest", "version": "0"}})
    print("server:", init["serverInfo"], "protocol", init["protocolVersion"])
    c.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    tools = c.rpc("tools/list")["tools"]
    print(len(tools), "tools:", ", ".join(t["name"] for t in tools))

    state, _ = c.tool("get_state")
    res, _ = c.tool("find_resources", radius=120)
    iron = next(p for p in res["patches"] if p["name"] == "iron-ore" and p["tiles"] > 50)

    c.tool("mine", name="stone", count=5)
    c.tool("mine", name="tree", count=3)
    c.tool("craft", recipe="stone-furnace", count=1)

    # burner drill on the iron patch, facing south, furnace under its drop position
    bx, by = iron["center"]["x"], iron["center"]["y"]
    bx, by = float(round(bx)), float(round(by))
    c.tool("move_to", x=bx + 3, y=by + 1)
    drill, err = c.tool("place", item="burner-mining-drill", x=bx, y=by, direction="south")
    if err:
        return
    dx, dy = drill["drop_position"]["x"], drill["drop_position"]["y"]
    furnace, err = c.tool("place", item="stone-furnace", x=round(dx), y=round(dy) + 1)
    c.tool("inspect", x=bx, y=by)
    c.tool("insert_items", x=bx, y=by, item="wood", count=4)
    c.tool("insert_items", x=round(dx), y=round(dy) + 1, item="wood", count=4)
    c.tool("wait", seconds=20)
    c.tool("inspect", x=round(dx), y=round(dy) + 1)
    c.tool("take_items", x=round(dx), y=round(dy) + 1, item="iron-plate")
    c.tool("view_map", radius=6)
    c.tool("research_status")
    c.tool("say", message="selftest finished")
    c.p.stdin.close()
    c.p.wait(timeout=10)


if __name__ == "__main__":
    main()
