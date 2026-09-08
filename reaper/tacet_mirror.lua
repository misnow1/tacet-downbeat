--[[
  tacet-downbeat: mirror the annotation queue into Reaper markers.

  Tails the tab-separated queue the box appends to (see tacet/mirror.py) and
  drops a marker at the current playhead for each new line. Instants become
  markers; a span's start is remembered and its end creates a region.

  Positions come from Reaper itself, so there is no clock arithmetic here and
  nothing to drift. The box's JSONL log remains the source of truth: if this
  script is not running, or Reaper falls over, the markers can be regenerated
  offline from the log (tacet/markers.py). Losing this costs the view, not the
  data.

  Install: put this in REAPER/Scripts, add it via Actions > Load ReaScript, and
  run it. It will ask for the queue path once and remember it.

  Stop it from the Actions list, or by running it again.
]]

local SECTION      = "tacet_downbeat"
local KEY_QUEUE    = "queue_path"
local KEY_OFFSET   = "queue_offset"
local POLL_SECONDS = 0.25
local DELIMITER    = "\t"
local ABSENT       = "-"

-- Reaper's play state is a bitmask: 1 playing, 2 paused, 4 recording.
local PLAYING   = 1
local RECORDING = 4

local queue_path = nil
local offset = 0
local open_spans = {}
local last_poll = 0
local running = true

local function log(message)
  reaper.ShowConsoleMsg("[tacet] " .. message .. "\n")
end

-- ---------------------------------------------------------------------------
-- configuration
-- ---------------------------------------------------------------------------

local function default_queue_path()
  return reaper.GetResourcePath() .. "/tacet/queue.tsv"
end

local function resolve_queue_path()
  local stored = reaper.GetExtState(SECTION, KEY_QUEUE)
  if stored ~= nil and stored ~= "" then
    return stored
  end
  local ok, entered = reaper.GetUserInputs(
    "tacet-downbeat mirror", 1, "Queue file path:,extrawidth=420", default_queue_path()
  )
  if not ok or entered == "" then
    return nil
  end
  reaper.SetExtState(SECTION, KEY_QUEUE, entered, true)
  return entered
end

local function file_size(path)
  local handle = io.open(path, "rb")
  if not handle then return nil end
  local size = handle:seek("end")
  handle:close()
  return size
end

local function load_offset(path)
  local stored = tonumber(reaper.GetExtState(SECTION, KEY_OFFSET)) or 0
  local size = file_size(path)
  if size == nil then return 0 end
  -- A queue smaller than the stored offset is a different game, or a file that
  -- was cleared. Start again rather than reading from the middle of a line.
  if stored > size then
    log("queue is shorter than the stored offset; starting from the beginning")
    return 0
  end
  return stored
end

local function save_offset(value)
  reaper.SetExtState(SECTION, KEY_OFFSET, tostring(value), true)
end

-- ---------------------------------------------------------------------------
-- placing marks
-- ---------------------------------------------------------------------------

local function current_position()
  local state = reaper.GetPlayState()
  if state & PLAYING == PLAYING or state & RECORDING == RECORDING then
    return reaper.GetPlayPosition()
  end
  return reaper.GetCursorPosition()
end

local function add_marker(name, position)
  reaper.AddProjectMarker2(0, false, position, 0, name, -1, 0)
end

local function add_region(name, start_position, end_position)
  if end_position < start_position then
    end_position = start_position
  end
  reaper.AddProjectMarker2(0, true, start_position, end_position, name, -1, 0)
end

local function split(line)
  local fields = {}
  for field in (line .. DELIMITER):gmatch("([^" .. DELIMITER .. "]*)" .. DELIMITER) do
    fields[#fields + 1] = field
  end
  return fields
end

local function handle(line)
  local fields = split(line)
  if #fields < 3 then
    log("ignoring malformed queue line: " .. line)
    return
  end

  local name, phase, span_id = fields[1], fields[2], fields[3]
  local position = current_position()

  if phase == ABSENT then
    add_marker(name, position)
    return
  end

  if phase == "start" then
    open_spans[span_id] = position
    return
  end

  if phase == "end" then
    local started = open_spans[span_id]
    if started == nil then
      -- The script started mid-span, so the region has no beginning. Say so and
      -- leave a marker rather than silently dropping the event.
      log("no start for span " .. span_id .. "; placing a marker instead")
      add_marker(name .. " (end)", position)
      return
    end
    open_spans[span_id] = nil
    add_region(name, started, position)
    return
  end

  log("unknown phase '" .. phase .. "' in: " .. line)
end

-- ---------------------------------------------------------------------------
-- tailing
-- ---------------------------------------------------------------------------

local function last_newline(chunk)
  local found, index = 0, 1
  while true do
    local position = chunk:find("\n", index, true)
    if not position then break end
    found, index = position, position + 1
  end
  return found
end

-- Forward declaration: poll() calls this, and it is defined below.
local handle_safely

local function poll()
  local handle = io.open(queue_path, "rb")
  if handle then
    handle:seek("set", offset)
    local chunk = handle:read("a") or ""
    handle:close()

    -- Only consume whole lines. A partial tail is a write in progress, and it
    -- will be complete by the next poll.
    local cut = last_newline(chunk)
    if cut > 0 then
      local complete = chunk:sub(1, cut)
      offset = offset + #complete
      save_offset(offset)
      for line in complete:gmatch("([^\n]+)") do
        handle_safely(line)
      end
    end
  end
end

-- A bad line must not take down the mirror mid-game.
handle_safely = function(line)
  local ok, err = pcall(handle, line)
  if not ok then
    log("error handling line: " .. tostring(err))
  end
end

local function loop()
  if not running then return end
  local now = reaper.time_precise()
  if now - last_poll >= POLL_SECONDS then
    last_poll = now
    local ok, err = pcall(poll)
    if not ok then
      log("error polling queue: " .. tostring(err))
    end
  end
  reaper.defer(loop)
end

local function main()
  queue_path = resolve_queue_path()
  if queue_path == nil then
    log("no queue path given; not starting")
    return
  end
  offset = load_offset(queue_path)
  log("mirroring " .. queue_path .. " from byte " .. offset)
  if file_size(queue_path) == nil then
    log("queue does not exist yet; waiting for the box to create it")
  end
  reaper.atexit(function() log("mirror stopped") end)
  loop()
end

main()
