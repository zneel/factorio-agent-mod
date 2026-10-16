# factorio-agent

An interface that lets an AI agent play Factorio 2.0 as a normal character, through MCP tools.

```
 agent (Claude Code / any MCP client)
      │  MCP over stdio
 agent/mcp_server.py ── 32 tools (observe, move, mine, craft, place, insert, research, chat…)
      │  RCON  "/agent {json}"
 headless factorio.exe (own profile in runtime/, no effect on your normal saves)
      │
 mod/agent-bridge/control.lua ── player-less character, stepped every tick
```

Everything is original code with no third-party dependencies: the Lua mod, the RCON client, and the
MCP server all use only the Python standard library.

## Fair play

The agent controls a real `character` entity and follows the same rules a human does:

| | |
|---|---|
| walking | engine pathfinder + native `walking_state`, so real running speed (about 9 tiles/s) |
| mining | real mining time (iron ore: 2 s per ore by hand), 2.7-tile resource reach, inventory limits |
| crafting | real hand-crafting queue and times; smelting needs furnaces |
| building | 10-tile build range and reach, normal collision rules, items come out of the inventory |
| research | 2.0 trigger techs fire on their own; later techs need labs and science packs |

**Time modes.** In `auto` mode (the default) the map is tick-paused whenever no agent action is
running and no human is connected. The agent's thinking costs no game time, but the factory only
runs while the agent acts or calls `wait`. `turn` pauses even with humans online, and `realtime`
never pauses. `set_game_speed` (up to 20x) makes long actions finish sooner in wall-clock time.

## Use it

With Claude Code, open a session in this folder (`E:\modding\factorio-agent`). `.mcp.json`
registers the `factorio` server; approve it once. Then just ask it to play, for example
"play Factorio: set up iron smelting with burner drills."

The MCP server starts the headless server on the first tool call if it isn't running. To manage it
yourself:

```bash
python agent/server.py start      # creates runtime/saves/agent.zip on first run
python agent/server.py status
python agent/server.py stop       # saves the map
python agent/server.py create --force [--seed 123] [--enemies]   # new map (default: peaceful, no biters)
```

(Use `E:\modding\tools\python\cpython-3.12.15-windows-x86_64-none\python.exe` if `python` is not on PATH.)

**Watch it play.** Start Factorio normally, then go to Multiplayer > Connect to address and enter
`localhost:34197`. In chat:
- `/agent-auto` (on by default when you join) is an auto director: the camera follows whichever agent
  is busiest. Building scores highest, then inserting/taking, mining, walking and hand-crafting, and
  idling scores zero. It waits 5 s before switching unless the watched agent has gone idle for 2 s,
  so it doesn't flick back and forth
- `/agent-follow [name]` pins your view on one agent, `/agent-free` lets you move the camera yourself
- `/agent-mode realtime|auto|turn` changes the time mode
- anything else you type reaches the agent through `read_chat`, and it answers with `say`

While you're connected in `auto` mode the map runs in real time.

**Watch in a browser (no client).** `python agent/viewer.py` then open http://localhost:8765:
a live top-down map drawn with the game's own sprites (drag to pan, wheel to zoom, "follow" an
agent). It shows buildings, animated while they work, plus belts, terrain, ore, trees, rocks and the
agents' characters with their current tasks. Problems (no fuel, no power, missing packs…) get red
outlines, and assemblers show alt-mode recipe icons. A sidebar lists research progress, building
counts and recent chat. The base refreshes every 1.5 s (`--interval`). Agent positions stream to the
page every 20 ms while the game is running (`--fast`; Factorio answers RCON once per tick, so about
50/s). The viewer is strictly read-only and never starts the server.

Sprites are read straight from your Factorio install and never copied: on first start,
`agent/viewer_assets.py` runs `factorio --dump-data` in `runtime/viewer-cache/` and builds a sprite
manifest from it. Terrain, belt corners and pipe joints need agent-bridge 0.1.8 (`terrain`
command). With 0.1.7 it draws building sprites over a coarse ore layer. Against older versions it
falls back to buildings within 300 tiles of `agent` and no chat.

**Several agents.** Each MCP server instance controls the character named by `FACTORIO_AGENT_NAME`.
Register more entries with different names and they share one map.

**Overseer + cheap workers.** A planner posts jobs; cheap subagents (Haiku only, for token cost) execute concrete briefs through
`cli.py t <tool>` and return an 8-line report. [agent/WORKER.md](agent/WORKER.md) is the shared brief, so each
dispatch prompt only carries the job itself. Workers on different characters (`FACTORIO_AGENT_NAME`)
run in parallel: one builds while a `hauler` character refuels and collects.

**Crew talk.** Characters coordinate on their own. `say` (optionally `to=<name>`) is stored in the
chat log, so teammates get it from `read_chat`, and so does a later worker on the same character. `set_intent`
posts a claim ("building labs at 64,-6") shown in `team` and above the character. `team` lists everyone's
position, task or idle time, intent and cargo. Hand-offs go through chests, announced in chat. Needs agent-bridge 0.1.13.

**Job board and always-on workers.** Planning and doing run in parallel, so the characters never wait
for the planner:
- The overseer posts jobs (`post_job` with a title, brief, optional character, priority 1-5, and an
  `after=` dependency) and edits them with `update_job`.
- One long-running worker per character loops on `python agent/crew.py next <character>`. This blocks
  until it claims a job (own jobs first, then by priority and age). While the board is empty, the
  character does standing chores such as refuelling.
- Every job carries machine-checkable `ACCEPT:` checks (`working X,Y`, `has X,Y ITEM >= N`, `belt X,Y ITEM`,
  `count ITEM >= N`, ...; see agent/STEERING.md). `post_job` refuses a job without them and refuses `place`
  tiles that `can_place` reports blocked.
- The worker does the job and closes it with `finish_job` (done, or blocked plus a note). A "done" whose
  checks fail comes back NOT DONE with the measured values; a second failure closes it blocked. Each
  outcome goes to `runtime/outcomes.jsonl`; `crew.py scorecard` shows the verified share per worker.
  The result goes to chat.
- `jobs` shows the board. An interrupted job resumes for the next worker on that character.
- See OVERSEER.md and WORKER.md ("Always-on mode"). Needs agent-bridge 0.1.19 (`probe`, `can_place`).

**Lean operation.** The prompts run the base like a Toyota line with a theory-of-constraints planner.
[agent/OVERSEER.md](agent/OVERSEER.md) is the overseer's cycle: measure, find the constraint, make one change, verify.
It also has planning rates and science bills of materials. The MCP instructions and WORKER.md carry the same rules
for the floor: link every output to a consumer, fix losses before expanding, don't hand-haul to hide blocked machines.
`base_status` measures it. It shows the % of machines working, then every loss as blocked (output full),
starved (no input) or down (fuel/power/setup).

**Manual console.** `python agent/cli.py t <tool> key=value ...` calls an MCP tool exactly as an
agent would, with the same terse output. For raw bridge commands, run `python agent/cli.py state`, `python agent/cli.py map radius=15`,
`python agent/cli.py mine name=iron-ore count=10`, or `python agent/cli.py repl`.
`python agent/selftest.py` runs a scripted early game through the MCP protocol (agent name `selftest`).

## Tools

| Observe | Act | Knowledge / meta |
|---|---|---|
| get_state | move_to | recipe_info |
| look_around | mine (ore, trees, rocks, own buildings) | list_recipes |
| view_map (ASCII, 1 char = 1 tile) | craft | research_status |
| find_resources (patch clustering) | place | tech_info |
| find_entities | insert_items / take_items | start_research |
| inspect (incl. belt lane contents/fill) | set_recipe / rotate / upgrade (in place) / configure (splitter, inserter filters) | say / read_chat |
| belt_status (follow a belt: items, fill, backups) | | |
| base_status | wait / wait_for_task / cancel_task / cancel_crafting | respawn, set_time_mode, set_game_speed |
| | batch / collect / refuel | |
| team (crew positions, intents, cargo, recent chat) | set_intent | say to= / read_chat (humans + teammates) |
| jobs (job board) | claim_job / finish_job | post_job / update_job (planner) |
| plant_status (planner's game state: alerts, ranked losses, research, crew, board) | | |
| production (per-minute made/used per item and fluid, from game statistics) | | |

Macros (one call instead of many round trips): **batch** (run a list of tool calls), **collect**
(walk your buildings and take their output), **refuel** (top up every burner in range),
**base_status** (flow % plus blocked / starved / down machines with positions).

**belt_status** follows a belt line downstream from a position (through undergrounds, splitters
and side-loads) and reports the items it carries, fill % overall and per lane, a one-char-per-belt fill
profile (`.` empty, `1`-`8` tenths, `#` 90%+), where it is backed up (full up to a dead end) and its
empty stretches. Needs agent-bridge 0.1.11.

### Token budget

Replies are terse `key=value` text instead of JSON: positions are `x,y`, item counts
`name:n,name:n`, and `inv+=` is the inventory change. Static facts (reach, weapons) and noise
(distances, finished task ids) are dropped, buildings are grouped by status, and the map legend
only lists symbols that are on screen. Measured against the original JSON: get_state −58%,
look_around −45%, inspect −32%, research_status −67%. Since every round trip resends the whole
conversation, `batch` and the macros save even more than the formatting. Placing a pole and two
labs, starting research, crafting 10 science packs and loading the labs took one call instead
of ten.

Every action reply includes the task's game-tick duration, the inventory change, and the final
position. Placing a drill reports the ore under it and what its output drops into.

## Bridge protocol

`/agent {"cmd": "...", "agent": "name", ...}` → `{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`.
Task commands (`move_to`, `mine`, `craft`, `wait`) return `{"task_id": n}`; poll `{"cmd": "task", "id": n}`
until `state` is `done` or `failed`. The command list is in `CMD` in `mod/agent-bridge/control.lua`.

## Layout

```
mod/agent-bridge/   the Factorio mod (copied into runtime/mods on every server start)
agent/rcon.py       Source RCON client
agent/bridge.py     JSON request + task polling
agent/server.py     headless server lifecycle (own config/write-data in runtime/)
agent/mcp_server.py MCP stdio server and tool definitions; INSTRUCTIONS holds the in-game primer
agent/macros.py     batch-style composite actions (collect, refuel, base_status)
agent/terse.py      token-lean reply rendering
agent/cli.py        manual console
agent/crew.py       always-on loop helpers: `next <char>` (claim a job; kanban crafting and refuel while
                    waiting), `check <id>`, `scorecard`, `audit`, `watch` / `status` (planner's game state with alerts)
agent/OVERSEER.md   planner's operating system (lean cycle, job board); WORKER.md is the crew's brief
agent/selftest.py   end-to-end test through MCP
runtime/            generated: config, mods, saves, logs, rcon.json (random RCON password)
```

## Known limits (v0.1)

- No combat tools yet; maps are peaceful by default.
- No tile placement (bricks, concrete, landfill), blueprints, vehicles or trains.
- Hand-crafting by a player-less character isn't seen by the engine, so the mod records it in
  production statistics and fires "craft-item" research triggers itself.
- Headless servers can't render, so the agent has no screenshots; it sees the world through data and `view_map`.
