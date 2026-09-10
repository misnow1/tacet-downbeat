--[[
  Tests for tacet_mirror.lua, against a stubbed Reaper API.

  Reaper is not needed and is not simulated beyond what the script touches: a
  playhead, an ExtState store, a marker list and a deferred callback the test
  drives by hand. That is enough to exercise every branch of the queue handling,
  which is where the bugs live. Whether Reaper really calls these functions the
  way this pretends is Session A's job (docs/handoff.md).

    lua reaper/test_tacet_mirror.lua
]]

local SCRIPT = (arg[0]:match("(.*/)") or "./") .. "tacet_mirror.lua"

local markers, console, ext, deferred, now, playpos

local function reset()
  markers, console, ext = {}, {}, {}
  deferred, now, playpos = nil, 0, 0
end

reaper = {
  ShowConsoleMsg = function(text) console[#console + 1] = text end,
  GetResourcePath = function() return "/tmp/tacet-resource" end,
  GetExtState = function(section, key) return ext[section .. "/" .. key] or "" end,
  SetExtState = function(section, key, value) ext[section .. "/" .. key] = value end,
  GetUserInputs = function() return false, "" end,
  GetPlayState = function() return 5 end, -- playing | recording
  GetPlayPosition = function() return playpos end,
  GetCursorPosition = function() return 0 end,
  defer = function(fn) deferred = fn end,
  atexit = function() end,
  time_precise = function() return now end,
  AddProjectMarker2 = function(_, isrgn, pos, rgnend, name)
    markers[#markers + 1] = { isrgn = isrgn, pos = pos, rgnend = rgnend, name = name }
    return #markers
  end,
}

-- --------------------------------------------------------------------------

local failures, checks = 0, 0

local function check(ok, label)
  checks = checks + 1
  if not ok then
    failures = failures + 1
    print("  FAIL  " .. label)
  end
end

local function equal(actual, expected, label)
  check(actual == expected, label .. " (got " .. tostring(actual) .. ", want " .. tostring(expected) .. ")")
end

local function logged(needle)
  for _, line in ipairs(console) do
    if line:find(needle, 1, true) then return true end
  end
  return false
end

local function write(path, text)
  local handle = assert(io.open(path, "wb"))
  handle:write(text)
  handle:close()
end

local function append(path, text)
  local handle = assert(io.open(path, "ab"))
  handle:write(text)
  handle:close()
end

--- Load the script fresh, pointed at `path`.
local function start(path)
  reset()
  ext["tacet_downbeat/queue_path"] = path
  dofile(SCRIPT)
end

--- Load the script fresh against an empty queue. This is the order gameday.md
--- prescribes - the script comes up first, the box then appends - and it is the
--- only order in which the script mirrors a line at a position it witnessed.
local function start_empty(path)
  write(path, "")
  start(path)
end

--- Advance past the poll interval and run one iteration.
local function tick()
  now = now + 1.0
  assert(deferred, "script did not schedule a callback")
  deferred()
end

-- --------------------------------------------------------------------------

local function test_instant_becomes_a_marker()
  local path = os.tmpname()
  start_empty(path)
  append(path, "NOTE|note\t-\t-\n")
  playpos = 12.5
  tick()
  equal(#markers, 1, "one marker")
  equal(markers[1].name, "NOTE|note", "marker name")
  equal(markers[1].isrgn, false, "not a region")
  equal(markers[1].pos, 12.5, "placed at the playhead")
  os.remove(path)
end

local function test_span_pair_becomes_a_region()
  local path = os.tmpname()
  start_empty(path)
  append(path, "GAME|q1\tstart\tq1-2\n")
  playpos = 5.0
  tick()
  equal(#markers, 0, "a span start places nothing on its own")
  append(path, "GAME|q1\tend\tq1-2\n")
  playpos = 30.0
  tick()
  equal(#markers, 1, "the end creates the region")
  equal(markers[1].isrgn, true, "is a region")
  equal(markers[1].pos, 5.0, "starts where the span opened")
  equal(markers[1].rgnend, 30.0, "ends at the playhead")
  equal(markers[1].name, "GAME|q1", "region name")
  os.remove(path)
end

local function test_orphan_end_is_reported_not_dropped()
  local path = os.tmpname()
  start_empty(path)
  append(path, "GAME|q1\tend\tq1-99\n")
  playpos = 8.0
  tick()
  equal(#markers, 1, "leaves a marker rather than nothing")
  equal(markers[1].isrgn, false, "degraded to a marker")
  check(markers[1].name:find("(end)", 1, true) ~= nil, "marker is labelled as an end")
  check(logged("no start for span"), "says so on the console")
  os.remove(path)
end

local function test_partial_line_waits_for_its_newline()
  local path = os.tmpname()
  start_empty(path)
  append(path, "NOTE|note\t-\t-\n" .. "NOTE|no")
  tick()
  equal(#markers, 1, "only the complete line is consumed")
  append(path, "te\t-\t-\n")
  tick()
  equal(#markers, 2, "the rest is consumed once terminated")
  equal(markers[2].name, "NOTE|note", "reassembled correctly")
  os.remove(path)
end

local function test_lines_are_not_replayed()
  local path = os.tmpname()
  start_empty(path)
  append(path, "NOTE|note\t-\t-\n")
  tick()
  tick()
  tick()
  equal(#markers, 1, "polling again does not re-add")
  os.remove(path)
end

local function test_offset_survives_a_restart()
  local path = os.tmpname()
  start_empty(path)
  append(path, "NOTE|note\t-\t-\n")
  tick()
  local saved = ext["tacet_downbeat/queue_offset"]
  check(saved ~= nil and tonumber(saved) > 0, "offset was persisted")

  -- Reload as if Reaper had restarted, keeping the stored offset.
  local carried = { ["tacet_downbeat/queue_path"] = path, ["tacet_downbeat/queue_offset"] = saved }
  reset()
  ext = carried
  dofile(SCRIPT)
  append(path, "DET|false-open\t-\t-\n")
  tick()
  equal(#markers, 1, "only the new line is placed")
  equal(markers[1].name, "DET|false-open", "resumed at the right byte")
  os.remove(path)
end

--- The failure this guards against: ExtState is lost - a reinstall, a cleared
--- reaper-extstate.ini, a different machine - while the queue, which is never
--- rotated, still holds every line of every past game. Reading it from the
--- beginning would drop all of that onto today's project at the playhead.
local function test_history_is_not_replayed_when_the_offset_is_lost()
  local path = os.tmpname()
  write(path, "BAND|fight-song\t-\t-\n" .. "BAND|cadence\t-\t-\n")
  start(path)  -- queue path known, no remembered offset
  tick()
  equal(#markers, 0, "an unremembered history is not stamped onto this project")
  check(logged("starting at its end"), "says why")
  append(path, "SYS|armed\t-\t-\n")
  tick()
  equal(#markers, 1, "new lines still mirror")
  equal(markers[1].name, "SYS|armed", "and only the new one")
  os.remove(path)
end

--- The other side of that rule: on a queue with no history there is nothing to
--- protect, so the first game's first line must not be skipped.
local function test_a_queue_created_after_the_script_is_read_from_the_beginning()
  local path = os.tmpname()
  os.remove(path)
  start(path)
  check(logged("waiting for the box"), "says it is waiting")
  append(path, "SYS|armed\t-\t-\n")
  tick()
  equal(#markers, 1, "the first line of a new queue is mirrored")
  equal(markers[1].name, "SYS|armed", "from the beginning of the file")
  os.remove(path)
end

local function test_a_shorter_queue_restarts_from_the_beginning()
  local path = os.tmpname()
  write(path, "NOTE|note\t-\t-\n")
  reset()
  ext["tacet_downbeat/queue_path"] = path
  ext["tacet_downbeat/queue_offset"] = "999999"
  dofile(SCRIPT)
  tick()
  equal(#markers, 1, "read from the start rather than mid-line")
  check(logged("shorter than the stored offset"), "says why")
  os.remove(path)
end

local function test_malformed_line_does_not_stop_the_mirror()
  local path = os.tmpname()
  start_empty(path)
  append(path, "not-enough-fields\n" .. "NOTE|note\t-\t-\n")
  tick()
  equal(#markers, 1, "the good line still lands")
  check(logged("malformed"), "the bad one is reported")
  os.remove(path)
end

local function test_missing_queue_is_survivable()
  start("/tmp/tacet-does-not-exist-" .. tostring(os.time()) .. ".tsv")
  tick()
  tick()
  equal(#markers, 0, "nothing placed")
  check(logged("waiting for the box"), "says it is waiting")
end

-- --------------------------------------------------------------------------

local tests = {
  test_instant_becomes_a_marker,
  test_span_pair_becomes_a_region,
  test_orphan_end_is_reported_not_dropped,
  test_partial_line_waits_for_its_newline,
  test_lines_are_not_replayed,
  test_offset_survives_a_restart,
  test_history_is_not_replayed_when_the_offset_is_lost,
  test_a_queue_created_after_the_script_is_read_from_the_beginning,
  test_a_shorter_queue_restarts_from_the_beginning,
  test_malformed_line_does_not_stop_the_mirror,
  test_missing_queue_is_survivable,
}

for _, test in ipairs(tests) do
  local ok, err = pcall(test)
  if not ok then
    failures = failures + 1
    print("  ERROR " .. tostring(err))
  end
end

print(string.format("%d checks, %d failures", checks, failures))
os.exit(failures == 0 and 0 or 1)
