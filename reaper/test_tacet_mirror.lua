--[[
  Tests for tacet_mirror.lua, against a stubbed Reaper API.

  Reaper is not needed and is not simulated beyond what the script touches: a
  playhead, an ExtState store, a marker list and a deferred callback the test
  drives by hand. That is enough to exercise every branch of the queue handling,
  which is where the bugs live. Whether Reaper really calls these functions the
  way this pretends is Session A's job (docs/handoff.md).

    lua reaper/test_tacet_mirror.lua
]]

local HERE = arg[0]:match("(.*/)") or "./"
local SCRIPT = HERE .. "tacet_mirror.lua"
--- Written by the box's own code: see tests/queue_fixtures.py.
local FIXTURES = HERE .. "../tests/fixtures/"

--- Hashed by the script when a test says nothing else: every byte value once.
local CHECKSUM_INPUT = FIXTURES .. "script-checksum-input.bin"
local CHECKSUM_OF_INPUT = "90a458c5"

--- The script only hashes a file called tacet_mirror.lua, so the fixtures are
--- copied into one: `named_script(bytes)` writes it and returns its path.
local SCRIPT_DIR = os.tmpname()
os.remove(SCRIPT_DIR)
os.execute("mkdir '" .. SCRIPT_DIR .. "'")
local NAMED_SCRIPT = SCRIPT_DIR .. "/tacet_mirror.lua"

local function named_script(bytes)
  local handle = assert(io.open(NAMED_SCRIPT, "wb"))
  handle:write(bytes)
  handle:close()
  return NAMED_SCRIPT
end

local function named_copy_of(path)
  local handle = assert(io.open(path, "rb"))
  local bytes = handle:read("a")
  handle:close()
  return named_script(bytes)
end

local markers, console, ext, deferred, now, playpos, project_file, project_dir, action_script, tracks

--- Floats on purpose: Reaper's arm state is a float, and Lua 5.4 prints 1.0 as "1.0".
local function default_tracks()
  return {
    { name = "MV Conductor", arm = 1.0 },
    { name = "MV Spare", arm = 0.0 },
    { name = "Pilot Reference", arm = 1.0 },
    { name = "Pilot", arm = 0.0 },
  }
end

local function reset()
  markers, console, ext = {}, {}, {}
  deferred, now, playpos = nil, 0, 0
  action_script = nil
  tracks = default_tracks()
  project_file, project_dir = "/Users/x/Reaper Media/20261114 X/20261114 X.RPP", "/Users/x/Reaper Media/20261114 X/Media"
end

reaper = {
  ShowConsoleMsg = function(text) console[#console + 1] = text end,
  -- (is_new_value, filename, sectionID, cmdID, ...): the script reads `filename`.
  get_action_context = function() return true, action_script, 0, 0, 0, 0, 0 end,
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
  EnumProjects = function() if project_file == nil then return nil end return {}, project_file end,
  GetProjectPath = function() return project_dir end,
  CountTracks = function() return #tracks end,
  GetTrack = function(_, index) return tracks[index + 1] end,
  GetMediaTrackInfo_Value = function(track, key)
    assert(key == "I_RECARM", "unexpected track info " .. tostring(key))
    return track.arm
  end,
  -- GetTrackName is deliberately absent: it answers "Track N" for an unnamed track.
  GetSetMediaTrackInfo_String = function(track, key, _, set)
    assert(key == "P_NAME" and not set, "only reads the name")
    return true, track.name
  end,
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

local report_paths = {}

--- Load the script fresh, pointed at `path`. `script` is what Reaper says the
--- running script's file is; by default the every-byte file, so its checksum is known.
local function start(path, script)
  reset()
  action_script = script or named_copy_of(CHECKSUM_INPUT)
  report_paths[#report_paths + 1] = path .. ".report"
  report_paths[#report_paths + 1] = path .. ".report.tmp"
  ext["tacet_downbeat/queue_path"] = path
  dofile(SCRIPT)
end

--- Load the script fresh against an empty queue. This is the order gameday.md
--- prescribes - the script comes up first, the box then appends - and it is the
--- only order in which the script mirrors a line at a position it witnessed.
local function start_empty(path, script)
  write(path, "")
  start(path, script)
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

local function read(path)
  local handle = assert(io.open(path, "rb"))
  local text = handle:read("a")
  handle:close()
  return text
end

--- The contract with the box (#39). The queue was written by tacet.mirror in
--- Python and the marks beside it are what the box expects a mirror watching
--- from the first line to place, checked there against tacet.markers. These
--- tests used to read only lines written by hand, so a change on either side
--- passed both suites while markers stopped appearing.
local function replay(name)
  local queue = read(FIXTURES .. "queue-" .. name .. ".tsv")
  local expected = {}
  for line in read(FIXTURES .. "queue-" .. name .. ".marks"):gmatch("([^\n]+)") do
    local kind, mark = line:match("^(%a+)\t(.+)$")
    expected[#expected + 1] = { isrgn = kind == "region", name = mark }
  end

  local path = os.tmpname()
  start_empty(path)
  local said = #console
  append(path, queue)
  tick()
  equal(#markers, #expected, name .. ": places every mark")
  for i, want in ipairs(expected) do
    local got = markers[i] or {}
    if got.name ~= want.name or got.isrgn ~= want.isrgn then
      check(false, name .. ": mark " .. i .. " is " .. tostring(got.name) .. ", want " .. want.name)
      break
    end
  end
  equal(#console, said, name .. ": reads every line without complaint")
  os.remove(path)
end

local function test_the_game_2_queue_replays()
  replay("game-2")
end

local function test_every_event_in_the_vocabulary_replays()
  replay("vocabulary")
end

--- Each of these is refused and reported, and the line after it still lands.
local function refused(line, label)
  local path = os.tmpname()
  start_empty(path)
  append(path, line .. "\n" .. "NOTE|note\t-\t-\n")
  tick()
  equal(#markers, 1, label .. ": only the good line lands")
  equal(markers[1] and markers[1].name, "NOTE|note", label .. ": and it is the good one")
  check(logged("ignoring malformed queue line"), label .. ": is reported")
  os.remove(path)
end

local function test_lines_are_read_as_strictly_as_the_box_writes_them()
  -- What appending after a torn write produced (#26): two lines glued into one.
  refused("NOTE|note\t-\t-NOTE|note\t-\t-", "too many fields")
  -- The same, starting with a span line: every other rule passes this one.
  refused("GAME|q1\tstart\tq1-2NOTE|note\t-\t-", "too many fields after a span")
  -- A fragment with a whole line after it: three fields, but no name.
  refused("FDR|up-whNOTE|note\t-\t-", "not a marker name")
  refused("GAME|q1\tbegin\tq1-2", "an unknown phase")
  refused("GAME|q1\tstart\t-", "a span with no id")
  refused("NOTE|note\t-\tnote-3", "an instant with an id")
end

local function test_missing_queue_is_survivable()
  start("/tmp/tacet-does-not-exist-" .. tostring(os.time()) .. ".tsv")
  tick()
  tick()
  equal(#markers, 0, "nothing placed")
  check(logged("waiting for the box"), "says it is waiting")
end


-- --------------------------------------------------------------------------
-- the report (#204)
-- --------------------------------------------------------------------------

local function report_of(path)
  local handle = io.open(path .. ".report", "rb")
  if not handle then return nil end
  local text = handle:read("a")
  handle:close()
  return text
end

local function seq_of(text)
  return tonumber(text:match("seq\t(%d+)\n"))
end

local function test_the_report_matches_the_golden_file()
  local path = os.tmpname()
  start_empty(path)
  local text = report_of(path)
  check(text ~= nil, "a report is written at start")
  local golden = read(FIXTURES .. "report-v1-tracks.report")
  -- seq is the one field that moves; the golden file pins the rest.
  equal(text and text:gsub("seq\t%d+", "seq\t42"), golden, "bytes equal the golden file")
  os.remove(path)
end

local function test_an_unsaved_project_writes_an_empty_project()
  local path = os.tmpname()
  start_empty(path)
  project_file = ""
  now = now + 10
  tick()
  check(report_of(path):find("project\t\n", 1, true) ~= nil, "empty project value")
  os.remove(path)
end

local function test_no_project_omits_the_key()
  local path = os.tmpname()
  start_empty(path)
  project_file = nil
  now = now + 10
  tick()
  local text = report_of(path)
  check(text:find("project", 1, true) == nil, "no project key")
  check(text:find("record_path", 1, true) ~= nil, "record_path still reported")
  os.remove(path)
end

local function test_seq_rises_and_the_file_is_replaced()
  local path = os.tmpname()
  start_empty(path)
  local first = seq_of(report_of(path))
  now = now + 2
  tick()
  local second = seq_of(report_of(path))
  check(second > first, "seq rises")
  local _, lines = report_of(path):gsub("\n", "")
  equal(lines, 6 + #tracks, "replaced, not appended")
  os.remove(path)
end

local function test_the_report_is_throttled()
  local path = os.tmpname()
  start_empty(path)
  local first = seq_of(report_of(path))
  now = now + 0.5
  deferred()
  equal(seq_of(report_of(path)), first, "no write inside the interval")
  now = now + 2
  deferred()
  check(seq_of(report_of(path)) > first, "writes once the interval has passed")
  os.remove(path)
end

local function test_an_unwritable_report_still_mirrors()
  local path = os.tmpname()
  start_empty(path)
  os.remove(path .. ".report")
  -- A directory where the temp file should go makes every write fail.
  os.execute("mkdir '" .. path .. ".report.tmp'")
  append(path, "NOTE|note\t-\t-\n")
  tick()
  equal(#markers, 1, "the queue is still mirrored")
  os.execute("rmdir '" .. path .. ".report.tmp'")
  os.remove(path)
end

local function test_a_missing_api_still_mirrors()
  local path = os.tmpname()
  start_empty(path)
  local saved = reaper.EnumProjects
  reaper.EnumProjects = nil
  reaper.GetProjectPath = nil
  append(path, "NOTE|note\t-\t-\n")
  tick()
  equal(#markers, 1, "the queue is still mirrored")
  reaper.EnumProjects = saved
  reaper.GetProjectPath = function() return project_dir end
  os.remove(path)
end

local function test_the_report_leaves_offset_and_open_spans_alone()
  local path = os.tmpname()
  start_empty(path)
  append(path, "GAME|q1\tstart\tq1-2\n")
  playpos = 5.0
  tick()
  -- Several report intervals pass with the span open.
  for _ = 1, 4 do
    now = now + 2
    tick()
  end
  append(path, "GAME|q1\tend\tq1-2\n")
  playpos = 30.0
  tick()
  equal(#markers, 1, "one region")
  equal(markers[1].isrgn, true, "it is a region")
  equal(markers[1].pos, 5.0, "at the original start")
  append(path, "NOTE|note\t-\t-\n")
  now = now + 2
  tick()
  equal(#markers, 2, "exactly one marker for the instant")
  local size = #read(path)
  equal(tonumber(ext["tacet_downbeat/queue_offset"]), size, "stored offset is the queue's byte length")
  os.remove(path)
end

local function test_a_failed_write_does_not_replace_the_report()
  local path = os.tmpname()
  start_empty(path)
  local before = report_of(path)
  local real_open = io.open
  io.open = function(name, mode)
    if name == path .. ".report.tmp" then
      return { write = function() return nil, "no space" end, close = function() return true end }
    end
    return real_open(name, mode)
  end
  now = now + 2
  local ok = pcall(tick)
  io.open = real_open
  check(ok, "a failed write does not raise")
  equal(report_of(path), before, "the old report is kept whole")
  os.remove(path)
end

local function test_the_report_prints_nothing_and_mirroring_stays_last()
  local path = os.tmpname()
  start_empty(path)
  local said = #console
  now = now + 2
  tick()
  now = now + 2
  tick()
  equal(#console, said, "the report prints nothing")
  check(console[#console]:find("mirroring", 1, true) ~= nil, "mirroring is the last console line")
  os.remove(path)
end

local function test_newlines_in_values_are_stripped()
  local path = os.tmpname()
  start_empty(path)
  project_dir = "/a\r\nb"
  now = now + 10
  tick()
  check(report_of(path):find("record_path\t/ab\n", 1, true) ~= nil, "value is one line")
  os.remove(path)
end

local function checksum_of(text)
  return text:match("script_checksum\t([^\n]*)\n")
end

local function test_the_checksum_follows_record_path_and_matches_the_input()
  local path = os.tmpname()
  start_empty(path)
  local text = report_of(path)
  equal(checksum_of(text), CHECKSUM_OF_INPUT, "checksum of the script's own file")
  check(text:match("record_path\t[^\n]*\nscript_checksum\t") ~= nil, "after record_path")
  check(text:match("version\t1\n") ~= nil, "the format version is still 1")
  os.remove(path)
end

-- --------------------------------------------------------------------------
-- every track's arm state (#167)
-- --------------------------------------------------------------------------

local function track_lines_of(text)
  local lines = {}
  for line in text:gmatch("track\t([^\n]*)\n") do lines[#lines + 1] = line end
  return lines
end

local function test_tracks_follow_the_checksum_in_track_order()
  local path = os.tmpname()
  start_empty(path)
  local text = report_of(path)
  local count = tonumber(text:match("\ntracks\t(%d+)\n"))
  equal(count, #tracks, "the count is the number of tracks")
  local lines = track_lines_of(text)
  equal(#lines, count, "one line per track")
  for index, line in ipairs(lines) do
    equal(tonumber(line:match("^(%d+)\t")), index, "track number " .. index)
  end
  check(text:find("script_checksum\t[^\n]*\ntracks\t", 1) ~= nil, "tracks come after script_checksum")
  os.remove(path)
end

local function test_arm_is_written_as_0_or_1_not_a_float()
  local path = os.tmpname()
  start_empty(path)
  local text = report_of(path)
  check(text:find("1.0", 1, true) == nil, "no float in the report")
  check(text:find("track\t1\t1\tMV Conductor\n", 1, true) ~= nil, "armed is 1")
  check(text:find("track\t2\t0\tMV Spare\n", 1, true) ~= nil, "unarmed is 0")
  os.remove(path)
end

local function test_an_unnamed_track_writes_an_empty_name()
  local path = os.tmpname()
  start_empty(path)
  tracks = { { name = "A", arm = 1.0 }, { name = "B", arm = 1.0 }, { name = "", arm = 1.0 } }
  now = now + 10
  tick()
  check(report_of(path):find("track\t3\t1\t\n", 1, true) ~= nil, "empty name, trailing tab kept")
  os.remove(path)
end

local function test_newlines_in_a_name_are_stripped_and_a_tab_is_kept()
  local path = os.tmpname()
  start_empty(path)
  tracks = { { name = "Horns\r\n 4\tB", arm = 1.0 } }
  now = now + 10
  tick()
  check(report_of(path):find("track\t1\t1\tHorns 4\tB\n", 1, true) ~= nil, "one line, tab kept")
  os.remove(path)
end

local function test_no_tracks_writes_a_zero_count_and_no_track_lines()
  local path = os.tmpname()
  start_empty(path)
  tracks = {}
  now = now + 10
  tick()
  local text = report_of(path)
  check(text:find("\ntracks\t0\n", 1, true) ~= nil, "a zero count")
  equal(#track_lines_of(text), 0, "no track lines")
  os.remove(path)
end

local function test_arm_state_is_read_fresh_every_report()
  local path = os.tmpname()
  start_empty(path)
  check(report_of(path):find("track\t2\t0\tMV Spare\n", 1, true) ~= nil, "unarmed at first")
  tracks[2].arm = 1.0
  now = now + 10
  tick()
  check(report_of(path):find("track\t2\t1\tMV Spare\n", 1, true) ~= nil, "armed on the next report")
  os.remove(path)
end

local function test_a_missing_or_raising_track_api_still_reports_the_project()
  local broken = {
    { "CountTracks", function() return nil end },
    { "GetMediaTrackInfo_Value", function() error("boom") end },
  }
  for _, case in ipairs(broken) do
    local path = os.tmpname()
    start_empty(path)
    local real = reaper[case[1]]
    local said = #console
    reaper[case[1]] = case[2]
    now = now + 10
    tick()
    reaper[case[1]] = real
    local text = report_of(path)
    local label = case[1]
    check(text:find("project\t", 1, true) ~= nil, label .. ": project still reported")
    check(text:find("record_path\t", 1, true) ~= nil, label .. ": record_path still reported")
    check(text:find("script_checksum\t", 1, true) ~= nil, label .. ": checksum still reported")
    check(text:find("tracks", 1, true) == nil, label .. ": no tracks key")
    equal(#console, said, label .. ": prints nothing")
    append(path, "NOTE|note\t-\t-\n")
    now = now + 1
    tick()
    equal(#markers, 1, label .. ": the queue still mirrors")
    os.remove(path)
  end
end

local function test_pilot_is_reported_like_any_other_track()
  local path = os.tmpname()
  start_empty(path)
  check(report_of(path):find("track\t4\t0\tPilot\n", 1, true) ~= nil, "Pilot is a plain track line")
  os.remove(path)
end

local function test_the_published_vectors_through_the_report()
  local cases = { { "", "811c9dc5" }, { "a", "e40c292c" }, { "foobar", "bf9cf968" } }
  for _, case in ipairs(cases) do
    local queue = os.tmpname()
    start_empty(queue, named_script(case[1]))
    equal(checksum_of(report_of(queue)), case[2], "vector for '" .. case[1] .. "'")
    os.remove(queue)
  end
end

local function test_the_script_is_read_once_at_start()
  local source, queue = named_script("one"), os.tmpname()
  start_empty(queue, source)
  local first = checksum_of(report_of(queue))
  write(source, "a different file, replaced while the old code still runs")
  for _ = 1, 3 do
    now = now + 2
    tick()
  end
  equal(checksum_of(report_of(queue)), first, "still the checksum of what was loaded")
  check(first ~= "", "it had one")
  os.remove(queue)
end

local function test_a_script_not_launched_as_itself_reports_empty()
  local other, queue = os.tmpname(), os.tmpname()
  write(other, "something else, such as a startup script")
  start_empty(queue, other)
  equal(checksum_of(report_of(queue)), "", "a file with another name is not hashed")
  check(console[#console]:find("mirroring", 1, true) ~= nil, "mirroring is the last console line")
  os.remove(other)
  os.remove(queue)
end

local function test_an_unreadable_self_reports_empty_and_still_mirrors()
  local path = os.tmpname()
  start_empty(path, "/nonexistent/tacet_mirror.lua")
  equal(checksum_of(report_of(path)), "", "empty value, key still present")
  check(logged("script"), "one line says why")
  check(console[#console]:find("mirroring", 1, true) ~= nil, "mirroring is the last console line")
  append(path, "NOTE|note\t-\t-\n")
  tick()
  equal(#markers, 1, "the queue is still mirrored")
  os.remove(path)
end

local function test_a_raising_or_missing_context_call_reports_empty()
  local saved = reaper.get_action_context
  for _, stub in ipairs({ function() error("boom") end, function() return true, nil end, false }) do
    local path = os.tmpname()
    reaper.get_action_context = stub or nil
    start_empty(path)
    equal(checksum_of(report_of(path)), "", "empty value")
    check(console[#console]:find("mirroring", 1, true) ~= nil, "mirroring is the last console line")
    append(path, "NOTE|note\t-\t-\n")
    tick()
    equal(#markers, 1, "the queue is still mirrored")
    os.remove(path)
  end
  reaper.get_action_context = saved
end

local function cleanup_reports()
  for _, p in ipairs(report_paths) do os.remove(p) end
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
  test_the_game_2_queue_replays,
  test_every_event_in_the_vocabulary_replays,
  test_lines_are_read_as_strictly_as_the_box_writes_them,
  test_the_report_matches_the_golden_file,
  test_an_unsaved_project_writes_an_empty_project,
  test_no_project_omits_the_key,
  test_seq_rises_and_the_file_is_replaced,
  test_the_report_is_throttled,
  test_an_unwritable_report_still_mirrors,
  test_a_missing_api_still_mirrors,
  test_the_report_leaves_offset_and_open_spans_alone,
  test_a_failed_write_does_not_replace_the_report,
  test_the_report_prints_nothing_and_mirroring_stays_last,
  test_newlines_in_values_are_stripped,
  test_the_checksum_follows_record_path_and_matches_the_input,
  test_tracks_follow_the_checksum_in_track_order,
  test_arm_is_written_as_0_or_1_not_a_float,
  test_an_unnamed_track_writes_an_empty_name,
  test_newlines_in_a_name_are_stripped_and_a_tab_is_kept,
  test_no_tracks_writes_a_zero_count_and_no_track_lines,
  test_arm_state_is_read_fresh_every_report,
  test_a_missing_or_raising_track_api_still_reports_the_project,
  test_pilot_is_reported_like_any_other_track,
  test_the_published_vectors_through_the_report,
  test_the_script_is_read_once_at_start,
  test_a_script_not_launched_as_itself_reports_empty,
  test_an_unreadable_self_reports_empty_and_still_mirrors,
  test_a_raising_or_missing_context_call_reports_empty,
}

-- `lua reaper/test_tacet_mirror.lua --checksum-of FILE` prints what the script
-- would report for a script that is FILE. tests/test_script_check.py compares
-- it with Python's, so the two implementations are pinned to each other.
if arg[1] == "--checksum-of" then
  local queue = os.tmpname()
  start_empty(queue, named_copy_of(arg[2]))
  print(checksum_of(report_of(queue)))
  os.remove(queue)
  cleanup_reports()
  os.execute("rm -rf '" .. SCRIPT_DIR .. "'")
  os.exit(0)
end

for _, test in ipairs(tests) do
  local ok, err = pcall(test)
  if not ok then
    failures = failures + 1
    print("  ERROR " .. tostring(err))
  end
end

cleanup_reports()
os.execute("rm -rf '" .. SCRIPT_DIR .. "'")
print(string.format("%d checks, %d failures", checks, failures))
os.exit(failures == 0 and 0 or 1)
