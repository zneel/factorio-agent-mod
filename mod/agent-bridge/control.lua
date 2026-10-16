-- Agent Bridge: lets an external agent play Factorio as a character.
--
-- Transport: RCON. Every request is `/agent <json>` with {"cmd": "...", "agent": "name", ...args};
-- the reply is a single JSON line {"ok": true, "result": ...} or {"ok": false, "error": "..."}.
--
-- Agents are player-less character entities. Actions follow the same rules as a human player:
-- reach distances, inventory, hand-crafting time, mining time and walking speed. Long actions
-- (walking, mining, waiting, crafting) run as tasks stepped in on_tick; the client polls them.
--
-- Time modes:
--   auto     - the map is tick-paused whenever no agent task is running and no human is connected
--   turn     - like auto, but also paused while humans are connected
--   realtime - the map always runs

local HISTORY = 20
local CHAT_KEEP = 200
local DEFAULT_AGENT = "agent"

local DIR_NAMES = {}
for name, v in pairs(defines.direction) do DIR_NAMES[v] = name end
local STATUS_NAMES = {}
for name, v in pairs(defines.entity_status) do STATUS_NAMES[v] = name end

-- octant 0 = east, counting clockwise (map y grows southwards)
local WALK_DIRS = {
  defines.direction.east, defines.direction.southeast, defines.direction.south, defines.direction.southwest,
  defines.direction.west, defines.direction.northwest, defines.direction.north, defines.direction.northeast,
}

local RESOURCE_CHARS = {
  ["iron-ore"] = "i", ["copper-ore"] = "c", ["coal"] = "k", ["stone"] = "s",
  ["uranium-ore"] = "u", ["crude-oil"] = "o",
}
local BELT_CHARS = {
  [defines.direction.north] = "^", [defines.direction.east] = ">",
  [defines.direction.south] = "v", [defines.direction.west] = "<",
}

local atan2 = math.atan2 or math.atan

---------------------------------------------------------------------------------------------------
-- helpers

local function fail(msg, ...)
  if select("#", ...) > 0 then msg = string.format(msg, ...) end
  error({ agent_error = msg }, 0)
end

local function r2(v) return math.floor(v * 100 + 0.5) / 100 end
local function pos_of(p) return { x = r2(p.x), y = r2(p.y) } end
local function dist(a, b)
  local dx, dy = a.x - b.x, a.y - b.y
  return math.sqrt(dx * dx + dy * dy)
end
local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end

local function parse_pos(a, optional)
  local p = a.position
  if type(p) == "table" then
    local x, y = p.x or p[1], p.y or p[2]
    if type(x) == "number" and type(y) == "number" then return { x = x, y = y } end
  end
  if type(a.x) == "number" and type(a.y) == "number" then return { x = a.x, y = a.y } end
  if optional then return nil end
  fail("missing position: give x and y")
end

local function parse_dir(d)
  if d == nil then return defines.direction.north end
  if type(d) == "number" then return d end
  local v = defines.direction[d]
  if v == nil then fail("unknown direction '%s' (use north/east/south/west)", tostring(d)) end
  return v
end

local function contents(inv)
  local out = {}
  if not (inv and inv.valid) then return out end
  for _, it in pairs(inv.get_contents()) do
    local key = it.name
    if it.quality and it.quality ~= "normal" then key = key .. "@" .. it.quality end
    out[key] = (out[key] or 0) + it.count
  end
  return out
end

local function delta(before, after)
  local d, any = {}, false
  for k, v in pairs(after) do
    local n = v - (before[k] or 0)
    if n ~= 0 then d[k] = n; any = true end
  end
  for k, v in pairs(before) do
    if after[k] == nil then d[k] = -v; any = true end
  end
  return any and d or nil
end

local function item_list(items)
  -- array of {name,amount|count,...} -> {name = amount}
  local out = {}
  for _, it in pairs(items or {}) do
    local n = it.amount or it.count
    if n == nil and it.amount_min then n = (it.amount_min + it.amount_max) / 2 end
    if it.probability and it.probability < 1 then n = (n or 1) * it.probability end
    out[it.name] = (out[it.name] or 0) + (n or 1)
  end
  return out
end

local function water_layer_tiles()
  if storage.water_tiles then return storage.water_tiles end
  local names = {}
  for name, proto in pairs(prototypes.tile) do
    local layers = proto.collision_mask and proto.collision_mask.layers
    if layers and layers.water_tile then names[#names + 1] = name end
  end
  storage.water_tiles = names
  return names
end

---------------------------------------------------------------------------------------------------
-- entity description

local function brief(ent, origin)
  local s = { name = ent.name, pos = pos_of(ent.position) }
  if ent.unit_number then s.id = ent.unit_number end
  if ent.supports_direction then s.dir = DIR_NAMES[ent.direction] end
  local ok, st = pcall(function() return ent.status end)
  if ok and st then s.status = STATUS_NAMES[st] end
  if ent.type == "assembling-machine" or ent.type == "furnace" then
    local r = ent.get_recipe()
    s.recipe = r and r.name or nil
  elseif ent.type == "resource" then
    s.amount = ent.amount
  end
  if origin then s.dist = r2(dist(origin, ent.position)) end
  return s
end

-- Belts keep items on transport lines, not inventories. Line 1 is the left lane, 2 the right
-- (seen in the direction of travel). Underground inputs carry the tunnel on lines 3/4; splitters
-- have 8 lines (two input halves and two output halves). In 2.0 items sit 0.25 tiles apart, so a
-- lane holds line_length / 0.25 items (4 per straight tile; curves are shorter/longer inside/out).
local BELT_TYPES = { "transport-belt", "underground-belt", "splitter", "linked-belt", "loader", "loader-1x1" }
local IS_BELT = {}
for _, t in pairs(BELT_TYPES) do IS_BELT[t] = true end
local ITEM_SPACING = 0.25

local function add_lines(ent, lanes, lines)
  for _, i in pairs(lines) do
    local line = ent.get_transport_line(i)
    local lane = lanes[i % 2 == 1 and "left" or "right"]
    for k, v in pairs(contents(line)) do lane.items[k] = (lane.items[k] or 0) + v end
    lane.n = lane.n + #line
    local ok, len = pcall(function() return line.line_length end)
    lane.cap = lane.cap + (ok and len or 1) / ITEM_SPACING
  end
  return lanes
end

local function new_lanes()
  return { left = { items = {}, n = 0, cap = 0 }, right = { items = {}, n = 0, cap = 0 } }
end

local function belt_lanes(ent, one_side)
  -- {left = {items, n, cap}, right = {...}}; n = item positions used, cap = positions available.
  -- one_side: for a splitter take the fuller of its two sides per lane (a line passes one side).
  if ent.type == "splitter" and one_side then
    local a = add_lines(ent, new_lanes(), { 1, 2, 5, 6 })
    local b = add_lines(ent, new_lanes(), { 3, 4, 7, 8 })
    for _, side in pairs({ "left", "right" }) do
      local la, lb = a[side], b[side]
      if lb.n / math.max(lb.cap, 1) > la.n / math.max(la.cap, 1) then a[side] = lb end
    end
    return a
  end
  local max = ent.get_max_transport_line_index()
  if ent.type == "underground-belt" and ent.belt_to_ground_type == "output" then max = 2 end
  local lines = {}
  for i = 1, max do lines[i] = i end
  return add_lines(ent, new_lanes(), lines)
end

local function pct(n, cap)
  if cap <= 0 then return 0 end
  return math.min(100, math.floor(n / cap * 100 + 0.5))
end

local function detail(ent, ch)
  local s = brief(ent, ch and ch.position)
  s.type = ent.type
  if ent.health and ent.prototype.get_max_health() > 0 then
    s.health = r2(ent.health) .. "/" .. ent.prototype.get_max_health()
  end
  local bb = ent.bounding_box
  s.bbox = { pos_of(bb.left_top), pos_of(bb.right_bottom) }
  local invs = {}
  local max = ent.get_max_inventory_index() or 0
  for i = 1, max do
    local inv = ent.get_inventory(i)
    if inv and inv.valid and #inv > 0 then
      invs[inv.name or tostring(i)] = { slots = #inv, items = contents(inv) }
    end
  end
  if next(invs) then s.inventories = invs end
  if ent.type == "mining-drill" then
    local t = ent.mining_target
    s.mining_target = t and t.name or nil
    s.drop_position = pos_of(ent.drop_position)
  elseif ent.type == "inserter" then
    s.pickup_position = pos_of(ent.pickup_position)
    s.drop_position = pos_of(ent.drop_position)
  end
  if ent.type == "assembling-machine" or ent.type == "furnace" then
    s.crafting_progress = r2(ent.crafting_progress)
  end
  if IS_BELT[ent.type] then
    local lanes = belt_lanes(ent)
    s.lanes = {}
    for _, side in pairs({ "left", "right" }) do
      local l = lanes[side]
      s.lanes[side] = { items = l.items, fill = pct(l.n, l.cap) .. "%" }
    end
    s.fill = pct(lanes.left.n + lanes.right.n, lanes.left.cap + lanes.right.cap) .. "%"
    s.max_rate = r2(ent.prototype.belt_speed * 480) .. "/s"
    if ent.type == "underground-belt" then
      s.belt_type = ent.belt_to_ground_type
      local n = ent.neighbours
      s.pair = n and pos_of(n.position) or "none"
    end
  end
  if ent.burner then
    local cur = ent.burner.currently_burning
    s.burning = cur and cur.name.name or nil
    s.remaining_burning_fuel = math.floor(ent.burner.remaining_burning_fuel)
  end
  local fb = ent.fluidbox
  if fb and #fb > 0 then
    local fl = {}
    for i = 1, #fb do
      local f = fb[i]
      if f then fl[#fl + 1] = { name = f.name, amount = r2(f.amount), temperature = f.temperature } end
    end
    if #fl > 0 then s.fluids = fl end
    local ports = {}
    for i = 1, #fb do
      local filt = fb.get_filter(i)
      for _, c in pairs(fb.get_pipe_connections(i)) do
        if c.connection_type == "normal" then
          ports[#ports + 1] = { box = i, fluid = filt and filt.name or nil, flow = c.flow_direction,
            at = pos_of(c.position), to = pos_of(c.target_position), linked = c.target ~= nil }
        end
      end
    end
    if #ports > 0 then s.fluid_ports = ports end
  end
  if ent.prototype.mineable_properties.minable then
    s.minable = true
  end
  return s
end

---------------------------------------------------------------------------------------------------
-- agents

local function update_label(agent)
  local ch = agent.character
  if not (ch and ch.valid) then return end
  local text = agent.name
  if agent.task then text = text .. " [" .. agent.task.kind .. "]" end
  if agent.intent then text = text .. ": " .. agent.intent:sub(1, 48) end
  if agent.label and agent.label.valid then
    agent.label.text = text
  else
    agent.label = rendering.draw_text {
      text = text, surface = ch.surface, target = { entity = ch, offset = { 0, -2.6 } },
      color = { 0.5, 0.85, 1 }, scale = 1.1, alignment = "center", vertical_alignment = "bottom",
    }
  end
end

local function starting_items()
  if remote.interfaces["freeplay"] and remote.interfaces["freeplay"]["get_created_items"] then
    local ok, items = pcall(remote.call, "freeplay", "get_created_items")
    if ok and items and next(items) then return items end
  end
  return {
    ["iron-plate"] = 8, ["wood"] = 1, ["pistol"] = 1, ["firearm-magazine"] = 10,
    ["burner-mining-drill"] = 1, ["stone-furnace"] = 1,
  }
end

local function spawn_character(agent, items)
  local surface = game.surfaces[agent.surface or "nauvis"]
  local force = game.forces.player
  local spawn = force.get_spawn_position(surface)
  surface.request_to_generate_chunks(spawn, 3)
  surface.force_generate_chunk_requests()
  local p = surface.find_non_colliding_position("character", spawn, 32, 0.5) or spawn
  local ch = surface.create_entity { name = "character", position = p, force = force }
  if not ch then fail("could not create character") end
  pcall(function() ch.color = { 0.3, 0.75, 1 } end)
  for name, count in pairs(items or {}) do
    if prototypes.item[name] then ch.insert { name = name, count = count } end
  end
  agent.character = ch
  agent.label = nil
  update_label(agent)
  force.chart(surface, { { p.x - 96, p.y - 96 }, { p.x + 96, p.y + 96 } })
  return ch
end

local function get_agent(name)
  name = name or DEFAULT_AGENT
  local agent = storage.agents[name]
  if not agent then
    agent = { name = name, history = {}, next_id = 0, chat_cursor = 0 }
    storage.agents[name] = agent
    spawn_character(agent, starting_items())
  end
  return agent
end

local function char_of(agent)
  local ch = agent.character
  if not (ch and ch.valid) then fail("agent '%s' has no living character; call respawn", agent.name) end
  return ch
end

local function set_walk(ch, dir)
  if dir then
    ch.walking_state = { walking = true, direction = dir }
  else
    ch.walking_state = { walking = false, direction = ch.walking_state.direction }
  end
end

local function mining_ticks(ch, ent)
  local props = ent.prototype.mineable_properties
  local speed = prototypes.entity["character"].mining_speed
    * (1 + ch.character_mining_speed_modifier + ch.force.manual_mining_speed_modifier)
  return math.max(1, math.ceil(60 * props.mining_time / speed))
end

local function in_reach(ch, ent, is_resource)
  if is_resource then
    return dist(ch.position, ent.position) <= ch.resource_reach_distance
  end
  return ch.can_reach_entity(ent)
end

local function require_reach(ch, ent)
  if not ch.can_reach_entity(ent) then
    fail("%s at (%.1f, %.1f) is out of reach (distance %.1f, reach %.1f); move closer first",
      ent.name, ent.position.x, ent.position.y, dist(ch.position, ent.position), ch.reach_distance)
  end
end

---------------------------------------------------------------------------------------------------
-- tasks

local function task_view(t)
  if not t then return nil end
  local v = {
    id = t.id, kind = t.kind, state = t.state, ticks = (t.finished or game.tick) - t.started,
    result = t.result, error = t.error, inventory_delta = t.inventory_delta,
  }
  if t.kind == "mine" then v.progress = { mined = t.mined, count = t.count } end
  if t.mv and t.mv.goal then v.goal = pos_of(t.mv.goal) end
  return v
end

local function finish_task(agent, state, payload)
  local t = agent.task
  if not t then return end
  t.state = state
  t.finished = game.tick
  if state == "done" then t.result = payload else t.error = payload end
  local ch = agent.character
  if ch and ch.valid then
    set_walk(ch, nil)
    t.inventory_delta = delta(t.inv_before or {}, contents(ch.get_main_inventory()))
    t.result = t.result or {}
    t.result.position = pos_of(ch.position)
  end
  t.inv_before = nil
  t.mv = nil
  t.target = nil
  agent.history[t.id] = t
  agent.history[t.id - HISTORY] = nil
  agent.task = nil
  update_label(agent)
end

local function start_task(agent, task)
  local ch = char_of(agent)
  if agent.task then
    fail("agent is busy with task %d (%s); wait for it or cancel it", agent.task.id, agent.task.kind)
  end
  agent.next_id = agent.next_id + 1
  task.id = agent.next_id
  task.state = "running"
  task.started = game.tick
  task.inv_before = task.inv_before or contents(ch.get_main_inventory())
  agent.task = task
  game.tick_paused = false
  update_label(agent)
  return { task_id = task.id, kind = task.kind }
end

-- If the character's body overlaps a tree/rock/building (it can't walk out of that), move it to
-- the nearest free spot. Returns true if it had to.
local function unwedge(ch)
  for _, e in pairs(ch.surface.find_entities_filtered { area = ch.bounding_box, collision_mask = "player" }) do
    if e ~= ch then
      local free = ch.surface.find_non_colliding_position("character", ch.position, 3, 0.1)
      if free then ch.teleport(free) end
      return true
    end
  end
  return false
end

local function request_path(agent, mv)
  local ch = agent.character
  mv.path = nil
  mv.wp = 1
  mv.requested = game.tick
  mv.start = { x = ch.position.x, y = ch.position.y }
  local surface = ch.surface
  if not mv.prepared then
    mv.prepared = true
    -- The pathfinder can't cross ungenerated chunks: generate a corridor from here to the goal
    -- (idea from the Factorio Learning Environment's request_path, MIT).
    local s, g = mv.start, mv.goal
    local d = dist(s, g)
    local steps = math.max(1, math.ceil(d / 32))
    for i = 0, steps do
      local t = i / steps
      surface.request_to_generate_chunks({ x = s.x + (g.x - s.x) * t, y = s.y + (g.y - s.y) * t }, 2)
    end
    surface.force_generate_chunk_requests()
    -- A plain move whose goal is inside something (building, tree, water) aims for the nearest
    -- free spot instead, so the pathfinder doesn't give up.
    if not mv.reach_entity and not surface.can_place_entity { name = "character", position = g } then
      local free = surface.find_non_colliding_position("character", g, 12, 0.5, true)
      if free then
        mv.goal_requested = { x = g.x, y = g.y }
        mv.goal = free
        mv.radius = math.max(mv.radius, 0.7)
      end
    end
  end
  mv.path_id = surface.request_path {
    bounding_box = prototypes.entity["character"].collision_box,
    collision_mask = prototypes.entity["character"].collision_mask,
    start = ch.position,
    goal = mv.goal,
    force = ch.force,
    radius = math.max(mv.radius - 0.1, 0.1),
    entity_to_ignore = ch,
    pathfind_flags = { prefer_straight_paths = true, cache = false, no_break = true },
  }
  storage.path_requests[mv.path_id] = agent.name
end

local function new_move(goal, radius, reach_entity, reach_resource)
  return { goal = { x = goal.x, y = goal.y }, radius = radius, reach_entity = reach_entity,
    reach_resource = reach_resource, stuck = 0, retries = 0 }
end

-- returns "running" | "arrived" | "failed", reason
local function move_step(agent, mv)
  local ch = agent.character
  local p = ch.position
  local arrived = dist(p, mv.goal) <= mv.radius
  if not arrived and mv.reach_entity then
    if not mv.reach_entity.valid then return "failed", "target disappeared" end
    arrived = in_reach(ch, mv.reach_entity, mv.reach_resource)
  end
  if arrived then
    set_walk(ch, nil)
    return "arrived"
  end
  if not mv.path_id then
    unwedge(ch)
    request_path(agent, mv)
  end
  if mv.retry_at and game.tick >= mv.retry_at then
    mv.retry_at = nil
    request_path(agent, mv)
  end
  if mv.no_path then
    -- usually the start spot is wedged against something; step half a tile away and ask again
    mv.no_path = nil
    mv.path_fails = (mv.path_fails or 0) + 1
    if mv.path_fails > 3 then
      return "failed", string.format("no path to (%.1f, %.1f)", mv.goal.x, mv.goal.y)
    end
    local p0 = ch.position
    for i = 0, 7 do
      local ang = (i + mv.path_fails) * math.pi / 4
      local q = { x = p0.x + 0.5 * math.cos(ang), y = p0.y + 0.5 * math.sin(ang) }
      if ch.teleport(q, nil, false, false, defines.build_check_type.manual) then break end
    end
    unwedge(ch)
    request_path(agent, mv)
  end
  if not mv.path then
    set_walk(ch, nil)
    if game.tick - mv.requested > 900 then return "failed", "pathfinder timed out" end
    return "running"
  end

  local target
  while true do
    local wp = mv.path[mv.wp]
    if not wp then target = mv.goal; break end
    if dist(p, wp) < 0.3 then mv.wp = mv.wp + 1 else target = wp; break end
  end
  local on_path = target ~= mv.goal
  if not on_path and not mv.reach_entity and dist(p, mv.goal) <= mv.radius + 1 then
    set_walk(ch, nil)
    return "arrived" -- the pathfinder's "close enough" is good enough for a plain move
  end

  -- Walking is 8-directional, so following a diagonal segment drifts sideways into trees and
  -- rocks. Keep the character on the pathfinder's collision-free segment (small nudges only).
  local from = mv.path[mv.wp - 1] or mv.start
  if on_path and from then
    local sx, sy = target.x - from.x, target.y - from.y
    local len2 = sx * sx + sy * sy
    if len2 > 0.0001 then
      local u = clamp(((p.x - from.x) * sx + (p.y - from.y) * sy) / len2, 0, 1)
      local q = { x = from.x + u * sx, y = from.y + u * sy }
      local off = dist(p, q)
      if off > 0.15 and off < 2 and ch.teleport(q, nil, false, false, defines.build_check_type.manual) then
        p = q
      end
    end
  end

  local oct = math.floor((atan2(target.y - p.y, target.x - p.x) + math.pi / 8) / (math.pi / 4)) % 8
  set_walk(ch, WALK_DIRS[oct + 1])

  -- stuck = no real progress toward the current waypoint for 2 seconds (catches wobbling too)
  local d = dist(p, target)
  if mv.prog_wp ~= mv.wp or not mv.prog_best or d < mv.prog_best - 0.25 then
    mv.prog_wp, mv.prog_best, mv.prog_tick = mv.wp, d, game.tick
  elseif game.tick - mv.prog_tick > 120 then
    mv.retries = mv.retries + 1
    mv.prog_best = nil
    if mv.retries > 4 then
      set_walk(ch, nil)
      return "failed", string.format("stuck at (%.1f, %.1f)", p.x, p.y)
    end
    if not on_path then mv.radius = math.max(mv.radius * 0.6, 0.5) end -- ask for a path that ends closer
    unwedge(ch)
    request_path(agent, mv)
  end
  return "running"
end

local function match_target(t, e)
  if t.match_type then return e.type == t.match_type end
  return e.name == t.match_name
end

local function next_target(ch, t)
  -- nearest entity like the original target, around the original position
  local found = ch.surface.find_entities_filtered {
    position = t.origin, radius = t.search_radius,
    type = t.match_type, name = (not t.match_type) and t.match_name or nil,
    limit = 200,
  }
  local best, bd
  for _, e in pairs(found) do
    if e.valid and match_target(t, e) and e.prototype.mineable_properties.minable then
      local d = dist(ch.position, e.position)
      if not bd or d < bd then best, bd = e, d end
    end
  end
  return best
end

local TASKS = {}

TASKS.move = function(agent, t)
  local st, why = move_step(agent, t.mv)
  if st == "arrived" then
    finish_task(agent, "done", t.mv.goal_requested and {
      note = "goal was blocked; went to the nearest free spot", goal_used = pos_of(t.mv.goal),
    } or {})
  elseif st == "failed" then finish_task(agent, "failed", why) end
end

TASKS.mine = function(agent, t)
  local ch = agent.character
  local ent = t.target
  if not (ent and ent.valid) then
    if t.mined >= t.count then return finish_task(agent, "done", { mined = t.mined }) end
    ent = next_target(ch, t)
    if not ent then
      return finish_task(agent, t.mined > 0 and "done" or "failed",
        t.mined > 0 and { mined = t.mined, note = "no more targets nearby" } or "nothing left to mine nearby")
    end
    t.target = ent
    t.progress = 0
    t.need = mining_ticks(ch, ent)
    t.mv = nil
  end
  if not in_reach(ch, ent, t.is_resource) then
    if not t.mv then
      local reach = t.is_resource and ch.resource_reach_distance or ch.reach_distance
      t.mv = new_move(ent.position, math.max(reach - 0.6, 0.5), ent, t.is_resource)
    end
    local st, why = move_step(agent, t.mv)
    if st == "failed" then return finish_task(agent, "failed", why) end
    if st ~= "arrived" then return end
  end
  t.mv = nil
  -- in reach: stand still. Reach can return between move steps, and the walking state from the
  -- approach would otherwise keep the character sliding back and forth across the ore while it mines.
  if ch.walking_state.walking then set_walk(ch, nil) end
  t.progress = t.progress + 1
  if t.progress >= t.need then
    t.progress = 0
    local amount_before = t.is_resource and ent.amount
    local ok = ch.mine_entity(ent, false)
    -- on ore tiles mine_entity takes one unit and returns false because the tile survives
    if t.is_resource then ok = (not ent.valid) or ent.amount < amount_before end
    if not ok then
      return finish_task(agent, t.mined > 0 and "done" or "failed",
        t.mined > 0 and { mined = t.mined, note = "inventory full" } or "inventory full")
    end
    t.mined = t.mined + 1
    if t.mined >= t.count then return finish_task(agent, "done", { mined = t.mined }) end
  end
end

TASKS.wait = function(agent, t)
  if game.tick >= t.until_tick then finish_task(agent, "done", {}) end
end

TASKS.craft = function(agent, t)
  local ch = agent.character
  if ch.crafting_queue_size == 0 then
    finish_task(agent, "done", { crafted = t.recipe, count = t.count })
  elseif game.tick > t.deadline then
    finish_task(agent, "done", { note = "still crafting", queue = ch.crafting_queue })
  end
end

---------------------------------------------------------------------------------------------------
-- queries

local function find_entity(a, ch)
  if a.id then
    local e = game.get_entity_by_unit_number(a.id)
    if not (e and e.valid) then fail("no entity with id %d", a.id) end
    return e
  end
  local p = parse_pos(a)
  local found = ch.surface.find_entities_filtered {
    area = { { p.x - 0.5, p.y - 0.5 }, { p.x + 0.5, p.y + 0.5 } }, name = a.name,
  }
  local best, bscore
  for _, e in pairs(found) do
    if e ~= ch and e.type ~= "character-corpse" then
      local score = dist(p, e.position)
      if e.type == "resource" then score = score + 100 end
      if not bscore or score < bscore then best, bscore = e, score end
    end
  end
  if not best then
    fail("no entity%s at (%.1f, %.1f)", a.name and (" named " .. a.name) or "", p.x, p.y)
  end
  return best
end

local function nearest(ch, filter, origin)
  filter.position = origin or ch.position
  filter.radius = filter.radius or 64
  local best, bd
  for _, e in pairs(ch.surface.find_entities_filtered(filter)) do
    if e ~= ch then
      local d = dist(ch.position, e.position)
      if not bd or d < bd then best, bd = e, d end
    end
  end
  return best
end

local function craftable_by_hand(recipe)
  local cats = prototypes.entity["character"].crafting_categories or {}
  return cats[recipe.category] == true
end

local function recipe_view(r, ch)
  local v = {
    name = r.name, category = r.category, seconds = r.energy, enabled = r.enabled,
    ingredients = item_list(r.ingredients), products = item_list(r.products),
    handcraft = craftable_by_hand(r),
  }
  if ch and v.handcraft and r.enabled then v.can_craft_now = ch.get_craftable_count(r.name) end
  return v
end

local function tech_view(t)
  local cost = {}
  for _, ing in pairs(t.research_unit_ingredients) do cost[ing.name] = ing.amount end
  local unlocks = {}
  for _, eff in pairs(t.prototype.effects or {}) do
    if eff.type == "unlock-recipe" then unlocks[#unlocks + 1] = eff.recipe end
  end
  local trig = t.prototype.research_trigger
  if trig then
    -- 2.0 trigger techs complete by doing something (crafting, mining, building), not with science packs
    local what = trig.item and trig.item.name or trig.entity or trig.type
    if type(what) == "table" then what = what.name end
    return {
      name = t.name, unlocks = unlocks, researched = t.researched,
      trigger = string.format("%s %s x%d", trig.type, tostring(what), trig.count or 1),
    }
  end
  local ok, count = pcall(function() return t.research_unit_count end)
  return {
    name = t.name, units = ok and count or nil, unit_cost = cost,
    seconds_per_unit = t.research_unit_energy and r2(t.research_unit_energy / 60) or nil,
    unlocks = unlocks, researched = t.researched,
  }
end

local function agent_state(agent)
  local ch = agent.character
  local s = { name = agent.name, alive = ch ~= nil and ch.valid }
  if s.alive then
    s.position = pos_of(ch.position)
    s.health = r2(ch.health)
    s.reach = ch.reach_distance
    s.resource_reach = ch.resource_reach_distance
    s.inventory = contents(ch.get_main_inventory())
    local inv = ch.get_main_inventory()
    s.free_slots = inv.count_empty_stacks()
    if ch.crafting_queue_size > 0 then s.crafting_queue = ch.crafting_queue end
    local guns = ch.get_inventory(defines.inventory.character_guns)
    local ammo = ch.get_inventory(defines.inventory.character_ammo)
    s.weapons = contents(guns)
    s.ammo = contents(ammo)
  end
  s.task = task_view(agent.task)
  return s
end

---------------------------------------------------------------------------------------------------
-- commands

-- Spectator director (/agent-auto): how interesting an action or a running task is to watch.
-- Building beats hauling beats walking beats hand-crafting in place.
local DIRECTOR_ACT = {
  place = 6, upgrade = 6, set_recipe = 4, configure = 4, rotate = 3, insert = 4, take = 4, mine = 3, research = 2, say = 2,
  craft = 1.5, move_to = 1,
}
local DIRECTOR_TASK = { mine = 3, move = 2, craft = 1, wait = 0.3 }
local DIRECTOR_RECENT = 600     -- ticks an action keeps adding interest (fading linearly)
local DIRECTOR_DWELL = 300      -- minimum ticks on one agent before a switch...
local DIRECTOR_IDLE = 120       -- ...unless the current one has been idle this long and another is busy

local function director_score(agent, tick)
  local ch = agent.character
  if not (ch and ch.valid) then return -1, "dead" end
  local score, why = 0, nil
  local t = agent.task
  if t then
    score = score + (DIRECTOR_TASK[t.kind] or 1)
    why = t.kind
  end
  if ch.walking_state.walking then score = score + 1 end
  if agent.last_act then
    local age = tick - agent.last_act
    if age < DIRECTOR_RECENT then
      local s = (DIRECTOR_ACT[agent.last_act_kind] or 1) * (1 - age / DIRECTOR_RECENT)
      score = score + s
      if not why or s > 1.5 then why = agent.last_act_kind end
    end
  end
  if ch.crafting_queue_size > 0 then
    score = score + 0.5
    why = why or "crafting"
  end
  return score, why or "idle"
end

local CMD = {}

CMD.ping = function() return { tick = game.tick, version = script.active_mods["agent-bridge"] } end

-- Chat log entries come from humans (on_console_chat) and from agents (say). An agent reads what
-- humans and teammates said to everyone or to it, never its own lines.
local function chat_for(m, name)
  return m.from ~= name and (m.to == nil or m.to == name)
end

CMD.state = function(a, agent)
  local f = game.forces.player
  local players = {}
  for _, p in pairs(game.connected_players) do players[#players + 1] = p.name end
  local unread = 0
  for _, m in pairs(storage.chat) do
    if m.seq > agent.chat_cursor and chat_for(m, agent.name) then unread = unread + 1 end
  end
  return {
    tick = game.tick, game_seconds = r2(game.tick / 60), paused = game.tick_paused, mode = storage.mode,
    speed = game.speed,
    agent = agent_state(agent),
    research = f.current_research and {
      current = f.current_research.name, progress = r2(f.research_progress),
    } or nil,
    humans_online = players, unread_chat = unread,
  }
end

CMD.inventory = function(a, agent)
  local ch = char_of(agent)
  return { inventory = contents(ch.get_main_inventory()), free_slots = ch.get_main_inventory().count_empty_stacks() }
end

CMD.nearby = function(a, agent)
  local ch = char_of(agent)
  local center = parse_pos(a, true) or ch.position
  local radius = clamp(a.radius or 16, 1, 64)
  local out = { center = pos_of(center), radius = radius, buildings = {}, resources = {}, other = {} }
  local trees, rocks = { count = 0 }, { count = 0 }
  local others = {}
  for _, e in pairs(ch.surface.find_entities_filtered { position = center, radius = radius }) do
    if e ~= ch then
      local d = dist(ch.position, e.position)
      if e.type == "resource" then
        local r = out.resources[e.name]
        if not r then r = { tiles = 0, amount = 0 }; out.resources[e.name] = r end
        r.tiles = r.tiles + 1
        r.amount = r.amount + e.amount
        if not r.dist or d < r.dist then r.dist = r2(d); r.nearest = pos_of(e.position) end
      elseif e.type == "tree" then
        trees.count = trees.count + 1
        if not trees.dist or d < trees.dist then trees.dist = r2(d); trees.nearest = pos_of(e.position); trees.name = e.name end
      elseif e.type == "simple-entity" then
        rocks.count = rocks.count + 1
        if not rocks.dist or d < rocks.dist then rocks.dist = r2(d); rocks.nearest = pos_of(e.position); rocks.name = e.name end
      elseif e.force == ch.force and e.type ~= "character" then
        out.buildings[#out.buildings + 1] = brief(e, ch.position)
      elseif e.type ~= "cliff" and e.type ~= "fish" and e.type ~= "corpse" and e.type ~= "deconstructible-tile-proxy" then
        local key = e.name .. (e.force and e.force.name ~= "neutral" and ("/" .. e.force.name) or "")
        local o = others[key]
        if not o then o = { count = 0 }; others[key] = o end
        o.count = o.count + 1
        if not o.dist or d < o.dist then o.dist = r2(d); o.nearest = pos_of(e.position) end
      end
    end
  end
  table.sort(out.buildings, function(x, y) return x.dist < y.dist end)
  local limit = a.limit or 60
  if #out.buildings > limit then
    out.buildings_truncated = #out.buildings
    for i = #out.buildings, limit + 1, -1 do out.buildings[i] = nil end
  end
  if trees.count > 0 then out.trees = trees end
  if rocks.count > 0 then out.rocks = rocks end
  out.other = others
  local water = ch.surface.count_tiles_filtered { position = center, radius = radius, name = water_layer_tiles() }
  if water > 0 then out.water_tiles = water end
  return out
end

CMD.find = function(a, agent)
  -- nearest entities matching name or type
  local ch = char_of(agent)
  local filter = { radius = clamp(a.radius or 64, 1, 300), limit = nil }
  if a.name then filter.name = a.name end
  if a.type then filter.type = a.type end
  if a.name == "tree" then filter.name = nil; filter.type = "tree" end
  if a.name == "rock" then filter.name = nil; filter.type = "simple-entity" end
  if not (filter.name or filter.type) then fail("give name or type") end
  filter.position = parse_pos(a, true) or ch.position
  local list = {}
  for _, e in pairs(ch.surface.find_entities_filtered(filter)) do
    if e ~= ch then list[#list + 1] = e end
  end
  table.sort(list, function(x, y) return dist(ch.position, x.position) < dist(ch.position, y.position) end)
  local out = {}
  for i = 1, math.min(#list, a.limit or 10) do out[i] = brief(list[i], ch.position) end
  return { count = #list, nearest = out }
end

CMD.resources = function(a, agent)
  -- cluster resource tiles into patches
  local ch = char_of(agent)
  local center = parse_pos(a, true) or ch.position
  local radius = clamp(a.radius or 80, 8, 300)
  local filter = { type = "resource", position = center, radius = radius }
  if a.name then filter.name = a.name end
  local by_name = {}
  for _, e in pairs(ch.surface.find_entities_filtered(filter)) do
    local g = by_name[e.name]
    if not g then g = {}; by_name[e.name] = g end
    local x, y = math.floor(e.position.x), math.floor(e.position.y)
    g[x .. "," .. y] = { x = x, y = y, amount = e.amount }
  end
  local patches = {}
  local gap = (a.name == "crude-oil") and 12 or 2
  for name, tiles in pairs(by_name) do
    local seen = {}
    if name == "crude-oil" then gap = 12 else gap = 2 end
    for key, t in pairs(tiles) do
      if not seen[key] then
        seen[key] = true
        local stack = { t }
        local p = { name = name, tiles = 0, amount = 0, sx = 0, sy = 0, x1 = t.x, y1 = t.y, x2 = t.x, y2 = t.y }
        while #stack > 0 do
          local c = table.remove(stack)
          p.tiles = p.tiles + 1
          p.amount = p.amount + c.amount
          p.sx, p.sy = p.sx + c.x, p.sy + c.y
          p.x1, p.y1 = math.min(p.x1, c.x), math.min(p.y1, c.y)
          p.x2, p.y2 = math.max(p.x2, c.x), math.max(p.y2, c.y)
          local d = dist(ch.position, { x = c.x + 0.5, y = c.y + 0.5 })
          if not p.dist or d < p.dist then p.dist = d; p.nearest = { x = c.x + 0.5, y = c.y + 0.5 } end
          for dx = -gap, gap do
            for dy = -gap, gap do
              local k = (c.x + dx) .. "," .. (c.y + dy)
              local n = tiles[k]
              if n and not seen[k] then seen[k] = true; stack[#stack + 1] = n end
            end
          end
        end
        patches[#patches + 1] = {
          name = name, tiles = p.tiles, amount = p.amount,
          center = { x = r2(p.sx / p.tiles + 0.5), y = r2(p.sy / p.tiles + 0.5) },
          bbox = { { x = p.x1, y = p.y1 }, { x = p.x2 + 1, y = p.y2 + 1 } },
          nearest = p.nearest, dist = r2(p.dist),
        }
      end
    end
  end
  table.sort(patches, function(x, y) return x.dist < y.dist end)
  for i = #patches, (a.limit or 15) + 1, -1 do patches[i] = nil end
  local water = ch.surface.find_tiles_filtered { position = center, radius = radius, name = water_layer_tiles(), limit = 4000 }
  local wbest, wd
  for _, tile in pairs(water) do
    local tp = { x = tile.position.x + 0.5, y = tile.position.y + 0.5 }
    local d = dist(ch.position, tp)
    if not wd or d < wd then wbest, wd = tp, d end
  end
  return { patches = patches, nearest_water = wbest and { pos = wbest, dist = r2(wd) } or nil }
end

CMD.map = function(a, agent)
  local ch = char_of(agent)
  local center = parse_pos(a, true) or ch.position
  local r = clamp(a.radius or 12, 3, 40)
  local cx, cy = math.floor(center.x), math.floor(center.y)
  local x0, y0, w = cx - r, cy - r, 2 * r + 1
  local grid = {}
  for j = 1, w do
    local row = {}
    for i = 1, w do row[i] = "." end
    grid[j] = row
  end
  local function put(x, y, c)
    local i, j = x - x0 + 1, y - y0 + 1
    if i >= 1 and i <= w and j >= 1 and j <= w then grid[j][i] = c end
  end
  local area = { { x0, y0 }, { x0 + w, y0 + w } }
  for _, tile in pairs(ch.surface.find_tiles_filtered { area = area, name = water_layer_tiles() }) do
    put(tile.position.x, tile.position.y, "~")
  end
  for _, tile in pairs(ch.surface.find_tiles_filtered { area = area, name = "out-of-map" }) do
    put(tile.position.x, tile.position.y, " ")
  end
  local legend, codes = {}, {}
  local pool = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
  local next_code = 1
  local function code_for(name)
    local c = codes[name]
    if c then return c end
    if next_code > #pool then return "?" end
    c = pool:sub(next_code, next_code)
    next_code = next_code + 1
    codes[name] = c
    legend[c] = name
    return c
  end
  local ents = ch.surface.find_entities_filtered { area = area }
  local layers = { {}, {}, {}, {} }
  for _, e in pairs(ents) do
    local t = e.type
    if t == "resource" then layers[1][#layers[1] + 1] = e
    elseif t == "tree" or t == "simple-entity" or t == "cliff" or t == "item-entity" then layers[2][#layers[2] + 1] = e
    elseif t == "character" then layers[4][#layers[4] + 1] = e
    elseif t ~= "corpse" and t ~= "fish" and t ~= "highlight-box" and e.prototype.selectable_in_game then
      layers[3][#layers[3] + 1] = e
    end
  end
  for li = 1, 4 do
    for _, e in pairs(layers[li]) do
      local t = e.type
      if t == "resource" then
        local c = RESOURCE_CHARS[e.name] or "?"
        if c == "?" then c = code_for(e.name):lower() end
        put(math.floor(e.position.x), math.floor(e.position.y), c)
      elseif t == "character" then
        put(math.floor(e.position.x), math.floor(e.position.y), e == ch and "@" or "P")
      else
        local c
        if t == "tree" then c = "t"
        elseif t == "simple-entity" then c = "r"
        elseif t == "cliff" then c = "#"
        elseif t == "item-entity" then c = "*"
        elseif t == "transport-belt" then c = BELT_CHARS[e.direction] or "="
        elseif e.force and e.force.name == "enemy" then c = "!"
        else c = code_for(e.name) end
        local bb = e.bounding_box
        local xa, ya = math.floor(bb.left_top.x + 0.01), math.floor(bb.left_top.y + 0.01)
        local xb, yb = math.ceil(bb.right_bottom.x - 0.01) - 1, math.ceil(bb.right_bottom.y - 0.01) - 1
        for y = ya, math.max(ya, yb) do
          for x = xa, math.max(xa, xb) do put(x, y, c) end
        end
      end
    end
  end
  local rows = {}
  for j = 1, w do rows[j] = table.concat(grid[j]) end
  return { origin = { x = x0, y = y0 }, rows = rows, legend = legend }
end

-- GUI settings shown by inspect and changed by configure (splitters, filtering inserters)
local function settings_of(e)
  if e.type == "splitter" then
    local f = e.splitter_filter
    return { input_priority = e.splitter_input_priority, output_priority = e.splitter_output_priority,
      filter = f and f.name or "none" }
  elseif e.type == "inserter" and (e.prototype.filter_count or 0) > 0 then
    local fs = {}
    for i = 1, e.prototype.filter_count do
      local f = e.get_filter(i)
      if f then fs[#fs + 1] = type(f) == "table" and f.name or tostring(f) end
    end
    if not e.use_filters and #fs == 0 then return nil end
    return { use_filters = e.use_filters, filter_mode = e.inserter_filter_mode, filters = fs }
  end
end

CMD.inspect = function(a, agent)
  local ch = char_of(agent)
  local e = find_entity(a, ch)
  local s = detail(e, ch)
  s.settings = settings_of(e)
  return s
end

-- Follow a belt line downstream from x,y (through undergrounds, splitters and side-loads) and
-- summarise it: items carried, fill per lane, a one-char-per-belt fill profile, where it is backed
-- up and where it runs empty. Read-only and does not need (or spawn) an agent.
local BELT_FULL = 0.9

local function next_belt(e)
  if e.type == "underground-belt" and e.belt_to_ground_type == "input" then return e.neighbours end
  if e.type == "linked-belt" and e.linked_belt_type == "input" then return e.linked_belt_neighbour end
  local outs = e.belt_neighbours.outputs
  return outs[1], outs[2]
end

local function fill_char(l)
  if l.n == 0 then return "." end
  local f = l.cap > 0 and l.n / l.cap or 1
  if f >= BELT_FULL then return "#" end
  return tostring(clamp(math.floor(f * 10), 1, 8))
end

CMD.belt_status = function(a)
  local ag = storage.agents[a.agent or DEFAULT_AGENT]
  local surface = ag and ag.character and ag.character.valid and ag.character.surface or game.surfaces[1]
  local e
  if a.id then
    e = game.get_entity_by_unit_number(a.id)
  else
    local p = parse_pos(a)
    local bd
    for _, c in pairs(surface.find_entities_filtered {
      area = { { p.x - 0.5, p.y - 0.5 }, { p.x + 0.5, p.y + 0.5 } }, type = BELT_TYPES }) do
      local d = dist(p, c.position)
      if not bd or d < bd then e, bd = c, d end
    end
  end
  if not (e and e.valid and IS_BELT[e.type]) then fail("no belt there") end
  local max_tiles = clamp(math.floor(a.max_tiles or 200), 1, 1000)

  local seen, rows, items = {}, {}, {}
  local tot = { left = { n = 0, cap = 0 }, right = { n = 0, cap = 0 } }
  local splits, sideloads, path = {}, {}, {}
  local end_reason, last_dir, lastb
  while true do
    seen[e.unit_number] = true
    local lanes = belt_lanes(e, true)
    local pos = pos_of(e.position)
    if lastb and e.type ~= "splitter" and e.direction == last_dir then
      -- same direction but shifted sideways (through a splitter): mark both ends of the jog
      local ns = e.direction == defines.direction.north or e.direction == defines.direction.south
      if (ns and pos.x ~= lastb.x) or (not ns and pos.y ~= lastb.y) then
        if path[#path] ~= lastb then path[#path + 1] = lastb end
        path[#path + 1] = pos
      end
    end
    if e.type ~= "splitter" then lastb = pos end
    rows[#rows + 1] = { pos = pos, lanes = lanes }
    for _, side in pairs({ "left", "right" }) do
      local l = lanes[side]
      tot[side].n, tot[side].cap = tot[side].n + l.n, tot[side].cap + l.cap
      for k, v in pairs(l.items) do items[k] = (items[k] or 0) + v end
    end
    if e.direction ~= last_dir then path[#path + 1] = pos; last_dir = e.direction end
    local nxt, alt = next_belt(e)
    if alt then
      if seen[nxt.unit_number] then nxt, alt = alt, nxt end
      splits[#splits + 1] = string.format("%s at %s,%s also feeds %s,%s", e.name, r2(e.position.x),
        r2(e.position.y), r2(alt.position.x), r2(alt.position.y))
    end
    if not (nxt and nxt.valid) then
      end_reason = e.type == "underground-belt" and e.belt_to_ground_type == "input"
        and "underground has no exit" or "dead end"
      break
    end
    if seen[nxt.unit_number] then
      end_reason = string.format("loops back to %s,%s", r2(nxt.position.x), r2(nxt.position.y))
      break
    end
    if #rows >= max_tiles then end_reason = "max_tiles"; break end
    if nxt.type == "transport-belt" and nxt.belt_shape == "straight" and nxt.direction ~= e.direction then
      sideloads[#sideloads + 1] = string.format("%s,%s onto %s,%s", r2(e.position.x), r2(e.position.y),
        r2(nxt.position.x), r2(nxt.position.y))
    end
    e = nxt
  end
  local endpos = rows[#rows].pos
  local lp = path[#path]
  if lp.x ~= endpos.x or lp.y ~= endpos.y then path[#path + 1] = endpos end

  local out = {
    start = rows[1].pos, ["end"] = endpos, end_reason = end_reason, tiles = #rows, items = items,
    fill = pct(tot.left.n + tot.right.n, tot.left.cap + tot.right.cap) .. "%",
    left = pct(tot.left.n, tot.left.cap) .. "%", right = pct(tot.right.n, tot.right.cap) .. "%",
    max_rate = r2(e.prototype.belt_speed * 480) .. "/s", path = path,
    splits = splits, sideloads = sideloads,
  }
  -- per-lane profile, and the full run that ends at a blocked end (backed up)
  local blocked = end_reason == "dead end" or end_reason == "underground has no exit"
  out.backed_up = {}
  for _, side in pairs({ "left", "right" }) do
    local chars = {}
    for i, r in ipairs(rows) do chars[i] = fill_char(r.lanes[side]) end
    out["profile_" .. side] = table.concat(chars)
    local i = #rows
    while i >= 1 and chars[i] == "#" do i = i - 1 end
    if blocked and i < #rows then
      local p = rows[i + 1].pos
      out.backed_up[side] = string.format("%d belts from %s,%s to end", #rows - i, p.x, p.y)
    end
  end
  -- empty stretches (both lanes empty), 2+ belts long
  local empty, i = {}, 1
  while i <= #rows do
    local j = i
    while j <= #rows and rows[j].lanes.left.n + rows[j].lanes.right.n == 0 do j = j + 1 end
    if j - i >= 2 then
      local p, q = rows[i].pos, rows[j - 1].pos
      empty[#empty + 1] = string.format("%s,%s..%s,%s (%d)", p.x, p.y, q.x, q.y, j - i)
    end
    i = math.max(j, i + 1)
  end
  out.empty = empty
  return out
end

local DIR_VEC = { [0] = { 0, -1 }, [4] = { 1, 0 }, [8] = { 0, 1 }, [12] = { -1, 0 } }

-- Where would this entity's pipe connections be if placed at x,y facing direction? (planning aid)
CMD.fluid_ports = function(a)
  local proto = prototypes.entity[a.name or ""]
  if not proto then fail("unknown entity '%s'", tostring(a.name)) end
  local p = parse_pos(a)
  local dir = parse_dir(a.direction)
  local k = math.floor(dir / 4) + 1
  local fluids_in, fluids_out = {}, {}
  if a.recipe and prototypes.recipe[a.recipe] then
    for _, i in pairs(prototypes.recipe[a.recipe].ingredients) do if i.type == "fluid" then fluids_in[#fluids_in + 1] = i.name end end
    for _, i in pairs(prototypes.recipe[a.recipe].products) do if i.type == "fluid" then fluids_out[#fluids_out + 1] = i.name end end
  end
  local out, n_in, n_out = {}, 0, 0
  for i, fb in pairs(proto.fluidbox_prototypes or {}) do
    local guess
    if fb.filter then guess = fb.filter.name
    elseif fb.production_type == "input" then n_in = n_in + 1; guess = fluids_in[n_in]
    elseif fb.production_type == "output" then n_out = n_out + 1; guess = fluids_out[n_out] end
    for _, c in pairs(fb.pipe_connections) do
      if c.connection_type == "normal" and c.positions then
        local off = c.positions[k]
        local d = (c.direction + dir) % 16
        local v = DIR_VEC[d] or { 0, 0 }
        local at = { x = p.x + off.x, y = p.y + off.y }
        out[#out + 1] = { box = i, kind = fb.production_type, fluid = guess, flow = c.flow_direction,
          at = pos_of(at), pipe_goes_at = pos_of({ x = at.x + v[1], y = at.y + v[2] }) }
      end
    end
  end
  return { name = proto.name, pos = pos_of(p), dir = DIR_NAMES[dir], ports = out,
    note = "pipe_goes_at = the tile outside the entity where a pipe must be to connect" }
end

CMD.recipe = function(a, agent)
  local ch = agent.character
  local f = game.forces.player
  if not a.name then fail("give recipe or item name") end
  local r = f.recipes[a.name]
  local out = {}
  if r then out.recipe = recipe_view(r, ch and ch.valid and ch or nil) end
  -- other recipes that produce this item
  local makers = {}
  for name, rec in pairs(f.recipes) do
    if name ~= a.name and not rec.hidden then
      for _, p in pairs(rec.products) do
        if p.name == a.name then makers[#makers + 1] = recipe_view(rec, ch and ch.valid and ch or nil) end
      end
    end
  end
  if #makers > 0 then out.other_recipes_producing = makers end
  if not r and #makers == 0 then fail("no recipe produces '%s'", a.name) end
  local item = prototypes.item[a.name]
  if item then
    out.item = { stack_size = item.stack_size, places = item.place_result and item.place_result.name or nil,
      fuel_value_MJ = item.fuel_value > 0 and r2(item.fuel_value / 1e6) or nil }
  end
  return out
end

CMD.recipes = function(a, agent)
  local ch = agent.character
  local f = game.forces.player
  local out = {}
  for name, r in pairs(f.recipes) do
    if r.enabled and not r.hidden then
      if a.handcraft_only == false or craftable_by_hand(r) then
        local n = (ch and ch.valid and craftable_by_hand(r)) and ch.get_craftable_count(name) or nil
        if not a.craftable_now or (n and n > 0) then out[name] = n or "machine:" .. r.category end
      end
    end
  end
  return { recipes = out }
end

CMD.research_status = function(a, agent)
  local f = game.forces.player
  local available = {}
  for name, t in pairs(f.technologies) do
    if t.enabled and not t.researched then
      local ready = true
      for _, pre in pairs(t.prerequisites) do if not pre.researched then ready = false; break end end
      if ready then available[#available + 1] = tech_view(t) end
    end
  end
  table.sort(available, function(x, y) return (x.units or 0) < (y.units or 0) end)
  local queue = {}
  for _, t in pairs(f.research_queue or {}) do queue[#queue + 1] = t.name end
  local researched = 0
  for _, t in pairs(f.technologies) do if t.researched then researched = researched + 1 end end
  return {
    current = f.current_research and f.current_research.name or nil,
    progress = r2(f.research_progress), queue = queue, researched_count = researched,
    pending = #(storage.pending_research or {}) > 0 and storage.pending_research or nil,
    available = available,
  }
end

CMD.tech = function(a, agent)
  local t = game.forces.player.technologies[a.name or ""]
  if not t then fail("unknown technology '%s'", tostring(a.name)) end
  local v = tech_view(t)
  local pre = {}
  for name, p in pairs(t.prerequisites) do pre[name] = p.researched end
  v.prerequisites = pre
  return v
end

CMD.chat = function(a, agent)
  local out = {}
  local last = agent.chat_cursor
  for _, m in pairs(storage.chat) do
    if m.seq > agent.chat_cursor then
      last = m.seq
      if chat_for(m, agent.name) then out[#out + 1] = m end
    end
  end
  agent.chat_cursor = last
  return { messages = out }
end

CMD.task = function(a, agent)
  if a.id and (not agent.task or agent.task.id ~= a.id) then
    local t = agent.history[a.id]
    if not t then fail("unknown task id %d", a.id) end
    return task_view(t)
  end
  return task_view(agent.task) or { state = "idle" }
end

-- Teammates at a glance: where each character is, what it is doing, how long it has been idle,
-- its posted intent (claim) and what it carries, plus the last chat lines (no cursor moves).
-- Read-only; needs no agent.
CMD.team = function(a)
  local out = {}
  for name, agent in pairs(storage.agents) do
    local ch = agent.character
    local m = { you = (name == a.agent) or nil, intent = agent.intent, task = agent.task and agent.task.kind or nil }
    if ch and ch.valid then
      m.pos = pos_of(ch.position)
      local inv = ch.get_main_inventory()
      local items = {}
      for k, v in pairs(contents(inv)) do items[#items + 1] = { k, v } end
      table.sort(items, function(x, y) return x[2] > y[2] end)
      m.carries = {}
      for i = 1, math.min(8, #items) do m.carries[items[i][1]] = items[i][2] end
      m.free_slots = inv.count_empty_stacks()
    else
      m.dead = true
    end
    if not agent.task then m.idle_s = math.floor((game.tick - (agent.last_act or 0)) / 60) end
    out[name] = m
  end
  local recent = {}
  for i = math.max(1, #storage.chat - clamp(a.chat or 8, 0, 50) + 1), #storage.chat do
    recent[#recent + 1] = storage.chat[i]
  end
  return { members = out, recent_chat = recent }
end

-- Real throughput from the force's production statistics: items and fluids made and used per
-- minute over a window (made = "input", used = "output" in the engine's terms). Read-only.
CMD.production = function(a)
  local force = game.forces.player
  local surface = game.surfaces[a.surface or "nauvis"] or game.surfaces[1]
  local win = defines.flow_precision_index[a.window or "ten_minutes"]
  if not win then fail("window must be one of five_seconds, one_minute, ten_minutes, one_hour, ten_hours") end
  local min_rate = a.min_rate or 0.5
  local function read(stats)
    local out = {}
    local names = {}
    for n in pairs(stats.input_counts) do names[n] = true end
    for n in pairs(stats.output_counts) do names[n] = true end
    for n in pairs(names) do
      local made = stats.get_flow_count { name = n, category = "input", precision_index = win }
      local used = stats.get_flow_count { name = n, category = "output", precision_index = win }
      if made >= min_rate or used >= min_rate then out[n] = { made = r2(made), used = r2(used) } end
    end
    return out
  end
  return {
    window = a.window or "ten_minutes",
    items = read(force.get_item_production_statistics(surface)),
    fluids = read(force.get_fluid_production_statistics(surface)),
  }
end

---------------------------------------------------------------------------------------------------
-- resource audit (read-only): what we own and where, ore left, power, machine capacity

local function audit_surface(a) return game.surfaces[a.surface or "nauvis"] or game.surfaces[1] end

-- Items across the whole factory by place: chests, belts, machines, labs, carried by agents.
-- {item=...} narrows it to one item and lists the chests holding it.
CMD.stock = function(a)
  local surface, force = audit_surface(a), game.forces.player
  local only, min = a.item, a.min or 25
  local by = {}
  local function add(kind, item, n)
    if only and item ~= only then return end
    local e = by[item]; if not e then e = {}; by[item] = e end
    e[kind] = (e[kind] or 0) + n
  end
  local chests = {}
  for _, e in pairs(surface.find_entities_filtered { type = { "container", "logistic-container" }, force = force }) do
    local inv = e.get_inventory(defines.inventory.chest)
    if inv then
      local items, total = {}, 0
      for _, it in pairs(inv.get_contents()) do
        add("chest", it.name, it.count)
        if not only or it.name == only then items[#items + 1] = { it.name, it.count }; total = total + it.count end
      end
      if total > 0 then
        table.sort(items, function(x, y) return x[2] > y[2] end)
        local top = {}
        for i = 1, math.min(only and 1 or 4, #items) do top[items[i][1]] = items[i][2] end
        chests[#chests + 1] = { pos = pos_of(e.position), total = total, items = top }
      end
    end
  end
  for _, e in pairs(surface.find_entities_filtered { type = { "transport-belt", "underground-belt", "splitter" }, force = force }) do
    for i = 1, e.get_max_transport_line_index() do
      for _, it in pairs(e.get_transport_line(i).get_contents()) do add("belt", it.name, it.count) end
    end
  end
  for _, e in pairs(surface.find_entities_filtered { type = { "assembling-machine", "furnace", "boiler", "lab", "mining-drill" }, force = force }) do
    local kind = e.type == "lab" and "lab" or "machine"
    for i = 1, (e.get_max_inventory_index() or 0) do
      local inv = e.get_inventory(i)
      if inv and inv.valid then for _, it in pairs(inv.get_contents()) do add(kind, it.name, it.count) end end
    end
  end
  for _, ag in pairs(storage.agents or {}) do
    local ch = ag.character
    if ch and ch.valid then for _, it in pairs(ch.get_main_inventory().get_contents()) do add("carried", it.name, it.count) end end
  end
  local items, order = {}, {}
  for name, e in pairs(by) do
    local total = 0
    for _, n in pairs(e) do total = total + n end
    if only or total >= min then e.total = total; items[name] = e; order[#order + 1] = name end
  end
  table.sort(order, function(x, y) return items[x].total > items[y].total end)
  local ranked = {}
  for i = 1, math.min(#order, a.limit or 70) do ranked[order[i]] = items[order[i]] end
  table.sort(chests, function(x, y) return x.total > y.total end)
  local top_chests = {}
  for i = 1, math.min(#chests, a.chests or (only and 12 or 15)) do top_chests[i] = chests[i] end
  return { items = ranked, chests = top_chests, chest_count = #chests }
end

-- Ore left per type, how many drills mine it and the hours until it runs out at that rate.
CMD.ore = function(a)
  local surface, force = audit_surface(a), game.forces.player
  local bonus = force.mining_drill_productivity_bonus
  local rate = {}
  for _, d in pairs(surface.find_entities_filtered { type = "mining-drill", force = force }) do
    local t = d.mining_target
    if t and t.valid then
      local r = rate[t.name] or { drills = 0, per_s = 0 }
      r.drills = r.drills + 1
      r.per_s = r.per_s + d.prototype.mining_speed * (1 + bonus) / t.prototype.mineable_properties.mining_time
      rate[t.name] = r
    end
  end
  local out = {}
  for _, name in pairs({ "iron-ore", "copper-ore", "coal", "stone", "uranium-ore" }) do
    local tiles, amount = 0, 0
    for _, r in pairs(surface.find_entities_filtered { name = name, area = { { -400, -400 }, { 400, 400 } } }) do
      tiles = tiles + 1; amount = amount + r.amount
    end
    if tiles > 0 then
      local m = rate[name] or { drills = 0, per_s = 0 }
      out[name] = { tiles = tiles, amount = amount, drills = m.drills, per_min = r2(m.per_s * 60),
        hours_left = m.per_s > 0 and r2(amount / m.per_s / 3600) or nil }
    end
  end
  return { ore = out, productivity_bonus = r2(bonus) }
end

-- Electric supply: engines, what they produce now versus their capacity, boilers, starved machines.
CMD.power = function(a)
  local surface, force = audit_surface(a), game.forces.player
  local engines, gen, cap = 0, 0, 0
  for _, e in pairs(surface.find_entities_filtered { type = "generator", force = force }) do
    engines = engines + 1
    gen = gen + e.energy_generated_last_tick * 60
    local ok, mx = pcall(function() return e.prototype.get_max_energy_production(e.quality) end)
    cap = cap + ((ok and mx) and mx * 60 or 900000)
  end
  local boilers = {}
  for _, e in pairs(surface.find_entities_filtered { type = "boiler", force = force }) do
    local s = STATUS_NAMES[e.status] or "?"
    boilers[s] = (boilers[s] or 0) + 1
  end
  local solar = #surface.find_entities_filtered { type = "solar-panel", force = force }
  local low, nopow = 0, 0
  for _, e in pairs(surface.find_entities_filtered { type = { "assembling-machine", "lab", "mining-drill", "inserter", "furnace" }, force = force }) do
    if e.status == defines.entity_status.low_power then low = low + 1
    elseif e.status == defines.entity_status.no_power then nopow = nopow + 1 end
  end
  return { engines = engines, capacity_mw = r2(cap / 1e6), producing_mw = r2(gen / 1e6),
    load_pct = cap > 0 and math.floor(gen / cap * 100) or 0, boilers = boilers, solar_panels = solar,
    low_power_machines = low, no_power_machines = nopow }
end

-- What every recipe could make per minute with the machines we have, by status; Python compares it
-- with the production statistics (made/used) to find the real bottleneck.
CMD.capacity = function(a)
  local surface, force = audit_surface(a), game.forces.player
  local rows = {}
  for _, e in pairs(surface.find_entities_filtered { type = { "assembling-machine", "furnace" }, force = force }) do
    local r = e.get_recipe()
    if r and r.products[1] then
      local p = r.products[1]
      local amount = p.amount or ((p.amount_min or 1) + (p.amount_max or 1)) / 2
      amount = amount * (p.probability or 1)
      local row = rows[r.name]
      if not row then row = { product = p.name, machines = 0, per_min = 0, st = {} }; rows[r.name] = row end
      row.machines = row.machines + 1
      row.per_min = row.per_min + 60 / (r.energy / math.max(e.crafting_speed, 0.01)) * amount
      local s = STATUS_NAMES[e.status] or "?"
      row.st[s] = (row.st[s] or 0) + 1
    end
  end
  for _, row in pairs(rows) do row.per_min = r2(row.per_min) end
  return { recipes = rows }
end

CMD.set_intent = function(a, agent)
  local t = a.text and tostring(a.text) or nil
  if t == "" then t = nil end
  agent.intent = t and t:sub(1, 160) or nil
  update_label(agent)
  return { intent = agent.intent or "cleared" }
end

---------------------------------------------------------------------------------------------------
-- job board: the overseer posts jobs, each character's worker claims the next one, does it and
-- finishes it. Planning and doing run in parallel, so characters don't wait for the planner.
-- job = {id, title, brief, ["for"] = character or nil (anyone), priority 1 (urgent)..5, after = job id
--        that must be done first, state = open|claimed|done|blocked|cancelled, by, note, ticks}

local JOB_KEEP = 60
local JOB_STATES = { open = true, claimed = true, done = true, blocked = true, cancelled = true }

local function job_store()
  storage.jobs = storage.jobs or {}
  storage.job_seq = storage.job_seq or 0
  return storage.jobs
end

local function job_view(j)
  local v = { id = j.id, title = j.title, brief = j.brief, ["for"] = j["for"], priority = j.priority,
    after = j.after, state = j.state, by = j.by, posted_by = j.posted_by, note = j.note }
  local t = j.done_tick or j.claimed_tick
  if t then v.age_s = math.floor((game.tick - t) / 60) end
  return v
end

local function job_chat(from, msg, to)
  storage.chat_seq = storage.chat_seq + 1
  storage.chat[#storage.chat + 1] = { seq = storage.chat_seq, tick = game.tick, from = from, to = to, message = msg }
  if #storage.chat > CHAT_KEEP then table.remove(storage.chat, 1) end
end

local function prune_jobs()
  local jobs = job_store()
  local finished = 0
  for i = #jobs, 1, -1 do
    local s = jobs[i].state
    if s == "done" or s == "cancelled" then
      finished = finished + 1
      if finished > JOB_KEEP then table.remove(jobs, i) end
    end
  end
end

local function job_by_id(id)
  for _, j in pairs(job_store()) do if j.id == id then return j end end
  fail("no job #%s", tostring(id))
end

CMD.post_job = function(a)
  local title = a.title and tostring(a.title) or ""
  if title == "" then fail("give a title") end
  if a["for"] and not storage.agents[a["for"]] then fail("no character named '%s'", a["for"]) end
  if a.after then job_by_id(a.after) end
  storage.job_seq = (storage.job_seq or 0) + 1
  local j = {
    id = storage.job_seq, title = title:sub(1, 120), brief = a.brief and tostring(a.brief) or nil,
    ["for"] = a["for"], priority = clamp(math.floor(a.priority or 3), 1, 5), after = a.after,
    state = "open", posted_by = a.from or "overseer", posted_tick = game.tick,
  }
  local jobs = job_store()
  jobs[#jobs + 1] = j
  prune_jobs()
  return job_view(j)
end

-- open jobs a character may take: addressed to it or to anyone, with their `after` job done
local function claimable(j, name)
  if j.state ~= "open" then return false end
  if j["for"] and j["for"] ~= name then return false end
  if j.after then
    for _, o in pairs(storage.jobs) do
      if o.id == j.after then return o.state == "done" end
    end
  end
  return true
end

CMD.claim_job = function(a, agent)
  local jobs = job_store()
  for _, j in pairs(jobs) do
    if j.state == "claimed" and j.by == agent.name then return { job = job_view(j), resumed = true } end
  end
  local best
  for _, j in pairs(jobs) do
    if claimable(j, agent.name) then
      -- own jobs first, then priority, then oldest
      local key = { j["for"] and 0 or 1, j.priority, j.id }
      if not best or key[1] < best[1] or (key[1] == best[1] and (key[2] < best[2]
        or (key[2] == best[2] and key[3] < best[3]))) then
        best = key; best.job = j
      end
    end
  end
  if not best then
    local open = 0
    for _, j in pairs(jobs) do if j.state == "open" then open = open + 1 end end
    return { job = nil, open_for_others = open }
  end
  local j = best.job
  j.state, j.by, j.claimed_tick = "claimed", agent.name, game.tick
  agent.intent = "job #" .. j.id .. ": " .. j.title
  update_label(agent)
  return { job = job_view(j) }
end

CMD.finish_job = function(a, agent)
  local j = job_by_id(a.id)
  if j.by ~= agent.name then fail("job #%d is not claimed by %s", j.id, agent.name) end
  local state = a.state or "done"
  if state ~= "done" and state ~= "blocked" then fail("state must be done or blocked") end
  j.state, j.note, j.done_tick = state, a.note and tostring(a.note):sub(1, 300) or nil, game.tick
  agent.intent = nil
  update_label(agent)
  job_chat(agent.name, string.format("job #%d %s: %s%s", j.id, state, j.title, j.note and (" - " .. j.note) or ""),
    j.posted_by ~= agent.name and j.posted_by or nil)
  prune_jobs()
  return job_view(j)
end

-- overseer edits: reopen a blocked/claimed job, cancel it, or change priority/brief/owner
CMD.update_job = function(a)
  local j = job_by_id(a.id)
  if a.state then
    if not JOB_STATES[a.state] then fail("unknown state '%s'", tostring(a.state)) end
    if a.state == "open" then j.by, j.claimed_tick = nil, nil end
    j.state = a.state
  end
  if a.priority then j.priority = clamp(math.floor(a.priority), 1, 5) end
  if a.brief then j.brief = tostring(a.brief) end
  if a.title then j.title = tostring(a.title):sub(1, 120) end
  if a["for"] ~= nil then
    if a["for"] == "" or a["for"] == "any" then j["for"] = nil
    elseif storage.agents[a["for"]] then j["for"] = a["for"]
    else fail("no character named '%s'", a["for"]) end
  end
  if a.note then j.note = tostring(a.note):sub(1, 300) end
  return job_view(j)
end

CMD.jobs = function(a)
  local out, done = {}, {}
  for _, j in pairs(job_store()) do
    if a.all or j.state == "open" or j.state == "claimed" or j.state == "blocked" then
      out[#out + 1] = job_view(j)
    elseif j.state == "done" then
      done[#done + 1] = j
    end
  end
  -- plus the last few finished, newest first, so the planner sees results
  table.sort(done, function(x, y) return (x.done_tick or 0) > (y.done_tick or 0) end)
  if not a.all then
    for i = 1, math.min(clamp(a.recent or 5, 0, 30), #done) do out[#out + 1] = job_view(done[i]) end
  end
  return { jobs = out }
end

CMD.agents = function()
  local out = {}
  for name, agent in pairs(storage.agents) do
    local ch = agent.character
    out[name] = { alive = ch ~= nil and ch.valid, position = ch and ch.valid and pos_of(ch.position) or nil,
      task = agent.task and agent.task.kind or nil }
  end
  return out
end

-- Read-only whole-base view for agent/viewer.py. Never touches agents' chat cursors or tasks.
-- Entities: {n=name, t=type, x, y, w, h, d=direction (0..15), s=status, r=recipe}.
-- With resources=true also returns resource and water tiles (as flat x,y lists) around the base.
CMD.snapshot = function(a)
  local surface = game.surfaces[a.surface or "nauvis"] or game.surfaces[1]
  local f = game.forces.player
  local ents = {}
  local x1, y1, x2, y2
  local function grow(x, y)
    if not x1 then x1, y1, x2, y2 = x, y, x, y
    else x1, y1, x2, y2 = math.min(x1, x), math.min(y1, y), math.max(x2, x), math.max(y2, y) end
  end
  for _, e in pairs(surface.find_entities_filtered { force = f }) do
    local t = e.type
    if t ~= "character" and t ~= "highlight-box" and t ~= "character-corpse" then
      -- tile footprint (rotation-aware); collision boxes are smaller than the tiles they occupy
      local ok_wh, w, h = pcall(function() return e.tile_width, e.tile_height end)
      if not ok_wh then
        local bb = e.bounding_box
        w, h = math.ceil(bb.right_bottom.x - bb.left_top.x - 0.01), math.ceil(bb.right_bottom.y - bb.left_top.y - 0.01)
      end
      local s = { n = e.name, t = t, x = r2(e.position.x), y = r2(e.position.y), w = w, h = h }
      if e.supports_direction then s.d = e.direction end
      local ok, st = pcall(function() return e.status end)
      if ok and st then s.s = STATUS_NAMES[st] end
      if t == "assembling-machine" or t == "furnace" then
        local r = e.get_recipe()
        s.r = r and r.name or nil
      elseif t == "transport-belt" then
        s.bs = e.belt_shape
      elseif t == "underground-belt" then
        s.bt = e.belt_to_ground_type
      elseif t == "pipe" then
        -- connected sides as a bitmask: 1 north, 2 east, 4 south, 8 west
        local mask = 0
        pcall(function()
          for _, c in pairs(e.fluidbox.get_pipe_connections(1)) do
            if c.target then
              local dx, dy = c.target_position.x - e.position.x, c.target_position.y - e.position.y
              if math.abs(dy) > math.abs(dx) then mask = bit32.bor(mask, dy < 0 and 1 or 4)
              else mask = bit32.bor(mask, dx > 0 and 2 or 8) end
            end
          end
        end)
        s.pc = mask
      end
      ents[#ents + 1] = s
      grow(e.position.x, e.position.y)
    end
  end
  local agents = {}
  for name, agent in pairs(storage.agents) do
    local ch = agent.character
    local v = { name = name, alive = ch ~= nil and ch.valid, task = task_view(agent.task) }
    if v.alive then
      v.position = pos_of(ch.position)
      v.health = r2(ch.health)
      v.dir = ch.direction
      v.walking = ch.walking_state.walking or nil
      v.mining = ch.mining_state.mining or nil
      v.score, v.why = director_score(agent, game.tick)
      if ch.crafting_queue_size > 0 then
        local q = ch.crafting_queue[1]
        v.crafting = { recipe = q.recipe, count = q.count, queue = ch.crafting_queue_size }
      end
      grow(ch.position.x, ch.position.y)
    end
    agents[#agents + 1] = v
  end
  local players = {}
  for _, p in pairs(game.connected_players) do players[#players + 1] = p.name end
  local queue = {}
  for _, t in pairs(f.research_queue or {}) do queue[#queue + 1] = t.name end
  local researched = 0
  for _, t in pairs(f.technologies) do if t.researched then researched = researched + 1 end end
  local chat = {}
  local n = clamp(a.chat or 20, 0, CHAT_KEEP)
  for i = math.max(1, #storage.chat - n + 1), #storage.chat do chat[#chat + 1] = storage.chat[i] end
  local out = {
    tick = game.tick, paused = game.tick_paused, mode = storage.mode, speed = game.speed,
    humans_online = players, entities = ents, agents = agents, chat = chat,
    research = {
      current = f.current_research and f.current_research.name or nil,
      progress = r2(f.research_progress), queue = queue, researched_count = researched,
    pending = #(storage.pending_research or {}) > 0 and storage.pending_research or nil,
    },
  }
  if x1 then out.bbox = { x1 = math.floor(x1), y1 = math.floor(y1), x2 = math.ceil(x2), y2 = math.ceil(y2) } end
  if a.resources and x1 then
    local m = clamp(a.margin or 48, 0, 200)
    local area = { { math.floor(x1) - m, math.floor(y1) - m }, { math.ceil(x2) + m, math.ceil(y2) + m } }
    local res = {}
    for _, e in pairs(surface.find_entities_filtered { area = area, type = "resource" }) do
      local l = res[e.name]
      if not l then l = {}; res[e.name] = l end
      l[#l + 1] = math.floor(e.position.x)
      l[#l + 1] = math.floor(e.position.y)
    end
    local water = {}
    for _, tile in pairs(surface.find_tiles_filtered { area = area, name = water_layer_tiles(), limit = 40000 }) do
      water[#water + 1] = tile.position.x
      water[#water + 1] = tile.position.y
    end
    out.resources = res
    out.water = water
    out.area = { x1 = area[1][1], y1 = area[1][2], x2 = area[2][1], y2 = area[2][2] }
  end
  return out
end

-- Read-only ground layer for agent/viewer.py, per 32x32 chunk: {chunks = {{cx, cy}, ...}} (max 16).
-- Names are indices into `names`. tiles = run-length "id*count,..." row by row; res = flat
-- {id, x, y, amount}; trees = flat {id, x, y, variation, color}; rocks = flat {id, x, y, variation}.
CMD.terrain = function(a)
  local surface = game.surfaces[a.surface or "nauvis"] or game.surfaces[1]
  local names, index = {}, {}
  local function id(n)
    local i = index[n]
    if not i then i = #names; names[#names + 1] = n; index[n] = i end
    return i
  end
  local chunks = {}
  for k, c in pairs(a.chunks or {}) do
    if k > 16 then break end
    local cx, cy = math.floor(c[1]), math.floor(c[2])
    local x0, y0 = cx * 32, cy * 32
    local out = { cx = cx, cy = cy }
    if not surface.is_chunk_generated { cx, cy } then
      out.missing = true
    else
      local runs, last, n = {}, nil, 0
      for _, tile in pairs(surface.find_tiles_filtered { area = { { x0, y0 }, { x0 + 32, y0 + 32 } } }) do
        -- find_tiles_filtered order is not guaranteed; index by position instead
        runs[(tile.position.y - y0) * 32 + (tile.position.x - x0) + 1] = id(tile.name)
      end
      local rle = {}
      for i = 1, 1024 do
        local t = runs[i] or -1
        if t == last then n = n + 1 else
          if last then rle[#rle + 1] = last .. "*" .. n end
          last, n = t, 1
        end
      end
      rle[#rle + 1] = last .. "*" .. n
      out.tiles = table.concat(rle, ",")
      local res, trees, rocks = {}, {}, {}
      local area = { { x0, y0 }, { x0 + 32, y0 + 32 } }
      for _, e in pairs(surface.find_entities_filtered { area = area, type = { "resource", "tree", "simple-entity" } }) do
        local p = e.position
        if p.x >= x0 and p.x < x0 + 32 and p.y >= y0 and p.y < y0 + 32 then
          local var = 1
          pcall(function() var = e.graphics_variation or 1 end)
          if e.type == "resource" then
            res[#res + 1] = id(e.name); res[#res + 1] = math.floor(p.x); res[#res + 1] = math.floor(p.y)
            res[#res + 1] = e.amount
          elseif e.type == "tree" then
            local col = 1
            pcall(function() col = e.tree_color_index or 1 end)
            trees[#trees + 1] = id(e.name); trees[#trees + 1] = r2(p.x); trees[#trees + 1] = r2(p.y)
            trees[#trees + 1] = var; trees[#trees + 1] = col
          else
            rocks[#rocks + 1] = id(e.name); rocks[#rocks + 1] = r2(p.x); rocks[#rocks + 1] = r2(p.y)
            rocks[#rocks + 1] = var
          end
        end
      end
      out.res, out.trees, out.rocks = res, trees, rocks
    end
    chunks[#chunks + 1] = out
  end
  return { names = names, chunks = chunks }
end

-------------------------------------------------- actions

CMD.move_to = function(a, agent)
  local ch = char_of(agent)
  local goal = parse_pos(a)
  local radius = a.radius or 1
  if dist(ch.position, goal) <= radius then return { task_id = 0, already_there = true, position = pos_of(ch.position) } end
  return start_task(agent, { kind = "move", mv = new_move(goal, radius) })
end

CMD.mine = function(a, agent)
  local ch = char_of(agent)
  local count = math.max(1, math.floor(a.count or 1))
  local p = parse_pos(a, true)
  local target
  local want = a.name
  if p then
    local found = ch.surface.find_entities_filtered {
      area = { { p.x - 0.5, p.y - 0.5 }, { p.x + 0.5, p.y + 0.5 } },
      name = (want ~= "tree" and want ~= "rock") and want or nil,
    }
    local bd
    for _, e in pairs(found) do
      if e ~= ch and e.prototype.mineable_properties.minable and e.type ~= "character" then
        if want ~= "tree" or e.type == "tree" then
          local d = dist(p, e.position) + (e.type == "resource" and 10 or 0) -- prefer buildings over ore under them
          if want and e.type == "resource" then d = d - 10 end
          if not bd or d < bd then target, bd = e, d end
        end
      end
    end
  elseif want then
    local radius = clamp(a.radius or 160, 1, 300)
    local filter = { name = want, radius = radius }
    if want == "tree" then filter = { type = "tree", radius = radius } end
    if want == "rock" then filter = { type = "simple-entity", radius = radius } end
    if not prototypes.entity[want] and not filter.type then fail("unknown entity name '%s'", want) end
    target = nearest(ch, filter)
    if not target then fail("no %s within %d tiles; explore (find_resources with a bigger radius, move_to)", want, radius) end
  else
    fail("give a position (x, y) or a name to mine")
  end
  if not target then fail("nothing minable at (%.1f, %.1f)", p.x, p.y) end
  local is_resource = target.type == "resource"
  local t = {
    kind = "mine", target = target, count = count, mined = 0, progress = 0,
    need = mining_ticks(ch, target), is_resource = is_resource,
    origin = { x = target.position.x, y = target.position.y },
    search_radius = is_resource and 6 or 24,
  }
  if is_resource then
    if target.prototype.mineable_properties.required_fluid then
      fail("%s needs a mining drill with fluid; it can't be mined by hand", target.name)
    end
    t.match_name = target.name
  elseif target.type == "tree" or target.type == "simple-entity" then
    t.match_type = target.type
  else
    t.match_name = target.name
    t.count = 1 -- buildings: pick up exactly this one
    t.search_radius = 0
  end
  local r = start_task(agent, t)
  r.target = brief(target, ch.position)
  r.seconds_per_unit = r2(t.need / 60)
  return r
end

CMD.wait = function(a, agent)
  char_of(agent)
  local ticks = math.max(1, math.floor((a.seconds or 1) * 60))
  return start_task(agent, { kind = "wait", until_tick = game.tick + ticks })
end

CMD.craft = function(a, agent)
  local ch = char_of(agent)
  local name = a.recipe or a.name
  local r = ch.force.recipes[name or ""]
  if not r then fail("unknown recipe '%s'", tostring(name)) end
  if not r.enabled then fail("recipe '%s' is not unlocked yet (research it)", name) end
  if not craftable_by_hand(r) then fail("'%s' can't be crafted by hand (category %s); use a machine", name, r.category) end
  local count = math.max(1, math.floor(a.count or 1))
  local can = ch.get_craftable_count(name)
  if can < 1 then
    local inv = ch.get_main_inventory()
    local missing = {}
    for _, ing in pairs(r.ingredients) do
      local have = inv.get_item_count(ing.name)
      if have < ing.amount * count then missing[ing.name] = ing.amount * count - have end
    end
    fail("not enough ingredients for %s: missing %s (ingredients can be crafted first)", name, helpers.table_to_json(missing))
  end
  local before = contents(ch.get_main_inventory())
  local started = ch.begin_crafting { recipe = name, count = math.min(count, can) }
  local result = { started = started, requested = count }
  if started < count then result.note = string.format("only enough ingredients for %d", started) end
  if a.wait == false then return result end
  local secs = r.energy * started * 3 + 30
  local t = start_task(agent, { kind = "craft", recipe = name, count = started, inv_before = before,
    deadline = game.tick + math.ceil(secs * 60) })
  t.crafting = started
  t.note = result.note
  return t
end

CMD.cancel_crafting = function(a, agent)
  local ch = char_of(agent)
  local n = 0
  while ch.crafting_queue_size > 0 do
    local q = ch.crafting_queue
    local last = q[#q]
    ch.cancel_crafting { index = last.index, count = last.count }
    n = n + 1
    if n > 100 then break end
  end
  agent.craft_snapshot = nil -- cancelled entries are not crafts
  return { cancelled_entries = n }
end

CMD.cancel = function(a, agent)
  if not agent.task then return { cancelled = false } end
  local id = agent.task.id
  finish_task(agent, "failed", "cancelled")
  return { cancelled = true, task_id = id }
end

CMD.place = function(a, agent)
  local ch = char_of(agent)
  local name = a.item or a.name
  local item = prototypes.item[name or ""]
  if not item then fail("unknown item '%s'", tostring(name)) end
  local res = item.place_result
  if not res then fail("'%s' can't be placed as an entity", name) end
  if ch.get_item_count(name) < 1 then fail("no %s in inventory", name) end
  local p = parse_pos(a)
  local dir = parse_dir(a.direction)
  local build_dist = prototypes.entity["character"].build_distance + ch.force.character_build_distance_bonus
    + ch.character_build_distance_bonus
  local d = dist(ch.position, p)
  if d > build_dist then
    fail("(%.1f, %.1f) is %.1f tiles away, build range is %d; move closer first", p.x, p.y, d, build_dist)
  end
  local params = {
    name = res.name, position = p, direction = dir, force = ch.force,
    build_check_type = defines.build_check_type.manual,
  }
  if not ch.surface.can_place_entity(params) then
    -- a human building on their own feet gets nudged aside; do the same for the agent
    local box = res.collision_box
    local x1, y1 = p.x + box.left_top.x - 0.4, p.y + box.left_top.y - 0.4
    local x2, y2 = p.x + box.right_bottom.x + 0.4, p.y + box.right_bottom.y + 0.4
    local cp = ch.position
    if cp.x > x1 and cp.x < x2 and cp.y > y1 and cp.y < y2 then
      local best, bd
      for _, c in pairs({ { x1, cp.y }, { x2, cp.y }, { cp.x, y1 }, { cp.x, y2 }, { x1, y1 }, { x2, y1 }, { x1, y2 }, { x2, y2 } }) do
        local spot = { x = c[1], y = c[2] }
        if ch.surface.can_place_entity { name = "character", position = spot } then
          local d = dist(cp, spot)
          if not bd or d < bd then best, bd = spot, d end
        end
      end
      if best then
        ch.teleport(best)
        if not ch.surface.can_place_entity(params) then ch.teleport(cp) end
      end
    end
  end
  if not ch.surface.can_place_entity(params) then
    -- explain what is in the way
    local box = res.collision_box
    local blockers = {}
    for _, e in pairs(ch.surface.find_entities_filtered {
      area = { { p.x + box.left_top.x, p.y + box.left_top.y }, { p.x + box.right_bottom.x, p.y + box.right_bottom.y } },
    }) do
      if e.type ~= "resource" and e.type ~= "corpse" then blockers[#blockers + 1] = e.name .. "@" .. r2(e.position.x) .. "," .. r2(e.position.y) end
    end
    local water = ch.surface.count_tiles_filtered {
      area = { { p.x + box.left_top.x, p.y + box.left_top.y }, { p.x + box.right_bottom.x, p.y + box.right_bottom.y } },
      name = water_layer_tiles(),
    }
    local why = #blockers > 0 and ("blocked by " .. table.concat(blockers, ", ")) or ""
    if water > 0 then why = why .. (why ~= "" and "; " or "") .. water .. " water tiles" end
    if why == "" then why = "invalid spot (e.g. needs resources/water underneath or wrong terrain)" end
    fail("can't place %s at (%.1f, %.1f): %s", res.name, p.x, p.y, why)
  end
  local create = {
    name = res.name, position = p, direction = dir, force = ch.force, raise_built = true,
    move_stuck_players = true, create_build_effect_smoke = false,
  }
  if res.type == "underground-belt" then create.type = a.belt_type or "input" end
  local e = ch.surface.create_entity(create)
  if not e then fail("placement failed") end
  ch.remove_item { name = name, count = 1 }
  local out = brief(e, ch.position)
  if e.type == "mining-drill" then
    local r = e.prototype.mining_drill_radius
    local ores = {}
    for _, o in pairs(ch.surface.find_entities_filtered {
      type = "resource", area = { { e.position.x - r, e.position.y - r }, { e.position.x + r, e.position.y + r } },
    }) do ores[o.name] = (ores[o.name] or 0) + o.amount end
    if next(ores) then out.ore_under = ores else out.warning = "no resource under this drill" end
    out.drop_position = pos_of(e.drop_position)
    local dp = e.drop_position
    local target
    for _, o in pairs(ch.surface.find_entities_filtered {
      area = { { dp.x - 0.05, dp.y - 0.05 }, { dp.x + 0.05, dp.y + 0.05 } }, force = ch.force,
    }) do
      if o ~= e and o.type ~= "character" then target = o; break end
    end
    out.drops_into = target and target.name or "ground"
  end
  return out
end

CMD.insert = function(a, agent)
  local ch = char_of(agent)
  local ent = find_entity(a, ch)
  require_reach(ch, ent)
  local item = a.item
  if not (item and prototypes.item[item]) then fail("unknown item '%s'", tostring(item)) end
  local have = ch.get_item_count(item)
  if have < 1 then fail("no %s in inventory", item) end
  local count = math.min(math.floor(a.count or have), have)
  local inserted
  if a.inventory then
    local idx = defines.inventory[a.inventory]
    if not idx then fail("unknown inventory '%s'", a.inventory) end
    local inv = ent.get_inventory(idx)
    if not inv then fail("%s has no %s inventory", ent.name, a.inventory) end
    inserted = inv.insert { name = item, count = count }
  else
    inserted = ent.insert { name = item, count = count }
  end
  if inserted > 0 then ch.remove_item { name = item, count = inserted } end
  if inserted == 0 then fail("%s does not accept %s (or is full)", ent.name, item) end
  return { inserted = inserted, into = brief(ent) }
end

CMD.take = function(a, agent)
  local ch = char_of(agent)
  local ent = find_entity(a, ch)
  require_reach(ch, ent)
  local invs = {}
  if a.inventory then
    local idx = defines.inventory[a.inventory]
    if not idx then fail("unknown inventory '%s'", a.inventory) end
    invs[1] = ent.get_inventory(idx)
  else
    for i = 1, ent.get_max_inventory_index() or 0 do invs[#invs + 1] = ent.get_inventory(i) end
  end
  local taken = {}
  local left = a.count and math.floor(a.count) or math.huge
  for _, inv in pairs(invs) do
    if inv and inv.valid then
      for _, it in pairs(inv.get_contents()) do
        if left <= 0 then break end
        if not a.item or it.name == a.item then
          local n = math.min(it.count, left)
          local moved = ch.insert { name = it.name, count = n, quality = it.quality }
          if moved > 0 then
            inv.remove { name = it.name, count = moved, quality = it.quality }
            taken[it.name] = (taken[it.name] or 0) + moved
            left = left - moved
          end
        end
      end
    end
  end
  if not next(taken) then fail("nothing to take%s from %s", a.item and (" (" .. a.item .. ")") or "", ent.name) end
  return { taken = taken, from = brief(ent) }
end

CMD.set_recipe = function(a, agent)
  local ch = char_of(agent)
  local ent = find_entity(a, ch)
  require_reach(ch, ent)
  if ent.type ~= "assembling-machine" then fail("%s is not an assembling machine", ent.name) end
  local r = ch.force.recipes[a.recipe or ""]
  if not r then fail("unknown recipe '%s'", tostring(a.recipe)) end
  if not r.enabled then fail("recipe '%s' is not unlocked yet", a.recipe) end
  local removed = ent.set_recipe(a.recipe)
  for _, it in pairs(removed or {}) do ch.insert { name = it.name, count = it.count, quality = it.quality } end
  return brief(ent)
end

CMD.rotate = function(a, agent)
  local ch = char_of(agent)
  local ent = find_entity(a, ch)
  require_reach(ch, ent)
  if not ent.rotatable then fail("%s can't be rotated", ent.name) end
  ent.rotate { reverse = a.reverse == true }
  return brief(ent)
end

-- Upgrade in place, like a player placing a steel furnace over a stone one: same footprint and
-- fast-replace group (furnace, belt, inserter, assembler, pole, chest tiers). Contents, recipe and
-- settings carry over; the old building goes back to the inventory (spilled if it has no room).
CMD.upgrade = function(a, agent)
  local ch = char_of(agent)
  local name = a.item
  local item = prototypes.item[name or ""]
  if not item then fail("unknown item '%s'", tostring(name)) end
  local res = item.place_result
  if not res then fail("'%s' can't be placed as an entity", name) end
  if ch.get_item_count(name) < 1 then fail("no %s in inventory", name) end
  local old = find_entity({ x = a.x, y = a.y, id = a.id, position = a.position }, ch)
  if old.name == res.name then fail("%s is already %s (use rotate to turn it)", old.name, res.name) end
  local build_dist = prototypes.entity["character"].build_distance + ch.force.character_build_distance_bonus
    + ch.character_build_distance_bonus
  local d = dist(ch.position, old.position)
  if d > build_dist then fail("%s is %.1f tiles away, build range is %d; move closer first", old.name, d, build_dist) end
  local dir = a.direction and parse_dir(a.direction) or old.direction
  local params = { name = res.name, position = old.position, direction = dir, force = ch.force }
  if not ch.surface.can_fast_replace(params) then
    fail("can't upgrade %s to %s in place (needs the same size and upgrade family, e.g. stone-furnace -> "
      .. "steel-furnace, belt/inserter/assembler tiers)", old.name, res.name)
  end
  local was, pos = old.name, old.position
  local create = {
    name = res.name, position = pos, direction = dir, force = ch.force, fast_replace = true,
    character = ch, spill = true, raise_built = true, create_build_effect_smoke = false,
  }
  if old.type == "underground-belt" then create.type = old.belt_to_ground_type end
  local e = ch.surface.create_entity(create)
  if not (e and e.valid) then fail("upgrade failed") end
  ch.remove_item { name = name, count = 1 }
  local out = brief(e, ch.position)
  out.replaced = was
  return out
end

-- Settings a player changes in the entity GUI: splitter input/output priority and item filter,
-- inserter filters (whitelist/blacklist). Omitted fields stay as they are.
local SIDES = { left = true, right = true, none = true }

CMD.configure = function(a, agent)
  local ch = char_of(agent)
  local e = find_entity(a, ch)
  require_reach(ch, e)
  if e.type == "splitter" then
    for _, k in pairs({ "input_priority", "output_priority" }) do
      if a[k] ~= nil then
        if not SIDES[a[k]] then fail("%s must be left, right or none", k) end
        e["splitter_" .. k] = a[k]
      end
    end
    if a.filter ~= nil then
      if a.filter == "" or a.filter == "none" then
        e.splitter_filter = nil
      else
        if not prototypes.item[a.filter] then fail("unknown item '%s'", tostring(a.filter)) end
        e.splitter_filter = { name = a.filter }
        -- a filter without an output priority does nothing; send filtered items left by default
        if e.splitter_output_priority == "none" and a.output_priority == nil then e.splitter_output_priority = "left" end
      end
    end
  elseif e.type == "inserter" then
    local n = e.prototype.filter_count or 0
    if n == 0 then fail("%s has no filter slots", e.name) end
    if a.filter_mode ~= nil then
      if a.filter_mode ~= "whitelist" and a.filter_mode ~= "blacklist" then fail("filter_mode must be whitelist or blacklist") end
      e.inserter_filter_mode = a.filter_mode
    end
    if a.filters ~= nil then
      local list = type(a.filters) == "table" and a.filters or { a.filters }
      if #list > n then fail("%s has only %d filter slots", e.name, n) end
      for _, it in pairs(list) do
        if not prototypes.item[it] then fail("unknown item '%s'", tostring(it)) end
      end
      for i = 1, n do e.set_filter(i, list[i]) end
      e.use_filters = #list > 0
    end
  else
    fail("%s has no settings to configure (splitters and inserters do)", e.name)
  end
  local out = brief(e)
  out.settings = settings_of(e)
  return out
end

CMD.research = function(a, agent)
  local f = game.forces.player
  local t = f.technologies[a.name or ""]
  if not t then fail("unknown technology '%s'", tostring(a.name)) end
  if t.researched then fail("%s is already researched", a.name) end
  if t.prototype.research_trigger then
    fail("%s is a trigger technology: it completes by itself when you %s", a.name, tech_view(t).trigger)
  end
  -- queue unresearched prerequisites first (depth-first, like the game's own queue button)
  local order, seen, triggers = {}, {}, {}
  local function visit(tech)
    if seen[tech.name] or tech.researched then return end
    seen[tech.name] = true
    for _, pre in pairs(tech.prerequisites) do visit(pre) end
    if tech.prototype.research_trigger then triggers[#triggers + 1] = tech.name .. " (" .. tech_view(tech).trigger .. ")"
    else order[#order + 1] = tech end
  end
  visit(t)
  if #triggers > 0 then
    fail("%s first needs trigger technologies: %s", a.name, table.concat(triggers, ", "))
  end
  -- the API only queues techs whose prerequisites are finished; the rest wait in a pending list
  -- that on_research_finished drains as prerequisites complete
  local queued_now = {}
  for _, q in pairs(f.research_queue or {}) do queued_now[q.name] = true end
  for _, p in pairs(storage.pending_research) do queued_now[p] = true end
  local pending = {}
  for _, tech in ipairs(order) do
    if not queued_now[tech.name] then
      local ready = true
      for _, pre in pairs(tech.prerequisites) do if not pre.researched then ready = false; break end end
      if not (ready and f.add_research(tech)) then
        storage.pending_research[#storage.pending_research + 1] = tech.name
        pending[#pending + 1] = tech.name
      end
    end
  end
  local queue = {}
  for _, q in pairs(f.research_queue or {}) do queue[#queue + 1] = q.name end
  return { queued = a.name, queue = queue, pending = #storage.pending_research > 0 and storage.pending_research or nil,
    note = "labs need science packs inserted to make progress" }
end

CMD.say = function(a, agent)
  local msg = tostring(a.message or "")
  if msg == "" then fail("empty message") end
  local to = a.to and tostring(a.to) or nil
  if to == "" or to == "all" then to = nil end
  if to and not storage.agents[to] and not game.get_player(to) then fail("nobody named '%s'", to) end
  game.print("[color=0.5,0.85,1][" .. agent.name .. (to and (" > " .. to) or "") .. "][/color] " .. msg)
  storage.chat_seq = storage.chat_seq + 1
  storage.chat[#storage.chat + 1] = { seq = storage.chat_seq, tick = game.tick, from = agent.name, to = to, message = msg }
  if #storage.chat > CHAT_KEEP then table.remove(storage.chat, 1) end
  local ch = agent.character
  if ch and ch.valid then
    rendering.draw_text {
      text = msg, surface = ch.surface, target = { entity = ch, offset = { 0, -3.4 } },
      color = { 1, 1, 1 }, scale = 1.2, alignment = "center", vertical_alignment = "bottom",
      time_to_live = 60 * 8,
    }
  end
  return { said = msg }
end

CMD.respawn = function(a, agent)
  local ch = agent.character
  if ch and ch.valid then fail("agent is alive; nothing to do") end
  if agent.task then finish_task(agent, "failed", "character died") end
  local respawn = { ["pistol"] = 1, ["firearm-magazine"] = 10 }
  if remote.interfaces["freeplay"] and remote.interfaces["freeplay"]["get_respawn_items"] then
    local ok, items = pcall(remote.call, "freeplay", "get_respawn_items")
    if ok and items then respawn = items end
  end
  spawn_character(agent, respawn)
  return agent_state(agent)
end

CMD.set_mode = function(a)
  if a.mode ~= "auto" and a.mode ~= "turn" and a.mode ~= "realtime" then
    fail("mode must be auto, turn or realtime")
  end
  storage.mode = a.mode
  if a.mode == "realtime" then game.tick_paused = false end
  return { mode = storage.mode }
end

CMD.set_speed = function(a)
  game.speed = clamp(a.speed or 1, 0.1, 20)
  return { speed = game.speed }
end

---------------------------------------------------------------------------------------------------
-- dispatch

local NO_AGENT = { ping = true, agents = true, snapshot = true, terrain = true, set_mode = true, set_speed = true,
  belt_status = true, team = true, post_job = true, update_job = true, jobs = true, production = true, stock = true, ore = true, power = true, capacity = true }


local function dispatch(req)
  local fn = CMD[req.cmd or ""]
  if not fn then
    local names = {}
    for k in pairs(CMD) do names[#names + 1] = k end
    table.sort(names)
    fail("unknown cmd '%s'; known: %s", tostring(req.cmd), table.concat(names, ", "))
  end
  local agent = (not NO_AGENT[req.cmd]) and get_agent(req.agent) or nil
  local res = fn(req, agent)
  -- remember the last successful action for the spectator director (see director_score)
  if agent and DIRECTOR_ACT[req.cmd] then agent.last_act, agent.last_act_kind = game.tick, req.cmd end
  return res
end

local function reply(cmd, text)
  if cmd.player_index then
    game.get_player(cmd.player_index).print(text)
  else
    rcon.print(text)
  end
end

commands.add_command("agent", "Agent bridge request: /agent <json>", function(cmd)
  local req = cmd.parameter and helpers.json_to_table(cmd.parameter)
  if type(req) ~= "table" then
    return reply(cmd, helpers.table_to_json { ok = false, error = "parameter must be a JSON object" })
  end
  local ok, res = pcall(dispatch, req)
  local out
  if ok then
    out = { ok = true, result = res }
  else
    out = { ok = false, error = type(res) == "table" and res.agent_error or tostring(res) }
  end
  local good, text = pcall(helpers.table_to_json, out)
  if not good then text = helpers.table_to_json { ok = false, error = "could not encode reply: " .. tostring(text) } end
  reply(cmd, text)
end)

commands.add_command("agent-mode", "Time mode for agents: auto | turn | realtime", function(cmd)
  local ok, res = pcall(CMD.set_mode, { mode = cmd.parameter })
  reply(cmd, ok and ("agent mode: " .. res.mode) or ("usage: /agent-mode auto|turn|realtime"))
end)

-- Humans watch as spectators by default; their own character is parked and given back on /agent-play.
local function spectate(player, name)
  local agent = storage.agents[name or DEFAULT_AGENT]
  if player.controller_type == defines.controllers.character and player.character then
    storage.parked[player.index] = player.character
    player.set_controller { type = defines.controllers.spectator }
  elseif player.controller_type ~= defines.controllers.spectator then
    player.set_controller { type = defines.controllers.spectator }
  end
  if agent and agent.character and agent.character.valid then
    player.teleport(agent.character.position, agent.character.surface)
    storage.followers[player.index] = agent.name
    player.print("Spectating " .. agent.name .. ". /agent-play gives you your own character, "
      .. "/agent-follow <name> switches agent, /agent-auto follows the busiest one, /agent-mode auto|turn|realtime sets time.")
  else
    player.print("Spectating (no agent yet).")
  end
end

commands.add_command("agent-follow", "Spectate an agent: /agent-follow [name]", function(cmd)
  local player = cmd.player_index and game.get_player(cmd.player_index)
  if not player then return end
  if cmd.parameter and not storage.agents[cmd.parameter] then return player.print("no such agent") end
  storage.prefers_play[player.index] = nil
  storage.directors[player.index] = nil
  spectate(player, cmd.parameter)
end)

commands.add_command("agent-free", "Keep spectating but stop following (move the camera yourself)", function(cmd)
  if cmd.player_index then
    storage.followers[cmd.player_index] = nil
    storage.directors[cmd.player_index] = nil
  end
end)

-- Spectator director, like an esports observer: keep the camera on whichever agent is doing the
-- most watchable thing, without flicking back and forth.
--   score  = running task (mine 3, move 2, craft 1, wait 0.3) + 1 if actually walking
--          + last action (place 6 ... move_to 1), fading to 0 over DIRECTOR_RECENT ticks
--          + 0.5 if hand-crafting
--   switch when the current agent is gone, or after DIRECTOR_DWELL ticks if another scores more than
--   1.3x + 1 the current one, or after DIRECTOR_IDLE ticks of the current one scoring 0 while
--   another is busy. With everyone idle the camera stays put.

local function director_switch(player, d, name, why)
  d.target, d.since, d.idle_since = name, game.tick, nil
  storage.followers[player.index] = name
  player.print("[color=0.6,0.8,1][auto] watching " .. name .. " (" .. why .. ")[/color]")
end

local function run_directors(tick)
  if not next(storage.directors) then return end
  local scores = {}
  for name, agent in pairs(storage.agents) do
    local s, why = director_score(agent, tick)
    scores[name] = { s = s, why = why }
  end
  for index, d in pairs(storage.directors) do
    local player = game.get_player(index)
    if not (player and player.connected and player.controller_type == defines.controllers.spectator) then
      storage.directors[index] = nil
    else
      local best, bs = nil, -1
      for name, v in pairs(scores) do
        if name ~= d.target and v.s > bs then best, bs = name, v.s end
      end
      local cur = d.target and scores[d.target]
      if not cur or cur.s < 0 then
        if best and bs >= 0 then director_switch(player, d, best, scores[best].why) end
      else
        if cur.s <= 0 then d.idle_since = d.idle_since or tick else d.idle_since = nil end
        local dwell = tick - d.since
        if best and bs > 0 and ((dwell >= DIRECTOR_DWELL and bs > cur.s * 1.3 + 1)
            or (d.idle_since and tick - d.idle_since >= DIRECTOR_IDLE)) then
          director_switch(player, d, best, scores[best].why)
        end
      end
    end
  end
end

commands.add_command("agent-auto", "Spectate whichever agent is busiest (auto director); /agent-follow <name> pins one", function(cmd)
  local player = cmd.player_index and game.get_player(cmd.player_index)
  if not player then return end
  storage.prefers_play[player.index] = nil
  spectate(player)
  storage.directors[player.index] = { since = game.tick }
  run_directors(game.tick)
  player.print("Auto director on: the camera follows the busiest agent. /agent-follow <name> pins one, /agent-free lets you roam.")
end)

commands.add_command("agent-play", "Stop spectating and take your own character", function(cmd)
  local player = cmd.player_index and game.get_player(cmd.player_index)
  if not player then return end
  storage.followers[player.index] = nil
  storage.directors[player.index] = nil
  storage.prefers_play[player.index] = true
  if player.controller_type == defines.controllers.character then return end
  local ch = storage.parked[player.index]
  storage.parked[player.index] = nil
  if ch and ch.valid then
    player.set_controller { type = defines.controllers.character, character = ch }
  else
    player.set_controller { type = defines.controllers.god }
    player.teleport(player.force.get_spawn_position(player.surface), player.surface)
    player.create_character()
  end
end)

---------------------------------------------------------------------------------------------------
-- events

local function init_storage()
  storage.agents = storage.agents or {}
  storage.path_requests = storage.path_requests or {}
  storage.chat = storage.chat or {}
  storage.chat_seq = storage.chat_seq or 0
  storage.mode = storage.mode or "auto"
  storage.parked = storage.parked or {}
  storage.followers = storage.followers or {}
  storage.prefers_play = storage.prefers_play or {}
  storage.directors = storage.directors or {}
  storage.pending_research = storage.pending_research or {}
  storage.water_tiles = nil
end

script.on_init(function()
  init_storage()
  if remote.interfaces["freeplay"] then
    pcall(remote.call, "freeplay", "set_skip_intro", true)
  end
end)

script.on_configuration_changed(init_storage)

-- Hand-crafting by a player-less character is invisible to the engine's production statistics
-- and to "craft-item" research triggers. Watch the crafting queue and do the bookkeeping the engine
-- does for real players.
local function record_crafted(ch, item, count)
  local stats = ch.force.get_item_production_statistics(ch.surface)
  stats.on_flow(item, count)
  local total = stats.get_input_count(item)
  for _, tech in pairs(ch.force.technologies) do
    local trig = not tech.researched and tech.prototype.research_trigger
    if trig and trig.type == "craft-item" and trig.item and trig.item.name == item and total >= (trig.count or 1) then
      local ready = true
      for _, pre in pairs(tech.prerequisites) do if not pre.researched then ready = false; break end end
      if ready then
        tech.researched = true
        game.print("[color=0.5,0.85,1]Research completed: " .. tech.name .. "[/color]")
      end
    end
  end
end

local function track_crafting(agent)
  local ch = agent.character
  local now = {}
  if ch.crafting_queue_size > 0 then
    for _, q in pairs(ch.crafting_queue) do now[q.recipe] = (now[q.recipe] or 0) + q.count end
  end
  for recipe, before in pairs(agent.craft_snapshot or {}) do
    local done = before - (now[recipe] or 0)
    if done > 0 then
      for _, p in pairs(prototypes.recipe[recipe].products) do
        if p.type == "item" then record_crafted(ch, p.name, (p.amount or p.amount_min or 1) * done) end
      end
    end
  end
  agent.craft_snapshot = next(now) and now or nil
end

local function should_pause()
  if storage.mode == "realtime" then return false end
  if storage.mode == "turn" then return true end
  return #game.connected_players == 0
end

script.on_event(defines.events.on_tick, function(e)
  local busy = false
  for _, agent in pairs(storage.agents) do
    if agent.character and agent.character.valid and (agent.craft_snapshot or agent.character.crafting_queue_size > 0) then
      track_crafting(agent)
    end
    local t = agent.task
    if t then
      local ch = agent.character
      if not (ch and ch.valid) then
        finish_task(agent, "failed", "character died")
      else
        TASKS[t.kind](agent, t)
        if agent.task then busy = true end
      end
    end
  end
  if e.tick % 60 == 0 then
    for _, agent in pairs(storage.agents) do
      local ch = agent.character
      if ch and ch.valid then
        local p = ch.position
        ch.force.chart(ch.surface, { { p.x - 64, p.y - 64 }, { p.x + 64, p.y + 64 } })
        ch.surface.request_to_generate_chunks(p, 3)
      end
    end
  end
  if e.tick % 30 == 0 then run_directors(e.tick) end
  for index, name in pairs(storage.followers) do
    local player = game.get_player(index)
    local agent = storage.agents[name]
    local ch = agent and agent.character
    if player and player.connected and player.controller_type == defines.controllers.spectator and ch and ch.valid then
      if player.surface ~= ch.surface or dist(player.position, ch.position) > 0.05 then
        player.teleport(ch.position, ch.surface)
      end
    else
      storage.followers[index] = nil
    end
  end
  if not busy and should_pause() then game.tick_paused = true end
end)

script.on_event(defines.events.on_script_path_request_finished, function(e)
  local name = storage.path_requests[e.id]
  if not name then return end
  storage.path_requests[e.id] = nil
  local agent = storage.agents[name]
  local mv = agent and agent.task and agent.task.mv
  if not (mv and mv.path_id == e.id) then return end
  if e.path then
    local pts = {}
    for i, wp in ipairs(e.path) do pts[i] = { x = wp.position.x, y = wp.position.y } end
    mv.path = pts
    mv.wp = 1
  elseif e.try_again_later then
    mv.retry_at = game.tick + 10
  else
    mv.no_path = true
  end
end)

script.on_event(defines.events.on_research_finished, function(e)
  local f = e.research.force
  local left = {}
  for _, name in ipairs(storage.pending_research or {}) do
    local tech = f.technologies[name]
    if tech and not tech.researched then
      local ready = true
      for _, pre in pairs(tech.prerequisites) do if not pre.researched then ready = false; break end end
      if not (ready and f.add_research(tech)) then left[#left + 1] = name end
    end
  end
  storage.pending_research = left
end)

script.on_event(defines.events.on_console_chat, function(e)
  if not e.player_index then return end
  storage.chat_seq = storage.chat_seq + 1
  local p = game.get_player(e.player_index)
  storage.chat[#storage.chat + 1] = { seq = storage.chat_seq, tick = e.tick, from = p and p.name or "?", message = e.message }
  if #storage.chat > CHAT_KEEP then table.remove(storage.chat, 1) end
end)

script.on_event(defines.events.on_player_joined_game, function(e)
  if storage.mode == "auto" then game.tick_paused = false end
  local player = game.get_player(e.player_index)
  if player and not storage.prefers_play[player.index] then
    if player.controller_type ~= defines.controllers.spectator then spectate(player) end
    storage.directors[player.index] = { since = game.tick }  -- auto director by default
    player.print("Auto director on (/agent-auto). /agent-follow <name> pins an agent.")
  end
end)

script.on_event(defines.events.on_player_left_game, function(e)
  storage.followers[e.player_index] = nil
  storage.directors[e.player_index] = nil
end)
